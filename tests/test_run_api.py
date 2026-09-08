from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from readyagents.mcp.run_api import (
    RunCoordinator,
    load_run_exact,
)
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.state import RunState, persist_run

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[misc, assignment]


SLOW_WF = """
name: slow-one
start: s
nodes:
  - id: s
    type: tool
    tool: slow
    output_key: summary
"""

TWO_SLOW_WF = """
name: two-slow
start: a
nodes:
  - id: a
    type: tool
    tool: slow_a
    output_key: first
    next: b
  - id: b
    type: tool
    tool: slow_b
    output_key: summary
"""

BOOM_WF = """
name: boom
start: x
nodes:
  - id: x
    type: tool
    tool: boom
    retry:
      max_attempts: 1
      backoff_seconds: 0
    output_key: summary
"""

NEED_WF = """
name: need-input
required_inputs: [need_me]
start: t
nodes:
  - id: t
    type: transform
    template: "{{need_me}}"
    output_key: summary
"""

GATE_SLOW_WF = """
name: gate-slow
start: gate
nodes:
  - id: gate
    type: approval
    prompt: go?
    then: slow
    else: denied
  - id: slow
    type: tool
    tool: slow
    output_key: summary
  - id: denied
    type: transform
    template: "denied"
    output_key: summary
"""

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def _make_app(coordinator: RunCoordinator, settings: Any) -> Any:
    from readyagents.mcp.http import compose_http_app
    from readyagents.mcp.server import construct_server

    server = construct_server(allow_http=False, workspace=coordinator.workspace)
    return compose_http_app(
        server=server,
        coordinator=coordinator,
        token=None,
        bind_host="127.0.0.1",
        bind_port=8765,
    )


def _wait(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s; last={last!r}")


def _json(response) -> dict[str, Any]:
    data = response.json()
    assert isinstance(data, dict)
    assert "traceback" not in {k.lower() for k in data}
    return data


class _HandleClient:
    """Drive RunCoordinator.handle_* when Starlette is unavailable."""

    def __init__(self, coordinator: RunCoordinator) -> None:
        self.coord = coordinator

    def post(
        self, path: str, json: Any = None, headers: dict | None = None, content: bytes | None = None
    ):
        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        lower = {k.lower(): v for k, v in headers.items()}
        request_id = lower.get("x-request-id")
        raw_len = len(content) if content is not None else None
        payload = json
        if content is not None:
            if raw_len is not None and raw_len > self.coord.max_body_bytes:
                status, hdrs, body = self.coord.handle_start(
                    {}, request_id=request_id, raw_len=raw_len
                )
                return _FakeResponse(status, body, hdrs)
            try:
                payload = json.loads(content) if content else {}
            except json.JSONDecodeError as exc:
                from readyagents.mcp.run_api import HttpRequestError

                status, hdrs, body = self.coord._caught(
                    HttpRequestError(f"Malformed JSON: {exc}"),
                    request_id=request_id,
                )
                return _FakeResponse(status, body, hdrs)
        if path == "/runs":
            status, hdrs, body = self.coord.handle_start(
                payload,
                idempotency_key=lower.get("idempotency-key"),
                request_id=request_id,
                raw_len=raw_len,
            )
            return _FakeResponse(status, body, hdrs)
        if path.endswith("/decide"):
            run_id = path.split("/")[2]
            status, hdrs, body = self.coord.handle_decide(
                run_id, payload, request_id=request_id, raw_len=raw_len
            )
            return _FakeResponse(status, body, hdrs)
        if path.endswith("/cancel"):
            run_id = path.split("/")[2]
            status, hdrs, body = self.coord.handle_cancel(
                run_id, payload, request_id=request_id, raw_len=raw_len
            )
            return _FakeResponse(status, body, hdrs)
        raise AssertionError(f"unsupported POST {path}")

    def get(self, path: str, headers: dict | None = None):
        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        request_id = {k.lower(): v for k, v in headers.items()}.get("x-request-id")
        run_id = path.rsplit("/", 1)[-1]
        status, hdrs, body = self.coord.handle_get(run_id, request_id=request_id)
        return _FakeResponse(status, body, hdrs)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, headers: dict) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}

    def json(self) -> dict:
        return self._body


