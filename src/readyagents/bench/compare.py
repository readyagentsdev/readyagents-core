"""Compare bench results to a baseline. Wall-clock uses a declared tolerance."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.bench.layout import (
    DEFAULT_WALL_MIN_MS,
    DEFAULT_WALL_PCT,
    METRIC_KEYS,
    SCHEMA_BASELINE,
    SCHEMA_RESULT,
)
from readyagents.bench.run import BenchReport
from readyagents.errors import BenchError, BenchRefused


@dataclass
class CompareReport:
    ok: bool
    deltas: list[dict[str, Any]] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deltas": list(self.deltas),
            "regressions": list(self.regressions),
        }


def load_baseline(path: Path | str) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file():
        raise BenchError(f"baseline not found: {file}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchRefused(f"malformed baseline JSON: {exc}", reason="baseline") from exc
    if not isinstance(data, dict):
        raise BenchRefused("baseline must be a mapping", reason="baseline")
    if data.get("schema") != SCHEMA_BASELINE:
        raise BenchRefused(
            f"baseline schema {data.get('schema')!r} != {SCHEMA_BASELINE}",
            reason="baseline",
        )
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        raise BenchRefused("baseline needs a scenarios mapping", reason="baseline")
    return data


def compare_results(
    current: BenchReport | dict[str, Any],
    baseline: dict[str, Any],
    *,
    tolerance: dict[str, float] | None = None,
) -> CompareReport:
    if isinstance(current, BenchReport):
        payload = current.as_dict()
    else:
        payload = dict(current)
    if payload.get("schema") not in {SCHEMA_RESULT, None}:
        raise BenchRefused("current result is not a bench result document", reason="result")
    declared = dict(baseline.get("tolerance") or {})
    if tolerance:
        declared.update(tolerance)
    wall_pct = float(declared.get("wall_ms_pct", DEFAULT_WALL_PCT))
    wall_min = float(declared.get("wall_ms_min", DEFAULT_WALL_MIN_MS))
    expected = dict(baseline.get("scenarios") or {})
    by_name = {
        row.get("name"): row for row in payload.get("scenarios") or [] if isinstance(row, dict)
    }
    deltas: list[dict[str, Any]] = []
    regressions: list[str] = []
    for name, want in expected.items():
        got = by_name.get(name)
        if not isinstance(want, dict):
            raise BenchRefused(f"baseline scenario {name!r} must be a mapping", reason="baseline")
        if got is None:
            regressions.append(f"{name}: missing from current")
            continue
        row: dict[str, Any] = {"name": name}
        for key in METRIC_KEYS:
            if key not in want:
                continue
            left = got.get(key)
            right = want.get(key)
            row[key] = {"got": left, "want": right, "delta": _delta(left, right)}
            if left != right:
                regressions.append(f"{name}.{key}: {left!r} != {right!r}")
        _check_wall(name, got, want, wall_pct, wall_min, row, regressions)
        deltas.append(row)
    return CompareReport(ok=not regressions, deltas=deltas, regressions=regressions)


def _check_wall(
    name: str,
    got: dict[str, Any],
    want: dict[str, Any],
    wall_pct: float,
    wall_min: float,
    row: dict[str, Any],
    regressions: list[str],
) -> None:
    got_ms = _wall_ms(got)
    want_ms = want.get("wall_ms")
    if got_ms is None or want_ms is None:
        return
    want_f = float(want_ms)
    row["wall_ms"] = {"got": got_ms, "want": want_f, "delta": got_ms - want_f}
    if want_f <= 0:
        return
    pct = abs(got_ms - want_f) / want_f * 100.0
    abs_delta = abs(got_ms - want_f)
    if pct > wall_pct and abs_delta > wall_min:
        regressions.append(
            f"{name}.wall_ms: {got_ms} vs {want_f} exceeds {wall_pct}% and {wall_min}ms"
        )


def _wall_ms(row: dict[str, Any]) -> float | None:
    for key in ("timing_offline", "timing_live"):
        blob = row.get(key)
        if isinstance(blob, dict) and blob.get("wall_ms") is not None:
            return float(blob["wall_ms"])
    return None


def _delta(left: Any, right: Any) -> Any:
    try:
        return type(right)(left - right)  # type: ignore[operator]
    except (TypeError, ValueError):
        return None
