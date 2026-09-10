"""Run-record status ↔ A2A task state. Wire uses hyphenated ``input-required``."""

from __future__ import annotations

from typing import Any

from readyagents.errors import A2ATransitionError
from readyagents.workflow.state import RunState

# Objective/TASK name ``input_required``; A2A v1.0 wire token is ``input-required``.
WIRE_SUBMITTED = "submitted"
WIRE_WORKING = "working"
WIRE_INPUT_REQUIRED = "input-required"
WIRE_COMPLETED = "completed"
WIRE_FAILED = "failed"
WIRE_CANCELED = "canceled"

_RUN_TO_WIRE = {
    "queued": WIRE_SUBMITTED,
    "running": WIRE_WORKING,
    "cancel_requested": WIRE_WORKING,
    "paused": WIRE_INPUT_REQUIRED,
    "succeeded": WIRE_COMPLETED,
    "failed": WIRE_FAILED,
    "cancelled": WIRE_CANCELED,
}

_ALLOWED: dict[str, frozenset[str]] = {
    WIRE_SUBMITTED: frozenset(
        {WIRE_WORKING, WIRE_INPUT_REQUIRED, WIRE_COMPLETED, WIRE_FAILED, WIRE_CANCELED}
    ),
    WIRE_WORKING: frozenset(
        {WIRE_WORKING, WIRE_INPUT_REQUIRED, WIRE_COMPLETED, WIRE_FAILED, WIRE_CANCELED}
    ),
    WIRE_INPUT_REQUIRED: frozenset(
        {WIRE_WORKING, WIRE_INPUT_REQUIRED, WIRE_COMPLETED, WIRE_FAILED, WIRE_CANCELED}
    ),
    WIRE_COMPLETED: frozenset({WIRE_COMPLETED}),
    WIRE_FAILED: frozenset({WIRE_FAILED}),
    WIRE_CANCELED: frozenset({WIRE_CANCELED}),
}

_TERMINAL = frozenset({WIRE_COMPLETED, WIRE_FAILED, WIRE_CANCELED})
NOT_FOUND_MESSAGE = "Task not found"


def map_run_to_task_state(status: str | None) -> str:
    if not status:
        return WIRE_WORKING
    return _RUN_TO_WIRE.get(str(status), WIRE_WORKING)


def normalize_wire_state(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace("_", "-")
    if text == "cancelled":
        return WIRE_CANCELED
    return text


def validate_transition(current: str, target: str) -> None:
    allowed = _ALLOWED.get(current)
    if allowed is None or target not in allowed:
        raise A2ATransitionError(f"illegal A2A transition {current!r} -> {target!r}")


def is_terminal(state: str) -> bool:
    return state in _TERMINAL


def task_status_object(state: RunState, *, message: dict[str, Any] | None = None) -> dict[str, Any]:
    wire = map_run_to_task_state(state.status)
    body: dict[str, Any] = {
        "state": wire,
        "timestamp": state.finished_at or state.started_at,
    }
    if message is not None:
        body["message"] = message
    return body


def artifacts_from_state(state: RunState, *, limit: int = 1_000_000) -> list[dict[str, Any]]:
    outputs = dict(state.output_keys or {})
    if not outputs:
        return []
    text = _bounded_json(outputs, limit)
    return [
        {
            "artifactId": "outputs",
            "name": "outputs",
            "parts": [{"kind": "text", "text": text}],
        }
    ]


def _bounded_json(value: Any, limit: int) -> str:
    import json

    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) > limit:
        return raw[:limit]
    return raw
