"""Durable wait record. Extra keys are ignored so a version bump still loads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from readyagents.approvals.gate import parse_clock, parse_expires_in
from readyagents.errors import WaitError

WAIT_RECORD_VERSION = 1
ON_DEADLINE = frozenset({"fail", "continue", "escalate", "branch"})
WHICHEVER = frozenset({"first", "all"})


@dataclass
class WaitRecord:
    node_id: str
    until: str
    deadline_at: str
    on_deadline: str = "fail"
    whichever: str = "first"
    for_event: dict[str, Any] | None = None
    for_file: dict[str, Any] | None = None
    for_run: dict[str, Any] | None = None
    default: Any = None
    escalate_to: list[str] = field(default_factory=list)
    created_at: str = ""
    wait_record_version: int = WAIT_RECORD_VERSION
    dormant_since: str = ""
    rebroker: bool = True

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "wait_record_version": self.wait_record_version,
            "node_id": self.node_id,
            "until": self.until,
            "deadline_at": self.deadline_at,
            "on_deadline": self.on_deadline,
            "whichever": self.whichever,
            "created_at": self.created_at,
            "dormant_since": self.dormant_since,
            "rebroker": self.rebroker,
        }
        if self.for_event:
            row["for_event"] = dict(self.for_event)
        if self.for_file:
            row["for_file"] = dict(self.for_file)
        if self.for_run:
            row["for_run"] = dict(self.for_run)
        if self.default is not None:
            row["default"] = self.default
        if self.escalate_to:
            row["escalate_to"] = list(self.escalate_to)
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> WaitRecord:
        data = dict(raw or {})
        whichever = str(data.get("whichever") or "first").strip().lower()
        if whichever not in WHICHEVER:
            whichever = "first"
        action = str(data.get("on_deadline") or "fail").strip().lower()
        if action not in ON_DEADLINE:
            action = "fail"
        return cls(
            node_id=str(data.get("node_id") or ""),
            until=str(data.get("until") or ""),
            deadline_at=str(data.get("deadline_at") or ""),
            on_deadline=action,
            whichever=whichever,
            for_event=dict(data["for_event"]) if isinstance(data.get("for_event"), dict) else None,
            for_file=dict(data["for_file"]) if isinstance(data.get("for_file"), dict) else None,
            for_run=dict(data["for_run"]) if isinstance(data.get("for_run"), dict) else None,
            default=data.get("default"),
            escalate_to=[str(x) for x in list(data.get("escalate_to") or [])],
            created_at=str(data.get("created_at") or ""),
            wait_record_version=int(data.get("wait_record_version") or WAIT_RECORD_VERSION),
            dormant_since=str(data.get("dormant_since") or ""),
            rebroker=bool(data.get("rebroker", True)),
        )


def resolve_deadline(until: str, *, now: datetime) -> datetime:
    text = str(until or "").strip()
    if not text:
        raise WaitError("wait requires until (timestamp or duration)")
    stamp = parse_clock(text)
    if stamp is not None:
        return stamp
    try:
        seconds = parse_expires_in(text)
    except Exception as exc:
        raise WaitError(
            f"until must be a duration like 72h or an ISO timestamp, not {until!r}"
        ) from exc
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC) + timedelta(seconds=seconds)


def parse_now(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        stamp = parse_clock(value) or datetime.now(UTC)
    else:
        stamp = datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)
