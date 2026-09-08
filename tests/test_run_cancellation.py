from __future__ import annotations

import threading
from pathlib import Path

import pytest

from readyagents.errors import CancellationRequested, WorkflowError
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.cancellation import CancellationToken
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState, load_run, persist_run

_JOIN_TIMEOUT = 5.0


def _join(thread: threading.Thread, timeout: float = _JOIN_TIMEOUT) -> None:
    thread.join(timeout)
    assert not thread.is_alive(), f"thread {thread.name} did not finish in {timeout}s"


def _ctx(
    spec: WorkflowSpec,
    tools: ToolRegistry | None = None,
    *,
    cancellation: CancellationToken | None = None,
    on_persist=None,
) -> ExecutionContext:
    return ExecutionContext(
        spec,
        tools or ToolRegistry(),
        dry_run=False,
        default_model="mock:test",
        cancellation=cancellation,
        on_persist=on_persist,
    )


def test_cancel_before_first_node(tmp_settings) -> None:
    spec = WorkflowSpec.model_validate(
        {
            "name": "cancel-before",
            "nodes": [
                {
                    "id": "only",
                    "type": "transform",
                    "template": "should-not-run",
                    "output_key": "out",
                }
            ],
        }
    )
    token = CancellationToken()
    token.request(actor="test", reason="before-start")
    queued = RunState.start(spec.name, {}, run_id="cancel-before-first-node")
    queued.status = "queued"

    def on_persist(state: RunState) -> None:
        persist_run(state, tmp_settings.runs_dir())

    ctx = _ctx(spec, cancellation=token, on_persist=on_persist)
    with pytest.raises(CancellationRequested) as exc:
        run_workflow(spec, {}, ctx, state=queued)
    assert queued.status == "cancelled"
    assert queued.results == []
    assert queued.node_outputs == {}
    assert exc.value.state is queued
    assert exc.value.run_id == queued.run_id
    loaded = load_run(tmp_settings.runs_dir(), queued.run_id)
    assert loaded.status == "cancelled"
    assert loaded.results == []
    assert loaded.node_outputs == {}


def test_cancel_between_nodes(tmp_settings) -> None:
    first_persisted = threading.Event()
    cancel_acked = threading.Event()
    second_started = threading.Event()
    parked = threading.Event()

    def first() -> str:
        return "one"

    def second() -> str:
        second_started.set()
        parked.wait(timeout=_JOIN_TIMEOUT)
        return "two"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="first", description="first", handler=first))
    tools.register(FunctionTool(name="second", description="second", handler=second))
    spec = WorkflowSpec.model_validate(
        {
            "name": "cancel-between",
            "start": "first",
            "nodes": [
                {
                    "id": "first",
                    "type": "tool",
                    "tool": "first",
                    "output_key": "a",
                    "next": "second",
                },
                {"id": "second", "type": "tool", "tool": "second", "output_key": "b"},
            ],
        }
    )
    token = CancellationToken()

    def on_persist(state: RunState) -> None:
        persist_run(state, tmp_settings.runs_dir())
        if any(r.node_id == "first" and r.status == "ok" for r in state.results):
            if not first_persisted.is_set():
                first_persisted.set()
                assert cancel_acked.wait(timeout=_JOIN_TIMEOUT)

    def canceler() -> None:
        assert first_persisted.wait(timeout=_JOIN_TIMEOUT)
        token.request(reason="between-nodes")
        cancel_acked.set()

    ctx = _ctx(spec, tools, cancellation=token, on_persist=on_persist)
    side = threading.Thread(target=canceler, name="cancel-between")
    side.start()
    try:
        with pytest.raises(CancellationRequested) as exc:
            run_workflow(spec, {}, ctx)
    finally:
        parked.set()
        _join(side)

    state = exc.value.state
    assert state is not None
    assert state.status == "cancelled"
    assert "first" in state.node_outputs
    assert "second" not in state.node_outputs
    assert all(r.node_id != "second" for r in state.results)
    assert not second_started.is_set()
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.status == "cancelled"
    assert "second" not in loaded.node_outputs


def test_cancel_during_retry_backoff(tmp_settings) -> None:
    hits = {"n": 0}
    first_fail = threading.Event()

    def flaky() -> str:
        hits["n"] += 1
        if hits["n"] == 1:
            first_fail.set()
            raise RuntimeError("transient")
        return "should-not-succeed"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="flaky", description="x", handler=flaky))
    spec = WorkflowSpec.model_validate(
        {
            "name": "cancel-backoff",
            "nodes": [
                {
                    "id": "f",
                    "type": "tool",
                    "tool": "flaky",
                    "retry": {"max_attempts": 3, "backoff_seconds": 0.4},
                    "output_key": "v",
                }
            ],
        }
    )
    token = CancellationToken()

    def on_persist(state: RunState) -> None:
        persist_run(state, tmp_settings.runs_dir())

    def canceler() -> None:
        assert first_fail.wait(timeout=_JOIN_TIMEOUT)
        token.request(reason="during-backoff")

    ctx = _ctx(spec, tools, cancellation=token, on_persist=on_persist)
    side = threading.Thread(target=canceler, name="cancel-backoff")
    side.start()
    try:
        with pytest.raises(CancellationRequested) as exc:
            run_workflow(spec, {}, ctx)
    finally:
        _join(side)

    state = exc.value.state
    assert state is not None
    assert state.status == "cancelled"
    assert hits["n"] == 1
    assert "v" not in state.node_outputs
    assert all(r.status != "ok" for r in state.results)
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.status == "cancelled"


