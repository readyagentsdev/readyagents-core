"""Size, depth, and rate caps applied to raw event bytes before JSON parse."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from readyagents.errors import TriggerRefused

MAX_EVENT_BYTES = 64_000
MAX_JSON_DEPTH = 8
DEFAULT_RATE = 30
DEFAULT_RATE_WINDOW = 60.0


def refuse_before_parse(
    raw: bytes,
    *,
    trigger: str,
    limiter: RateLimiter | None = None,
    max_bytes: int = MAX_EVENT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
) -> None:
    if len(raw) > max_bytes:
        raise TriggerRefused("event exceeds size cap", reason="too_large")
    if json_nesting_depth(raw) > max_depth:
        raise TriggerRefused("event exceeds depth cap", reason="too_deep")
    if limiter is not None and not limiter.allow(trigger):
        raise TriggerRefused("event exceeds rate cap", reason="rate")


def json_nesting_depth(raw: bytes) -> int:
    text = raw.decode("utf-8", errors="replace")
    depth = 0
    max_depth = 0
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in "{[":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch in "}]":
            depth = max(0, depth - 1)
    return max_depth


class RateLimiter:
    """Sliding window of event arrivals per trigger. Injected clock; no thread."""

    def __init__(
        self,
        *,
        limit: int = DEFAULT_RATE,
        window_s: float = DEFAULT_RATE_WINDOW,
        clock: Any = None,
    ) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def _now(self) -> float:
        clock = self.clock
        if callable(clock):
            stamp = clock()
        elif clock is not None:
            stamp = clock
        else:
            stamp = datetime.now(UTC)
        if isinstance(stamp, datetime):
            return stamp.timestamp()
        return float(stamp)

    def allow(self, trigger: str) -> bool:
        now = self._now()
        with self._lock:
            hits = [t for t in self._hits.get(trigger, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[trigger] = hits
                return False
            hits.append(now)
            self._hits[trigger] = hits
            return True
