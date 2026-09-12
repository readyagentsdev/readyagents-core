"""Cadence and unused-after durations. Days, not a scheduler."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from readyagents.approvals.gate import parse_clock
from readyagents.errors import RegistryRefused

_DUR = re.compile(r"^(\d+)\s*(d|day|days|w|week|weeks)$", re.I)


def parse_days(text: str) -> int:
    raw = str(text or "").strip()
    match = _DUR.fullmatch(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        return amount * 7 if unit.startswith("w") else amount
    try:
        from readyagents.approvals.gate import parse_expires_in

        seconds = parse_expires_in(raw)
    except Exception as extra:
        raise RegistryRefused(
            f"duration must be like 90d or 12w, not {text!r}",
            reason="duration",
        ) from extra
    return max(1, int(seconds // 86400))


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    stamp = parse_clock(str(value).strip())
    if stamp is not None:
        return stamp
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def is_overdue(last: str | None, cadence: str, *, now: datetime | None = None) -> bool:
    stamp = parse_date(last)
    if stamp is None:
        return True
    clock = now or datetime.now(UTC)
    return clock > stamp + timedelta(days=parse_days(cadence))


def is_stale(last_run: str | None, unused_after: str, *, now: datetime | None = None) -> bool:
    """True when there is no run, or the last run is older than unused_after."""
    clock = now or datetime.now(UTC)
    stamp = parse_date(last_run)
    if stamp is None:
        return True
    return clock > stamp + timedelta(days=parse_days(unused_after))


def past_date(value: str | None, *, now: datetime | None = None) -> bool:
    stamp = parse_date(value)
    if stamp is None:
        return False
    clock = now or datetime.now(UTC)
    return clock.date() > stamp.date()
