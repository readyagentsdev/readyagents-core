"""Run a simulation: generate, score via eval, cover, cluster, freeze."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.errors import SimulateRefused
from readyagents.firewall.enforce import ToolRequest, evaluate
from readyagents.replay.cassette import Cassette
from readyagents.simulate.cluster import (
    cluster_key,
    load_clusters,
    pick_representatives,
    save_clusters,
)
from readyagents.simulate.coverage import apply_run, declared_coverage
from readyagents.simulate.generate import SimCase, generate_cases
from readyagents.simulate.layout import DEFAULT_CASES
from readyagents.simulate.redact import redact_inputs, secret_shaped_values
from readyagents.testing.eval import EvalCase, EvalResult, run_eval
from readyagents.workflow.runner import load_workflow
from readyagents.workflow.state import RunState


@dataclass
class SimulateReport:
    cases: int = 0
    passed: int = 0
    failed: int = 0
    clusters: list[str] = field(default_factory=list)
    new_failures: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0
    dry_run: bool = True
    seed: int = 42
    case_names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "passed": self.passed,
            "failed": self.failed,
            "clusters": list(self.clusters),
            "new_failures": list(self.new_failures),
            "frozen": list(self.frozen),
            "coverage": dict(self.coverage),
            "spend_usd": self.spend_usd,
            "dry_run": self.dry_run,
            "seed": self.seed,
            "case_names": list(self.case_names),
        }


def simulate_workflow(
    path: Path | str,
    *,
    seed: int = 42,
    cap: int = DEFAULT_CASES,
    out_dir: Path | str | None = None,
    fail_on_new: bool = False,
    live_side_effects: bool = False,
    policy: Any | None = None,
    settings: Any | None = None,
    llm: Any = None,
    model: str | None = None,
    personas: list[str] | None = None,
    max_spend: float | None = None,
    sovereign: bool = False,
    tools: Any | None = None,
) -> SimulateReport:
    source = Path(path)
    workflow = load_workflow(source)
    dry_run = not live_side_effects
    if live_side_effects:
        _require_live_policy(policy)
        dry_run = False
    extra: list[SimCase] = []
    spend = 0.0
    if model:
        from readyagents.simulate.personas import generate_personas

        if llm is None and not sovereign:
            from readyagents.llm.registry import get_provider

            llm, _ = get_provider(model, settings=settings)
        extra, spend = generate_personas(
            workflow,
            model=model,
            personas=list(personas or ["hostile"]),
            llm=llm,
            max_spend=max_spend,
            sovereign=sovereign,
        )
    if extra and cap > 0:
        det_cap = max(0, cap - len(extra))
        cases = generate_cases(workflow, seed=seed, cap=det_cap) if det_cap > 0 else []
        cases.extend(extra)
        cases = cases[:cap]
    else:
        cases = generate_cases(workflow, seed=seed, cap=cap)
        if extra:
            cases.extend(extra)
    eval_cases = [
        EvalCase(
            name=item.name,
            workflow=source,
            inputs=item.inputs,
            decisions=item.decisions,
            expect_status="succeeded",
        )
        for item in cases
    ]
    report_eval = run_eval(
        eval_cases,
        llm=llm,
        tools=tools,
        settings=settings,
        dry_run=dry_run,
        record=True,
    )
    coverage = declared_coverage(workflow)
    by_name = {item.name: item for item in cases}
    for row in report_eval.results:
        if row.state is not None:
            sim = by_name.get(row.name, SimCase("", {}))
            apply_run(coverage, row.state, decisions=sim.decisions)
    reps = pick_representatives(list(report_eval.results))
    known = load_clusters(Path(out_dir) if out_dir is not None else None)
    new_keys = [key for key in reps if key not in known]
    frozen: list[str] = []
    dest = Path(out_dir) if out_dir is not None else None
    if dest is not None:
        dest.mkdir(parents=True, exist_ok=True)
        for _key, row in reps.items():
            sim = by_name.get(row.name)
            state = row.state
            if state is None:
                state = RunState.start(
                    workflow.name,
                    dict(sim.inputs if sim else {}),
                    metadata={"source": str(source.resolve())},
                )
                state.status = "failed"
                state.errors = [row.reason]
                row.state = state
            elif not state.metadata.get("source"):
                state.metadata["source"] = str(source.resolve())
            frozen.append(
                _freeze_one(row, dest, settings=settings, workspace=_workspace(settings, source))
            )
        save_clusters(dest, known | set(reps))
    result = SimulateReport(
        cases=len(eval_cases),
        passed=report_eval.passed,
        failed=report_eval.failed,
        clusters=sorted(reps),
        new_failures=sorted(new_keys),
        frozen=frozen,
        coverage=coverage.as_dict(),
        spend_usd=spend,
        dry_run=dry_run,
        seed=seed,
        case_names=[item.name for item in cases],
    )
    if fail_on_new and new_keys:
        raise SimulateRefused(
            f"new failure class(es): {', '.join(sorted(new_keys))}",
            reason="new-failure",
            report=result,
        )
    return result


def _require_live_policy(policy: Any) -> None:
    if policy is None:
        raise SimulateRefused(
            "live side effects require a policy that allows them",
            reason="policy",
        )
    dummy = RunState.start("simulate", {})
    for name in ("write_file", "http_get"):
        decision = evaluate(
            ToolRequest(name=name, arguments={}, node_id="simulate", raw_arguments={}),
            dummy,
            policy,
        )
        if decision.action == "deny":
            raise SimulateRefused(
                f"live side effects denied by policy for {name}",
                reason="policy",
            )


def _workspace(settings: Any, source: Path) -> Path:
    if settings is not None and hasattr(settings, "workspace_path"):
        return Path(settings.workspace_path())
    return source.parent


def _freeze_one(row: EvalResult, dest: Path, *, settings: Any, workspace: Path) -> str:
    from readyagents.replay.freeze import freeze_run
    from readyagents.replay.record import known_secret_values

    state = row.state
    assert state is not None
    cassette = _cassette_for(state)
    secrets = list(known_secret_values(settings) if settings is not None else [])
    secrets.extend(secret_shaped_values(state.inputs))
    state.inputs = redact_inputs(dict(state.inputs or {}))
    folder = dest / f"fail-{cluster_key(row)}"
    freeze_run(
        state,
        cassette,
        out_dir=folder,
        workspace=workspace,
        secrets=secrets,
        allow_unsealed=True,
    )
    return str(folder)


def _cassette_for(state: RunState) -> Cassette:
    raw = state.metadata.get("cassette") if state.metadata else None
    if isinstance(raw, str) and raw.strip() and Path(raw).is_file():
        return Cassette.load(raw)
    return Cassette.new(run_id=state.run_id, workflow=state.workflow_name)
