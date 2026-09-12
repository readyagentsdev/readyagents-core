"""Promotion gate: min-improvement, named frozen regressions, mandatory hold-out."""

from __future__ import annotations

from typing import Any

from readyagents.optimize.record import ScoreSnapshot
from readyagents.prompts.registry import diff_versions
from readyagents.testing.eval import EvalCase


def split_holdout(
    cases: list[EvalCase],
    hold_out: list[EvalCase] | None,
) -> tuple[list[EvalCase], list[EvalCase]]:
    if hold_out:
        return list(cases), list(hold_out)
    if len(cases) < 2:
        return list(cases), []
    n_hold = max(1, len(cases) // 3)
    return cases[:-n_hold], cases[-n_hold:]


def frozen_regressions(
    baseline: ScoreSnapshot,
    candidate: ScoreSnapshot,
    frozen_names: set[str],
) -> list[str]:
    """Name fixtures that passed at baseline and fail under the candidate."""
    named: list[str] = []
    for name in frozen_names:
        base_fail = name in baseline.failed_names
        cand_fail = name in candidate.failed_names
        if not base_fail and cand_fail:
            named.append(name)
    return named


def holdout_regressed(baseline: ScoreSnapshot, candidate: ScoreSnapshot) -> bool:
    return candidate.pass_rate < baseline.pass_rate


def should_promote(
    *,
    delta: float,
    min_improvement: float,
    regressions: list[str],
    holdout_regression: bool,
) -> bool:
    if regressions:
        return False
    if holdout_regression:
        return False
    return delta >= min_improvement


def approval_payload(
    workflow: Any,
    prompt_id: str,
    *,
    left: int,
    right: int,
    delta: float,
) -> dict[str, Any]:
    diff = ""
    try:
        diff = diff_versions(workflow, prompt_id, left=left, right=right)
    except Exception:  # noqa: BLE001
        diff = ""
    return {
        "required": True,
        "diff": diff,
        "delta": delta,
        "from_version": left,
        "to_version": right,
        "prompt_id": prompt_id,
    }
