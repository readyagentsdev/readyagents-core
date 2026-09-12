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


def compare_models(
    refs: list[str],
    *,
    suite: Path | str | None = None,
    scenario: str | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Run the same suite/scenario once per model ref on identical inputs."""
    from readyagents.bench.run import run_bench
    from readyagents.bench.suite import load_suite

    if len(refs) < 2:
        raise BenchRefused("--models needs at least two refs", reason="models")
    rows = load_suite(suite)
    names = [scenario] if scenario else None
    if names:
        rows = [row for row in rows if row.name in set(names)]
        if not rows:
            raise BenchRefused(f"no matching scenario {scenario!r}", reason="scenarios")
    snapshot = {row.name: dict(row.inputs) for row in rows}
    reports = [
        run_bench(suite, settings=settings, scenarios=[row.name for row in rows], model=ref)
        for ref in refs
    ]
    for report, ref in zip(reports, refs, strict=True):
        for sc in report.scenarios:
            if sc.model != ref:
                raise BenchRefused(
                    f"model {ref!r} was not applied (run used {sc.model!r})",
                    reason="model",
                )
    ok = all(sc.success for report in reports for sc in report.scenarios)
    return {
        "kind": "models",
        "models": list(refs),
        "inputs": snapshot,
        "reports": [row.as_dict() for row in reports],
        "ok": ok,
    }


def compare_workflows(
    paths: list[str],
    *,
    inputs: dict[str, Any] | None = None,
    settings: Any = None,
) -> dict[str, Any]:
    """Run each workflow through eval with the SAME inputs; emit side-by-side metrics."""
    import time

    from readyagents.bench.layout import MODE_OFFLINE
    from readyagents.bench.metrics import collect
    from readyagents.testing.eval import EvalCase, run_eval
    from readyagents.workflow.state import RunState

    if len(paths) < 2:
        raise BenchRefused("--workflows needs at least two paths", reason="workflows")
    shared = dict(inputs or {})
    reports: list[dict[str, Any]] = []
    for raw in paths:
        wf = Path(raw)
        case = EvalCase(
            name=wf.stem,
            workflow=wf,
            inputs=dict(shared),
            expect_status="succeeded",
        )
        started = time.perf_counter()
        scored = run_eval([case], settings=settings)
        wall_ms = (time.perf_counter() - started) * 1000.0
        result = scored.results[0]
        state = result.state
        if state is None:
            state = RunState.start(wf.stem, dict(shared))
            state.status = "failed"
            state.errors = [result.reason]
        metrics = collect(
            state,
            name=wf.stem,
            shape=wf.stem,
            wall_ms=wall_ms,
            mode=MODE_OFFLINE,
        )
        reports.append(
            {
                "workflow": str(wf),
                "inputs": dict(state.inputs),
                "ok": scored.ok,
                "metrics": metrics.as_dict(),
            }
        )
    first = reports[0]["inputs"]
    if any(row["inputs"] != first for row in reports):
        raise BenchRefused("workflow compare used different inputs", reason="inputs")
    for key, value in shared.items():
        if first.get(key) != value:
            raise BenchRefused(
                "workflow compare did not apply the shared inputs to the run",
                reason="inputs",
            )
    return {
        "kind": "workflows",
        "workflows": [str(Path(p)) for p in paths],
        "inputs": first,
        "reports": reports,
        "ok": all(bool(row["ok"]) for row in reports),
    }