@pytest.fixture
def workspace(tmp_path: Path, tmp_settings, examples_dir: Path) -> Path:
    shutil.copy2(examples_dir / "calc_pipeline.yaml", tmp_path / "calc_pipeline.yaml")
    shutil.copy2(examples_dir / "approval_gate.yaml", tmp_path / "approval_gate.yaml")
    (tmp_path / "slow.yaml").write_text(SLOW_WF, encoding="utf-8")
    (tmp_path / "two_slow.yaml").write_text(TWO_SLOW_WF, encoding="utf-8")
    (tmp_path / "boom.yaml").write_text(BOOM_WF, encoding="utf-8")
    (tmp_path / "need.yaml").write_text(NEED_WF, encoding="utf-8")
    (tmp_path / "gate_slow.yaml").write_text(GATE_SLOW_WF, encoding="utf-8")
    return tmp_path


@pytest.fixture
def api_factory(tmp_settings, workspace: Path):
    coords: list[RunCoordinator] = []
    clients: list[Any] = []

    def factory(**kwargs: Any):
        extra_tools = kwargs.pop("extra_tools", None)
        coord = RunCoordinator(
            settings=tmp_settings,
            workspace=workspace,
            extra_tools=extra_tools,
            **kwargs,
        )
        coords.append(coord)
        if TestClient is not None:
            client = TestClient(
                _make_app(coord, tmp_settings),
                base_url="http://127.0.0.1:8765",
                raise_server_exceptions=True,
            )
        else:
            client = _HandleClient(coord)
        clients.append(client)
        return coord, client

    yield factory
    for coord in coords:
        coord.shutdown(timeout=2.0)


def _run_files(settings) -> list[Path]:
    runs = settings.runs_dir()
    if not runs.is_dir():
        return []
    return sorted(p for p in runs.glob("*.json") if not p.name.startswith("."))


def _wait_status(client, run_id: str, wanted, *, timeout: float = 5.0) -> dict[str, Any]:
    wanted_set = set(wanted)

    def _poll():
        response = client.get(f"/runs/{run_id}")
        if response.status_code != 200:
            return False
        data = _json(response)
        if data.get("status") in wanted_set:
            return data
        return False

    return _wait(_poll, timeout=timeout)


def _blocking_tool(name: str = "slow"):
    release = threading.Event()
    entered = threading.Event()
    calls = {"n": 0}

    def handler() -> str:
        calls["n"] += 1
        entered.set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if release.wait(0.05):
                break
        return f"{name}-ok"

    tools = ToolRegistry()
    tools.register(FunctionTool(name=name, description=name, handler=handler))
    return tools, release, entered, calls


def test_start_returns_202_and_persists_run_file(api_factory, tmp_settings) -> None:
    tools, release, entered, _calls = _blocking_tool()
    coord, client = api_factory(extra_tools=tools, max_concurrent_runs=1)
    try:
        response = client.post("/runs", json={"path": "slow.yaml"})
        assert response.status_code == 202
        body = _json(response)
        assert body["ok"] is True
        assert body["status"] == "queued"
        run_id = body["run_id"]
        assert load_run_exact.__module__ == "readyagents.mcp.run_api"
        assert len(run_id) == 32
        path = tmp_settings.runs_dir() / f"{run_id}.json"
        assert path.is_file()
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["run_id"] == run_id
        assert record["status"] in {"queued", "running"}
        assert response.headers.get("location") == f"/runs/{run_id}"
        started = coord.start_run  # public API must exist
        assert callable(started)
        assert callable(coord.get_run)
        assert callable(coord.decide)
        assert callable(coord.cancel)
    finally:
        release.set()


