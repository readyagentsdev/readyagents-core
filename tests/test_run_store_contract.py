from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from readyagents.errors import ConfigError, RunStoreConflict
from readyagents.run_store import JsonRunStore, RunQuery, SQLiteRunStore
from readyagents.workflow.state import RunState, persist_run


def _stores(tmp_path):
    return [
        JsonRunStore(tmp_path / "runs"),
        SQLiteRunStore(tmp_path / "runs.sqlite3"),
    ]


def _state(name: str = "wf", status: str = "succeeded") -> RunState:
    state = RunState.start(name, {"n": 1})
    state.status = status
    return state


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_insert_revision_one_then_increment(tmp_path, backend: str) -> None:
    store = (
        JsonRunStore(tmp_path / "runs") if backend == "json" else SQLiteRunStore(tmp_path / "db")
    )
    state = _state()
    rev = store.save(state)
    assert rev == 1
    state.status = "failed"
    rev2 = store.save(state)
    assert rev2 == 2
    loaded = store.get(state.run_id, allow_prefix=False)
    assert loaded.revision == 2
    assert loaded.state.status == "failed"
    store.close()
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_cas_success_and_stale_conflict(tmp_path, backend: str) -> None:
    store = JsonRunStore(tmp_path / "r") if backend == "json" else SQLiteRunStore(tmp_path / "s.db")
    state = _state()
    store.save(state)
    state.status = "paused"
    assert store.save(state, expected_revision=1) == 2
    with pytest.raises(RunStoreConflict):
        store.save(state, expected_revision=1)
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_get_prefix_and_ambiguous(tmp_path, backend: str) -> None:
    store = JsonRunStore(tmp_path / "r") if backend == "json" else SQLiteRunStore(tmp_path / "s.db")
    a = RunState.start("wf", {}, run_id="aaaabbbbccccddddeeeeffff00001111")
    b = RunState.start("wf", {}, run_id="aaaabbbbccccddddeeeeffff00002222")
    store.save(a)
    store.save(b)
    one = RunState.start("wf", {}, run_id="bbbb1111222233334444555566667777")
    store.save(one)
    got = store.get("bbbb1111", allow_prefix=True)
    assert got.state.run_id == one.run_id
    with pytest.raises(ConfigError, match="ambiguous"):
        store.get("aaaa", allow_prefix=True)
    with pytest.raises(ConfigError, match="not found"):
        store.get("aaaa", allow_prefix=False)
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_list_newest_first_status_limit(tmp_path, backend: str) -> None:
    store = JsonRunStore(tmp_path / "r") if backend == "json" else SQLiteRunStore(tmp_path / "s.db")
    first = _state("alpha", "succeeded")
    first.started_at = "2020-01-01T00:00:00+00:00"
    second = _state("beta", "paused")
    second.started_at = "2021-01-01T00:00:00+00:00"
    store.save(first)
    store.save(second)
    listed = store.list(RunQuery())
    assert [i.state.run_id for i in listed] == [second.run_id, first.run_id]
    paused = store.list(RunQuery(status="paused"))
    assert [i.state.run_id for i in paused] == [second.run_id]
    limited = store.list(RunQuery(limit=1))
    assert len(limited) == 1
    assert limited[0].state.run_id == second.run_id
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_gc_spares_paused(tmp_path, backend: str) -> None:
    store = JsonRunStore(tmp_path / "r") if backend == "json" else SQLiteRunStore(tmp_path / "s.db")
    done = _state("a", "succeeded")
    paused = _state("b", "paused")
    store.save(done)
    store.save(paused)
    deleted = store.gc(statuses=["succeeded", "failed", "cancelled"])
    assert done.run_id in deleted
    assert store.get(paused.run_id).state.status == "paused"
    store.delete(paused.run_id)
    with pytest.raises(ConfigError, match="not found"):
        store.get(paused.run_id)
    store.close()


def test_old_json_without_revision_loads_as_one(tmp_path) -> None:
    store = JsonRunStore(tmp_path / "runs")
    state = _state()
    persist_run(state, tmp_path / "runs")
    loaded = store.get(state.run_id)
    assert loaded.revision == 1
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_concurrent_cas_one_winner(tmp_path, backend: str) -> None:
    store = JsonRunStore(tmp_path / "r") if backend == "json" else SQLiteRunStore(tmp_path / "s.db")
    state = _state()
    store.save(state)

    def _attempt() -> str:
        try:
            store.save(state, expected_revision=1)
            return "ok"
        except RunStoreConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _attempt(), range(2)))
    assert results.count("ok") == 1
    assert results.count("conflict") == 1
    store.close()
