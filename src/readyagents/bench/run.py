"""Execute the benchmark suite. Offline by default; live is opt-in and metered."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.bench.guard import no_network
from readyagents.bench.layout import MODE_LIVE, MODE_OFFLINE, SCHEMA_RESULT
from readyagents.bench.metrics import ScenarioMetrics, collect, method_statement
from readyagents.bench.suite import (
    BenchScenario,
    assert_synthetic_cassette,
    cassette_digest,
    load_suite,
)
from readyagents.errors import BenchRefused
from readyagents.testing.eval import EvalCase, run_eval


@dataclass
class BenchReport:
    schema: str = SCHEMA_RESULT
    mode: str = MODE_OFFLINE
    scenarios: list[ScenarioMetrics] = field(default_factory=list)
    method: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "scenarios": [row.as_dict() for row in self.scenarios],
            "aggregate": self.aggregate(),
            "method": dict(self.method),
            "spend_usd": self.spend_usd,
        }

    def aggregate(self) -> dict[str, Any]:
        n = len(self.scenarios)
        successes = sum(1 for row in self.scenarios if row.success)
        return {
            "scenarios": n,
            "success_rate": (successes / n) if n else 0.0,
            "tokens_in": sum(row.tokens_in for row in self.scenarios),
            "tokens_out": sum(row.tokens_out for row in self.scenarios),
            "cost_usd": round(sum(row.cost_usd for row in self.scenarios), 6),
            "tool_calls": sum(row.tool_calls for row in self.scenarios),
            "node_count": sum(row.node_count for row in self.scenarios),
        }

    def markdown(self) -> str:
        lines = [
            "# ReadyAgents bench",
            "",
            f"mode: **{self.mode}**",
            "",
            "| scenario | shape | success | nodes | tools | tokens in/out | cost USD | timing |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in self.scenarios:
            timing = row.timing_offline or row.timing_live
            label = "—"
            if timing is not None:
                label = f"{timing.kind} {timing.wall_ms}ms"
            lines.append(
                f"| {row.name} | {row.shape} | {row.success} | {row.node_count} | "
                f"{row.tool_calls} | {row.tokens_in}/{row.tokens_out} | "
                f"{row.cost_usd} | {label} |"
            )
        agg = self.aggregate()
        lines.extend(
            [
                "",
                f"aggregate success_rate={agg['success_rate']} cost_usd={agg['cost_usd']}",
                "",
                f"reproduce: `{self.method.get('reproduce')}`",
                "",
                str(self.method.get("offline_vs_live") or ""),
                "",
            ]
        )
        return "\n".join(lines)


def run_bench(
    suite: Path | str | None = None,
    *,
    live: bool = False,
    allow_ci_live: bool = False,
    settings: Any | None = None,
    scenarios: list[str] | None = None,
    max_spend: float | None = None,
    model: str | None = None,
) -> BenchReport:
    mode = MODE_LIVE if live else MODE_OFFLINE
    if live:
        _refuse_live_in_ci(allow_ci_live)
        if max_spend is not None and max_spend <= 0:
            raise BenchRefused("live bench requires a positive --max-spend", reason="spend")
    rows = load_suite(suite)
    if scenarios:
        wanted = {name.strip() for name in scenarios if name.strip()}
        rows = [row for row in rows if row.name in wanted]
        if not rows:
            raise BenchRefused("no matching scenarios", reason="scenarios")
    for row in rows:
        assert_synthetic_cassette(row.cassette)
    collected: list[ScenarioMetrics] = []
    digests: dict[str, str] = {}
    for row in rows:
        digest = cassette_digest(row.cassette)
        digests[row.name] = digest
        metrics = _run_one(row, mode=mode, settings=settings, model=model)
        metrics.cassette_digest = digest
        collected.append(metrics)
    spend = sum(row.cost_usd for row in collected)
    if mode == MODE_OFFLINE and spend != 0.0:
        raise BenchRefused(f"offline bench spent {spend} USD", reason="spend")
    if max_spend is not None and spend > max_spend:
        raise BenchRefused(f"bench spend {spend} exceeded cap {max_spend}", reason="spend")
    reproduce = "readyagents bench run --offline"
    if live:
        reproduce = "readyagents bench run --live --allow-ci-live"
    report = BenchReport(
        mode=mode,
        scenarios=collected,
        spend_usd=round(spend, 6),
        method=method_statement(mode=mode, cassette_digests=digests, reproduce=reproduce),
    )
    return report


def _run_one(
    row: BenchScenario,
    *,
    mode: str,
    settings: Any,
    model: str | None,
) -> ScenarioMetrics:
    bound = bind_model(settings, model)
    case = EvalCase(
        name=row.name,
        workflow=row.workflow,
        inputs=dict(row.inputs),
        decisions=dict(row.decisions),
        expect_status=row.expect_status,
        cassette=row.cassette if mode == MODE_OFFLINE else None,
    )
    started = time.perf_counter()
    if mode == MODE_OFFLINE:
        with no_network():
            report = run_eval([case], settings=bound, dry_run=False)
    else:
        report = run_eval([case], settings=bound, dry_run=False)
    wall_ms = (time.perf_counter() - started) * 1000.0
    result = report.results[0]
    state = result.state
    if state is None:
        from readyagents.workflow.state import RunState

        state = RunState.start(row.name, dict(row.inputs))
        state.status = "failed"
        state.errors = [result.reason]
    metrics = collect(
        state,
        name=row.name,
        shape=row.shape,
        wall_ms=wall_ms,
        mode=mode,
    )
    return metrics


def bind_model(settings: Any, model: str | None) -> Any:
    """Copy settings with ``default_model`` bound to ``model``. Never a label-only write."""
    if not model:
        return settings
    if settings is None:
        from readyagents.config import get_settings

        settings = get_settings()
    copier = getattr(settings, "model_copy", None)
    if callable(copier):
        return copier(update={"default_model": model})
    settings.default_model = model
    return settings


def _refuse_live_in_ci(allow_ci_live: bool) -> None:
    if not os.environ.get("CI"):
        return
    if allow_ci_live:
        return
    raise BenchRefused(
        "live bench is refused when CI is set; pass --allow-ci-live to override",
        reason="ci",
    )
