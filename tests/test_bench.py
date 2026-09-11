"""Shipped bench: offline suite, metrics, compare, method, live refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.bench.compare import compare_results, load_baseline
from readyagents.bench.layout import MODE_OFFLINE, TIMING_LIVE, TIMING_OFFLINE
from readyagents.bench.run import run_bench
from readyagents.bench.suite import assert_synthetic_cassette, load_suite
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import BenchRefused

runner = CliRunner()
_ROOT = Path(__file__).resolve().parents[1]
_SUITE = _ROOT / "examples" / "bench" / "suite.yaml"
_BASELINE = _ROOT / "baselines" / "bench_offline.json"


def test_suite_has_six_shapes() -> None:
    rows = load_suite(_SUITE)
    assert {row.shape for row in rows} == {
        "classify",
        "research",
        "approval",
        "foreach",
        "team",
        "document",
    }
    for row in rows:
        assert_synthetic_cassette(row.cassette)


def test_bench_run_offline_zero_cost_and_metrics(tmp_settings) -> None:
    first = run_bench(_SUITE, settings=tmp_settings)
    second = run_bench(_SUITE, settings=tmp_settings)
    assert first.mode == MODE_OFFLINE
    assert first.spend_usd == 0.0
    assert first.aggregate()["success_rate"] == 1.0
    assert len(first.scenarios) == 6
    by_name = {row.name: row for row in first.scenarios}
    assert by_name["research"].node_count == 3
    assert by_name["research"].tool_calls == 2
    assert by_name["research"].success is True
    for row in first.scenarios:
        assert row.timing_offline is not None
        assert row.timing_offline.kind == TIMING_OFFLINE
        assert row.timing_live is None
        assert "wall_ms" in row.timing_offline.as_dict()
        assert TIMING_LIVE not in json.dumps(row.as_dict()["timing_offline"])
    for a, b in zip(first.scenarios, second.scenarios, strict=True):
        assert a.name == b.name
        assert a.tokens_in == b.tokens_in
        assert a.tokens_out == b.tokens_out
        assert a.cost_usd == b.cost_usd
        assert a.tool_calls == b.tool_calls
        assert a.node_count == b.node_count
        assert a.success == b.success
        assert a.determinism == b.determinism
        assert a.timing_offline is not None and b.timing_offline is not None
        assert a.timing_offline.kind == b.timing_offline.kind
    method = first.method
    for key in (
        "hardware",
        "os",
        "python",
        "package_version",
        "cassette_digests",
        "mode",
        "reproduce",
        "offline_vs_live",
    ):
        assert method.get(key)
    assert method["mode"] == MODE_OFFLINE
    assert "readyagents bench run" in str(method["reproduce"])


def test_bench_compare_regression_and_tolerance(tmp_settings) -> None:
    report = run_bench(_SUITE, settings=tmp_settings)
    baseline = load_baseline(_BASELINE)
    ok = compare_results(report, baseline)
    assert ok.ok
    broken = json.loads(json.dumps(report.as_dict()))
    broken["scenarios"][0]["node_count"] = 99
    bad = compare_results(broken, baseline)
    assert not bad.ok
    assert any("node_count" in line for line in bad.regressions)


def test_malformed_baseline_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchRefused, match="malformed"):
        load_baseline(path)
    path.write_text('{"schema": "nope", "scenarios": {"x": {}}}\n', encoding="utf-8")
    with pytest.raises(BenchRefused, match="schema"):
        load_baseline(path)


def test_live_refused_in_ci(monkeypatch: pytest.MonkeyPatch, tmp_settings) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(BenchRefused, match="CI"):
        run_bench(_SUITE, live=True, settings=tmp_settings)


def test_cli_bench_help_twice_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    first = runner.invoke(app, ["bench", "--help"])
    second = runner.invoke(app, ["bench", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    ran = runner.invoke(app, ["bench", "run", "--offline", "--json"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    payload = json.loads(ran.stdout[ran.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "bench run"
    assert payload["mode"] == "offline"
    assert payload["spend_usd"] == 0.0
    assert "timing_offline" in json.dumps(payload)
    dest = tmp_path / "out.json"
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    cmp_ = runner.invoke(
        app,
        ["bench", "compare", str(dest), "--baseline", str(_BASELINE), "--json"],
    )
    assert cmp_.exit_code == 0, cmp_.stdout + cmp_.stderr
    models = runner.invoke(
        app,
        ["bench", "compare", "--models", "mock:a,mock:b", "--scenario", "classify", "--json"],
    )
    assert models.exit_code == 0, models.stdout + models.stderr
    blob = json.loads(models.stdout[models.stdout.find("{") :])
    assert blob["inputs"] == "identical"
    assert len(blob["models"]) == 2
    clear_settings_cache()