def test_get_works_while_tool_blocked(api_factory) -> None:
    tools, release, entered, _calls = _blocking_tool()
    coord, client = api_factory(extra_tools=tools)
    try:
        response = client.post("/runs", json={"path": "slow.yaml"})
        assert response.status_code == 202
        run_id = _json(response)["run_id"]
        assert entered.wait(timeout=5)
        fetched = client.get(f"/runs/{run_id}")
        assert fetched.status_code == 200
        body = _json(fetched)
        assert body["ok"] is True
        assert body["run_id"] == run_id
        assert body["status"] in {"queued", "running"}
        assert body["links"]["self"] == f"/runs/{run_id}"
        via_coord = coord.get_run(run_id)
        assert via_coord["run_id"] == run_id
        assert fetched.headers.get("cache-control") == "no-store"
    finally:
        release.set()
    terminal = _wait_status(client, run_id, TERMINAL)
    assert terminal["status"] == "succeeded"


def test_success_failure_pause_resume_approve_reject(api_factory, tmp_settings) -> None:
    _coord, client = api_factory()
    ok = client.post("/runs", json={"path": "calc_pipeline.yaml"})
    assert ok.status_code == 202
    run_id = _json(ok)["run_id"]
    done = _wait_status(client, run_id, TERMINAL)
    assert done["status"] == "succeeded"
    summary = str(
        done.get("output_keys", {}).get("summary") or done.get("outputs", {}).get("summary")
    )
    assert "calc_pipeline ok" in summary

    boom_tools = ToolRegistry()

    def boom() -> str:
        raise RuntimeError("tool exploded")

    boom_tools.register(FunctionTool(name="boom", description="fail", handler=boom))
    _coord2, client2 = api_factory(extra_tools=boom_tools)
    failed = client2.post("/runs", json={"path": "boom.yaml"})
    assert failed.status_code == 202
    fail_id = _json(failed)["run_id"]
    fail_rec = _wait_status(client2, fail_id, TERMINAL)
    assert fail_rec["status"] == "failed"

    before = {p.name for p in _run_files(tmp_settings)}
    missing = client.post("/runs", json={"path": "need.yaml", "inputs": {}})
    assert missing.status_code == 400
    body = _json(missing)
    assert body["ok"] is False
    after = {p.name for p in _run_files(tmp_settings)}
    assert after == before

    paused_post = client.post("/runs", json={"path": "approval_gate.yaml"})
    assert paused_post.status_code == 202
    gate_id = _json(paused_post)["run_id"]
    paused = _wait_status(client, gate_id, {"paused"})
    assert paused["pending_node"] == "gate"
    prompt = (paused.get("pending") or {}).get("prompt") or ""
    assert "Release payment" in prompt

    approve = client.post(
        f"/runs/{gate_id}/decide",
        json={"node_id": "gate", "decision": "approve"},
    )
    assert approve.status_code == 202
    assert _json(approve)["status"] == "running"
    approved = _wait_status(client, gate_id, TERMINAL)
    assert approved["status"] == "succeeded"
    text = str(
        approved.get("output_keys", {}).get("summary") or approved.get("outputs", {}).get("summary")
    )
    assert "approval_gate ok" in text
    assert "paid" in text

    reject_post = client.post("/runs", json={"path": "approval_gate.yaml"})
    reject_id = _json(reject_post)["run_id"]
    _wait_status(client, reject_id, {"paused"})
    denied = client.post(
        f"/runs/{reject_id}/decide",
        json={"node_id": "gate", "decision": "reject"},
    )
    assert denied.status_code == 202
    rejected = _wait_status(client, reject_id, TERMINAL)
    assert rejected["status"] == "succeeded"
    denied_text = str(
        rejected.get("output_keys", {}).get("summary") or rejected.get("outputs", {}).get("summary")
    )
    assert "denied" in denied_text


