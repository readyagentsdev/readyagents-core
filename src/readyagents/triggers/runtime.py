"""Process-local stores so concurrent duplicates share one lock. No threads started."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from readyagents.triggers.caps import RateLimiter
from readyagents.triggers.store import ConcurrencyGate, IdempotencyStore, TriggerSpend

_lock = threading.Lock()
_stores: dict[str, IdempotencyStore] = {}
_gates: dict[str, ConcurrencyGate] = {}
_spends: dict[str, TriggerSpend] = {}
_limiters: dict[str, RateLimiter] = {}


def _key(home: Path) -> str:
    return str(Path(home).expanduser().resolve())


def store_for(home: Path, *, clock: Any = None) -> IdempotencyStore:
    key = _key(home)
    with _lock:
        found = _stores.get(key)
        if found is None:
            found = IdempotencyStore(Path(home), clock=clock)
            _stores[key] = found
        return found


def gate_for(home: Path) -> ConcurrencyGate:
    key = _key(home)
    with _lock:
        found = _gates.get(key)
        if found is None:
            found = ConcurrencyGate()
            _gates[key] = found
        return found


def spend_for(home: Path) -> TriggerSpend:
    key = _key(home)
    with _lock:
        found = _spends.get(key)
        if found is None:
            found = TriggerSpend()
            _spends[key] = found
        return found


def limiter_for(home: Path, *, clock: Any = None) -> RateLimiter:
    key = _key(home)
    with _lock:
        found = _limiters.get(key)
        if found is None:
            found = RateLimiter(clock=clock)
            _limiters[key] = found
        return found
