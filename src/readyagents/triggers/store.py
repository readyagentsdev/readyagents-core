"""Idempotency, concurrency, spend, and dead letters. File-backed, no daemon."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import TriggerCeiling
from readyagents.permissions import restrict_file
from readyagents.workflow.state import utc_now

MAX_DEFER = 32


def _now(clock: Any) -> datetime:
    if callable(clock):
        stamp = clock()
    elif isinstance(clock, datetime):
        stamp = clock
    else:
        stamp = datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


class IdempotencyStore:
    """Map (workflow, trigger, key) to run_id inside the declared window."""

    def __init__(self, home: Path, *, clock: Any = None) -> None:
        self.path = Path(home) / "triggers" / "idempotency.json"
        self.clock = clock
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._rows: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(self._rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
            restrict=True,
        )
        restrict_file(self.path)

    def lock_for(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def lookup(self, key: str, *, window_s: float) -> str | None:
        now = _now(self.clock)
        with self._lock:
            row = self._rows.get(key)
            if not isinstance(row, dict):
                return None
            started = str(row.get("started_at") or "")
            try:
                at = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                return None
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if (now - at.astimezone(UTC)).total_seconds() > window_s:
                return None
            run_id = str(row.get("run_id") or "")
            return run_id or None

    def put(self, key: str, run_id: str, *, trigger: str) -> None:
        now = _now(self.clock)
        with self._lock:
            self._rows[key] = {
                "run_id": run_id,
                "trigger": trigger,
                "started_at": now.isoformat(),
            }
            self._save()


class ConcurrencyGate:
    """In-flight count per trigger. Drop or defer when the ceiling is hit."""

    def __init__(self, *, max_defer: int = MAX_DEFER) -> None:
        self.max_defer = int(max_defer)
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self._deferred: dict[str, deque[dict[str, Any]]] = {}

    def used(self, trigger: str) -> int:
        with self._lock:
            return int(self._active.get(trigger, 0))

    def try_enter(self, trigger: str, limit: int, *, on_ceiling: str) -> str:
        with self._lock:
            used = int(self._active.get(trigger, 0))
            if used < limit:
                self._active[trigger] = used + 1
                return "enter"
            if on_ceiling == "defer":
                q = self._deferred.setdefault(trigger, deque())
                if len(q) >= self.max_defer:
                    raise TriggerCeiling(used, limit, action="drop")
                return "defer"
            raise TriggerCeiling(used, limit, action="drop")

    def defer(self, trigger: str, payload: dict[str, Any]) -> int:
        with self._lock:
            q = self._deferred.setdefault(trigger, deque())
            used = int(self._active.get(trigger, 0))
            if len(q) >= self.max_defer:
                raise TriggerCeiling(used, self.max_defer, action="drop")
            q.append(payload)
            return len(q)

    def leave(self, trigger: str) -> None:
        with self._lock:
            used = int(self._active.get(trigger, 0))
            self._active[trigger] = max(0, used - 1)

    def deferred_count(self, trigger: str) -> int:
        with self._lock:
            return len(self._deferred.get(trigger, ()))


class TriggerSpend:
    """Running spend per trigger so a flood cannot drain the account."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usd: dict[str, float] = {}
        self._tokens: dict[str, int] = {}

    def used_usd(self, trigger: str) -> float:
        with self._lock:
            return float(self._usd.get(trigger, 0.0))

    def used_tokens(self, trigger: str) -> int:
        with self._lock:
            return int(self._tokens.get(trigger, 0))

    def add(self, trigger: str, *, usd: float = 0.0, tokens: int = 0) -> None:
        with self._lock:
            self._usd[trigger] = float(self._usd.get(trigger, 0.0)) + float(usd)
            self._tokens[trigger] = int(self._tokens.get(trigger, 0)) + int(tokens)


class DeadLetterLog:
    """Inspectable, replayable refusals. Never silently discarded."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "triggers" / "dead-letters.jsonl"
        self._lock = threading.Lock()

    def record(
        self,
        *,
        reason: str,
        trigger: str | None,
        digest: str | None,
        event_id: str | None,
        raw: str,
        extra: dict[str, Any] | None = None,
    ) -> str:
        letter_id = (event_id or digest or utc_now()).replace(":", "")
        row = {
            "id": letter_id,
            "reason": reason,
            "trigger": trigger,
            "digest": digest,
            "event_id": event_id,
            "raw": raw,
            "ts": utc_now(),
            **(extra or {}),
        }
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prior = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            atomic_write_text(
                self.path, prior + line + "\n", encoding="utf-8", newline="\n", restrict=True
            )
            restrict_file(self.path)
        return letter_id

    def list(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def get(self, letter_id: str) -> dict[str, Any] | None:
        for row in self.list():
            if str(row.get("id") or "") == letter_id:
                return row
        return None


class EventLog:
    """Recent trigger outcomes for `readyagents triggers events`."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / "triggers" / "events.jsonl"
        self._lock = threading.Lock()

    def record(self, row: dict[str, Any]) -> None:
        line = json.dumps({"ts": utc_now(), **row}, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prior = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
            atomic_write_text(
                self.path, prior + line + "\n", encoding="utf-8", newline="\n", restrict=True
            )
            restrict_file(self.path)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows[-max(0, int(limit)) :] if limit else rows
