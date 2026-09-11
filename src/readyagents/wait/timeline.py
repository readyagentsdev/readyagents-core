"""Human-readable timeline for a long-horizon run."""

from __future__ import annotations

from typing import Any

from readyagents.wait.record import WaitRecord
from readyagents.workflow.state import RunState


def timeline(state: RunState) -> dict[str, Any]:
    happened = [
        {
            "node_id": row.node_id,
            "type": row.type,
            "status": row.status,
            "finished_at": row.finished_at,
        }
        for row in state.results
    ]
    pending = state.pending if isinstance(state.pending, dict) else {}
    waiting_for: list[str] = []
    expires_at = None
    expects = None
    if state.status == "waiting":
        waiting_for = list(pending.get("waiting_for") or [])
        expires_at = pending.get("expires_at")
        expects = "readyagents wake " + state.run_id
        rec = pending.get("wait")
        if isinstance(rec, dict):
            wr = WaitRecord.from_dict(rec)
            expects = f"wake when {', '.join(waiting_for) or 'until'} (deadline {wr.on_deadline})"
    elif state.status == "paused":
        expects = pending.get("resume") or "readyagents resume"
    spend = dict(state.usage or {})
    if isinstance(state.metadata, dict) and isinstance(state.metadata.get("spend"), dict):
        spend = dict(state.metadata["spend"])
    return {
        "run_id": state.run_id,
        "status": state.status,
        "happened": happened,
        "waiting_for": waiting_for,
        "expires_at": expires_at,
        "spend": {
            "prompt_tokens": spend.get("prompt_tokens") or state.usage.get("prompt_tokens") or 0,
            "completion_tokens": spend.get("completion_tokens")
            or state.usage.get("completion_tokens")
            or 0,
            "total_tokens": spend.get("total_tokens") or state.usage.get("total_tokens") or 0,
            "cost_micros": spend.get("cost_micros") or state.usage.get("cost_micros") or 0,
        },
        "expects_next": expects,
    }
