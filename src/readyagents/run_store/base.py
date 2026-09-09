"""Run-store protocol, query types, and conflict errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from readyagents.errors import RunStoreConflict, RunStoreError
from readyagents.workflow.state import RunState

__all__ = [
    "RunQuery",
    "RunStore",
    "RunStoreConflict",
    "RunStoreError",
    "StoredRun",
]


@dataclass(frozen=True)
class RunQuery:
    status: str | None = None
    workflow: str | None = None
    limit: int = 0
    cursor: str | None = None


@dataclass(frozen=True)
class StoredRun:
    state: RunState
    revision: int
    cursor: str | None = None


@runtime_checkable
class RunStore(Protocol):
    def save(
        self,
        state: RunState,
        *,
        redactor: Any = None,
        expected_revision: int | None = None,
    ) -> int: ...

    def get(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun: ...

    def list(self, query: RunQuery | None = None) -> list[StoredRun]: ...

    def delete(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun: ...

    def gc(
        self,
        *,
        statuses: list[str] | None = None,
        include_paused: bool = False,
        keep: int = 0,
        min_age_seconds: float | None = None,
        override_retention: bool = False,
    ) -> list[str]: ...

    def close(self) -> None: ...
