"""Pack-source adapters. Each kind calls the same core start decision.

Core ships no webhook server, file watcher, queue poller, or scheduler.
The optional continuous pack binds those listeners and calls these functions.
"""

from __future__ import annotations

from typing import Any

from readyagents.triggers.decide import TriggerDecision, decide_trigger
from readyagents.workflow.schema import WorkflowSpec

KINDS = ("webhook", "file", "queue", "schedule")


def fire_source(
    kind: str,
    workflow: WorkflowSpec,
    *,
    trigger_name: str,
    raw: bytes | str | dict[str, Any],
    **kwargs: Any,
) -> TriggerDecision:
    """Every pack source kind enters here. Governance cannot drift by source."""
    return decide_trigger(
        workflow,
        trigger_name=trigger_name,
        raw=raw,
        source_kind=kind,
        **kwargs,
    )


def fire_webhook(workflow: WorkflowSpec, **kwargs: Any) -> TriggerDecision:
    """Loopback by default. An operator-exposed bind is the operator's decision."""
    return fire_source("webhook", workflow, **kwargs)


def fire_file(workflow: WorkflowSpec, **kwargs: Any) -> TriggerDecision:
    return fire_source("file", workflow, **kwargs)


def fire_queue(workflow: WorkflowSpec, **kwargs: Any) -> TriggerDecision:
    return fire_source("queue", workflow, **kwargs)


def fire_schedule(workflow: WorkflowSpec, **kwargs: Any) -> TriggerDecision:
    return fire_source("schedule", workflow, **kwargs)