def test_malformed_unknown_inputs_large_body_invalid_and_prefix_ids(
    api_factory, tmp_settings
) -> None:
    _coord, client = api_factory(max_body_bytes=2048)
    started = client.post("/runs", json={"path": "calc_pipeline.yaml"})
    assert started.status_code == 202
    run_id = _json(started)["run_id"]
    _wait_status(client, run_id, TERMINAL)

    malformed = client.post(
        "/runs",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert malformed.status_code == 400
    assert _json(malformed)["ok"] is False

    unknown = client.post("/runs", json={"path": "calc_pipeline.yaml", "surprise": 1})
    assert unknown.status_code == 400
    assert "Unknown" in _json(unknown)["message"]

    non_object_inputs = client.post(
        "/runs", json={"path": "calc_pipeline.yaml", "inputs": ["nope"]}
    )
    assert non_object_inputs.status_code == 400

    non_object = client.post(
        "/runs",
        content=b"[1, 2]",
        headers={"content-type": "application/json"},
    )
    assert non_object.status_code == 400

    large = client.post(
        "/runs",
        content=b"{" + b"x" * 3000 + b"}",
        headers={"content-type": "application/json"},
    )
    assert large.status_code == 413

    bad_id = client.get("/runs/not-a-valid-id")
    assert bad_id.status_code == 400
    prefix = client.get(f"/runs/{run_id[:8]}")
    assert prefix.status_code == 400
    missing = client.get(f"/runs/{'ab' * 16}")
    assert missing.status_code == 404
    full = client.get(f"/runs/{run_id}")
    assert full.status_code == 200
    assert _json(full)["run_id"] == run_id

    yes = client.post(
        f"/runs/{run_id}/decide",
        json={"node_id": "gate", "decision": "yes"},
    )
    assert yes.status_code in {400, 409}


def test_queue_overflow_second_start_429_no_extra_file(api_factory, tmp_settings) -> None:
    tools, release, entered, _calls = _blocking_tool()
    _coord, client = api_factory(
        extra_tools=tools,
        max_concurrent_runs=1,
        max_pending_runs=1,
    )
    try:
        first = client.post("/runs", json={"path": "slow.yaml"})
        assert first.status_code == 202
        assert entered.wait(timeout=5)
        before = {p.name for p in _run_files(tmp_settings)}
        second = client.post("/runs", json={"path": "slow.yaml"})
        assert second.status_code == 429
        assert _json(second)["ok"] is False
        after = {p.name for p in _run_files(tmp_settings)}
        assert after == before
    finally:
        release.set()


def test_concurrent_duplicate_decide_one_202_one_409(api_factory) -> None:
    tools, release, entered, calls = _blocking_tool("slow")
    coord, client = api_factory(extra_tools=tools, max_concurrent_runs=2)
    try:
        started = client.post("/runs", json={"path": "gate_slow.yaml"})
        run_id = _json(started)["run_id"]
        _wait_status(client, run_id, {"paused"})
        payload = {"node_id": "gate", "decision": "approve"}
        results: list[tuple[int, dict]] = []
        barrier = threading.Barrier(2)

        def _post() -> None:
            barrier.wait(timeout=5)
            status, _hdrs, body = coord.handle_decide(run_id, payload)
            results.append((status, body))

        t1 = threading.Thread(target=_post)
        t2 = threading.Thread(target=_post)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        codes = sorted(status for status, _body in results)
        assert codes == [202, 409]
        assert entered.wait(timeout=5)
        assert calls["n"] == 1
    finally:
        release.set()
    done = _wait_status(client, run_id, TERMINAL)
    assert done["status"] == "succeeded"
    assert calls["n"] == 1


def test_idempotency_replay_and_mismatch_409(api_factory) -> None:
    tools, release, entered, _calls = _blocking_tool()
    _coord, client = api_factory(extra_tools=tools)
    headers = {"Idempotency-Key": "same-key"}
    try:
        first = client.post("/runs", json={"path": "slow.yaml"}, headers=headers)
        assert first.status_code == 202
        run_id = _json(first)["run_id"]
        replay = client.post("/runs", json={"path": "slow.yaml"}, headers=headers)
        assert replay.status_code == 202
        assert _json(replay)["run_id"] == run_id
        mismatch = client.post(
            "/runs",
            json={"path": "slow.yaml", "dry_run": True},
            headers=headers,
        )
        assert mismatch.status_code == 409
        err = _json(mismatch)
        assert err["ok"] is False
        assert err["error"] in {"RunConflict", "_RunConflict"}
        fresh = client.post("/runs", json={"path": "slow.yaml"})
        assert fresh.status_code == 202
        assert _json(fresh)["run_id"] != run_id
    finally:
        release.set()


def test_concurrent_same_idempotency_key_creates_one_run(api_factory, tmp_settings) -> None:
    tools, release, entered, _calls = _blocking_tool()
    _coord, client = api_factory(extra_tools=tools)
    barrier = threading.Barrier(2)
    results: list[Any] = []

    def _post() -> None:
        barrier.wait(timeout=5)
        results.append(
            client.post(
                "/runs",
                json={"path": "slow.yaml"},
                headers={"Idempotency-Key": "race-key"},
            )
        )

    try:
        workers = [threading.Thread(target=_post, name=f"idem-{i}") for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert len(results) == 2
        assert all(item.status_code == 202 for item in results)
        ids = {_json(item)["run_id"] for item in results}
        assert len(ids) == 1
        files = [p for p in _run_files(tmp_settings) if p.name.endswith(".json")]
        assert len(files) == 1
        assert files[0].stem in ids
    finally:
        release.set()


def test_cancel_queued_between_nodes_terminal_and_during_tool(api_factory) -> None:
    a_release, b_release = threading.Event(), threading.Event()
    a_entered, b_entered = threading.Event(), threading.Event()
    calls = {"a": 0, "b": 0}

    def slow_a() -> str:
        calls["a"] += 1
        a_entered.set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if a_release.wait(0.05):
                break
        return "A"

    def slow_b() -> str:
        calls["b"] += 1
        b_entered.set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if b_release.wait(0.05):
                break
        return "B"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="slow_a", description="a", handler=slow_a))
    tools.register(FunctionTool(name="slow_b", description="b", handler=slow_b))
    tools.register(FunctionTool(name="slow", description="s", handler=slow_a))  # unused here
    _coord, client = api_factory(extra_tools=tools, max_concurrent_runs=1, max_pending_runs=4)

    try:
        occupy = client.post("/runs", json={"path": "two_slow.yaml"})
        occupy_id = _json(occupy)["run_id"]
        assert a_entered.wait(timeout=5)

        queued = client.post("/runs", json={"path": "two_slow.yaml"})
        assert queued.status_code == 202
        queued_id = _json(queued)["run_id"]
        queued_get = client.get(f"/runs/{queued_id}")
        assert _json(queued_get)["status"] in {"queued", "running"}
        cancel_q = client.post(f"/runs/{queued_id}/cancel", json={"reason": "never start"})
        assert cancel_q.status_code == 202
        assert _json(cancel_q)["status"] == "cancel_requested"
        a_release.set()
        b_release.set()
        occupied_done = _wait_status(client, occupy_id, TERMINAL)
        assert occupied_done["status"] in {"succeeded", "cancelled"}
        queued_done = _wait_status(client, queued_id, TERMINAL)
        assert queued_done["status"] == "cancelled"
    finally:
        a_release.set()
        b_release.set()

    a_release.clear()
    b_release.clear()
    a_entered.clear()
    b_entered.clear()
    calls["a"] = 0
    calls["b"] = 0
    mid_tools = ToolRegistry()

    def mid_a() -> str:
        calls["a"] += 1
        a_entered.set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if a_release.wait(0.05):
                break
        return "A"

    def mid_b() -> str:
        calls["b"] += 1
        b_entered.set()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if b_release.wait(0.05):
                break
        return "B"

    mid_tools.register(FunctionTool(name="slow_a", description="a", handler=mid_a))
    mid_tools.register(FunctionTool(name="slow_b", description="b", handler=mid_b))
    _coord2, client2 = api_factory(extra_tools=mid_tools, max_concurrent_runs=1)
    try:
        started = client2.post("/runs", json={"path": "two_slow.yaml"})
        mid_id = _json(started)["run_id"]
        assert a_entered.wait(timeout=5)
        a_release.set()
        assert b_entered.wait(timeout=5)
        cancel_mid = client2.post(f"/runs/{mid_id}/cancel", json={})
        assert cancel_mid.status_code == 202
        assert _json(cancel_mid)["status"] == "cancel_requested"
        b_release.set()
        mid_done = _wait_status(client2, mid_id, TERMINAL)
        assert mid_done["status"] == "cancelled"
    finally:
        a_release.set()
        b_release.set()

    block_tools, block_release, block_entered, _c = _blocking_tool()
    _coord3, client3 = api_factory(extra_tools=block_tools)
    try:
        started = client3.post("/runs", json={"path": "slow.yaml"})
        block_id = _json(started)["run_id"]
        assert block_entered.wait(timeout=5)
        cancel_block = client3.post(
            f"/runs/{block_id}/cancel", json={"actor": "ops", "reason": "stop"}
        )
        assert cancel_block.status_code == 202
        assert _json(cancel_block)["status"] == "cancel_requested"
        during = client3.get(f"/runs/{block_id}")
        assert _json(during)["status"] in {"cancel_requested", "cancelled", "running"}
        block_release.set()
        blocked_done = _wait_status(client3, block_id, TERMINAL)
        assert blocked_done["status"] == "cancelled"
        again = client3.post(f"/runs/{block_id}/cancel", json={})
        assert again.status_code == 200
        assert _json(again)["status"] == "cancelled"
    finally:
        block_release.set()


