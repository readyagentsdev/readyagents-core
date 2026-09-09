from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from readyagents.errors import ConfigError
from readyagents.mcp.http import compose_http_app
from readyagents.mcp.protocol import LATEST_PROTOCOL_VERSION, TASKS_EXTENSION
from readyagents.mcp.run_api import RunCoordinator
from readyagents.mcp.server import construct_server, mcp_available
from readyagents.mcp.tasks import input_request_key, map_run_status, require_task_id

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[misc, assignment]

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "completed"})


def _headers(method: str, *, name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": method,
        "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if name:
        headers["Mcp-Name"] = name
    return headers


def _params(extra: dict[str, Any] | None = None, *, tasks: bool = True) -> dict[str, Any]:
    caps: dict[str, Any] = {"extensions": {TASKS_EXTENSION: {}}} if tasks else {}
    params = dict(extra or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": caps,
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
    }
    return params


def _rpc_body(response: Any) -> dict[str, Any]:
    data = response.json()
    assert isinstance(data, dict)
    return data


def _wait_task(client, task_id: str, wanted, *, timeout: float = 5.0) -> dict[str, Any]:
    wanted_set = set(wanted)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = client.post(
            "/mcp",
            headers=_headers("tasks/get", name=task_id),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": _params({"taskId": task_id}),
            },
        )
        payload = _rpc_body(resp)
        result = payload.get("result")
        last = result
        if isinstance(result, dict) and result.get("status") in wanted_set:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {wanted}; last={last!r}")


@pytest.fixture
def task_env(tmp_path: Path, tmp_settings, examples_dir: Path):
    if TestClient is None:
        pytest.skip("starlette not installed")
    (tmp_path / "ok.yaml").write_text(
        'name: ok\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: "ok"\n    output_key: summary\n',
        encoding="utf-8",
    )
    src = examples_dir / "approval_gate.yaml"
    (tmp_path / "approval_gate.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "boom.yaml").write_text(
        "name: boom\nstart: x\nnodes:\n  - id: x\n    type: tool\n    tool: missing\n    output_key: summary\n",
        encoding="utf-8",
    )
    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_path)
    server = construct_server(allow_http=False, workspace=tmp_path)
    asgi = compose_http_app(
        server=server,
        coordinator=coord,
        token=None,
        bind_host="127.0.0.1",
        bind_port=8765,
    )
    client = TestClient(asgi, base_url="http://127.0.0.1:8765")
    yield coord, client, tmp_path
    coord.shutdown(timeout=2.0)
    client.close()


def test_require_task_id_rejects_prefix() -> None:
    with pytest.raises(ConfigError, match="Task not found"):
        require_task_id("abc")
    with pytest.raises(ConfigError, match="Task not found"):
        require_task_id("a" * 8)
    assert require_task_id("a" * 32) == "a" * 32


def test_status_map() -> None:
    assert map_run_status("running") == "working"
    assert map_run_status("queued") == "working"
    assert map_run_status("paused") == "input_required"
    assert map_run_status("succeeded") == "completed"
    assert map_run_status("failed") == "failed"
    assert map_run_status("cancelled") == "cancelled"


def test_create_then_get_under_persist_delay(task_env) -> None:
    coord, client, _tmp = task_env
    coord._persist_delay_s = 0.15
    started = time.monotonic()
    resp = client.post(
        "/mcp",
        headers=_headers("tools/call", name="run_workflow"),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": _params({"name": "run_workflow", "arguments": {"path": "ok.yaml"}}),
        },
    )
    payload = _rpc_body(resp)
    assert resp.status_code == 200, payload
    result = payload["result"]
    assert result["resultType"] == "task"
    task_id = result["taskId"]
    assert len(task_id) == 32
    got = client.post(
        "/mcp",
        headers=_headers("tasks/get", name=task_id),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/get",
            "params": _params({"taskId": task_id}),
        },
    )
    fetched = _rpc_body(got)["result"]
    assert fetched["taskId"] == task_id
    assert fetched["pollIntervalMs"] == 1000
    assert fetched["ttlMs"] is None
    assert time.monotonic() - started >= 0.15


def test_prefix_unknown_unauthorized_same_not_found(task_env) -> None:
    coord, client, _tmp = task_env
    bodies = []
    for ident in ("deadbeef" * 4, "ab", "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"):
        resp = client.post(
            "/mcp",
            headers=_headers("tasks/get", name=ident),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/get",
                "params": _params({"taskId": ident}),
            },
        )
        payload = _rpc_body(resp)
        assert "error" in payload
        bodies.append(payload["error"]["message"])
    assert set(bodies) == {"Task not found"}


def test_runs_alias_matches_tasks_get(task_env) -> None:
    coord, client, _tmp = task_env
    started = client.post("/runs", json={"path": "ok.yaml"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert started.json()["deprecated"] is True
    http_rec = client.get(f"/runs/{run_id}")
    assert http_rec.status_code == 200
    rpc = client.post(
        "/mcp",
        headers=_headers("tasks/get", name=run_id),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tasks/get",
            "params": _params({"taskId": run_id}),
        },
    )
    task = _rpc_body(rpc)["result"]
    assert task["taskId"] == run_id
    mapped = map_run_status(http_rec.json()["status"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and mapped not in {"completed", "failed"}:
        time.sleep(0.05)
        http_rec = client.get(f"/runs/{run_id}")
        mapped = map_run_status(http_rec.json()["status"])
        rpc = client.post(
            "/mcp",
            headers=_headers("tasks/get", name=run_id),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tasks/get",
                "params": _params({"taskId": run_id}),
            },
        )
        task = _rpc_body(rpc)["result"]
    assert task["status"] == mapped


def test_cancel_idempotent(task_env) -> None:
    coord, client, _tmp = task_env
    started = client.post("/runs", json={"path": "ok.yaml"})
    run_id = started.json()["run_id"]
    first = client.post(
        "/mcp",
        headers=_headers("tasks/cancel", name=run_id),
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tasks/cancel",
            "params": _params({"taskId": run_id}),
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/mcp",
        headers=_headers("tasks/cancel", name=run_id),
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tasks/cancel",
            "params": _params({"taskId": run_id}),
        },
    )
    assert second.status_code == 200
    assert _rpc_body(second)["result"]["resultType"] == "complete"


def test_input_request_key_grammar() -> None:
    key = input_request_key("a" * 32, "gate", 0)
    assert key == f"readyagents.approval.{'a' * 32}.gate.0"
    assert key != input_request_key("a" * 32, "gate", 1)