def test_cancel_after_terminal_does_not_change_succeeded(tmp_settings) -> None:
    spec = WorkflowSpec.model_validate(
        {
            "name": "already-done",
            "nodes": [
                {"id": "t", "type": "transform", "template": "ok", "output_key": "out"},
            ],
        }
    )
    token = CancellationToken()

    def on_persist(state: RunState) -> None:
        persist_run(state, tmp_settings.runs_dir())

    ctx = _ctx(spec, cancellation=token, on_persist=on_persist)
    state = run_workflow(spec, {}, ctx)
    assert state.status == "succeeded"
    token.request(reason="too-late")
    assert state.status == "succeeded"
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.status == "succeeded"
    assert loaded.output_keys["out"] == "ok"


def test_cancel_during_blocking_node_sets_cancel_requested(tmp_settings) -> None:
    blocked = threading.Event()
    release = threading.Event()
    saw_cancel_requested = threading.Event()
    statuses: list[str] = []

    def blocker() -> str:
        blocked.set()
        assert release.wait(timeout=_JOIN_TIMEOUT)
        return "done"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="block", description="block", handler=blocker))
    spec = WorkflowSpec.model_validate(
        {
            "name": "cancel-block",
            "nodes": [
                {
                    "id": "block",
                    "type": "tool",
                    "tool": "block",
                    "timeout_seconds": 30,
                    "output_key": "out",
                }
            ],
        }
    )
    token = CancellationToken()

    def on_persist(state: RunState) -> None:
        statuses.append(state.status)
        persist_run(state, tmp_settings.runs_dir())
        if state.status == "cancel_requested":
            saw_cancel_requested.set()

    def canceler() -> None:
        assert blocked.wait(timeout=_JOIN_TIMEOUT)
        token.request(reason="blocked-node")
        assert saw_cancel_requested.wait(timeout=_JOIN_TIMEOUT)
        release.set()

    ctx = _ctx(spec, tools, cancellation=token, on_persist=on_persist)
    side = threading.Thread(target=canceler, name="cancel-block")
    side.start()
    try:
        with pytest.raises(CancellationRequested) as exc:
            run_workflow(spec, {}, ctx)
    finally:
        release.set()
        _join(side)

    state = exc.value.state
    assert state is not None
    assert state.status == "cancelled"
    assert "cancel_requested" in statuses
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.status == "cancelled"


def test_initial_state_uses_chosen_run_id(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "chosen.yaml"
    path.write_text(
        "name: chosen-id\nnodes:\n  - id: t\n    type: transform\n    template: ok\n    output_key: out\n",
        encoding="utf-8",
    )
    run_id = "cafebabedeadbeefcafebabedeadbeef"
    queued = RunState.start("chosen-id", {}, run_id=run_id)
    queued.status = "queued"
    state = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        initial_state=queued,
    )
    assert state.status == "succeeded"
    assert state.run_id == run_id
    record = tmp_settings.runs_dir() / f"{run_id}.json"
    assert record.is_file()
    loaded = load_run(tmp_settings.runs_dir(), run_id)
    assert loaded.run_id == run_id
    assert loaded.status == "succeeded"


def test_run_id_kwarg_names_persisted_file(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "rid.yaml"
    path.write_text(
        "name: rid-run\nnodes:\n  - id: t\n    type: transform\n    template: ok\n    output_key: out\n",
        encoding="utf-8",
    )
    run_id = "0123456789abcdef0123456789abcdef"
    state = run_workflow_file(path, settings=tmp_settings, persist=True, run_id=run_id)
    assert state.run_id == run_id
    assert (tmp_settings.runs_dir() / f"{run_id}.json").is_file()
    loaded = load_run(tmp_settings.runs_dir(), run_id)
    assert loaded.run_id == run_id
    assert loaded.status == "succeeded"


def test_initial_state_and_resume_state_are_exclusive(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "xor.yaml"
    path.write_text(
        "name: xor\nnodes:\n  - id: t\n    type: transform\n    template: ok\n    output_key: out\n",
        encoding="utf-8",
    )
    queued = RunState.start("xor", {}, run_id="aa" * 16)
    queued.status = "queued"
    with pytest.raises(WorkflowError, match="mutually exclusive"):
        run_workflow_file(
            path,
            settings=tmp_settings,
            persist=False,
            initial_state=queued,
            resume_state=queued,
        )