def test_disconnected_submitter_get_by_full_id_only(api_factory) -> None:
    _coord, client = api_factory()
    response = client.post("/runs", json={"path": "calc_pipeline.yaml"})
    assert response.status_code == 202
    run_id = _json(response)["run_id"]
    done = _wait_status(client, run_id, TERMINAL)
    assert done["status"] == "succeeded"
    prefix = client.get(f"/runs/{run_id[:8]}")
    assert prefix.status_code == 400
    full = client.get(f"/runs/{run_id}")
    assert full.status_code == 200
    assert _json(full)["run_id"] == run_id


def test_shutdown_rejects_new_starts(api_factory, tmp_settings) -> None:
    coord, client = api_factory()
    coord.shutdown(timeout=1.0)
    before = {p.name for p in _run_files(tmp_settings)}
    response = client.post("/runs", json={"path": "calc_pipeline.yaml"})
    assert response.status_code == 503
    after = {p.name for p in _run_files(tmp_settings)}
    assert after == before


def test_path_outside_workspace_is_400(api_factory) -> None:
    _coord, client = api_factory()
    response = client.post("/runs", json={"path": "../calc_pipeline.yaml"})
    assert response.status_code == 400
    message = _json(response)["message"].lower()
    assert "outside" in message or "workspace" in message


