"""Run observer seam. Events fire only after a durable-state boundary.

Observers cannot change status, exit code, the run record, or the audit trail.
Each call is isolated: one redacted warning on failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from readyagents.logging import get_logger
from readyagents.workflow.state import utc_now

log = get_logger("observability")

JSONScalar = str | int | float | bool | None


@dataclass(frozen=True)
class RunEvent:
    name: str
    timestamp: str
    run_id: str
    workflow: str
    node_id: str | None = None
    node_type: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    attributes: Mapping[str, JSONScalar] = field(default_factory=dict)


@runtime_checkable
class Observer(Protocol):
    def on_event(self, event: RunEvent) -> None: ...

    def shutdown(self) -> None: ...


def emit_event(observers: Sequence[Any] | None, event: RunEvent, *, redactor: Any = None) -> None:
    """Notify observers. Failures never propagate."""
    if not observers:
        return
    for observer in observers:
        handler = getattr(observer, "on_event", None)
        if not callable(handler):
            continue
        try:
            handler(event)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            scrubber = redactor
            if scrubber is None:
                from readyagents.policy import Redactor

                scrubber = Redactor()
            if hasattr(scrubber, "redact_text"):
                message = scrubber.redact_text(message)
            log.warning(
                "observer failed: %s",
                message,
                extra={
                    "run_id": event.run_id,
                    "node_id": event.node_id or "-",
                    "event": event.name,
                },
            )


def shutdown_observers(observers: Sequence[Any] | None, *, redactor: Any = None) -> None:
    if not observers:
        return
    for observer in observers:
        handler = getattr(observer, "shutdown", None)
        if not callable(handler):
            continue
        try:
            handler()
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if redactor is not None and hasattr(redactor, "redact_text"):
                message = redactor.redact_text(message)
            log.warning("observer shutdown failed: %s", message)


def make_event(
    name: str,
    *,
    run_id: str,
    workflow: str,
    node_id: str | None = None,
    node_type: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    usage: Mapping[str, int] | None = None,
    attributes: Mapping[str, JSONScalar] | None = None,
) -> RunEvent:
    return RunEvent(
        name=name,
        timestamp=utc_now(),
        run_id=run_id,
        workflow=workflow,
        node_id=node_id,
        node_type=node_type,
        status=status,
        duration_ms=duration_ms,
        usage=dict(usage or {}),
        attributes=dict(attributes or {}),
    )
