"""Run explorer, timeline, and node inspector over the shipped run record."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.policy import Redactor
from readyagents.replay.diff import diff_runs
from readyagents.workflow.state import RunState


def list_runs(store: Any, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    from readyagents.run_store.base import RunQuery

    query = RunQuery(status=status or None, limit=limit)
    rows = []
    for item in store.list(query):
        state = item.state
        rows.append(
            {
                "run_id": state.run_id,
                "workflow": state.workflow_name,
                "status": state.status,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "pending_node": state.pending_node,
                "usage": dict(state.usage or {}),
            }
        )
    return rows


def run_timeline(state: RunState, *, redactor: Redactor | None = None) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for row in state.results:
        usage = dict(row.usage or {})
        nodes.append(
            {
                "node_id": row.node_id,
                "type": row.type,
                "status": row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "attempts": row.attempts,
                "usage": usage,
                "tokens": int(usage.get("total_tokens") or 0),
                "cost_micros": int(usage.get("cost_micros") or 0),
                "tool_calls": len(row.tool_rounds or []),
                "retries": max(0, int(row.attempts or 1) - 1),
                "error": row.error,
            }
        )
    policy = _policy_decisions(state)
    taint = _taint_rows(state)
    return {
        "run_id": state.run_id,
        "workflow": state.workflow_name,
        "status": state.status,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "pending_node": state.pending_node,
        "usage": dict(state.usage or {}),
        "nodes": nodes,
        "taint": taint,
        "policy": policy,
        "streaming": bool((state.metadata or {}).get("stream")),
        "source": (state.metadata or {}).get("source"),
        "cassette": (state.metadata or {}).get("cassette"),
    }


def node_inspector(
    state: RunState,
    node_id: str,
    *,
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    matches = [row for row in state.results if row.node_id == node_id]
    row = matches[-1] if matches else None
    output = None if row is None else row.output
    if redactor is not None and output is not None:
        output = redactor.redact(output)
    cassette_entry = _cassette_for_node(state, node_id)
    prompt = None
    model = None
    why = None
    if isinstance(cassette_entry, dict):
        prompt = cassette_entry.get("text") or cassette_entry.get("prompt")
        model = cassette_entry.get("model")
        why = cassette_entry.get("digest")
        if redactor is not None and prompt is not None:
            if hasattr(redactor, "redact_text"):
                prompt = redactor.redact_text(str(prompt))
    pending = state.pending if isinstance(state.pending, dict) else {}
    if prompt is None and pending.get("node_id") == node_id:
        prompt = pending.get("prompt")
        if redactor is not None and prompt is not None and hasattr(redactor, "redact_text"):
            prompt = redactor.redact_text(str(prompt))
    return {
        "run_id": state.run_id,
        "node_id": node_id,
        "status": None if row is None else row.status,
        "type": None if row is None else row.type,
        "inputs": dict(state.inputs or {}),
        "output": output,
        "prompt": prompt,
        "model": model,
        "why": why,
        "cassette": cassette_entry,
        "usage": dict(row.usage or {}) if row is not None else {},
        "tool_rounds": list(row.tool_rounds or []) if row is not None else [],
        "attempts": None if row is None else row.attempts,
    }


def compare_runs(left: RunState, right: RunState, *, redactor: Any = None) -> dict[str, Any]:
    return diff_runs(left, right, redactor=redactor)


def _cassette_for_node(state: RunState, node_id: str) -> dict[str, Any] | None:
    path = (state.metadata or {}).get("cassette")
    if not path:
        return None
    file = Path(str(path))
    if not file.is_file():
        return None
    from readyagents.replay.cassette import Cassette

    tape = Cassette.load(file)
    for row in tape.entries.values():
        if isinstance(row, dict) and row.get("node_id") == node_id:
            return dict(row)
    return None


def _taint_rows(state: RunState) -> list[dict[str, Any]]:
    provenance = getattr(state, "provenance", None) or {}
    rows: list[dict[str, Any]] = []
    if isinstance(provenance, dict):
        for key, value in provenance.items():
            if isinstance(value, dict):
                rows.append({"key": str(key), **{str(k): v for k, v in value.items()}})
            else:
                rows.append({"key": str(key), "value": value})
    return rows


def _policy_decisions(state: RunState) -> list[dict[str, Any]]:
    meta = state.metadata or {}
    for key in ("policy", "firewall", "policy_decisions", "denied"):
        blob = meta.get(key)
        if isinstance(blob, list):
            return [dict(item) if isinstance(item, dict) else {"value": item} for item in blob]
        if isinstance(blob, dict):
            return [{"key": str(k), "value": v} for k, v in blob.items()]
    return []
