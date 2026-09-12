"""Like-for-like incumbent vs candidate eval. Holdout score is mandatory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.distill.canary import CANARY_PROMPT, CANARY_TOKEN, canary_pass
from readyagents.distill.dataset import load_manifest
from readyagents.distill.schema import EvalComparison, SideScore
from readyagents.errors import DistillHoldoutMissing, DistillRefused
from readyagents.testing.eval import EvalCase, EvalReport, load_eval_suite, run_eval
from readyagents.trust.digest import digest_bytes


def evaluate(
    *,
    suite: Path | str,
    incumbent_llm: Any = None,
    candidate_llm: Any = None,
    dataset: Path | str | None = None,
    adapter: Path | str | None = None,
    tuner: Any = None,
    settings: Any = None,
    canary_secret: str = CANARY_TOKEN,
) -> EvalComparison:
    cases = load_eval_suite(suite)
    holdout = _holdout_cases(dataset) if dataset is not None else []
    if not holdout:
        raise DistillHoldoutMissing()
    fixture_digest = digest_bytes(Path(suite).read_bytes())
    inc = run_eval(cases, llm=incumbent_llm, settings=settings)
    cand = run_eval(cases, llm=candidate_llm, settings=settings)
    inc_h = run_eval(holdout, llm=incumbent_llm, settings=settings)
    cand_h = run_eval(holdout, llm=candidate_llm, settings=settings)
    regressions = [
        row.name
        for row, other in zip(inc.results, cand.results, strict=False)
        if row.passed and not other.passed
    ]
    planted = False
    if adapter is not None:
        planted = canary_pass(adapter, secret=canary_secret, tuner=tuner, prompt=CANARY_PROMPT)
    comparison = EvalComparison(
        incumbent=_side(inc, inc_h),
        candidate=_side(cand, cand_h),
        holdout={
            "named": True,
            "split": "holdout",
            "incumbent": _rate(inc_h),
            "candidate": _rate(cand_h),
            "cases": [row.name for row in cand_h.results],
        },
        frozen_regressions=regressions,
        canary_passed=planted if adapter is not None else None,
        fixture_digest=fixture_digest,
    )
    if comparison.candidate.holdout is None or comparison.incumbent.holdout is None:
        raise DistillHoldoutMissing()
    return comparison


def _holdout_cases(dataset: Path | str) -> list[EvalCase]:
    folder = Path(dataset)
    try:
        manifest = load_manifest(folder)
    except DistillRefused as extra:
        raise DistillHoldoutMissing() from extra
    if manifest.counts.get("holdout", 0) <= 0:
        raise DistillHoldoutMissing()
    path = folder / "holdout.jsonl"
    if not path.is_file():
        raise DistillHoldoutMissing()
    cases: list[EvalCase] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        expected = str(row.get("response") or "")
        instruction = str(row.get("instruction") or "echo")
        cases.append(
            EvalCase(
                name=str(row.get("id") or f"holdout-{index}"),
                workflow={
                    "name": f"holdout-{index}",
                    "default_model": "mock:distill",
                    "nodes": [
                        {
                            "id": "out",
                            "type": "agent",
                            "prompt": instruction,
                            "output_key": "output",
                        }
                    ],
                },
                expect_status="succeeded",
                expect_contains={"output": expected[:80]} if expected else None,
            )
        )
    if not cases:
        raise DistillHoldoutMissing()
    return cases


def _side(report: EvalReport, holdout: EvalReport) -> SideScore:
    latencies: list[float] = []
    cost = 0
    trajectory: list[str] = []
    for row in report.results:
        state = row.state
        if state is None:
            continue
        cost += int((state.usage or {}).get("cost_micros") or 0)
        for result in state.results:
            trajectory.append(str(result.node_id))
            if result.total_ms is not None:
                latencies.append(float(result.total_ms))
    return SideScore(
        accuracy=_rate(report),
        passed=report.passed,
        failed=report.failed,
        trajectory=trajectory,
        latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
        cost_micros=cost,
        holdout=_rate(holdout),
    )


def _rate(report: EvalReport) -> float:
    total = report.passed + report.failed
    if total <= 0:
        return 0.0
    return round(report.passed / total, 6)
