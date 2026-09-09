from __future__ import annotations

from typing import Any

from readyagents.workflow.state import RunState


def build_pause_event(
    exc: Any, state: RunState, *, approval_url: str | None = None
) -> dict[str, Any]:
    """Outbound approval_required payload. No tokens, secrets, inputs, or outputs."""
    node_id = getattr(exc, "node_id", state.pending_node)
    payload: dict[str, Any] = {
        "event": "approval_required",
        "run_id": state.run_id,
        "node_id": node_id,
        "prompt": getattr(exc, "prompt", ""),
        "resume": f"readyagents resume {state.run_id} --approve {node_id}",
    }
    if approval_url:
        payload["approval_url"] = approval_url
    return payload
