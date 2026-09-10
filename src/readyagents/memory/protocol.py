"""Memory store protocol. JSON default, SQLite opt-in. Stdlib only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from readyagents.errors import ConfigError, MemoryError

MAX_TEXT_BYTES = 32_000
MAX_RECORDS_PER_SCOPE = 256
MAX_QUERY_CHARS = 512
MAX_LIMIT = 50
MAX_METADATA_BYTES = 4_096
DEFAULT_SEARCH_LIMIT = 5

JSONScalar = str | int | float | bool | None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: str
    text: str
    metadata: Mapping[str, JSONScalar] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str | None = None
    source_run_id: str = ""
    provenance: str = "operator"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "text": self.text,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "source_run_id": self.source_run_id,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> MemoryRecord:
        meta = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        return cls(
            id=str(raw.get("id") or ""),
            scope=str(raw.get("scope") or ""),
            text=str(raw.get("text") or ""),
            metadata={str(k): v for k, v in dict(meta).items()},
            created_at=str(raw.get("created_at") or ""),
            expires_at=str(raw["expires_at"]) if raw.get("expires_at") else None,
            source_run_id=str(raw.get("source_run_id") or ""),
            provenance=str(raw.get("provenance") or "operator"),
        )


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    score: float


@runtime_checkable
class MemoryStore(Protocol):
    def write(self, record: MemoryRecord, *, vector: list[float] | None = None) -> str: ...

    def read(self, scope: str, *, limit: int = 0) -> list[MemoryRecord]: ...

    def get(self, record_id: str) -> MemoryRecord: ...

    def search(self, scope: str, query: str, *, limit: int = 5) -> list[MemoryHit]: ...

    def forget(
        self,
        *,
        scope: str | None = None,
        record_id: str | None = None,
        subject: str | None = None,
    ) -> int: ...

    def list(self, *, scope: str | None = None, limit: int = 0) -> list[MemoryRecord]: ...

    def vector(self, record_id: str) -> list[float] | None: ...

    def close(self) -> None: ...


def open_memory_store(
    home: Path,
    *,
    backend: str = "json",
    busy_timeout_ms: int = 5000,
) -> MemoryStore:
    kind = (backend or "json").strip().lower()
    root = Path(home) / "memory"
    if kind == "json":
        from readyagents.memory.json_store import JsonMemoryStore

        return JsonMemoryStore(root)
    if kind == "sqlite":
        from readyagents.memory.sqlite_store import SQLiteMemoryStore

        return SQLiteMemoryStore(root / "memory.sqlite3", busy_timeout_ms=busy_timeout_ms)
    raise ConfigError(f"Invalid memory store '{backend}'. Expected json or sqlite.")


def bound_limit(limit: int | None) -> int:
    if not limit or int(limit) <= 0:
        return DEFAULT_SEARCH_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def bound_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= MAX_TEXT_BYTES:
        return text
    raise MemoryError(f"memory text exceeds {MAX_TEXT_BYTES} bytes")
