"""Start a triggered run through the shipped runner. Taint event-mapped inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.firewall.taint import set_provenance, untrusted
from readyagents.workflow.schema import TriggerSpec, WorkflowSpec
from readyagents.workflow.state import RunState


def start_triggered_run(
    workflow: WorkflowSpec,
    inputs: dict[str, Any],
    *,
    provenance: dict[str, Any],
    trigger: TriggerSpec,
    persist: bool = True,
    settings: Any = None,
    home: Path | None = None,
    path: Path | str | None = None,
    policy: Path | str | None = None,
    llm: Any = None,
    remaining_usd: float | None = None,
    remaining_tokens: int | None = None,
) -> RunState:
    from readyagents.llm.resilience import usd_to_micros

    metadata = {"started_by": dict(provenance)}
    state = RunState.start(workflow.name, inputs, metadata=metadata)
    for key in inputs:
        set_provenance(state, key, untrusted(source="event"))
    max_spend = remaining_usd
    if max_spend is None and trigger.budget and trigger.budget.max_cost_usd is not None:
        max_spend = float(trigger.budget.max_cost_usd)
    max_tokens = remaining_tokens
    if max_tokens is None and trigger.budget and trigger.budget.max_tokens is not None:
        max_tokens = int(trigger.budget.max_tokens)
    if path is not None:
        from readyagents.workflow.runner import run_workflow_file

        return run_workflow_file(
            path,
            inputs=inputs,
            settings=settings,
            persist=persist,
            initial_state=state,
            started_by=provenance,
            max_spend=max_spend,
            max_tokens_cap=max_tokens,
            policy=policy,
            llm=llm,
        )
    from readyagents.tools import ToolRegistry
    from readyagents.workflow.engine import run_workflow
    from readyagents.workflow.nodes import ExecutionContext

    ctx = ExecutionContext(
        workflow,
        ToolRegistry(),
        llm=llm,
        pin_home=home,
        default_model=workflow.default_model or "mock:test",
        policy=policy,
        budget_cost_micros=usd_to_micros(max_spend),
        budget_tokens=max_tokens,
    )
    return run_workflow(workflow, inputs, ctx, state=state, metadata=metadata)
