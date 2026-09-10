"""Redacted view models for the local browser approval UI."""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from readyagents.policy import Redactor

VIEW_FIELDS: tuple[str, ...] = (
    "run_id",
    "workflow",
    "status",
    "started_at",
    "node_id",
    "prompt",
    "actor",
    "revision",
    "approvals_required",
    "approvals_received",
    "eligible_actors",
    "expires_at",
)

ASSET_STYLE_URL = "/approvals/assets/style.css"
ASSET_SCRIPT_URL = "/approvals/assets/app.js"


def escape_html(value: Any) -> str:
    """Escape text for any server-rendered HTML interpolation."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def is_approval_pause(state: Any) -> bool:
    """True when the run is paused on an approval node.

    Engine stores ``pending.type == "approval"``. A pause counts when status is
    ``paused`` and the pending type is approval, including when ``pending_node``
    is also set.
    """
    if _text(_field(state, "status")) != "paused":
        return False
    pending_type = _pending_type(_field(state, "pending"))
    pending_node = _field(state, "pending_node")
    return pending_type == "approval" or (bool(pending_node) and pending_type == "approval")


def approval_view(
    state: Any,
    *,
    revision: int,
    redactor: Any | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Return a display-only dict for one paused approval run.

    Always redacts with ``redactor`` or a default ``Redactor()`` so emails and
    ``sk-`` keys are masked even when persistence redaction is off.
    """
    active = redactor if redactor is not None else Redactor()
    pending = _field(state, "pending")
    prompt = ""
    required = 1
    received = 0
    eligible: list[str] = []
    expires_at = ""
    if isinstance(pending, Mapping):
        raw_prompt = pending.get("prompt")
        prompt = "" if raw_prompt is None else str(raw_prompt)
        raw_required = pending.get("approvals_required")
        if raw_required is not None:
            try:
                required = max(1, int(raw_required))
            except (TypeError, ValueError):
                required = 1
        votes = pending.get("approvals_received")
        if isinstance(votes, list):
            received = len(votes)
        raw_eligible = pending.get("eligible_actors")
        if isinstance(raw_eligible, list):
            eligible = [_text(item) for item in raw_eligible]
        expires_at = _text(pending.get("expires_at"))
    metadata = _field(state, "metadata")
    meta_actor = None
    if isinstance(metadata, Mapping):
        meta_actor = metadata.get("actor")
    resolved_actor = _first_text(meta_actor, actor)
    node_id = _field(state, "pending_node")
    view = {
        "run_id": _text(_field(state, "run_id")),
        "workflow": _redact(active, _text(_field(state, "workflow_name", "workflow"))),
        "status": _text(_field(state, "status")),
        "started_at": _text(_field(state, "started_at")),
        "node_id": _text(node_id),
        "prompt": _redact(active, prompt),
        "actor": _redact(active, resolved_actor),
        "revision": int(revision),
        "approvals_required": required,
        "approvals_received": received,
        "eligible_actors": eligible,
        "expires_at": expires_at,
    }
    return {key: view[key] for key in VIEW_FIELDS}


def _field(state: Any, *names: str) -> Any:
    for name in names:
        if isinstance(state, Mapping) and name in state:
            return state[name]
        if not isinstance(state, Mapping) and hasattr(state, name):
            return getattr(state, name)
    return None


def _pending_type(pending: Any) -> str:
    if not isinstance(pending, Mapping):
        return ""
    return _text(pending.get("type"))


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _redact(redactor: Any, value: str) -> str:
    if not value:
        return ""
    if hasattr(redactor, "redact_text"):
        return str(redactor.redact_text(value))
    if hasattr(redactor, "redact"):
        return str(redactor.redact(value))
    return value
