"""Read-only run diff: first divergence, bounded redacted output, usage delta."""

from __future__ import annotations

import difflib
import re
from typing import Any

from readyagents.mcp.protocol import sanitize_prompt
from readyagents.workflow.state import RunState

_DIFF_LIMIT = 4000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def diff_runs(
    left: RunState,
    right: RunState,
    *,
    redactor: Any = None,
) -> dict[str, Any]:
    """Compare two runs. Never mutates either record."""
    left_rows = _index(left)
    right_rows = _index(right)
    only_a = [key for key in left_rows if key not in right_rows]
    only_b = [key for key in right_rows if key not in left_rows]
    first: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = []
    usage_delta = _usage_delta(left.usage, right.usage)
    for key, row in left_rows.items():
        other = right_rows.get(key)
        if other is None:
            item = {
                "node_id": row["node_id"],
                "occurrence": row["occurrence"],
                "status_a": row["status"],
                "status_b": None,
                "changed": True,
            }
            nodes.append(item)
            if first is None:
                first = item
            continue
        changed = row["status"] != other["status"] or row["output"] != other["output"]
        item = {
            "node_id": row["node_id"],
            "occurrence": row["occurrence"],
            "status_a": row["status"],
            "status_b": other["status"],
            "changed": changed,
        }
        if changed:
            item["output_diff"] = _bounded_diff(row["output"], other["output"], redactor=redactor)
            if first is None:
                first = item
        nodes.append(item)
    for key, row in right_rows.items():
        if key in left_rows:
            continue
        item = {
            "node_id": row["node_id"],
            "occurrence": row["occurrence"],
            "status_a": None,
            "status_b": row["status"],
            "changed": True,
        }
        nodes.append(item)
        if first is None:
            first = item
    return {
        "run_a": left.run_id,
        "run_b": right.run_id,
        "identical": first is None and not only_a and not only_b,
        "first_divergence": first,
        "only_in_a": [{"node_id": n, "occurrence": o} for n, o in only_a],
        "only_in_b": [{"node_id": n, "occurrence": o} for n, o in only_b],
        "nodes": nodes,
        "usage_delta": usage_delta,
    }


def _index(state: RunState) -> dict[tuple[str, int], dict[str, Any]]:
    seen: dict[str, int] = {}
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for row in state.results:
        occ = seen.get(row.node_id, 0)
        seen[row.node_id] = occ + 1
        out[(row.node_id, occ)] = {
            "node_id": row.node_id,
            "occurrence": occ,
            "status": row.status,
            "output": row.output,
            "error": row.error,
            "usage": dict(row.usage),
        }
    return out


def _usage_delta(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    keys = set(left) | set(right)
    return {key: int(right.get(key, 0)) - int(left.get(key, 0)) for key in sorted(keys)}


def _bounded_diff(left: Any, right: Any, *, redactor: Any) -> str:
    a = _safe_text(left, redactor)
    b = _safe_text(right, redactor)
    lines = list(
        difflib.unified_diff(
            a.splitlines(),
            b.splitlines(),
            fromfile="a",
            tofile="b",
            lineterm="",
        )
    )
    text = "\n".join(lines)
    if len(text) > _DIFF_LIMIT:
        text = text[:_DIFF_LIMIT] + "\n… (diff truncated)"
    return text


def _safe_text(value: Any, redactor: Any) -> str:
    raw = "" if value is None else (value if isinstance(value, str) else str(value))
    cleaned = _CONTROL.sub("", raw)
    return sanitize_prompt(cleaned, redactor=redactor, limit=_DIFF_LIMIT)
