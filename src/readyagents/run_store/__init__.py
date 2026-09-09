"""Backend-neutral run persistence. JSON default; optional SQLite."""

from __future__ import annotations

from readyagents.config import Settings, get_settings
from readyagents.errors import ConfigError, RunStoreConflict, RunStoreError
from readyagents.run_store.base import RunQuery, RunStore, StoredRun
from readyagents.run_store.json_store import JsonRunStore
from readyagents.run_store.migrate import MigrationReport, migrate_json_to_sqlite
from readyagents.run_store.sqlite_store import SQLiteRunStore

__all__ = [
    "JsonRunStore",
    "MigrationReport",
    "RunQuery",
    "RunStore",
    "RunStoreConflict",
    "RunStoreError",
    "SQLiteRunStore",
    "StoredRun",
    "migrate_json_to_sqlite",
    "open_run_store",
]


def open_run_store(
    settings: Settings | None = None,
    *,
    backend: str | None = None,
) -> RunStore:
    settings = settings or get_settings()
    kind = (backend or getattr(settings, "run_store", "json") or "json").strip().lower()
    if kind == "json":
        return JsonRunStore(settings.runs_dir())
    if kind == "sqlite":
        timeout = int(getattr(settings, "sqlite_busy_timeout_ms", 5000))
        return SQLiteRunStore(settings.run_db_path(), busy_timeout_ms=timeout)
    raise ConfigError(f"Invalid run_store '{kind}'. Expected json or sqlite.")
