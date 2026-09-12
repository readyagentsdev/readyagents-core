"""Score eval cases through the shipped ``run_eval`` harness. Never a parallel scorer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.errors import OptimizeRefused
from readyagents.optimize.record import ScoreSnapshot
from readyagents.prompts.layout import content_hash
from readyagents.prompts.safety import case_secret_hits
from readyagents.testing.eval import EvalCase, run_eval
from readyagents.workflow.runner import load_workflow


def score_suite(
    cases: list[EvalCase],
    *,
    settings: Any = None,
    llm: Any = None,
    prompt_text: str | None = None,
    node_id: str | None = None,
    secrets: list[str] | None = None,
) -> tuple[ScoreSnapshot, list[dict[str, str]]]:
    """Drive ``run_eval`` so determinism / nodes / tools / usage fails are real fails.

    Cassette-backed cases are scored with ``llm=None`` so the engine uses
    CassetteProvider and does not construct a live provider.
    """
    if not cases:
        return ScoreSnapshot(), []
    blocked: list[dict[str, str]] = []
    usable: list[EvalCase] = []
    temps: list[Path] = []
    try:
        for case in cases:
            hits = case_secret_hits(
                {
                    "name": case.name,
                    "inputs": case.inputs,
                    "expect_contains": case.expect_contains,
                    "expect_outputs": case.expect_outputs,
                },
                secrets,
            )
            if hits:
                blocked.append({"name": case.name, "reason": "secret"})
                continue
            prepared = case
            if prompt_text is not None and node_id:
                wf = case.workflow
                if isinstance(wf, (Path, str)) and Path(wf).is_file():
                    overlay = _overlay_prompt(Path(wf), node_id, prompt_text)
                    if overlay != Path(wf):
                        temps.append(overlay)
                    prepared = replace(case, workflow=overlay)
            usable.append(prepared)
        cassette_cases = [row for row in usable if row.cassette is not None]
        other_cases = [row for row in usable if row.cassette is None]
        results = []
        if cassette_cases:
            report = run_eval(cassette_cases, settings=settings, llm=None)
            results.extend(report.results)
        if other_cases:
            if llm is None:
                raise OptimizeRefused(
                    "non-cassette scoring would construct a live provider; "
                    "give every case a cassette or an explicit scorer",
                    reason="scoring",
                )
            report = run_eval(other_cases, settings=settings, llm=llm, dry_run=False)
            results.extend(report.results)
    finally:
        for path in temps:
            try:
                path.unlink()
            except OSError:
                pass
    passed = [row for row in results if row.passed]
    failed = [row for row in results if not row.passed]
    spend = 0.0
    reasons: dict[str, str] = {}
    for row in results:
        reasons[row.name] = row.reason
        state = row.state
        if state is None:
            continue
        usage = dict(state.usage or {})
        micros = int(usage.get("cost_micros") or 0)
        spend += micros / 1_000_000.0
    total = len(results)
    rate = (len(passed) / total) if total else 0.0
    snap = ScoreSnapshot(
        passed=len(passed),
        failed=len(failed),
        total=total,
        pass_rate=rate,
        spend_usd=round(spend, 6),
        failed_names=[row.name for row in failed],
        reasons=reasons,
    )
    return snap, blocked


def _overlay_prompt(source: Path, node_id: str, text: str) -> Path:
    spec = load_workflow(source)
    found = False
    nodes: list[dict[str, Any]] = []
    for node in spec.nodes:
        data = node.model_dump(mode="json", by_alias=True, exclude_none=True)
        if node.id == node_id:
            data["prompt"] = text
            found = True
        nodes.append(data)
    if not found:
        return source
    payload = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    payload["nodes"] = nodes
    digest = content_hash(text)[:12]
    dest = source.parent / f".{source.stem}.opt-{digest}{source.suffix}"
    dumped = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    atomic_write_text(dest, dumped)
    return dest


def require_holdout(train: list[EvalCase], hold: list[EvalCase]) -> None:
    if not hold:
        raise OptimizeRefused(
            "a held-out set is mandatory; pass --hold-out or include at least two eval cases",
            reason="holdout",
        )
    if not train:
        raise OptimizeRefused(
            "optimize needs at least one train case besides hold-out", reason="holdout"
        )
