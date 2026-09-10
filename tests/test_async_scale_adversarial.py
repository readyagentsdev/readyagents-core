"""Adversarial contention tests for V2-07 async/scale. Drive shipped APIs only."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.audit import read_audit_events
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.cost.meter import SharedBudget, SpendMeter
from readyagents.cost.prices import load_price_table
from readyagents.errors import BudgetExceeded, GovernorBackpressure, GovernorShutdown
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.batch import load_input_rows, run_batch
from readyagents.workflow.governor import ConcurrencyGovernor, RunPriority
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import load_run

runner = CliRunner()

SECRET_A = "iso-A-k3y-7f3c9a-ALPHA"
SECRET_B = "iso-B-k3y-e91d2b-BRAVO"


def _wait_until(pred: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _join(threads: list[threading.Thread], *, timeout: float = 5.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive()


def _transform_wf(tmp: Path, *, name: str = "echo_row") -> Path:
    path = tmp / f"{name}.yaml"
    path.write_text(
        f"name: {name}\nrequired_inputs: [n]\nnodes:\n"
        "  - id: t\n    type: transform\n    template: 'n={{n}}'\n"
        "    output_key: msg\n",
        encoding="utf-8",
    )
    return path


def _agent_wf(tmp: Path) -> Path:
    path = tmp / "agent_row.yaml"
    path.write_text(
        "name: agent_row\nnodes:\n"
        "  - id: a\n    type: agent\n    model: gpt-4o-mini\n"
        "    prompt: 'say {{n}}'\n    output_key: text\n",
        encoding="utf-8",
    )
    return path


def _mixed_approval_wf(tmp: Path) -> Path:
    path = tmp / "mixed_approval.yaml"
    path.write_text(
        "name: mixed_approval\nrequired_inputs: [kind]\nstart: branch\nnodes:\n"
        "  - id: branch\n    type: condition\n    when: 'kind == \"pause\"'\n"
        "    then: gate\n    else: ok\n"
        "  - id: gate\n    type: approval\n    prompt: 'Approve row?'\n"
        "    then: ok\n    else: denied\n"
        "  - id: ok\n    type: transform\n    template: 'done-{{kind}}'\n"
        "    output_key: msg\n"
        "  - id: denied\n    type: transform\n    template: 'denied-{{kind}}'\n"
        "    output_key: msg\n",
        encoding="utf-8",
    )
    return path


def test_concurrent_batch_rows_isolate_secrets(tmp_path: Path, tmp_settings) -> None:
    wf = _transform_wf(tmp_path)
    rows_path = tmp_path / "rows.jsonl"
    rows_path.write_text(
        json.dumps({"n": SECRET_A}) + "\n" + json.dumps({"inputs": {"n": SECRET_B}}) + "\n{}\n",
        encoding="utf-8",
    )
    loaded = load_input_rows(rows_path)
    assert loaded[0] == {"n": SECRET_A}
    assert loaded[1] == {"n": SECRET_B}
    out = tmp_path / "results.jsonl"
    gov = ConcurrencyGovernor(global_limit=2, max_concurrency=2)
    report = run_batch(
        wf,
        rows_path,
        concurrency=2,
        persist=False,
        governor=gov,
        out=out,
        settings=tmp_settings,
    )
    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    by_index = {row.index: row for row in report.results}
    assert by_index[0].outputs == {"msg": f"n={SECRET_A}"}
    assert by_index[1].outputs == {"msg": f"n={SECRET_B}"}
    assert SECRET_B not in json.dumps(by_index[0].as_record())
    assert SECRET_A not in json.dumps(by_index[1].as_record())
    failed = by_index[2]
    assert failed.status == "failed"
    err = failed.error or ""
    assert SECRET_A not in err
    assert SECRET_B not in err
    summary = json.dumps(report.as_dict())
    assert SECRET_A not in summary
    assert SECRET_B not in summary
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert [row["index"] for row in lines] == [0, 1, 2]
    assert SECRET_B not in json.dumps(lines[0])
    assert SECRET_A not in json.dumps(lines[1])
    assert SECRET_A not in json.dumps(lines[2].get("error"))
    assert SECRET_B not in json.dumps(lines[2].get("error"))


def test_shared_budget_concurrent_consult_never_exceeds() -> None:
    unit = load_price_table().quote("gpt-4o-mini").cost_micros(1_000_000, 0)
    assert unit is not None and unit > 0
    cap = unit + unit // 2
    budget = SharedBudget(max_spend_micros=cap)
    workers = 8
    barrier = threading.Barrier(workers)
    ok: list[int] = []
    denied: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        meter = SpendMeter(shared_budget=budget)
        barrier.wait(timeout=5)
        try:
            meter.consult_before_call("gpt-4o-mini", prompt_tokens=1_000_000)
            meter.record_usage(
                "gpt-4o-mini",
                {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 0,
                    "total_tokens": 1_000_000,
                },
                reserved_prompt=1_000_000,
                reserved_completion=0,
            )
            with lock:
                ok.append(1)
                assert budget.cost_micros <= budget.max_spend_micros
        except BudgetExceeded:
            with lock:
                denied.append(1)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    _join(threads)
    assert len(ok) == 1
    assert len(denied) == workers - 1
    assert budget.cost_micros <= budget.max_spend_micros
    assert budget.cost_micros == unit
    assert budget.remaining_micros() == cap - unit


def test_batch_shared_max_spend_records_budget_exceeded(tmp_path: Path, tmp_settings) -> None:
    wf = _agent_wf(tmp_path)
    llm = ScriptedLLM()
    for i in range(4):
        llm.enqueue(
            f"out-{i}",
            usage={"prompt_tokens": 10, "completion_tokens": 10},
        )
    gov = ConcurrencyGovernor(global_limit=3, max_concurrency=3)
    report = run_batch(
        wf,
        [{"n": i} for i in range(4)],
        concurrency=3,
        persist=False,
        governor=gov,
        max_spend=0.0,
        settings=tmp_settings,
        run_kwargs={"llm": llm, "override_budget": True, "no_cache": True},
    )
    assert len(report.results) == 4
    assert report.failed + report.succeeded + report.paused + report.skipped == 4
    exceeded = [row for row in report.results if row.error_type == "BudgetExceeded"]
    assert exceeded
    assert report.failed >= 1
    for row in report.results:
        assert row.status in {"failed", "succeeded", "paused", "skipped"}
        blob = json.dumps(row.as_summary_row())
        assert "prompt" not in blob


def test_persisted_batch_rows_distinct_run_ids_and_audit(tmp_path: Path, tmp_settings) -> None:
    wf = _transform_wf(tmp_path, name="persist_row")
    gov = ConcurrencyGovernor(global_limit=2, max_concurrency=2)
    report = run_batch(
        wf,
        [{"n": SECRET_A}, {"n": SECRET_B}],
        concurrency=2,
        persist=True,
        governor=gov,
        settings=tmp_settings,
    )
    assert report.succeeded == 2
    ids = [row.run_id for row in report.results]
    assert ids[0] and ids[1]
    assert ids[0] != ids[1]
    runs_dir = tmp_settings.runs_dir()
    rec_a = load_run(runs_dir, ids[0])
    rec_b = load_run(runs_dir, ids[1])
    assert rec_a.run_id != rec_b.run_id
    assert rec_a.inputs == {"n": SECRET_A}
    assert rec_b.inputs == {"n": SECRET_B}
    assert SECRET_B not in json.dumps(rec_a.to_record())
    assert SECRET_A not in json.dumps(rec_b.to_record())
    audit_dir = tmp_settings.audit_dir()
    for run_id, other in ((ids[0], SECRET_B), (ids[1], SECRET_A)):
        events = read_audit_events(audit_dir, run_id)
        assert events
        blob = json.dumps(events)
        assert other not in blob
        for event in events:
            assert event.get("run_id") == run_id


def test_retry_after_blocks_provider_until_fake_clock_advances() -> None:
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    gov = ConcurrencyGovernor(
        global_limit=32,
        max_concurrency=32,
        provider_rate=1.0,
        provider_burst=1.0,
        clock=now,
    )
    gov.note_retry_after("OpenAI", 10.0)
    acquired = 0
    lock = threading.Lock()

    def try_once() -> None:
        nonlocal acquired
        try:
            with gov.acquire(
                workflow="w",
                provider="openai",
                timeout=0.0,
            ):
                with lock:
                    acquired += 1
        except GovernorBackpressure:
            pass

    threads = [threading.Thread(target=try_once) for _ in range(24)]
    for thread in threads:
        thread.start()
    _join(threads, timeout=5)
    assert acquired == 0
    assert gov.in_flight() == 0
    clock["t"] = 11.0
    after: list[int] = []

    def try_after() -> None:
        try:
            with gov.acquire(workflow="w", provider="openai", timeout=0.0):
                after.append(1)
        except GovernorBackpressure:
            after.append(0)

    later = [threading.Thread(target=try_after) for _ in range(16)]
    for thread in later:
        thread.start()
    _join(later, timeout=5)
    assert 1 in after
    assert after.count(1) == 1


def test_shutdown_drains_holder_and_refuses_queued() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1, queue_bound=8)
    held = threading.Event()
    finish = threading.Event()
    granted: list[str] = []
    shutdown_hits: list[BaseException] = []

    def holder() -> None:
        with gov.acquire(workflow="hold", priority=RunPriority.BATCH):
            held.set()
            finish.wait(timeout=5)
            granted.append("holder-done")

    def waiter() -> None:
        try:
            with gov.acquire(workflow="queued", timeout=5):
                granted.append("waiter")
        except GovernorShutdown as exc:
            shutdown_hits.append(exc)

    t_hold = threading.Thread(target=holder)
    t_hold.start()
    assert held.wait(timeout=5)
    t_wait = threading.Thread(target=waiter)
    t_wait.start()
    _wait_until(lambda: gov.queued() >= 1)
    gov.request_shutdown()
    with pytest.raises(GovernorShutdown):
        with gov.acquire(workflow="new"):
            granted.append("new")
    finish.set()
    assert gov.wait_drain(timeout=5)
    _join([t_hold, t_wait])
    assert gov.in_flight() == 0
    assert "holder-done" in granted
    assert "waiter" not in granted
    assert "new" not in granted
    assert shutdown_hits
    assert gov.is_shutdown()


def test_queue_bound_overflow_raises_governor_backpressure() -> None:
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1, queue_bound=2)
    held = threading.Event()
    finish = threading.Event()

    def holder() -> None:
        with gov.acquire(workflow="h"):
            held.set()
            finish.wait(timeout=5)

    def waiter() -> None:
        with gov.acquire(workflow="w"):
            pass

    t_hold = threading.Thread(target=holder)
    t_hold.start()
    assert held.wait(timeout=5)
    waiters = [threading.Thread(target=waiter) for _ in range(2)]
    for i, thread in enumerate(waiters, start=1):
        thread.start()
        _wait_until(lambda n=i: gov.queued() >= n)
    with pytest.raises(GovernorBackpressure, match="full"):
        with gov.acquire(workflow="overflow"):
            pass
    finish.set()
    _join([t_hold, *waiters])
    assert gov.in_flight() == 0
    assert gov.queued() == 0


def test_nested_run_workflow_file_does_not_deadlock_global_limit_1(
    tmp_path: Path, tmp_settings
) -> None:
    wf = _transform_wf(tmp_path, name="nested_echo")
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    other_err: list[BaseException] = []
    finished = threading.Event()

    def competitor() -> None:
        try:
            with gov.acquire(workflow="other", timeout=0.0):
                other_err.append(RuntimeError("competitor got a slot"))
        except GovernorBackpressure as exc:
            other_err.append(exc)

    def nested() -> None:
        with gov.acquire(workflow="nested_echo", priority=RunPriority.INTERACTIVE):
            assert gov.in_flight() == 1
            t_other = threading.Thread(target=competitor)
            t_other.start()
            t_other.join(timeout=5)
            assert not t_other.is_alive()
            state = run_workflow_file(
                wf,
                inputs={"n": 7},
                persist=False,
                settings=tmp_settings,
                governor=gov,
                priority=RunPriority.INTERACTIVE,
            )
            assert state.status == "succeeded"
            assert state.output_keys["msg"] == "n=7"
            assert gov.in_flight() == 1
        finished.set()

    t_nested = threading.Thread(target=nested, daemon=True)
    t_nested.start()
    t_nested.join(timeout=5)
    assert not t_nested.is_alive(), "nested run_workflow_file deadlocked"
    assert finished.is_set()
    assert other_err and isinstance(other_err[0], GovernorBackpressure)
    assert gov.in_flight() == 0
    with gov.acquire(workflow="after", timeout=0.0):
        assert gov.in_flight() == 1


def test_batch_approval_pause_other_rows_succeed(tmp_path: Path, tmp_settings) -> None:
    wf = _mixed_approval_wf(tmp_path)
    gov = ConcurrencyGovernor(global_limit=3, max_concurrency=3)
    report = run_batch(
        wf,
        [{"kind": "go-1"}, {"kind": "pause"}, {"kind": "go-2"}],
        concurrency=3,
        persist=False,
        governor=gov,
        settings=tmp_settings,
    )
    assert report.total == 3
    assert report.paused == 1
    assert report.succeeded == 2
    assert report.failed == 0
    by_index = {row.index: row for row in report.results}
    assert by_index[0].status == "succeeded"
    assert by_index[0].outputs == {"msg": "done-go-1"}
    assert by_index[1].status == "paused"
    assert by_index[1].error_type == "ApprovalRequired"
    assert by_index[2].status == "succeeded"
    assert by_index[2].outputs == {"msg": "done-go-2"}
    assert "pause" not in (by_index[0].error or "")
    assert "go-1" not in json.dumps(by_index[1].as_summary_row())


def test_cli_batch_json_summary_omits_row_secrets(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    wf = _transform_wf(tmp_path)
    rows = tmp_path / "rows.jsonl"
    rows.write_text(
        json.dumps({"n": SECRET_A}) + "\n" + json.dumps({"n": SECRET_B}) + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "batch",
            str(wf),
            "--input-file",
            str(rows),
            "--concurrency",
            "2",
            "--no-persist",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["command"] == "batch"
    assert data["ok"] is True
    assert data["succeeded"] == 2
    blob = json.dumps(data)
    assert SECRET_A not in blob
    assert SECRET_B not in blob
    assert "inputs" not in data["rows"][0]
    assert "outputs" not in data["rows"][0]
    clear_settings_cache()
