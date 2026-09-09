"""SQLite RunStore. stdlib sqlite3 only; WAL; revision CAS."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from readyagents.errors import ConfigError, RunStoreConflict, RunStoreError
from readyagents.run_store.base import RunQuery, StoredRun
from readyagents.workflow.state import RunState, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    workflow TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pending_node TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON runs(started_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status_started
    ON runs(status, started_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_workflow_started
    ON runs(workflow, started_at DESC, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status_workflow_started
    ON runs(status, workflow, started_at DESC, run_id DESC);
"""


class SQLiteRunStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        self._closed = False
        self._init_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema(self._conn())

    def save(
        self,
        state: RunState,
        *,
        redactor: Any = None,
        expected_revision: int | None = None,
    ) -> int:
        self._ensure_open()
        record_obj: Any = state.to_record()
        if redactor is not None:
            record_obj = redactor.redact(record_obj)
        if not isinstance(record_obj, dict):
            record_obj = dict(state.to_record())
        blob = json.dumps(record_obj, ensure_ascii=False)
        conn = self._conn()
        now = utc_now()
        pending = state.pending_node
        if expected_revision is None:
            row = conn.execute(
                "SELECT revision FROM runs WHERE run_id = ?", (state.run_id,)
            ).fetchone()
            if row is None:
                next_rev = 1
                try:
                    conn.execute("BEGIN")
                    conn.execute(
                        """
                        INSERT INTO runs (
                            run_id, workflow, status, started_at, finished_at,
                            pending_node, revision, record_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            state.run_id,
                            state.workflow_name,
                            state.status,
                            state.started_at,
                            state.finished_at,
                            pending,
                            next_rev,
                            blob,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                except sqlite3.IntegrityError as exc:
                    conn.execute("ROLLBACK")
                    raise RunStoreConflict(f"Run already exists: {state.run_id}") from exc
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return next_rev
            next_rev = int(row[0]) + 1
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    UPDATE runs SET workflow=?, status=?, started_at=?, finished_at=?,
                        pending_node=?, revision=?, record_json=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (
                        state.workflow_name,
                        state.status,
                        state.started_at,
                        state.finished_at,
                        pending,
                        next_rev,
                        blob,
                        now,
                        state.run_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return next_rev
        next_rev = int(expected_revision) + 1
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                """
                UPDATE runs SET workflow=?, status=?, started_at=?, finished_at=?,
                    pending_node=?, revision=?, record_json=?, updated_at=?
                WHERE run_id=? AND revision=?
                """,
                (
                    state.workflow_name,
                    state.status,
                    state.started_at,
                    state.finished_at,
                    pending,
                    next_rev,
                    blob,
                    now,
                    state.run_id,
                    int(expected_revision),
                ),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise RunStoreConflict(
                    f"Run {state.run_id} revision mismatch (expected {expected_revision})"
                )
            conn.execute("COMMIT")
        except RunStoreConflict:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return next_rev

    def get(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun:
        self._ensure_open()
        conn = self._conn()
        row = conn.execute(
            "SELECT run_id, revision, record_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None and allow_prefix:
            matches = conn.execute(
                "SELECT run_id, revision, record_json FROM runs WHERE run_id LIKE ?",
                (f"{run_id}%",),
            ).fetchall()
            if len(matches) == 1:
                row = matches[0]
            elif len(matches) > 1:
                ids = ", ".join(str(item[0]) for item in matches[:8])
                raise ConfigError(f"Run id '{run_id}' is ambiguous. Matches: {ids}")
        if row is None:
            raise ConfigError(f"Run not found: {run_id}")
        return self._row_to_stored(row)

    def list(self, query: RunQuery | None = None) -> list[StoredRun]:
        self._ensure_open()
        query = query or RunQuery()
        sql = "SELECT run_id, revision, record_json FROM runs"
        clauses: list[str] = []
        args: list[Any] = []
        if query.status:
            clauses.append("status = ?")
            args.append(query.status.strip().lower())
        if query.workflow:
            clauses.append("workflow = ?")
            args.append(query.workflow.strip())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC, run_id DESC"
        rows = self._conn().execute(sql, args).fetchall()
        items = []
        for row in rows:
            try:
                items.append(self._row_to_stored(row))
            except (ConfigError, json.JSONDecodeError, TypeError, ValueError):
                continue
        if query.cursor:
            ids = [item.state.run_id for item in items]
            try:
                idx = ids.index(query.cursor)
                items = items[idx + 1 :]
            except ValueError:
                pass
        if query.limit and query.limit > 0:
            items = items[: query.limit]
        return items

    def delete(self, run_id: str, *, allow_prefix: bool = True) -> StoredRun:
        stored = self.get(run_id, allow_prefix=allow_prefix)
        self._conn().execute("DELETE FROM runs WHERE run_id = ?", (stored.state.run_id,))
        return stored

    def gc(
        self,
        *,
        statuses: list[str] | None = None,
        include_paused: bool = False,
        keep: int = 0,
    ) -> list[str]:
        wanted = {s.strip().lower() for s in (statuses or ["succeeded", "failed", "cancelled"])}
        if include_paused:
            wanted.add("paused")
        found = self.list(RunQuery())
        if keep and keep > 0:
            found = found[keep:]
        deleted: list[str] = []
        conn = self._conn()
        for item in found:
            if item.state.status == "paused" and not include_paused:
                continue
            if item.state.status not in wanted:
                continue
            conn.execute("DELETE FROM runs WHERE run_id = ?", (item.state.run_id,))
            deleted.append(item.state.run_id)
        return deleted

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
            raise ConfigError("Run store is closed")

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
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            self._local.conn = conn
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        with self._init_lock:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA user_version = 1")
            elif version == 1:
                return
            else:
                raise RunStoreError(f"Unsupported run store schema version {version}")

    def _row_to_stored(self, row: Any) -> StoredRun:
        run_id = str(row[0])
        revision = int(row[1])
        try:
            data = json.loads(row[2])
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Corrupt run record {run_id}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("run_id"):
            raise ConfigError(f"Invalid run record: {run_id}")
        if str(data.get("run_id")) != run_id:
            raise ConfigError(f"Invalid run record: {run_id}")
        state = RunState.from_record(data)
        return StoredRun(state=state, revision=revision, cursor=state.run_id)
