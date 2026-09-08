"""Cooperative cancellation for in-flight workflow runs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from readyagents.errors import CancellationRequested

_SLEEP_CHUNK = 0.05


class CancellationToken:
    """Thread-safe cooperative cancellation flag (threading.Event)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._actor: str | None = None
        self._reason: str | None = None
        self._listeners: list[Callable[[], None]] = []

    def request(self, *, actor: str | None = None, reason: str | None = None) -> None:
        listeners: list[Callable[[], None]]
        with self._lock:
            first = not self._event.is_set()
            if first:
                self._actor = actor
                self._reason = reason
            listeners = list(self._listeners)
            self._event.set()
        if first:
            for listener in listeners:
                listener()

    def is_requested(self) -> bool:
        return self._event.is_set()

    def raise_if_requested(self, *, run_id: str | None = None) -> None:
        if not self.is_requested():
            return
        raise CancellationRequested(run_id=run_id, reason=self.reason)

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds. True if cancellation was requested."""
        return self._event.wait(max(float(timeout), 0.0))

    @property
    def actor(self) -> str | None:
        with self._lock:
            return self._actor

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Invoke ``callback`` on the first ``request()`` (or immediately if already set)."""
        with self._lock:
            self._listeners.append(callback)
            pending = self._event.is_set()
        if pending:
            callback()


def cancellable_sleep(seconds: float, token: CancellationToken | None) -> None:
    """Sleep ``seconds``, or until ``token`` is requested (then raise CancellationRequested)."""
    delay = max(float(seconds), 0.0)
    if token is None:
        time.sleep(delay)
        return
    if delay <= 0:
        token.raise_if_requested()
        return
    remaining = delay
    while remaining > 0:
        chunk = remaining if remaining < _SLEEP_CHUNK else _SLEEP_CHUNK
        if token.wait(chunk):
            token.raise_if_requested()
            return
        remaining -= chunk
    token.raise_if_requested()
