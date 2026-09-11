"""Adversarial bench suite. Drive shipped APIs only. No skip/xfail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readyagents.bench.compare import compare_results, load_baseline
from readyagents.bench.guard import no_network
from readyagents.bench.layout import SECRET_NEEDLES
from readyagents.bench.run import run_bench
from readyagents.bench.suite import assert_synthetic_cassette, load_suite
from readyagents.errors import BenchError, BenchRefused

_ROOT = Path(__file__).resolve().parents[1]
_SUITE = _ROOT / "examples" / "bench" / "suite.yaml"
_BASELINE = _ROOT / "baselines" / "bench_offline.json"


def test_shipped_cassettes_are_synthetic_only() -> None:
    for row in load_suite(_SUITE):
        text = row.cassette.read_text(encoding="utf-8")
        lowered = text.lower()
        for needle in SECRET_NEEDLES:
            assert needle.lower() not in lowered
        assert_synthetic_cassette(row.cassette)
        assert "sk-abcdefghijksecret" not in text
        assert "@" not in text or "example.com" not in text


def test_paid_looking_secret_in_cassette_is_refused(tmp_path: Path) -> None:
    planted = tmp_path / "planted.json"
    planted.write_text(
        json.dumps(
            {
                "cassette_version": 1,
                "readyagents_version": "1.9.0",
                "run_id": "x",
                "workflow": "x",
                "recorded_at": "2026-01-01T00:00:00+00:00",
                "redacted": True,
                "positional_fallback": False,
                "entries": {},
                "determinism": {
                    "sealed": [],
                    "recomputed": [],
                    "unsealable": [],
                    "misses": [],
                    "positional_fallback": False,
                },
                "blocked_nodes": [],
                "note": "sk-abcdefghijksecret",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BenchError, match="synthetic"):
        assert_synthetic_cassette(planted)


def test_live_under_ci_without_trigger_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_settings
) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(BenchRefused) as caught:
        run_bench(_SUITE, live=True, allow_ci_live=False, settings=tmp_settings)
    assert caught.value.reason == "ci"


def test_wall_clock_noise_is_not_a_regression(tmp_settings) -> None:
    report = run_bench(_SUITE, settings=tmp_settings)
    baseline = load_baseline(_BASELINE)
    noisy = json.loads(json.dumps(report.as_dict()))
    for row in noisy["scenarios"]:
        off = row.get("timing_offline") or {}
        off["wall_ms"] = float(off.get("wall_ms") or 1) + 50.0
        row["timing_offline"] = off
        row["wall_ms"] = off["wall_ms"]
    baseline["scenarios"]["classify"]["wall_ms"] = 1.0
    compared = compare_results(noisy, baseline)
    assert compared.ok, compared.regressions


def test_offline_and_live_timings_are_not_one_number(tmp_settings) -> None:
    report = run_bench(_SUITE, settings=tmp_settings)
    blob = json.dumps(report.as_dict())
    assert "timing_offline" in blob
    assert "offline_engine" in blob
    for row in report.scenarios:
        assert row.timing_offline is not None
        assert row.timing_live is None
        dumped = row.as_dict()
        assert dumped["timing_offline"]["kind"] == "offline_engine"
        assert dumped["timing_live"] is None


def test_socket_guard_blocks_connect() -> None:
    import socket

    with no_network(), pytest.raises(BenchRefused, match="network"):
        socket.create_connection(("example.com", 80), timeout=1)
