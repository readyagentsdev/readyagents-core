"""Inspect declared triggers and dry-run mappings. No execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.triggers.decide import decide_trigger
from readyagents.triggers.store import DeadLetterLog, EventLog
from readyagents.workflow.schema import WorkflowSpec


def list_triggers(workflow: WorkflowSpec) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "kind": item.accepts.kind,
            "require_signature": item.require_signature,
            "idempotency_key": item.idempotency_key,
            "idempotency_window": item.idempotency_window,
            "concurrency": item.concurrency,
            "on_ceiling": item.on_ceiling,
            "budget": (
                {
                    "max_cost_usd": item.budget.max_cost_usd,
                    "max_tokens": item.budget.max_tokens,
                }
                if item.budget
                else None
            ),
        }
        for item in workflow.triggers
    ]


def show_trigger(workflow: WorkflowSpec, name: str) -> dict[str, Any]:
    for item in workflow.triggers:
        if item.name == name:
            return item.model_dump(by_alias=True)
    raise KeyError(name)


def test_trigger(
    workflow: WorkflowSpec,
    name: str,
    payload: dict[str, Any] | bytes,
    **kwargs: Any,
) -> dict[str, Any]:
    kind = None
    for item in workflow.triggers:
        if item.name == name:
            kind = item.accepts.kind
            break
    decision = decide_trigger(
        workflow,
        trigger_name=name,
        raw=payload,
        source_kind=kind or "webhook",
        dry_run=True,
        **kwargs,
    )
    return decision.as_dict()


def list_events(home: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    return EventLog(home).list(limit=limit)


def list_dead_letters(home: Path) -> list[dict[str, Any]]:
    return DeadLetterLog(home).list()
