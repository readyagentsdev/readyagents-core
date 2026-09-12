"""Score → reflect → candidates → score → keep-if-threshold. Typed budget stops."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from readyagents.errors import (
    OptimizeRefused,
    OptimizeStopIterations,
    OptimizeStopSpend,
    OptimizeStopWall,
)
from readyagents.optimize.gate import (
    approval_payload,
    frozen_regressions,
    holdout_regressed,
    should_promote,
    split_holdout,
)
from readyagents.optimize.generate import bind_generator, failure_payload, generate_candidates
from readyagents.optimize.layout import (
    DEFAULT_CANDIDATES,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MIN_IMPROVEMENT,
    STOP_COMPLETE,
    STOP_ITERATIONS,
    STOP_SPEND,
    STOP_WALL,
)
from readyagents.optimize.persist import iterations_from_state, load_state, save_state
from readyagents.optimize.record import CandidateRecord, IterationRecord, OptimizeReport
from readyagents.optimize.score import require_holdout, score_suite
from readyagents.policy import Redactor
from readyagents.prompts.layout import content_hash
from readyagents.prompts.registry import add_version, get_prompt, register_literals
from readyagents.prompts.safety import is_config_shaped
from readyagents.replay.record import known_secret_values
from readyagents.testing.eval import EvalCase, load_eval_suite
from readyagents.workflow.runner import load_workflow


def optimize_workflow(
    workflow: Path | str,
    eval_suite: Path | str | list[EvalCase],
    *,
    node: str | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_spend: float | None = None,
    max_wall_seconds: float | None = None,
    min_improvement: float = DEFAULT_MIN_IMPROVEMENT,
    candidates: int = DEFAULT_CANDIDATES,
    hold_out: Path | str | list[EvalCase] | None = None,
    frozen: Path | str | list[EvalCase] | None = None,
    require_approval: bool = False,
    model: str | None = None,
    llm: Any = None,
    score_llm: Any = None,
    settings: Any = None,
    resume: bool = True,
    clock: Any = None,
    secrets: list[str] | None = None,
    redactor: Any = None,
) -> OptimizeReport:
    source = Path(workflow)
    if not source.is_file():
        raise OptimizeRefused(f"workflow not found: {source}", reason="workflow")
    yaml_bytes = source.read_bytes()
    spec = load_workflow(source)
    node_id = node or _first_prompt_node(spec)
    if not node_id:
        raise OptimizeRefused("workflow has no prompt-bearing node", reason="node")
    register_literals(source)
    if source.read_bytes() != yaml_bytes:
        raise OptimizeRefused("optimize rewrote the workflow file", reason="rewrite")
    prompt_id = node_id
    for item in spec.nodes:
        if item.id == node_id:
            prompt_id = str(getattr(item, "prompt_id", None) or item.id)
            break
    current = get_prompt(source, prompt_id)
    cases = _load_cases(eval_suite)
    hold_cases = _load_cases(hold_out) if hold_out is not None else None
    train, hold = split_holdout(cases, hold_cases)
    require_holdout(train, hold)
    frozen_cases = _load_cases(frozen) if frozen is not None else []
    frozen_names = {row.name for row in frozen_cases}
    frozen_names.update(row.name for row in train if row.cassette is not None)
    frozen_names.update(row.name for row in hold if row.cassette is not None)
    secret_list = list(secrets or [])
    if settings is not None:
        secret_list.extend(known_secret_values(settings))
    scrubber = (
        redactor
        if redactor is not None
        else Redactor(
            literals=secret_list,
            patterns=(
                r"ghp_[A-Za-z0-9]{8,}",
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            ),
        )
    )
    now = clock or time.monotonic
    started = now()
    spend = 0.0
    iterations: list[IterationRecord] = []
    if resume:
        prior = load_state(source)
        if prior:
            iterations = iterations_from_state(prior)
            spend = float(prior.get("spend_usd") or spend)
    generator = None
    gen_model = model or "scripted"

    def _wall() -> None:
        if max_wall_seconds is not None and (now() - started) >= max_wall_seconds:
            raise OptimizeStopWall()

    def _spend() -> None:
        if max_spend is not None and spend >= max_spend:
            raise OptimizeStopSpend(f"spend budget exhausted ({spend} >= {max_spend})")

    baseline_train, blocked_train = score_suite(
        train,
        settings=settings,
        llm=score_llm,
        prompt_text=current.text,
        node_id=node_id,
        secrets=secret_list,
    )
    baseline_hold, blocked_hold = score_suite(
        hold,
        settings=settings,
        llm=score_llm,
        prompt_text=current.text,
        node_id=node_id,
        secrets=secret_list,
    )
    baseline_frozen, _ = (
        score_suite(
            frozen_cases,
            settings=settings,
            llm=score_llm,
            prompt_text=current.text,
            node_id=node_id,
            secrets=secret_list,
        )
        if frozen_cases
        else (None, [])
    )
    blocked = list(blocked_train) + list(blocked_hold)
    if baseline_hold.total == 0:
        raise OptimizeRefused(
            "held-out set is mandatory; every hold-out case was blocked or empty",
            reason="holdout",
        )
    hold_baseline_rate = baseline_hold.pass_rate
    last_hold_rate = hold_baseline_rate
    adopted_hold_rate: float | None = None
    best_score = baseline_train.pass_rate
    promoted = False
    approval: dict[str, Any] | None = None
    regressions: list[str] = []
    stop: Any = None
    stop_reason = STOP_COMPLETE
    start_index = 0
    if iterations:
        start_index = max(row.index for row in iterations) + 1
        for row in iterations:
            if row.promoted:
                promoted = True
            if row.candidates:
                best_score = max(best_score, max(c.train_score for c in row.candidates))

    try:
        while True:
            _wall()
            _spend()
            if start_index >= max_iterations:
                raise OptimizeStopIterations()
            if generator is None:
                generator, gen_model = bind_generator(llm, model, settings)
            failures = failure_payload(baseline_train)
            texts, spend = generate_candidates(
                current=current.text,
                failures=failures,
                n=max(1, candidates),
                llm=generator,
                model=gen_model,
                redactor=scrubber,
                spend_usd=spend,
                max_spend=max_spend,
            )
            _persist(source, node_id, prompt_id, spend, iterations)
            _wall()
            if max_spend is not None and spend > max_spend:
                raise OptimizeStopSpend(f"spend budget exhausted ({spend} > {max_spend})")
            iter_row = IterationRecord(
                index=start_index,
                train=baseline_train.as_dict(),
                held_out=baseline_hold.as_dict(),
                spend_usd=spend,
                blocked_cases=list(blocked),
            )
            for text in texts:
                stored = add_version(
                    source,
                    prompt_id,
                    text,
                    source="candidate",
                    activate=False,
                    node_id=node_id,
                )
                scorer = score_llm if score_llm is not None else generator
                train_snap, _ = score_suite(
                    train,
                    settings=settings,
                    llm=scorer,
                    prompt_text=text,
                    node_id=node_id,
                    secrets=secret_list,
                    replay=False,
                )
                hold_snap, _ = score_suite(
                    hold,
                    settings=settings,
                    llm=scorer,
                    prompt_text=text,
                    node_id=node_id,
                    secrets=secret_list,
                    replay=False,
                )
                spend = spend + train_snap.spend_usd + hold_snap.spend_usd
                _persist(source, node_id, prompt_id, spend, iterations)
                if max_spend is not None and spend > max_spend:
                    raise OptimizeStopSpend(f"spend budget exhausted ({spend} > {max_spend})")
                frozen_snap = baseline_frozen
                if frozen_cases:
                    frozen_snap, _ = score_suite(
                        frozen_cases,
                        settings=settings,
                        llm=scorer,
                        prompt_text=text,
                        node_id=node_id,
                        secrets=secret_list,
                        replay=False,
                    )
                    spend = spend + frozen_snap.spend_usd
                    _persist(source, node_id, prompt_id, spend, iterations)
                named = frozen_regressions(
                    baseline_frozen or baseline_train,
                    frozen_snap or train_snap,
                    frozen_names,
                )
                # Also name train cassette fixtures that newly fail.
                named.extend(frozen_regressions(baseline_train, train_snap, frozen_names))
                named.extend(frozen_regressions(baseline_hold, hold_snap, frozen_names))
                named = list(dict.fromkeys(named))
                last_hold_rate = hold_snap.pass_rate
                hold_bad = holdout_regressed(baseline_hold, hold_snap)
                if hold_bad:
                    named.append(f"hold-out:{','.join(hold_snap.failed_names) or 'score'}")
                delta = train_snap.pass_rate - baseline_train.pass_rate
                adopt = should_promote(
                    delta=delta,
                    min_improvement=min_improvement,
                    regressions=named,
                    holdout_regression=hold_bad,
                )
                cand = CandidateRecord(
                    prompt_id=prompt_id,
                    version=stored.version,
                    content_hash=stored.content_hash,
                    text=stored.text,
                    train_score=train_snap.pass_rate,
                    held_out_score=hold_snap.pass_rate,
                    regressions=named,
                    adopted=False,
                    config_shaped=is_config_shaped(text),
                )
                if adopt and not require_approval:
                    add_version(
                        source,
                        prompt_id,
                        text,
                        source="candidate",
                        activate=True,
                        node_id=node_id,
                    )
                    cand.adopted = True
                    promoted = True
                    current = get_prompt(source, prompt_id)
                    best_score = train_snap.pass_rate
                    baseline_train = train_snap
                    baseline_hold = hold_snap
                    last_hold_rate = hold_snap.pass_rate
                    adopted_hold_rate = hold_snap.pass_rate
                elif adopt and require_approval:
                    approval = approval_payload(
                        source,
                        prompt_id,
                        left=current.version,
                        right=stored.version,
                        delta=delta,
                    )
                    best_score = max(best_score, train_snap.pass_rate)
                else:
                    best_score = max(best_score, train_snap.pass_rate)
                    regressions.extend(named)
                iter_row.candidates.append(cand)
                iter_row.regressions.extend(named)
                iter_row.promoted = iter_row.promoted or cand.adopted
            iterations.append(iter_row)
            _persist(source, node_id, prompt_id, spend, iterations)
            start_index += 1
            if start_index >= max_iterations:
                raise OptimizeStopIterations()
    except OptimizeStopIterations as extra:
        stop = extra
        stop_reason = STOP_ITERATIONS
    except OptimizeStopSpend as extra:
        stop = extra
        stop_reason = STOP_SPEND
    except OptimizeStopWall as extra:
        stop = extra
        stop_reason = STOP_WALL

    if source.read_bytes() != yaml_bytes:
        raise OptimizeRefused("optimize rewrote the workflow file", reason="rewrite")
    active = get_prompt(source, prompt_id)
    first_train = baseline_train.pass_rate
    if iterations:
        raw = (iterations[0].train or {}).get("pass_rate")
        first_train = float(raw) if raw is not None else first_train
    delta = best_score - first_train
    held_score = hold_baseline_rate
    if adopted_hold_rate is not None:
        held_score = adopted_hold_rate
    elif last_hold_rate is not None:
        held_score = last_hold_rate
    held = {
        "score": held_score,
        "baseline": hold_baseline_rate,
        "regression": False,
    }
    if iterations:
        held["regression"] = bool(
            any("hold-out:" in n for row in iterations for n in row.regressions)
        )
    report = OptimizeReport(
        ok=True,
        stop_reason=stop_reason,
        promoted=promoted,
        prompt_id=prompt_id,
        node_id=node_id,
        active_version=active.version,
        content_hash=active.content_hash or content_hash(active.text),
        min_improvement=min_improvement,
        baseline_score=first_train,
        best_score=best_score,
        delta=delta,
        spend_usd=round(spend, 6),
        held_out=held,
        regressions=list(dict.fromkeys(regressions)),
        iterations=iterations,
        approval=approval,
        blocked_cases=blocked,
        stop=stop,
    )
    if stop is not None:
        stop.report = report
    return report


def _load_cases(raw: Path | str | list[EvalCase] | None) -> list[EvalCase]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    return load_eval_suite(raw)


def _first_prompt_node(spec: Any) -> str | None:
    for node in spec.nodes:
        if node.prompt:
            return node.id
    return None


def _persist(
    source: Path,
    node_id: str,
    prompt_id: str,
    spend: float,
    iterations: list[IterationRecord],
) -> None:
    save_state(
        source,
        {
            "workflow": source.name,
            "node_id": node_id,
            "prompt_id": prompt_id,
            "spend_usd": spend,
            "iterations": [row.as_dict() for row in iterations],
        },
    )
