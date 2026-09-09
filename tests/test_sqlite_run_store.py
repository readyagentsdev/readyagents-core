from __future__ import annotations

import sqlite3

import pytest

from readyagents.errors import RunStoreError
from readyagents.run_store.sqlite_store import SQLiteRunStore
from readyagents.workflow.state import RunState


def test_schema_version_and_indexes(tmp_path) -> None:
    path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(path)
    store.close()
    conn = sqlite3.connect(str(path))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    names = {row[1] for row in conn.execute("PRAGMA index_list('runs')").fetchall()}
    assert "idx_runs_started" in names
    assert "idx_runs_status_started" in names
    assert "idx_runs_workflow_started" in names
    assert "idx_runs_status_workflow_started" in names
    conn.close()


def test_reject_newer_schema_version(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RunStoreError, match="schema version"):
        SQLiteRunStore(path)


def test_roundtrip_record(tmp_path) -> None:
    store = SQLiteRunStore(tmp_path / "r.db")
    state = RunState.start("wf", {"x": 1})
    state.status = "paused"
    state.pending_node = "gate"
    store.save(state)
    loaded = store.get(state.run_id, allow_prefix=False)
    assert loaded.state.inputs == {"x": 1}
    assert loaded.state.pending_node == "gate"
    store.close()
