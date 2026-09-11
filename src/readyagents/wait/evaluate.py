"""Pure wake evaluation. Inject time and world; core starts no timer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from readyagents.approvals.gate import parse_clock
from readyagents.wait.record import WaitRecord, parse_now


@dataclass
class WaitWorld:
    """Injected world. Tests never start a watcher."""

    events: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, str] = field(default_factory=dict)


@dataclass
class WaitOutcome:
    satisfied: bool
    deadline: bool
    reason: str
    payload: Any = None
    waiting_for: list[str] = field(default_factory=list)


def evaluate_wait(
    record: WaitRecord,
    *,
    now: datetime | str,
    world: WaitWorld | None = None,
) -> WaitOutcome:
    """Return whether the wait is satisfied, past deadline, or still waiting."""
    clock = parse_now(now)
    env = world or WaitWorld()
    hits: list[tuple[str, Any]] = []
    waiting_for: list[str] = []
    event_hit = _event(record, env)
    if record.for_event:
        if event_hit is not None:
            hits.append(("event", event_hit))
        else:
            waiting_for.append("event")
    file_hit = _file(record, env, clock)
    if record.for_file:
        if file_hit is not None:
            hits.append(("file", file_hit))
        else:
            waiting_for.append("file")
    run_hit = _run(record, env)
    if record.for_run:
        if run_hit is not None:
            hits.append(("run", run_hit))
        else:
            waiting_for.append("run")
    extras = sum(1 for spec in (record.for_event, record.for_file, record.for_run) if spec)
    if extras:
        if record.whichever == "all":
            ready = len(hits) == extras
        else:
            ready = bool(hits)
    else:
        ready = False
    deadline_at = parse_clock(record.deadline_at)
    past = deadline_at is not None and clock >= deadline_at
    if ready:
        reason, payload = hits[0]
        if record.whichever == "all":
            payload = {name: val for name, val in hits}
            reason = "all"
        return WaitOutcome(
            satisfied=True, deadline=False, reason=reason, payload=payload, waiting_for=[]
        )
    if past:
        return WaitOutcome(
            satisfied=False,
            deadline=True,
            reason="deadline",
            payload=record.default,
            waiting_for=waiting_for,
        )
    waiting_for.append("until")
    return WaitOutcome(
        satisfied=False, deadline=False, reason="waiting", payload=None, waiting_for=waiting_for
    )


def _event(record: WaitRecord, world: WaitWorld) -> Any | None:
    spec = record.for_event
    if not spec:
        return None
    name = str(spec.get("name") or "").strip()
    match = spec.get("match") if isinstance(spec.get("match"), dict) else {}
    for item in world.events:
        if str(item.get("name") or "") != name:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if all(str(payload.get(k)) == str(v) for k, v in match.items()):
            return dict(item)
    return None


def _file(record: WaitRecord, world: WaitWorld, now: datetime) -> Any | None:
    spec = record.for_file
    if not spec:
        return None
    path = str(spec.get("path") or "").strip()
    kind = str(spec.get("on") or "created").strip().lower()
    info = world.files.get(path) or {}
    if info.get("symlink"):
        return None
    if not info.get("exists"):
        return None
    if kind == "changed":
        mtime = _as_instant(info.get("mtime"))
        created = _as_instant(record.created_at)
        if mtime is None:
            return None
        if created is not None and mtime <= created:
            return None
    return {"path": path, "on": kind}


def _as_instant(value: Any) -> datetime | None:
    """Parse ISO timestamps or unix epoch seconds. Never string-compare them."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return parse_now(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    parsed = parse_clock(text)
    if parsed is not None:
        return parse_now(parsed)
    try:
        return datetime.fromtimestamp(float(text), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _run(record: WaitRecord, world: WaitWorld) -> Any | None:
    spec = record.for_run
    if not spec:
        return None
    run_id = str(spec.get("run_id") or spec.get("id") or "").strip()
    wanted = str(spec.get("status") or "succeeded").strip().lower()
    status = str(world.runs.get(run_id) or "")
    if status == wanted:
        return {"run_id": run_id, "status": status}
    return None
