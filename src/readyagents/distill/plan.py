"""Viability plan from recorded runs and feedback. Honest verdicts, ledger costs."""

from __future__ import annotations

from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.cost.ledger import read_spend_entries
from readyagents.cost.prices import load_price_table
from readyagents.distill.schema import PlanReport
from readyagents.distill.store import load_config
from readyagents.feedback.collect import collect_corrections
from readyagents.feedback.consent import permits
from readyagents.run_store import open_run_store
from readyagents.run_store.base import RunQuery


def plan(
    node_id: str,
    *,
    settings: Settings | None = None,
    workflow: str | None = None,
    min_examples: int | None = None,
    scope: str | None = None,
) -> PlanReport:
    settings = settings or get_settings()
    cfg = load_config(settings)
    floor = int(min_examples if min_examples is not None else cfg.min_examples)
    floor = max(1, floor)
    pairs = collect_corrections(settings)
    consented: list[Any] = []
    for state, corr in pairs:
        if corr.node_id != node_id:
            continue
        if workflow and str(state.workflow_name) != workflow:
            continue
        if not permits(state, scope or corr.consent_scope or None):
            continue
        consented.append(corr)
    n = len(consented)
    diversity = _diversity(consented)
    cost, latency = _current_cost_latency(settings, node_id, workflow)
    saving = _estimated_saving(cost, consented)
    if n < floor:
        return PlanReport(
            node_id=node_id,
            verdict="not_enough_data",
            examples=n,
            consented=n,
            diversity=diversity,
            current_cost_micros=cost,
            current_latency_ms=latency,
            estimated_saving_micros=saving,
            reason=f"{n} consented examples; need at least {floor}",
        )
    if diversity > cfg.broad_ratio:
        return PlanReport(
            node_id=node_id,
            verdict="task_too_broad",
            examples=n,
            consented=n,
            diversity=diversity,
            current_cost_micros=cost,
            current_latency_ms=latency,
            estimated_saving_micros=saving,
            reason=f"unique-example ratio {diversity:.2f} exceeds {cfg.broad_ratio}",
        )
    return PlanReport(
        node_id=node_id,
        verdict="viable",
        examples=n,
        consented=n,
        diversity=diversity,
        current_cost_micros=cost,
        current_latency_ms=latency,
        estimated_saving_micros=saving,
        reason="enough consented examples and a narrow label/input mix",
    )


def _diversity(rows: list[Any]) -> float:
    if not rows:
        return 0.0
    keys = set()
    for row in rows:
        label = getattr(row, "label", None) or ""
        original = getattr(row, "original", None) or ""
        keys.add(f"{label}|{original}")
    return round(len(keys) / len(rows), 6)


def _current_cost_latency(
    settings: Settings, node_id: str, workflow: str | None
) -> tuple[int | None, float | None]:
    cost = _ledger_cost(settings, workflow)
    latency = None
    try:
        store = open_run_store(settings)
        try:
            runs = list(store.list(RunQuery(workflow=workflow, limit=200)))
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
    except Exception:  # noqa: BLE001
        return cost, latency
    times: list[float] = []
    node_cost = 0
    for item in runs:
        state = getattr(item, "state", item)
        for result in getattr(state, "results", None) or []:
            if str(getattr(result, "node_id", "")) != node_id:
                continue
            ms = getattr(result, "total_ms", None)
            if ms is not None:
                times.append(float(ms))
            usage = getattr(result, "usage", None) or {}
            node_cost += int(usage.get("cost_micros") or 0)
    if node_cost and cost is None:
        cost = node_cost
    if times:
        latency = sum(times) / len(times)
    return cost, latency


def _ledger_cost(settings: Settings, workflow: str | None) -> int | None:
    try:
        rows = read_spend_entries(settings.ledger_dir())
    except Exception:  # noqa: BLE001
        return None
    total = 0
    matched = 0
    for row in rows:
        if workflow and str(row.get("workflow") or "") != workflow:
            continue
        total += int(row.get("cost_micros") or 0)
        matched += 1
    if not matched:
        return None
    return total


def _estimated_saving(current: int | None, rows: list[Any]) -> int | None:
    """Price-table ratio when both models are priced; otherwise unnamed, not invented."""
    if current is None or current <= 0:
        return None
    models = [str(getattr(row, "model", "") or "") for row in rows if getattr(row, "model", "")]
    if not models:
        return None
    try:
        table = load_price_table()
    except Exception:  # noqa: BLE001
        return None
    incumbent = models[0]
    quote = table.quote(incumbent)
    if not quote.priced or quote.rate is None:
        return None
    local = None
    for ref in table.models:
        caps_quote = table.quote(ref)
        if not caps_quote.priced or caps_quote.rate is None:
            continue
        if "local" in ref or ref.startswith("ollama:"):
            local = caps_quote
            break
    if local is None or local.rate is None:
        return None
    denom = float(quote.rate.output or 0)
    if denom <= 0:
        return None
    ratio = float(local.rate.output or 0) / denom
    if ratio >= 1:
        return 0
    return int(current * (1.0 - ratio))
