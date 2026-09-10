"""Shipped `readyagents batch` and library runner: isolation, fail-continue, spend."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.cost.meter import SharedBudget, SpendMeter
from readyagents.errors import BudgetExceeded
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.batch import load_input_rows, run_batch
from readyagents.workflow.governor import ConcurrencyGovernor, RunPriority
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def _echo_wf(tmp: Path) -> Path:
    path = tmp / "echo.yaml"
    path.write_text(
        "name: echo_row\nrequired_inputs: [n]\nnodes:\n"
        "  - id: t\n    type: transform\n    template: 'n={{n}}'\n    output_key: msg\n",
        encoding="utf-8",
    )
    return path


def _agent_wf(tmp: Path) -> Path:
    path = tmp / "agent.yaml"
    path.write_text(
        "name: agent_row\nnodes:\n"
        "  - id: a\n    type: agent\n    prompt: 'say hi'\n    output_key: text\n",
        encoding="utf-8",
    )
    return path


def test_load_jsonl_and_csv(tmp_path: Path) -> None:
    jsonl = tmp_path / "rows.jsonl"
    jsonl.write_text('{"n": 1}\n{"n": 2}\n', encoding="utf-8")
    assert load_input_rows(jsonl) == [{"n": 1}, {"n": 2}]
    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("n\n1\n2\n", encoding="utf-8")
    assert load_input_rows(csv_path) == [{"n": 1}, {"n": 2}]
    array_path = tmp_path / "rows.json"
    array_path.write_text('[{"n": 3}, {"inputs": {"n": 4}}]', encoding="utf-8")
    assert load_input_rows(array_path) == [{"n": 3}, {"n": 4}]


def test_batch_per_row_isolation_and_failing_row(tmp_path: Path, tmp_settings) -> None:
    wf = _echo_wf(tmp_path)
    secret = "s3cret-other-row"
    rows = [{"n": 1}, {}, {"n": secret}]
    out = tmp_path / "results.jsonl"
    gov = ConcurrencyGovernor(global_limit=2, max_concurrency=2)
    report = run_batch(
        wf,
        rows,
        concurrency=2,
        persist=False,
        governor=gov,
        out=out,
        settings=tmp_settings,
    )
    assert report.total == 3
    assert report.succeeded == 2
    assert report.failed == 1
    assert report.results[0].outputs == {"msg": "n=1"}
    assert report.results[2].outputs == {"msg": f"n={secret}"}
    assert report.results[1].status == "failed"
    assert report.results[1].outputs != report.results[0].outputs
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert [row["index"] for row in lines] == [0, 1, 2]
    summary = json.dumps(report.as_dict())
    assert secret not in summary
    assert secret not in (report.results[1].error or "")


def test_batch_respects_concurrency(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    wf = _echo_wf(tmp_path)
    current = 0
    max_seen = 0
    lock = threading.Lock()
    real = run_workflow_file

    def wrapped(*args, **kwargs):
        nonlocal current, max_seen
        with lock:
            current += 1
            max_seen = max(max_seen, current)
        try:
            time.sleep(0.04)
            return real(*args, **kwargs)
        finally:
            with lock:
                current -= 1

    monkeypatch.setattr("readyagents.workflow.batch.run_workflow_file", wrapped)
    gov = ConcurrencyGovernor(global_limit=2, max_concurrency=2)
    report = run_batch(
        wf,
        [{"n": i} for i in range(6)],
        concurrency=2,
        persist=False,
        governor=gov,
        settings=tmp_settings,
    )
    assert report.succeeded == 6
    assert max_seen <= 2
    assert max_seen >= 1


def test_continue_on_error_false_skips_remaining(tmp_path: Path, tmp_settings) -> None:
    wf = _echo_wf(tmp_path)
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    report = run_batch(
        wf,
        [{"n": 1}, {}, {"n": 3}],
        concurrency=1,
        continue_on_error=False,
        persist=False,
        governor=gov,
        settings=tmp_settings,
    )
    assert report.failed == 1
    assert report.succeeded == 1
    assert report.skipped >= 1
    assert report.results[2].status == "skipped"


def test_cli_batch_jsonl_twice(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    wf = _echo_wf(tmp_path)
    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"n": 1}\n{}\n{"n": 3}\n', encoding="utf-8")
    out = tmp_path / "out.jsonl"
    args = [
        "batch",
        str(wf),
        "--input-file",
        str(rows),
        "--concurrency",
        "2",
        "--out",
        str(out),
        "--no-persist",
        "--json",
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 1, first.stdout + first.stderr
    data = json.loads(first.stdout[first.stdout.find("{") :])
    assert data["command"] == "batch"
    assert data["ok"] is False
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert "inputs" not in data["rows"][0]
    second = runner.invoke(app, args)
    assert second.exit_code == 1, second.stdout + second.stderr
    again = json.loads(second.stdout[second.stdout.find("{") :])
    assert again["succeeded"] == data["succeeded"]
    assert again["failed"] == data["failed"]
    assert again["total"] == data["total"]
    text = first.stdout + first.stderr + json.dumps(data)
    assert "n=3" not in json.dumps(data["rows"][1])
    del text
    clear_settings_cache()


def test_cli_batch_examples_jsonl() -> None:
    result = runner.invoke(
        app,
        [
            "batch",
            str(ROOT / "examples" / "batch_echo.yaml"),
            "--input-file",
            str(ROOT / "examples" / "batch_rows.jsonl"),
            "--concurrency",
            "2",
            "--no-persist",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["succeeded"] == 2
    assert data["failed"] == 1
    assert data["workflow"] == "batch_echo"


def test_cli_batch_csv(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    result = runner.invoke(
        app,
        [
            "batch",
            str(ROOT / "examples" / "batch_echo.yaml"),
            "--input-file",
            str(ROOT / "examples" / "batch_rows.csv"),
            "--concurrency",
            "2",
            "--no-persist",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["ok"] is True
    assert data["succeeded"] == 2
    assert data["failed"] == 0
    clear_settings_cache()


def test_run_still_byte_identical_keys() -> None:
    result = runner.invoke(
        app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    for key in ("ok", "command", "run_id", "status", "node_results", "usage"):
        assert key in data


def test_shared_budget_caps_across_rows(tmp_path: Path, tmp_settings) -> None:
    wf = _agent_wf(tmp_path)
    llm = ScriptedLLM()
    llm.enqueue("one", usage={"prompt_tokens": 10, "completion_tokens": 10})
    llm.enqueue("two", usage={"prompt_tokens": 10, "completion_tokens": 10})
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    report = run_batch(
        wf,
        [{}, {}],
        concurrency=1,
        persist=False,
        governor=gov,
        max_spend=0.0,
        settings=tmp_settings,
        run_kwargs={"llm": llm, "override_budget": True},
    )
    assert report.failed >= 1
    assert report.succeeded + report.failed == 2
    assert any(row.error_type == "BudgetExceeded" for row in report.results)


def test_shared_budget_unit_fail_closed() -> None:
    budget = SharedBudget(max_spend_micros=10)
    meter = SpendMeter(max_spend_micros=1000, shared_budget=budget)
    meter.consult_before_call("gpt-4o-mini", prompt_tokens=1, completion_tokens=1)
    with pytest.raises(BudgetExceeded):
        other = SpendMeter(max_spend_micros=1000, shared_budget=budget)
        other.consult_before_call(
            "gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000
        )


def test_nested_governor_with_run_workflow_file(tmp_path: Path, tmp_settings) -> None:
    wf = _echo_wf(tmp_path)
    gov = ConcurrencyGovernor(global_limit=1, max_concurrency=1)
    with gov.acquire(workflow="echo_row", priority=RunPriority.INTERACTIVE):
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
