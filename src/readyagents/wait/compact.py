"""Bound long-run records. Compacted outputs still replay as markers."""

from __future__ import annotations

from typing import Any

from readyagents.workflow.state import NodeResult, RunState

KEEP_TAIL = 16
MAX_OUTPUT_CHARS = 2048


def compact_results(state: RunState) -> None:
    """Drop bulky historical outputs; keep tail and markers. Record stays replayable."""
    if len(state.results) <= KEEP_TAIL:
        return
    head = state.results[:-KEEP_TAIL]
    tail = state.results[-KEEP_TAIL:]
    compacted: list[NodeResult] = []
    for row in head:
        output = row.output
        if _is_large(output):
            output = {"_compacted": True, "node_id": row.node_id, "type": row.type}
        compacted.append(
            NodeResult(
                node_id=row.node_id,
                type=row.type,
                status=row.status,
                output=output,
                error=row.error,
                attempts=row.attempts,
                started_at=row.started_at,
                finished_at=row.finished_at,
                usage=dict(row.usage),
            )
        )
    state.results = compacted + tail


def _is_large(value: Any) -> bool:
    text = str(value or "")
    return len(text) > MAX_OUTPUT_CHARS
