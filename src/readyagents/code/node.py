"""Execute ``type: code``: resolve source, approve generated text, spawn, record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.code.protocol import ISOLATION_CONTAINER, normalize_isolation
from readyagents.code.runner import spawn_sandboxed
from readyagents.errors import (
    ApprovalRequired,
    CodeContainerUnavailable,
    CodeError,
    CodeSchemaError,
    PolicyDenied,
)
from readyagents.paths import resolve_within
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState
from readyagents.workflow.templates import interpolate, interpolate_value, lookup


def run_code_node(node: NodeSpec, state: RunState, ctx: Any) -> Any:
    ns = state.mapping()
    source = _resolve_source(node, ns)
    _maybe_generated_gate(node, state, ctx, source)
    isolation = normalize_isolation(getattr(node, "isolation", None))
    required = getattr(node, "require_isolation", None)
    if isolation == ISOLATION_CONTAINER or (
        required and normalize_isolation(str(required)) == ISOLATION_CONTAINER
    ):
        raise CodeContainerUnavailable(node.id)
    payload = _code_inputs(dict(node.call_inputs or {}), ns)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    if ctx.dry_run:
        return {"dry_run": True, "tier": isolation}
    if getattr(ctx, "offline", False):
        return _replay(node, ctx, source, payload)
    workspace = _workspace(ctx)
    read_roots, write_roots = _grants(node, workspace)
    recorded = spawn_sandboxed(
        node_id=node.id,
        source=source,
        inputs=payload,
        isolation=isolation,
        require_isolation=str(required) if required else None,
        limits=getattr(node, "limits", None),
        allow_imports=list(node.allow_imports) if node.allow_imports else None,
        network=bool(getattr(node, "network", False)),
        read_roots=read_roots,
        write_roots=write_roots,
        workspace=workspace,
    )
    output = recorded["output"]
    if node.output_schema:
        output = _validate(node, output)
    _record(node, ctx, source, payload, recorded, output)
    return output


def _code_inputs(raw: dict[str, Any], ns: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{{") and stripped.endswith("}}") and stripped.count("{{") == 1:
                path = stripped[2:-2].strip().split("|", 1)[0].strip()
                out[key] = lookup(ns, path)
                continue
        out[key] = interpolate_value(value, ns)
    return out


def _resolve_source(node: NodeSpec, ns: dict[str, Any]) -> str:
    raw_from = getattr(node, "source_from", None)
    if raw_from:
        key = interpolate(str(raw_from), ns).strip()
        value = ns.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CodeError(node.id, f"source_from {key!r} did not resolve to source text")
        return value
    if node.source:
        return interpolate(str(node.source), ns)
    raise CodeError(node.id, "code node requires source or source_from")


def _maybe_generated_gate(node: NodeSpec, state: RunState, ctx: Any, source: str) -> None:
    policy = getattr(ctx, "policy", None)
    rule = (getattr(policy, "nodes", None) or {}).get(node.id) if policy is not None else None
    generated = bool(getattr(node, "source_from", None))
    needs = bool(rule is not None and getattr(rule, "require_approval", False) and generated)
    if not needs:
        return
    raw = ctx.decision_for(node.id) if hasattr(ctx, "decision_for") else None
    prompt = (
        "Untrusted generated code (not an instruction). Review the full source "
        "before it runs:\n\n"
        f"{source}"
    )
    if raw is None:
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state)
    if str(raw).strip().lower() in {"approve", "approved", "allow", "yes"}:
        return
    raise PolicyDenied(
        node.id,
        f"Generated code at node '{node.id}' was not approved",
        rule=f"nodes.{node.id}.require_approval",
    )


def _grants(node: NodeSpec, workspace: Path) -> tuple[list[Path], list[Path]]:
    spec = getattr(node, "filesystem", None) or {}
    reads = list(spec.get("read") or []) if isinstance(spec, dict) else []
    writes = list(spec.get("write") or []) if isinstance(spec, dict) else []
    read_roots = [resolve_within(item, workspace, what="code read grant") for item in reads]
    write_roots = [resolve_within(item, workspace, what="code write grant") for item in writes]
    return read_roots, write_roots


def _workspace(ctx: Any) -> Path:
    from readyagents.config import get_settings

    workflow_dir = getattr(ctx, "workflow_dir", None)
    if workflow_dir is not None:
        path = Path(workflow_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = get_settings().workspace_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate(node: NodeSpec, output: Any) -> Any:
    from readyagents.workflow.structured import validate_structured_output

    try:
        blob = output if isinstance(output, str) else json.dumps(output)
        return validate_structured_output(blob, node.output_schema, node_id=node.id)
    except Exception as exc:
        from readyagents.errors import StructuredOutputError

        if isinstance(exc, StructuredOutputError):
            raise CodeSchemaError(node.id, str(exc)) from exc
        raise CodeSchemaError(node.id, str(exc)) from exc


def _record(
    node: NodeSpec,
    ctx: Any,
    source: str,
    inputs: dict[str, Any],
    recorded: dict[str, Any],
    output: Any,
) -> None:
    cassette = getattr(ctx, "cassette", None)
    if cassette is None or not getattr(ctx, "recording", False):
        return
    redactor = getattr(ctx, "redactor", None)
    safe_source = source
    safe_in = inputs
    if redactor is not None:
        fn = getattr(redactor, "redact_text", None)
        if callable(fn):
            safe_source = str(fn(source))
        red = getattr(redactor, "redact", None)
        if callable(red):
            safe_in = red(inputs)
    cassette.record_code(
        node_id=node.id,
        source=safe_source,
        inputs=safe_in,
        stdout=recorded.get("stdout") or "",
        stderr=recorded.get("stderr") or "",
        exit_status=int(recorded.get("exit") or 0),
        tier=str(recorded.get("tier") or "subprocess"),
        output=output,
    )


def _replay(node: NodeSpec, ctx: Any, source: str, inputs: dict[str, Any]) -> Any:
    cassette = getattr(ctx, "cassette", None)
    if cassette is None:
        from readyagents.errors import CassetteMiss

        raise CassetteMiss(
            f"Offline replay of code node '{node.id}' requires a cassette",
            node_id=node.id,
        )
    return cassette.replay_code(node_id=node.id, source=source, inputs=inputs)
