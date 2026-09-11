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
) -> RunState:
    metadata = {"started_by": dict(provenance)}
    state = RunState.start(workflow.name, inputs, metadata=metadata)
    for key in inputs:
        set_provenance(state, key, untrusted(source="event"))
    max_spend = None
    if trigger.budget and trigger.budget.max_cost_usd is not None:
        max_spend = float(trigger.budget.max_cost_usd)
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
            policy=policy,
        )
    from readyagents.tools import ToolRegistry
    from readyagents.workflow.engine import run_workflow
    from readyagents.workflow.nodes import ExecutionContext

    ctx = ExecutionContext(
        workflow,
        ToolRegistry(),
        pin_home=home,
        default_model="mock:test",
        policy=policy,
    )
    return run_workflow(workflow, inputs, ctx, state=state, metadata=metadata)
