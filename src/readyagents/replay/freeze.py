"""Freeze a recorded run into an offline eval fixture."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import CassetteError
from readyagents.paths import resolve_within
from readyagents.replay.cassette import Cassette
from readyagents.replay.record import contains_secret, redact_value
from readyagents.workflow.state import RunState, utc_now

FREEZE_WARNING = (
    "WARNING: this fixture contains recorded model prompts and completions. "
    "Review it before committing. Cassettes are untrusted input."
)


def freeze_run(
    state: RunState,
    cassette: Cassette,
    *,
    out_dir: Path,
    workspace: Path,
    redactor: Any = None,
    secrets: list[str] | None = None,
    allow_unsealed: bool = False,
    exact: bool = False,
    settings: Any = None,
) -> Path:
    dest = resolve_within(out_dir, workspace, what="freeze output")
    dest.mkdir(parents=True, exist_ok=True)
    if cassette.blocked_nodes and not allow_unsealed:
        raise CassetteError(
            "Refuse to freeze a cassette with secret-blocked nodes "
            f"{sorted(cassette.blocked_nodes)}. Pass --allow-unsealed to override."
        )
    unsealable = list(cassette.report.unsealable)
    if unsealable and not allow_unsealed:
        raise CassetteError(
            "Refuse to freeze a run with unsealable nodes "
            f"{unsealable}. Pass --allow-unsealed to record the caveat."
        )
    frozen = Cassette.new(run_id=state.run_id, workflow=state.workflow_name)
    frozen.entries = {
        key: _reverify(row, redactor, secrets) for key, row in cassette.entries.items()
    }
    frozen.redacted = True
    frozen.recorded_at = cassette.recorded_at
    frozen.report = cassette.report
    frozen.blocked_nodes = set(cassette.blocked_nodes)
    cassette_path = dest / "cassette.json"
    frozen.save(cassette_path, root=dest)
    source = str(state.metadata.get("source") or "")
    copied = copy_workflow_beside(source, dest, workspace=workspace)
    case_doc = _case_yaml(
        state,
        cassette,
        source=str(copied.name if copied is not None else Path(source).name),
        exact=exact,
    )
    atomic_write_text(dest / "case.yaml", case_doc, encoding="utf-8", newline="\n")
    readme = _readme(state, allow_unsealed=allow_unsealed, unsealable=unsealable)
    atomic_write_text(dest / "README.md", readme, encoding="utf-8", newline="\n")
    gitignore = dest / ".gitignore"
    if not gitignore.exists():
        atomic_write_text(
            gitignore,
            "# Review cassette.json before removing this line.\ncassette.json\n",
            encoding="utf-8",
            newline="\n",
        )
    return dest


def _reverify(entry: dict[str, Any], redactor: Any, secrets: list[str] | None) -> dict[str, Any]:
    row = dict(entry)
    if contains_secret(row, list(secrets or [])):
        row["redacted_blocked"] = True
        row.pop("text", None)
        row.pop("result", None)
        row["sealed"] = False
        return row
    if "text" in row:
        row["text"] = redact_value(redactor, row["text"])
    if "result" in row:
        row["result"] = redact_value(redactor, row["result"])
    return row


def _case_yaml(state: RunState, cassette: Cassette, *, source: str, exact: bool) -> str:
    import json

    name = f"frozen-{state.workflow_name}"
    outputs = state.output_keys or state.node_outputs
    lines = [
        f"# Frozen from run {state.run_id} at {utc_now()}",
        "cases:",
        f"  - name: {json.dumps(name)}",
        f"    workflow: {json.dumps(Path(source).name if source else state.workflow_name)}",
        f"    expect_status: {json.dumps(state.status)}",
        "    cassette: cassette.json",
        "    inputs: " + json.dumps(state.inputs, ensure_ascii=False),
    ]
    if exact:
        lines.append("    expect_outputs: " + json.dumps(outputs, ensure_ascii=False, default=str))
    else:
        contains: dict[str, str] = {}
        for key, value in outputs.items():
            text = str(value)
            if text:
                contains[str(key)] = text[:40]
        if contains:
            lines.append(
                "    expect_contains: " + json.dumps(contains, ensure_ascii=False, default=str)
            )
    report = cassette.report.as_dict()
    lines.append("    expect_determinism:")
    for bucket in ("unsealable", "misses", "sealed", "recomputed"):
        ids = sorted(_bucket_node_ids(report, bucket))
        lines.append(f"      {bucket}: " + json.dumps(ids))
    nodes = [row.node_id for row in state.results]
    lines.append("    expect_nodes: " + json.dumps(nodes))
    tools = _tool_pins(cassette)
    if tools:
        lines.append("    expect_tools:")
        for item in tools:
            lines.append("      - name: " + json.dumps(item["name"]))
            if "arguments" in item:
                lines.append(
                    "        arguments: "
                    + json.dumps(item["arguments"], ensure_ascii=False, default=str)
                )
    usage_lines = _usage_pins(state)
    if usage_lines:
        lines.append("    expect_usage:")
        lines.extend(usage_lines)
    lines.append("")
    return "\n".join(lines)


def _bucket_node_ids(report: dict[str, Any], bucket: str) -> list[str]:
    value = report.get(bucket) or []
    if not isinstance(value, list):
        return []
    if bucket != "misses":
        return [str(item) for item in value if item]
    ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        nid = ""
        if isinstance(item, dict) and item.get("node_id"):
            nid = str(item["node_id"])
        elif isinstance(item, str) and item:
            nid = item
        if nid and nid not in seen:
            seen.add(nid)
            ids.append(nid)
    return ids


def _tool_pins(cassette: Cassette) -> list[dict[str, Any]]:
    pinned: list[dict[str, Any]] = []
    for entry in cassette.entries.values():
        if entry.get("kind") != "tool":
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        row: dict[str, Any] = {"name": name}
        if "arguments" in entry and not entry.get("redacted_blocked"):
            row["arguments"] = dict(entry.get("arguments") or {})
        pinned.append(row)
    return pinned


def _usage_pins(state: RunState) -> list[str]:
    import json

    metrics = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_micros")
    lines: list[str] = []
    for metric in metrics:
        if metric not in state.usage:
            continue
        lines.append(
            f"      {metric}: " + json.dumps({"max": int(state.usage[metric])})
        )
    return lines


def _readme(state: RunState, *, allow_unsealed: bool, unsealable: list[str]) -> str:
    caveat = ""
    if unsealable:
        caveat = (
            f"\nThis fixture was frozen with unsealable nodes {unsealable} "
            f"(allow_unsealed={allow_unsealed}).\n"
        )
    return (
        f"# Frozen run `{state.run_id}`\n\n"
        f"Workflow: `{state.workflow_name}`\n\n"
        f"{FREEZE_WARNING}\n"
        f"{caveat}\n"
        "This fixture pins determinism, node order, tool rounds, and usage ceilings "
        "when those were observed. Eval fails if they drift.\n\n"
        "Run:\n\n"
        "```bash\n"
        "readyagents eval case.yaml\n"
        "```\n"
    )


def copy_workflow_beside(source: str | None, dest: Path, *, workspace: Path) -> Path | None:
    if not source:
        return None
    src = Path(source)
    if not src.is_file():
        return None
    from readyagents.paths import resolve_within as _resolve

    confined = _resolve(src, src.parent, what="workflow")
    target = dest / src.name
    atomic_write_text(
        target,
        confined.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    return target
