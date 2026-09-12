"""Gated one-node promotion, fallback pin, auto-demotion, ledger cost delta."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.cost.ledger import append_spend
from readyagents.distill.adapters import require_signed
from readyagents.distill.evaluate import evaluate
from readyagents.distill.schema import AdapterRecord, DistillConfig, EvalComparison
from readyagents.distill.store import (
    load_adapter,
    load_config,
    load_promoted,
    save_adapter,
    save_promoted,
)
from readyagents.errors import (
    DistillApproval,
    DistillCanary,
    DistillCost,
    DistillHoldoutMissing,
    DistillLatency,
    DistillParity,
    DistillRegression,
)
from readyagents.workflow.state import utc_now


def promote(
    adapter_id: str,
    node_id: str,
    *,
    settings: Settings | None = None,
    comparison: EvalComparison | None = None,
    suite: Path | str | None = None,
    dataset: Path | str | None = None,
    incumbent_llm: Any = None,
    candidate_llm: Any = None,
    tuner: Any = None,
    approve: bool = False,
    actor: str | None = None,
    incumbent: str | None = None,
    keyring: Any = None,
) -> AdapterRecord:
    settings = settings or get_settings()
    cfg = load_config(settings)
    record = require_signed(adapter_id, settings=settings, keyring=keyring)
    if record.node_id and record.node_id != node_id:
        from readyagents.errors import DistillRefused

        raise DistillRefused(
            f"adapter {adapter_id} is bound to node {record.node_id}, not {node_id}",
            reason="node",
        )
    report = comparison
    if report is None:
        if suite is None or dataset is None:
            raise DistillHoldoutMissing("promotion requires a holdout evaluation")
        report = evaluate(
            suite=suite,
            dataset=dataset,
            incumbent_llm=incumbent_llm,
            candidate_llm=candidate_llm,
            adapter=record.path,
            tuner=tuner,
            settings=settings,
        )
    _enforce_thresholds(report, cfg)
    if cfg.approval_roles and not approve:
        raise DistillApproval(
            "promotion requires --approve showing the incumbent vs candidate comparison",
            comparison=report.model_dump(mode="python", by_alias=True),
        )
    record.status = "promoted"
    record.eval = report
    record.incumbent = incumbent
    record.promoted_at = utc_now()
    record.demoted_at = None
    record.demote_reason = None
    record.fixture_digest = report.fixture_digest
    record.node_id = node_id
    save_adapter(record, settings)
    pins = load_promoted(settings)
    pins[node_id] = {
        "adapter_id": record.id,
        "incumbent": incumbent,
        "fallback": True,
        "digest": record.digest,
    }
    save_promoted(pins, settings)
    _ledger_delta(
        settings,
        phase="before",
        node_id=node_id,
        adapter_id=record.id,
        cost_micros=report.incumbent.cost_micros,
        actor=actor,
    )
    _ledger_delta(
        settings,
        phase="after",
        node_id=node_id,
        adapter_id=record.id,
        cost_micros=report.candidate.cost_micros,
        actor=actor,
    )
    return record


def demote(
    adapter_id: str,
    *,
    settings: Settings | None = None,
    reason: str = "below_threshold",
) -> AdapterRecord:
    settings = settings or get_settings()
    record = load_adapter(adapter_id, settings)
    record.status = "demoted"
    record.demoted_at = utc_now()
    record.demote_reason = reason
    save_adapter(record, settings)
    pins = load_promoted(settings)
    for node, row in list(pins.items()):
        if isinstance(row, dict) and row.get("adapter_id") == adapter_id:
            pins.pop(node, None)
    save_promoted(pins, settings)
    return record


def rescore_promoted(
    *,
    settings: Settings | None = None,
    suite: Path | str,
    dataset: Path | str,
    incumbent_llm: Any = None,
    candidate_llm: Any = None,
    tuner: Any = None,
    keyring: Any = None,
) -> list[AdapterRecord]:
    """Re-score promoted adapters when the fixture suite digest changes; demote if below."""
    settings = settings or get_settings()
    cfg = load_config(settings)
    from readyagents.trust.digest import digest_bytes

    current = digest_bytes(Path(suite).read_bytes())
    changed: list[AdapterRecord] = []
    pins = load_promoted(settings)
    for _node_id, row in list(pins.items()):
        if not isinstance(row, dict):
            continue
        adapter_id = str(row.get("adapter_id") or "")
        if not adapter_id:
            continue
        record = load_adapter(adapter_id, settings)
        if record.fixture_digest and record.fixture_digest == current:
            continue
        report = evaluate(
            suite=suite,
            dataset=dataset,
            incumbent_llm=incumbent_llm,
            candidate_llm=candidate_llm,
            adapter=record.path,
            tuner=tuner,
            settings=settings,
        )
        try:
            _enforce_thresholds(report, cfg)
            record.eval = report
            record.fixture_digest = current
            save_adapter(record, settings)
        except (
            DistillParity,
            DistillRegression,
            DistillLatency,
            DistillCost,
            DistillCanary,
            DistillHoldoutMissing,
        ) as extra:
            demoted = demote(adapter_id, settings=settings, reason=extra.reason)
            changed.append(demoted)
    return changed


def _enforce_thresholds(report: EvalComparison, cfg: DistillConfig) -> None:
    holdout = report.holdout or {}
    if not holdout or report.candidate.holdout is None:
        raise DistillHoldoutMissing()
    if report.canary_passed is False:
        raise DistillCanary()
    if report.frozen_regressions:
        raise DistillRegression(
            "frozen fixture regression: " + ", ".join(report.frozen_regressions)
        )
    inc = float(report.incumbent.holdout or 0.0)
    cand = float(report.candidate.holdout or 0.0)
    if cand + 1e-9 < inc * float(cfg.min_parity):
        raise DistillParity(f"holdout {cand} is below parity {cfg.min_parity} of incumbent {inc}")
    if cfg.max_latency_ms is not None and report.candidate.latency_ms is not None:
        if float(report.candidate.latency_ms) > float(cfg.max_latency_ms):
            raise DistillLatency()
    if cfg.max_cost_micros is not None:
        if int(report.candidate.cost_micros) > int(cfg.max_cost_micros):
            raise DistillCost()


def _ledger_delta(
    settings: Settings,
    *,
    phase: str,
    node_id: str,
    adapter_id: str,
    cost_micros: int,
    actor: str | None,
) -> None:
    append_spend(
        settings.ledger_dir(),
        {
            "event": "distill_delta",
            "phase": phase,
            "node_id": node_id,
            "adapter_id": adapter_id,
            "cost_micros": int(cost_micros),
            "actor": actor,
            "labels": {"distill": phase, "node": node_id},
        },
    )
