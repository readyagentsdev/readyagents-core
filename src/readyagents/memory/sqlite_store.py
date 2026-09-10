"""SQLite memory backend. stdlib sqlite3; WAL; short transactions."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from readyagents.errors import ConfigError, MemoryError
from readyagents.memory.protocol import (
    MAX_RECORDS_PER_SCOPE,
    MemoryHit,
    MemoryRecord,
    bound_limit,
    bound_text,
)
from readyagents.memory.retrieve import bm25_search
from readyagents.workflow.state import utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    source_run_id TEXT NOT NULL,
    provenance TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON records(scope);
CREATE TABLE IF NOT EXISTS vectors (
    id TEXT PRIMARY KEY NOT NULL,
    vector_json TEXT NOT NULL
);
"""


class SQLiteMemoryStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        conn.executescript(_SCHEMA)

    def write(self, record: MemoryRecord, *, vector: list[float] | None = None) -> str:
        self._ensure_open()
        bound_text(record.text)
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM records WHERE scope = ? AND id != ? AND "
            "(expires_at IS NULL OR expires_at > ?)",
            (record.scope, record.id, utc_now()),
        ).fetchone()
        if int(row[0]) >= MAX_RECORDS_PER_SCOPE:
            raise MemoryError(
                f"memory scope {record.scope!r} exceeds {MAX_RECORDS_PER_SCOPE} records"
            )
        meta = json.dumps(dict(record.metadata), ensure_ascii=False, sort_keys=True)
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO records (
                    id, scope, text, metadata_json, created_at, expires_at,
                    source_run_id, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    scope=excluded.scope,
                    text=excluded.text,
                    metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    source_run_id=excluded.source_run_id,
                    provenance=excluded.provenance
                """,
                (
                    record.id,
                    record.scope,
                    record.text,
                    meta,
                    record.created_at,
                    record.expires_at,
                    record.source_run_id,
                    record.provenance,
                ),
            )
            if vector is None:
                conn.execute("DELETE FROM vectors WHERE id = ?", (record.id,))
            else:
                conn.execute(
                    "INSERT INTO vectors(id, vector_json) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET vector_json=excluded.vector_json",
                    (record.id, json.dumps([float(x) for x in vector])),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return record.id

    def read(self, scope: str, *, limit: int = 0) -> list[MemoryRecord]:
        self._ensure_open()
        rows = (
            self._conn()
            .execute(
                "SELECT * FROM records WHERE scope = ? AND "
                "(expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC",
                (scope, utc_now()),
            )
            .fetchall()
        )
        items = [self._row(row) for row in rows]
        if limit and limit > 0:
            items = items[: bound_limit(limit)]
        return items

    def get(self, record_id: str) -> MemoryRecord:
        self._ensure_open()
        row = (
            self._conn()
            .execute(
                "SELECT * FROM records WHERE id = ? AND (expires_at IS NULL OR expires_at > ?)",
                (record_id, utc_now()),
            )
            .fetchone()
        )
        if row is None:
            raise ConfigError(f"Memory record not found: {record_id}")
        return self._row(row)

    def search(self, scope: str, query: str, *, limit: int = 5) -> list[MemoryHit]:
        return bm25_search(self.read(scope), query, limit=limit)

    def forget(
        self,
        *,
        scope: str | None = None,
        record_id: str | None = None,
        subject: str | None = None,
    ) -> int:
        self._ensure_open()
        conn = self._conn()
        if record_id:
            ids = [record_id]
        else:
            want = f"subject:{subject}" if subject else scope
            if want:
                rows = conn.execute("SELECT id FROM records WHERE scope = ?", (want,)).fetchall()
            else:
                rows = conn.execute("SELECT id FROM records").fetchall()
            ids = [str(row[0]) for row in rows]
        if not ids:
            return 0
        conn.execute("BEGIN")
        try:
            conn.executemany("DELETE FROM records WHERE id = ?", [(i,) for i in ids])
            conn.executemany("DELETE FROM vectors WHERE id = ?", [(i,) for i in ids])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return len(ids)

    def list(self, *, scope: str | None = None, limit: int = 0) -> list[MemoryRecord]:
        self._ensure_open()
        if scope:
            items = self.read(scope)
        else:
            rows = (
                self._conn()
                .execute(
                    "SELECT * FROM records WHERE expires_at IS NULL OR expires_at > ? "
                    "ORDER BY created_at DESC",
                    (utc_now(),),
                )
                .fetchall()
            )
            items = [self._row(row) for row in rows]
        if limit and limit > 0:
            items = items[: bound_limit(limit)]
        return items

    def vector(self, record_id: str) -> list[float] | None:
        self._ensure_open()
        row = (
            self._conn()
            .execute("SELECT vector_json FROM vectors WHERE id = ?", (record_id,))
            .fetchone()
        )
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        return [float(x) for x in data]

    def close(self) -> None:
        self._closed = True
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigError("Memory store is closed")

    def _conn(self) -> sqlite3.Connection:
        self._ensure_open()
        conn = getattr(self._local, "conn", None)
        if conn is None:
            timeout = max(0.001, self.busy_timeout_ms / 1000)
            conn = sqlite3.connect(
                str(self.path),
                timeout=timeout,
                isolation_level=None,
                check_same_thread=True,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            self._local.conn = conn
        return conn

    def _row(self, row: sqlite3.Row) -> MemoryRecord:
        try:
            meta = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        return MemoryRecord(
            id=str(row["id"]),
            scope=str(row["scope"]),
            text=str(row["text"]),
            metadata=meta,
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]) if row["expires_at"] else None,
            source_run_id=str(row["source_run_id"]),
            provenance=str(row["provenance"]),
        )
