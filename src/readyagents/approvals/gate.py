"""Pure gate evaluation for quorum, SoD, roles, and lazy expiry.

Core starts no timer. Expiry is evaluated on resume, decide, and status query.
``on_expire: approve`` is unrepresentable — refused at schema validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from readyagents.errors import ConfigError, GateExpired

GateStatus = Literal["pending", "approved", "rejected", "expired", "escalated"]
ON_EXPIRE = frozenset({"reject", "escalate", "fail"})
_DUR = re.compile(r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|h|hr|hrs|d|day|days)$", re.I)
_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def normalize_actor(actor: str | None) -> str:
    return (actor or "").strip().casefold()


def parse_expires_in(text: str) -> float:
    raw = str(text or "").strip()
    match = _DUR.fullmatch(raw)
    if not match:
        raise ConfigError(f"expires_in must be a duration like 30m, 4h, or 1d, not {text!r}")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return float(amount * _UNITS[unit])


def parse_clock(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def clock_now() -> datetime:
    return datetime.now(UTC)


def format_clock(stamp: datetime) -> str:
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Vote:
    actor: str
    decision: str
    at: str
    role: str | None = None
    reason: str | None = None
    signature_status: str = "unsigned"
    delegated_from: str | None = None
    override: bool = False

    def actor_norm(self) -> str:
        return normalize_actor(self.actor)

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "role": self.role,
            "decision": self.decision,
            "reason": self.reason,
            "at": self.at,
            "signature_status": self.signature_status,
            "delegated_from": self.delegated_from,
            "override": self.override,
        }


@dataclass
class GateOutcome:
    status: GateStatus
    reason: str | None = None
    elapsed_seconds: float | None = None
    expires_at: str | None = None
    clock_source: str = "host"


@dataclass
class PauseState:
    approvals_required: int = 1
    approvals_received: list[Vote] = field(default_factory=list)
    distinct_actors: bool = True
    deny_actor: list[str] = field(default_factory=list)
    approver_roles: list[str] = field(default_factory=list)
    require: str = "any"
    eligible_actors: list[str] = field(default_factory=list)
    expires_at: str | None = None
    on_expire: str | None = None
    escalated_to: list[str] = field(default_factory=list)
    paused_at: str | None = None
    reject_short_circuit: bool = True
    require_reason: bool = False
    recommendation: str | None = None
    clock_source: str = "host"
    expired: bool = False
    escalated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approvals_required": self.approvals_required,
            "approvals_received": [vote.as_dict() for vote in self.approvals_received],
            "distinct_actors": self.distinct_actors,
            "deny_actor": list(self.deny_actor),
            "approver_roles": list(self.approver_roles),
            "require": self.require,
            "eligible_actors": list(self.eligible_actors),
            "expires_at": self.expires_at,
            "on_expire": self.on_expire,
            "escalated_to": list(self.escalated_to),
            "paused_at": self.paused_at,
            "reject_short_circuit": self.reject_short_circuit,
            "require_reason": self.require_reason,
            "recommendation": self.recommendation,
            "clock_source": self.clock_source,
            "expired": self.expired,
            "escalated_at": self.escalated_at,
        }


def vote_from_dict(raw: MappingLike) -> Vote:
    data = dict(raw or {})
    return Vote(
        actor=str(data.get("actor") or ""),
        decision=str(data.get("decision") or "").strip().lower(),
        at=str(data.get("at") or ""),
        role=str(data["role"]) if data.get("role") else None,
        reason=str(data["reason"]) if data.get("reason") else None,
        signature_status=str(data.get("signature_status") or "unsigned"),
        delegated_from=str(data["delegated_from"]) if data.get("delegated_from") else None,
        override=bool(data.get("override")),
    )


def pause_from_pending(pending: MappingLike | None, *, node: Any | None = None) -> PauseState:
    """Load pause fields. Missing keys mean a 0.9-era single-approval gate."""
    data = dict(pending or {})
    required = data.get("approvals_required")
    if required is None and node is not None:
        required = getattr(node, "approvals_required", None)
    if required is None:
        required = 1
    distinct = data.get("distinct_actors")
    if distinct is None and node is not None:
        distinct = getattr(node, "distinct_actors", None)
    if distinct is None:
        distinct = int(required) > 1
    deny = data.get("deny_actor")
    if deny is None and node is not None:
        deny = getattr(node, "deny_actor", None)
    roles = data.get("approver_roles")
    if roles is None and node is not None:
        roles = getattr(node, "approver_roles", None)
    require = data.get("require")
    if require is None and node is not None:
        require = getattr(node, "require", None)
    votes_raw = data.get("approvals_received") or []
    votes = [vote_from_dict(row) for row in votes_raw if isinstance(row, dict)]
    on_expire = data.get("on_expire")
    if on_expire is None and node is not None:
        on_expire = getattr(node, "on_expire", None)
    return PauseState(
        approvals_required=max(1, int(required)),
        approvals_received=votes,
        distinct_actors=bool(distinct),
        deny_actor=[str(item) for item in list(deny or [])],
        approver_roles=[str(item) for item in list(roles or [])],
        require=str(require or "any").strip().lower() or "any",
        eligible_actors=[str(item) for item in list(data.get("eligible_actors") or [])],
        expires_at=str(data["expires_at"]) if data.get("expires_at") else None,
        on_expire=str(on_expire).strip().lower() if on_expire else None,
        escalated_to=[str(item) for item in list(data.get("escalated_to") or [])],
        paused_at=str(data["paused_at"]) if data.get("paused_at") else None,
        reject_short_circuit=bool(
            data["reject_short_circuit"]
            if "reject_short_circuit" in data
            else (getattr(node, "reject_short_circuit", True) if node is not None else True)
        ),
        require_reason=bool(
            data["require_reason"]
            if "require_reason" in data
            else (getattr(node, "require_reason", False) if node is not None else False)
        ),
        recommendation=(
            str(data["recommendation"])
            if data.get("recommendation")
            else (getattr(node, "recommendation", None) if node is not None else None)
        ),
        clock_source=str(data.get("clock_source") or "host"),
        expired=bool(data.get("expired")),
        escalated_at=str(data["escalated_at"]) if data.get("escalated_at") else None,
    )


def enterprise_fields_set(node: Any) -> bool:
    """True when the node declared any TASK-08 field (opt-in)."""
    if node is None:
        return False
    if getattr(node, "approvals_required", None) is not None:
        return True
    if getattr(node, "distinct_actors", None) is not None:
        return True
    if getattr(node, "deny_actor", None):
        return True
    if getattr(node, "approver_roles", None):
        return True
    if getattr(node, "expires_in", None):
        return True
    if getattr(node, "on_expire", None):
        return True
    if getattr(node, "escalate_to", None):
        return True
    if getattr(node, "require_reason", False):
        return True
    if getattr(node, "notify", None):
        return True
    require = getattr(node, "require", None)
    if require not in (None, "any"):
        return True
    if getattr(node, "recommendation", None):
        return True
    if getattr(node, "reject_short_circuit", True) is False:
        return True
    return False


def legacy_pause(pending: MappingLike | None) -> bool:
    if not isinstance(pending, dict):
        return True
    return "approvals_required" not in pending and "approvals_received" not in pending


def evaluate_gate(
    pause: PauseState,
    now: datetime | None = None,
    *,
    ignore_expiry: bool = False,
) -> GateOutcome:
    """Single outcome used by CLI, decision files, and the browser UI.

    ``ignore_expiry`` is for the same touch that just escalated: the new
    eligible set may still vote once. The next resume/decide/status query
    sees the un-reset clock and fails.
    """
    stamp = now or clock_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC)
    elapsed = None
    paused_at = parse_clock(pause.paused_at)
    if paused_at is not None:
        elapsed = max(0.0, (stamp - paused_at).total_seconds())
    expires_at = parse_clock(pause.expires_at)
    if not ignore_expiry and expires_at is not None and stamp >= expires_at:
        on_expire = (pause.on_expire or "reject").strip().lower()
        if on_expire not in ON_EXPIRE:
            on_expire = "reject"
        if on_expire == "escalate" and not pause.escalated_at:
            return GateOutcome(
                status="escalated",
                reason="expired",
                elapsed_seconds=elapsed,
                expires_at=pause.expires_at,
                clock_source=pause.clock_source,
            )
        if on_expire == "fail" or (on_expire == "escalate" and pause.escalated_at):
            return GateOutcome(
                status="expired",
                reason="expired",
                elapsed_seconds=elapsed,
                expires_at=pause.expires_at,
                clock_source=pause.clock_source,
            )
        return GateOutcome(
            status="rejected",
            reason="expired",
            elapsed_seconds=elapsed,
            expires_at=pause.expires_at,
            clock_source=pause.clock_source,
        )

    rejects = [vote for vote in pause.approvals_received if vote.decision == "reject"]
    if rejects and pause.reject_short_circuit:
        return GateOutcome(
            status="rejected",
            reason="rejected",
            elapsed_seconds=elapsed,
            expires_at=pause.expires_at,
            clock_source=pause.clock_source,
        )

    approves = [vote for vote in pause.approvals_received if vote.decision == "approve"]
    if pause.distinct_actors:
        seen: set[str] = set()
        unique: list[Vote] = []
        for vote in approves:
            key = vote.actor_norm()
            if key in seen:
                continue
            seen.add(key)
            unique.append(vote)
        approves = unique
    if pause.require == "all" and pause.approver_roles:
        have = {normalize_actor(vote.role or vote.actor) for vote in approves}
        need = {normalize_actor(role) for role in pause.approver_roles}
        if not need.issubset(have):
            if rejects and not pause.reject_short_circuit:
                return GateOutcome(
                    status="rejected",
                    reason="rejected",
                    elapsed_seconds=elapsed,
                    expires_at=pause.expires_at,
                    clock_source=pause.clock_source,
                )
            return GateOutcome(
                status="pending",
                elapsed_seconds=elapsed,
                expires_at=pause.expires_at,
                clock_source=pause.clock_source,
            )
    if len(approves) >= pause.approvals_required:
        return GateOutcome(
            status="approved",
            elapsed_seconds=elapsed,
            expires_at=pause.expires_at,
            clock_source=pause.clock_source,
        )
    if rejects and not pause.reject_short_circuit:
        needed = pause.approvals_required
        if len(pause.approvals_received) >= needed:
            return GateOutcome(
                status="rejected",
                reason="rejected",
                elapsed_seconds=elapsed,
                expires_at=pause.expires_at,
                clock_source=pause.clock_source,
            )
    return GateOutcome(
        status="pending",
        elapsed_seconds=elapsed,
        expires_at=pause.expires_at,
        clock_source=pause.clock_source,
    )


def eligible_labels(roles: list[str] | None) -> list[str]:
    if not roles:
        return ["*"]
    return [f"role:{role}" for role in roles]


def actor_is_eligible(
    actor: str | None,
    pause: PauseState,
    *,
    roles: list[str] | None = None,
    delegated_role: str | None = None,
) -> bool:
    norm = normalize_actor(actor)
    if not norm:
        return False
    denied = {normalize_actor(item) for item in pause.deny_actor}
    if norm in denied:
        return False
    if not pause.approver_roles or pause.eligible_actors == ["*"]:
        return True
    held = {normalize_actor(item) for item in (roles or [])}
    if delegated_role:
        held.add(normalize_actor(delegated_role))
    held.add(norm)
    declared = {normalize_actor(item) for item in pause.approver_roles}
    if pause.require == "all":
        return bool(held & declared)
    return bool(held & declared)


def apply_escalation(pause: PauseState, *, now: datetime, targets: list[str]) -> PauseState:
    labels = eligible_labels(targets)
    pause.escalated_to = list(targets)
    pause.escalated_at = format_clock(now)
    pause.eligible_actors = labels
    pause.approver_roles = list(targets)
    return pause


def expires_at_for(node: Any, *, paused_at: datetime) -> str | None:
    raw = getattr(node, "expires_in", None)
    if not raw:
        return None
    seconds = parse_expires_in(str(raw))
    return format_clock(paused_at + timedelta(seconds=seconds))


__all__ = [
    "GateExpired",
    "GateOutcome",
    "PauseState",
    "Vote",
    "actor_is_eligible",
    "apply_escalation",
    "clock_now",
    "eligible_labels",
    "enterprise_fields_set",
    "evaluate_gate",
    "expires_at_for",
    "format_clock",
    "legacy_pause",
    "normalize_actor",
    "parse_clock",
    "parse_expires_in",
    "pause_from_pending",
    "vote_from_dict",
]


MappingLike = dict[str, Any] | Any
