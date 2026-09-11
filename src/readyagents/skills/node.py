"""type: skill — inject untrusted instructions; scripts only in the sandbox."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.code.runner import spawn_sandboxed
from readyagents.errors import SkillDrift, SkillRefused
from readyagents.firewall.enforce import ToolRequest, evaluate
from readyagents.firewall.taint import set_provenance, untrusted
from readyagents.skills.catalog import get_record, load_installed
from readyagents.skills.digest import digest_skill_dir
from readyagents.workflow.templates import interpolate, interpolate_value


def disclose_skills(home: Path) -> list[dict[str, str]]:
    from readyagents.skills.catalog import disclose_all

    return disclose_all(home)


def run_skill_node(node: Any, state: Any, ctx: Any) -> Any:
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "type": "skill", "skill": getattr(node, "skill", None)}
    home = _home(ctx)
    name = interpolate(str(getattr(node, "skill", "") or ""), state.mapping()).strip()
    if not name:
        raise SkillRefused("skill node requires skill", reason="missing")
    row = get_record(home, name)
    folder = Path(str(row.get("path") or ""))
    actual = digest_skill_dir(folder)
    expected = str(row.get("digest") or "")
    if expected and actual != expected:
        raise SkillDrift(name, expected, actual)
    record = load_installed(home, name)
    mapped = _map_inputs(node, state)
    tools = _filter_tools(record.allowed_tools, node, state, ctx)
    body = record.body
    set_provenance(state, node.id, untrusted(source=f"skill:{record.name}", node_id=node.id))
    if getattr(node, "output_key", None):
        set_provenance(
            state, node.output_key, untrusted(source=f"skill:{record.name}", node_id=node.id)
        )
    payload: dict[str, Any] = {
        "name": record.name,
        "description": record.description,
        "instructions": body,
        "attribution": f"skill:{record.name}",
        "inputs": mapped,
        "scripts": list(record.scripts),
        "tools": tools,
        "digest": actual,
        "signature_status": row.get("signature_status") or "unsigned",
        "disclosed": record.disclose(),
    }
    script = getattr(node, "script", None) or _default_script(record)
    if script:
        payload["script_output"] = _run_script(node, folder, script, mapped, ctx, state)
    return payload


def _map_inputs(node: Any, state: Any) -> dict[str, Any]:
    ns = state.mapping()
    raw = getattr(node, "call_inputs", None) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): interpolate_value(v, ns) for k, v in raw.items()}


def _filter_tools(requested: list[str], node: Any, state: Any, ctx: Any) -> list[str]:
    policy = getattr(ctx, "policy", None)
    allowed: list[str] = []
    for name in requested:
        token = name.split("(")[0].strip()
        if not token:
            continue
        decision = evaluate(
            ToolRequest(name=token, arguments={}, node_id=node.id, raw_arguments={}),
            state,
            policy,
        )
        if decision.action == "deny":
            continue
        allowed.append(token)
    return allowed


def _default_script(record: Any) -> str | None:
    pys = [s for s in record.scripts if s.endswith(".py")]
    if len(pys) == 1:
        return pys[0]
    return None


def _run_script(
    node: Any, folder: Path, script: str, inputs: dict[str, Any], ctx: Any, state: Any
) -> Any:
    rel = Path(script)
    if rel.is_absolute() or ".." in rel.parts:
        raise SkillRefused("skill script path escaped the skill folder", reason="path")
    path = (folder / rel).resolve()
    if folder.resolve() not in path.parents and path != folder.resolve():
        raise SkillRefused("skill script path escaped the skill folder", reason="path")
    if path.is_symlink():
        raise SkillRefused("skill script is a symlink", reason="symlink")
    if not path.is_file():
        raise SkillRefused(f"skill script not found: {script}", reason="missing")
    source = path.read_text(encoding="utf-8")
    workspace = Path(getattr(ctx, "workflow_dir", None) or Path.cwd())
    recorded = spawn_sandboxed(
        node_id=node.id,
        source=source,
        inputs=dict(inputs),
        isolation=getattr(node, "isolation", None) or "subprocess",
        require_isolation=None,
        limits=getattr(node, "limits", None),
        allow_imports=list(getattr(node, "allow_imports", None) or []),
        network=False,
        read_roots=[folder.resolve()],
        write_roots=[],
        workspace=workspace,
    )
    del state
    return recorded.get("output")


def _home(ctx: Any) -> Path:
    pin = getattr(ctx, "pin_home", None)
    if pin is not None:
        return Path(pin)
    from readyagents.config import get_settings

    return get_settings().home_path()
