"""Concurrency governor: limits, fair scheduling, back-pressure, shutdown."""

from __future__ import annotations

import threading
import time

import pytest

from readyagents.errors import GovernorBackpressure, GovernorShutdown
from readyagents.workflow.governor import ConcurrencyGovernor, RunPriority


def test_global_limit_not_exceeded() -> None:
    gov = ConcurrencyGovernor(global_limit=2, max_concurrency=2)
    current = 0
    max_seen = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal current, max_seen
        with gov.acquire(workflow="w", priority=RunPriority.BATCH):
            with lock:
                current += 1
                max_seen = max(max_seen, current)
            time.sleep(0.05)
            with lock:
                current -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert max_seen <= 2
    assert max_seen >= 1
    assert gov.in_flight() == 0


def test_interactive_is_served_before_batch() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1, queue_bound=16)
    order: list[str] = []
    held = threading.Event()
    finish = threading.Event()
    started_batch_wait = threading.Event()
    started_interactive = threading.Event()

    def holder() -> None:
        with gov.acquire(workflow="hold", priority=RunPriority.BATCH):
            held.set()
            finish.wait(timeout=5)

    def batch_wait() -> None:
        held.wait(timeout=5)
        started_batch_wait.set()
        with gov.acquire(workflow="b", priority=RunPriority.BATCH):
            order.append("batch")

    def interactive() -> None:
        started_batch_wait.wait(timeout=5)
        time.sleep(0.05)
        started_interactive.set()
        with gov.acquire(workflow="i", priority=RunPriority.INTERACTIVE):
            order.append("interactive")

    threads = [
        threading.Thread(target=holder),
        threading.Thread(target=batch_wait),
        threading.Thread(target=interactive),
    ]
    for thread in threads:
        thread.start()
    assert started_interactive.wait(timeout=5)
    time.sleep(0.05)
    finish.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert order[0] == "interactive"
    assert "batch" in order


def test_queue_bound_raises_backpressure() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1, queue_bound=1)
    held = threading.Event()
    finish = threading.Event()

    def holder() -> None:
        with gov.acquire(workflow="h"):
            held.set()
            finish.wait(timeout=5)

    t_hold = threading.Thread(target=holder)
    t_hold.start()
    assert held.wait(timeout=5)

    waiting = threading.Event()

    def waiter() -> None:
        waiting.set()
        with gov.acquire(workflow="w"):
            pass

    t_wait = threading.Thread(target=waiter)
    t_wait.start()
    waiting.wait(timeout=5)
    time.sleep(0.05)
    with pytest.raises(GovernorBackpressure, match="full"):
        with gov.acquire(workflow="x"):
            pass
    finish.set()
    t_hold.join(timeout=5)
    t_wait.join(timeout=5)


def test_retry_after_is_backpressure_not_retry_storm() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    gov = ConcurrencyGovernor(
        global_limit=8,
        max_concurrency=8,
        provider_rate=1.0,
        provider_burst=1.0,
        clock=now,
    )
    gov.note_retry_after("openai", 10.0)
    acquired = 0

    def try_once() -> None:
        nonlocal acquired
        try:
            with gov.acquire(
                workflow="w",
                provider="openai",
                timeout=0.0,
            ):
                acquired += 1
        except GovernorBackpressure:
            pass

    threads = [threading.Thread(target=try_once) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert acquired == 0
    clock["t"] = 11.0
    with gov.acquire(workflow="w", provider="openai", timeout=0.0):
        acquired += 1
    assert acquired == 1


def test_shutdown_refuses_new_leases_and_drains() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    held = threading.Event()
    finish = threading.Event()

    def holder() -> None:
        with gov.acquire(workflow="h"):
            held.set()
            finish.wait(timeout=5)

    t_hold = threading.Thread(target=holder)
    t_hold.start()
    assert held.wait(timeout=5)
    gov.request_shutdown()
    with pytest.raises(GovernorShutdown):
        with gov.acquire(workflow="new"):
            pass
    finish.set()
    t_hold.join(timeout=5)
    assert gov.wait_drain(timeout=2)
    assert gov.in_flight() == 0


def test_nested_acquire_does_not_consume_second_slot() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    with gov.acquire(workflow="outer"):
        assert gov.in_flight() == 1
        with gov.acquire(workflow="inner"):
            assert gov.in_flight() == 1
        assert gov.in_flight() == 1
    assert gov.in_flight() == 0


def test_hard_ceiling_clamps_global_limit() -> None:
    gov = ConcurrencyGovernor(global_limit=10_000, max_concurrency=4)
    assert gov.global_limit == 4
    assert gov.max_concurrency == 4


def test_per_workflow_limit() -> None:
    gov = ConcurrencyGovernor(global_limit=4, per_workflow_limit=1, max_concurrency=4)
    held = threading.Event()
    finish = threading.Event()

    def holder() -> None:
        with gov.acquire(workflow="only"):
            held.set()
            finish.wait(timeout=5)

    t_hold = threading.Thread(target=holder)
    t_hold.start()
    assert held.wait(timeout=5)
    with pytest.raises(GovernorBackpressure):
        with gov.acquire(workflow="only", timeout=0.0):
            pass
    with gov.acquire(workflow="other", timeout=0.0):
        pass
    finish.set()
    t_hold.join(timeout=5)
