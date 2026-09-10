"""Process-local concurrency governor.

Frozen contract
---------------
``acquire(workflow, provider, priority)`` returns a context-manager lease.
Interactive priority is served before batch. The wait queue is bounded;
overflow raises ``GovernorBackpressure`` rather than queueing without limit.
A token bucket per provider plus ``note_retry_after`` implements back-pressure
(wait / slow down) instead of a retry storm. ``request_shutdown`` refuses new
leases and lets holders finish. The hard ceiling cannot be raised by a
workflow file.

Re-entrancy: a thread that already holds a lease nested-acquires as a no-op
so ``include`` / ``run_workflow_file`` inside an in-flight run cannot deadlock.

The synchronous engine remains the implementation. This governor bounds who
may enter it.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from readyagents.errors import GovernorBackpressure, GovernorShutdown

_ABS_CEILING = 4096
_DEFAULT_GLOBAL = 32
_DEFAULT_QUEUE = 256
_TLS = threading.local()


class RunPriority(IntEnum):
    INTERACTIVE = 0
    BATCH = 1


def hard_concurrency_ceiling(raw: str | None = None) -> int:
    """Operator-set hard cap. Workflows cannot raise this."""
    text = raw if raw is not None else os.environ.get("READYAGENTS_MAX_CONCURRENCY", "")
    if text is None or not str(text).strip():
        value = _ABS_CEILING
    else:
        try:
            value = int(str(text).strip())
        except ValueError:
            value = _ABS_CEILING
    return max(1, min(value, _ABS_CEILING))


@dataclass
class _Waiter:
    priority: int
    seq: int
    workflow: str
    provider: str | None
    event: threading.Event = field(default_factory=threading.Event)
    granted: bool = False


@dataclass
class _Bucket:
    tokens: float
    updated: float
    rate: float
    burst: float


class GovernorLease:
    """Held slot. Nested leases do not release the outer slot."""

    def __init__(
        self,
        governor: ConcurrencyGovernor,
        *,
        workflow: str,
        provider: str | None,
        nested: bool,
    ) -> None:
        self._governor = governor
        self.workflow = workflow
        self.provider = provider
        self._nested = nested
        self._released = False

    def __enter__(self) -> GovernorLease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        depth = int(getattr(_TLS, "depth", 0) or 0)
        if depth > 0:
            _TLS.depth = depth - 1
        if self._nested:
            return
        self._governor._release(self.workflow, self.provider)


class ConcurrencyGovernor:
    """Global / per-workflow / per-provider slots + token-bucket back-pressure."""

    def __init__(
        self,
        *,
        global_limit: int | None = None,
        per_workflow_limit: int | None = None,
        per_provider_limit: int | None = None,
        queue_bound: int = _DEFAULT_QUEUE,
        provider_rate: float | None = None,
        provider_burst: float | None = None,
        max_concurrency: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        ceiling = (
            max(1, min(int(max_concurrency), _ABS_CEILING))
            if max_concurrency is not None
            else hard_concurrency_ceiling()
        )
        requested = _DEFAULT_GLOBAL if global_limit is None else int(global_limit)
        self.max_concurrency = ceiling
        self.global_limit = max(1, min(requested, ceiling))
        self.per_workflow_limit = (
            max(1, min(int(per_workflow_limit), self.global_limit))
            if per_workflow_limit is not None
            else None
        )
        self.per_provider_limit = (
            max(1, min(int(per_provider_limit), self.global_limit))
            if per_provider_limit is not None
            else None
        )
        self.queue_bound = max(1, int(queue_bound))
        self.provider_rate = None if provider_rate is None else max(0.0, float(provider_rate))
        burst = provider_burst
        if burst is None and self.provider_rate is not None:
            burst = max(1.0, self.provider_rate)
        self.provider_burst = None if burst is None else max(0.0, float(burst))
        self._clock = clock or time.monotonic
        self._cond = threading.Condition()
        self._global_used = 0
        self._wf_used: dict[str, int] = defaultdict(int)
        self._prov_used: dict[str, int] = defaultdict(int)
        self._interactive: deque[_Waiter] = deque()
        self._batch: deque[_Waiter] = deque()
        self._queued = 0
        self._seq = 0
        self._shutdown = False
        self._retry_until: dict[str, float] = {}
        self._buckets: dict[str, _Bucket] = {}
        self._hooks: list[Callable[[], None]] = []

    def on_shutdown(self, callback: Callable[[], None]) -> None:
        self._hooks.append(callback)

    def is_shutdown(self) -> bool:
        with self._cond:
            return self._shutdown

    def in_flight(self) -> int:
        with self._cond:
            return self._global_used

    def queued(self) -> int:
        with self._cond:
            return self._queued

    def request_shutdown(self) -> None:
        hooks: list[Callable[[], None]]
        with self._cond:
            if self._shutdown:
                hooks = []
            else:
                self._shutdown = True
                for waiter in list(self._interactive) + list(self._batch):
                    waiter.event.set()
                self._cond.notify_all()
                hooks = list(self._hooks)
        for hook in hooks:
            hook()

    def wait_drain(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else self._clock() + max(0.0, float(timeout))
        with self._cond:
            while self._global_used > 0:
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()
            return True

    def note_retry_after(self, provider: str, seconds: float) -> None:
        """Pause new work for ``provider`` until Retry-After elapses."""
        name = (provider or "").strip().lower()
        if not name:
            return
        until = self._clock() + max(0.0, float(seconds))
        with self._cond:
            prev = self._retry_until.get(name, 0.0)
            if until > prev:
                self._retry_until[name] = until
            self._cond.notify_all()

    def acquire(
        self,
        *,
        workflow: str,
        provider: str | None = None,
        priority: RunPriority | int = RunPriority.BATCH,
        timeout: float | None = None,
    ) -> GovernorLease:
        depth = int(getattr(_TLS, "depth", 0) or 0)
        if depth > 0:
            _TLS.depth = depth + 1
            return GovernorLease(self, workflow=workflow, provider=provider, nested=True)
        self._acquire_slot(
            workflow=workflow or "-",
            provider=_provider_key(provider),
            priority=int(priority),
            timeout=timeout,
        )
        _TLS.depth = 1
        return GovernorLease(self, workflow=workflow or "-", provider=provider, nested=False)

    def _acquire_slot(
        self,
        *,
        workflow: str,
        provider: str | None,
        priority: int,
        timeout: float | None,
    ) -> None:
        deadline = None if timeout is None else self._clock() + max(0.0, float(timeout))
        waiter: _Waiter | None = None
        with self._cond:
            while True:
                if self._shutdown and (waiter is None or not waiter.granted):
                    if waiter is not None:
                        self._drop_waiter(waiter)
                    raise GovernorShutdown("governor shutdown; no new runs")
                if waiter is not None and waiter.granted:
                    return
                if (
                    waiter is None
                    and self._queued == 0
                    and self._fits(workflow, provider)
                    and self._tokens_ready(provider)
                ):
                    self._enter(workflow, provider)
                    self._consume_token(provider)
                    return
                if waiter is None:
                    if self._queued >= self.queue_bound:
                        raise GovernorBackpressure(f"governor queue is full ({self.queue_bound})")
                    self._seq += 1
                    waiter = _Waiter(
                        priority=priority,
                        seq=self._seq,
                        workflow=workflow,
                        provider=provider,
                    )
                    if priority <= int(RunPriority.INTERACTIVE):
                        self._interactive.append(waiter)
                    else:
                        self._batch.append(waiter)
                    self._queued += 1
                    self._grant_waiters_locked()
                    if waiter.granted:
                        return
                remaining: float | None = None
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._drop_waiter(waiter)
                        raise GovernorBackpressure("governor acquire timed out")
                delay = self._next_wake_delay(provider)
                if remaining is not None:
                    delay = remaining if delay is None else min(delay, remaining)
                self._cond.wait(timeout=delay)
                self._grant_waiters_locked()

    def _fits(self, workflow: str, provider: str | None) -> bool:
        if self._global_used >= self.global_limit:
            return False
        if (
            self.per_workflow_limit is not None
            and self._wf_used[workflow] >= self.per_workflow_limit
        ):
            return False
        if (
            provider
            and self.per_provider_limit is not None
            and self._prov_used[provider] >= self.per_provider_limit
        ):
            return False
        return True

    def _enter(self, workflow: str, provider: str | None) -> None:
        self._global_used += 1
        self._wf_used[workflow] += 1
        if provider:
            self._prov_used[provider] += 1

    def _release(self, workflow: str, provider: str | None) -> None:
        key = _provider_key(provider)
        with self._cond:
            self._global_used = max(0, self._global_used - 1)
            self._wf_used[workflow] = max(0, self._wf_used[workflow] - 1)
            if key:
                self._prov_used[key] = max(0, self._prov_used[key] - 1)
            self._grant_waiters_locked()
            self._cond.notify_all()

    def _drop_waiter(self, waiter: _Waiter) -> None:
        for queue in (self._interactive, self._batch):
            try:
                queue.remove(waiter)
                self._queued = max(0, self._queued - 1)
                return
            except ValueError:
                continue

    def _grant_waiters_locked(self) -> None:
        progressed = True
        while progressed:
            progressed = False
            for queue in (self._interactive, self._batch):
                idx = 0
                while idx < len(queue):
                    waiter = queue[idx]
                    if waiter.granted:
                        idx += 1
                        continue
                    if self._shutdown:
                        waiter.event.set()
                        idx += 1
                        continue
                    if self._fits(waiter.workflow, waiter.provider) and self._tokens_ready(
                        waiter.provider
                    ):
                        queue.remove(waiter)
                        self._queued = max(0, self._queued - 1)
                        self._enter(waiter.workflow, waiter.provider)
                        self._consume_token(waiter.provider)
                        waiter.granted = True
                        waiter.event.set()
                        progressed = True
                        break
                    idx += 1
                if progressed:
                    break

    def _bucket(self, provider: str) -> _Bucket:
        bucket = self._buckets.get(provider)
        if bucket is None:
            burst = self.provider_burst if self.provider_burst is not None else 1.0
            rate = self.provider_rate if self.provider_rate is not None else 0.0
            bucket = _Bucket(tokens=burst, updated=self._clock(), rate=rate, burst=burst)
            self._buckets[provider] = bucket
        return bucket

    def _refill(self, provider: str) -> None:
        if self.provider_rate is None:
            return
        bucket = self._bucket(provider)
        now = self._clock()
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(bucket.burst, bucket.tokens + bucket.rate * elapsed)
        bucket.updated = now

    def _tokens_ready(self, provider: str | None) -> bool:
        if not provider or self.provider_rate is None:
            return self._retry_until.get(provider or "", 0.0) <= self._clock()
        if self._retry_until.get(provider, 0.0) > self._clock():
            return False
        self._refill(provider)
        return self._bucket(provider).tokens >= 1.0

    def _consume_token(self, provider: str | None) -> None:
        if not provider or self.provider_rate is None:
            return
        self._refill(provider)
        bucket = self._bucket(provider)
        bucket.tokens = max(0.0, bucket.tokens - 1.0)

    def _next_wake_delay(self, provider: str | None) -> float | None:
        now = self._clock()
        delays: list[float] = []
        if provider:
            until = self._retry_until.get(provider, 0.0)
            if until > now:
                delays.append(until - now)
            if self.provider_rate:
                self._refill(provider)
                bucket = self._bucket(provider)
                if bucket.tokens < 1.0 and bucket.rate > 0:
                    delays.append((1.0 - bucket.tokens) / bucket.rate)
        for until in self._retry_until.values():
            if until > now:
                delays.append(until - now)
        if not delays:
            return None
        return max(0.01, min(delays))


_DEFAULT: ConcurrencyGovernor | None = None
_DEFAULT_LOCK = threading.Lock()


def get_governor() -> ConcurrencyGovernor:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ConcurrencyGovernor()
        return _DEFAULT


def reset_governor_for_tests() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None
    if hasattr(_TLS, "depth"):
        _TLS.depth = 0


def _provider_key(provider: str | None) -> str | None:
    if provider is None:
        return None
    text = str(provider).strip().lower()
    return text or None


def parse_retry_after_seconds(raw: Any) -> float | None:
    """Seconds from a Retry-After header (delta-seconds only)."""
    if raw is None or not str(raw).strip():
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None