def test_request_id_in_error_envelope(api_factory) -> None:
    _coord, client = api_factory()
    response = client.get(
        "/runs/not-valid",
        headers={"X-Request-Id": "req-123"},
    )
    assert response.status_code == 400
    body = _json(response)
    assert body["ok"] is False
    assert body.get("request_id") == "req-123"


def test_coordinator_defaults_workspace_from_settings(tmp_settings) -> None:
    coord = RunCoordinator(settings=tmp_settings)
    try:
        assert coord.workspace == tmp_settings.workspace_path()
    finally:
        coord.shutdown()


def test_cancel_inactive_queued_run_does_not_deadlock(tmp_settings) -> None:
    """Cancel a persisted queued run that has no in-process worker.

    ``_cancel`` holds the per-run lock and then calls ``_finish_cancelled``,
    which takes the same lock. A non-reentrant lock deadlocks this path.
    """
    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_settings.workspace_path())
    run_id = "ab" * 16
    queued = RunState.start("inactive-cancel", {}, run_id=run_id)
    queued.status = "queued"
    persist_run(queued, tmp_settings.runs_dir())
    box: dict[str, object] = {}

    def _cancel() -> None:
        try:
            box["out"] = coord.cancel(run_id, {"actor": "tester"})
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    thread = threading.Thread(target=_cancel, name="cancel-inactive")
    thread.start()
    thread.join(3.0)
    try:
        assert not thread.is_alive(), "cancel deadlocked on per-run lock"
        assert "err" not in box, box.get("err")
        payload = box["out"]
        assert isinstance(payload, dict)
        assert payload["run_id"] == run_id
        assert payload["status"] == "cancelled"
        loaded = load_run_exact(tmp_settings.runs_dir(), run_id)
        assert loaded.status == "cancelled"
    finally:
        coord.shutdown()
