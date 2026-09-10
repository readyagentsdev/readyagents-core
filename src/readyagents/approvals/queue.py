"""Local approvals queue. Unauthorized and missing look identical."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from readyagents.approvals.delegate import find_delegation
from readyagents.approvals.gate import (
    actor_is_eligible,
    evaluate_gate,
    parse_expires_in,
    pause_from_pending,
)
from readyagents.approvals.view import is_approval_pause
from readyagents.errors import ApprovalRequired, ReadyAgentsError


def list_approvals(
    runs: list[Any],
    *,
    actor: str | None = None,
    role: str | None = None,
    expiring_within: str | None = None,
    now: datetime | None = None,
    home: Path | str | None = None,
) -> list[dict[str, Any]]:
    stamp = now or datetime.now(UTC)
    horizon = None
    if expiring_within:
        horizon = stamp + timedelta(seconds=parse_expires_in(expiring_within))
    rows: list[dict[str, Any]] = []
    for state in runs:
        if not is_approval_pause(state):
            continue
        pending = getattr(state, "pending", None) or {}
        if not isinstance(pending, dict):
            continue
        pause = pause_from_pending(pending)
        outcome = evaluate_gate(pause, stamp)
        if not _visible(pause, actor=actor, role=role, home=home, now=stamp):
            continue
        expires_at = pause.expires_at
        if horizon is not None:
            from readyagents.approvals.gate import parse_clock

            deadline = parse_clock(expires_at)
            if deadline is None or deadline > horizon:
                continue
        rows.append(
            {
                "run_id": getattr(state, "run_id", ""),
                "workflow": getattr(state, "workflow_name", "") or getattr(state, "workflow", ""),
                "node_id": pending.get("node_id") or getattr(state, "pending_node", ""),
                "prompt": pending.get("prompt") or "",
                "approvals_required": pause.approvals_required,
                "approvals_received": len(pause.approvals_received),
                "eligible_actors": list(pause.eligible_actors),
                "expires_at": expires_at,
                "on_expire": pause.on_expire,
                "status": outcome.status,
                "elapsed_seconds": outcome.elapsed_seconds,
            }
        )
    return rows


def fire_lazy_expiry(state: Any, *, settings: Any = None) -> Any:
    """Resume a paused gate whose deadline has passed. Used by status query.

    Listing does not call this — display-only evaluation must not mutate runs.
    """
    if not is_approval_pause(state):
        return state
    pending = getattr(state, "pending", None) or {}
    if not isinstance(pending, dict) or not pending.get("expires_at"):
        return state
    pause = pause_from_pending(pending)
    outcome = evaluate_gate(pause)
    if outcome.status not in {"expired", "escalated"} and outcome.reason != "expired":
        return state
    from readyagents.workflow.runner import resume_run

    try:
        return resume_run(state.run_id, settings=settings, persist=True)
    except ApprovalRequired as extra:
        return extra.state or state
    except ReadyAgentsError as extra:
        found = getattr(extra, "state", None)
        return found if found is not None else state


def _visible(
    pause: Any,
    *,
    actor: str | None,
    role: str | None,
    home: Path | str | None,
    now: datetime,
) -> bool:
    if not actor and not role:
        return True
    roles = [role] if role else None
    grant = None
    if actor:
        grant = find_delegation(
            actor=actor,
            roles=list(pause.approver_roles or []) + (roles or []),
            home=home,
            now=now,
        )
    delegated = grant.scope if grant is not None else None
    if actor_is_eligible(actor or role, pause, roles=roles, delegated_role=delegated):
        return True
    if role and not actor:
        from readyagents.approvals.gate import normalize_actor

        declared = {normalize_actor(item) for item in pause.approver_roles}
        if not declared or pause.eligible_actors == ["*"]:
            return True
        return normalize_actor(role) in declared
    return False
