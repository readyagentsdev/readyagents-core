"""Live TCP tests: mcp probe against bearer /mcp, and an HTTP JSON-RPC MRTR transcript."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.mcp.http import compose_http_app
from readyagents.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    TASKS_EXTENSION,
    honoured_protocol_versions,
)
from readyagents.mcp.run_api import RunCoordinator
from readyagents.mcp.server import construct_server, mcp_available

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

TOKEN = "live-probe-bearer-token-for-tests"
runner = CliRunner()


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@contextmanager
def _uvicorn(app: Any, *, host: str = "127.0.0.1", port: int | None = None) -> Iterator[str]:
    uvicorn = pytest.importorskip("uvicorn")
    port = int(port or _free_port())
    config = uvicorn.Config(app, host=host, port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="readyagents-live-mcp", daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    if not getattr(server, "started", False):
        raise RuntimeError("live MCP HTTP server failed to start")
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=8)


def _rpc(
    url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    rpc_id: int = 1,
    token: str | None = TOKEN,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    params = dict(params or {})
    meta = dict(params.get("_meta") or {})
    meta.setdefault("io.modelcontextprotocol/protocolVersion", LATEST_PROTOCOL_VERSION)
    caps = dict(meta.get("io.modelcontextprotocol/clientCapabilities") or {})
    ext = dict(caps.get("extensions") or {})
    ext.setdefault(TASKS_EXTENSION, {})
    caps["extensions"] = ext
    meta["io.modelcontextprotocol/clientCapabilities"] = caps
    params["_meta"] = meta
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": method,
        "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = str(params.get("name") or "")
    elif method.startswith("tasks/"):
        headers["Mcp-Name"] = str(params.get("taskId") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode(
        "utf-8"
    )
    req = Request(url + "/mcp", data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type") or ""
    except HTTPError as err:
        raw = err.read()
        ctype = err.headers.get("content-type") if err.headers else ""
        parsed = json.loads(raw.decode("utf-8") or "null")
        if isinstance(parsed, dict):
            return parsed
        raise
    text = raw.decode("utf-8")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                blob = line[5:].strip()
                if blob and blob != "[DONE]":
                    data = json.loads(blob)
                    if isinstance(data, dict):
                        return data
        raise AssertionError(f"no JSON-RPC in SSE: {text[:400]!r}")
    data = json.loads(text)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "result" in item:
                return item
        return data[0] if data else {}
    assert isinstance(data, dict)
    return data


def test_mcp_probe_cli_against_live_bearer_server(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    if LATEST_PROTOCOL_VERSION not in honoured_protocol_versions():
        pytest.skip("installed SDK does not honour 2026-07-28 discover")
    port = _free_port()
    server = construct_server(allow_http=False, workspace=tmp_path)
    asgi = compose_http_app(
        server=server,
        coordinator=None,
        token=TOKEN,
        bind_host="127.0.0.1",
        bind_port=port,
    )
    with _uvicorn(asgi, port=port) as origin:
        monkeypatch.delenv("READYAGENTS_MCP_TOKEN", raising=False)
        denied = runner.invoke(app, ["mcp", "probe", f"{origin}/mcp", "--json"])
        assert denied.exit_code == 1, denied.stdout + denied.stderr
        assert "tools/call" not in (denied.stdout + denied.stderr)

        monkeypatch.setenv("READYAGENTS_MCP_TOKEN", TOKEN)
        ok = runner.invoke(app, ["mcp", "probe", f"{origin}/mcp", "--json"])
        assert ok.exit_code == 0, ok.stdout + ok.stderr
        data = json.loads(ok.stdout)
        assert data["ok"] is True
        assert data["command"] == "mcp probe"
        assert LATEST_PROTOCOL_VERSION in data["protocol_versions"]
        assert TASKS_EXTENSION in data["extensions"]
        assert "tools/call" not in ok.stdout
        runs = tmp_path / ".readyagents" / "runs"
        assert not runs.exists() or not any(runs.glob("*.json"))


def test_http_jsonrpc_mrtr_round_trip(tmp_path: Path, tmp_settings, examples_dir: Path) -> None:
    if LATEST_PROTOCOL_VERSION not in honoured_protocol_versions():
        pytest.skip("installed SDK does not honour 2026-07-28 tasks")
    src = examples_dir / "approval_gate.yaml"
    (tmp_path / "approval_gate.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_path)
    server = construct_server(allow_http=False, workspace=tmp_path)
    port = _free_port()
    asgi = compose_http_app(
        server=server,
        coordinator=coord,
        token=TOKEN,
        bind_host="127.0.0.1",
        bind_port=port,
    )
    log: list[dict[str, Any]] = []
    try:
        with _uvicorn(asgi, port=port) as origin:
            created = _rpc(
                origin,
                "tools/call",
                {
                    "name": "run_workflow",
                    "arguments": {"path": "approval_gate.yaml", "actor": "reviewer"},
                },
                rpc_id=1,
            )
            log.append({"step": "create", "body": created})
            result = created.get("result") or {}
            assert result.get("resultType") == "task", created
            task_id = result["taskId"]
            paused = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                polled = _rpc(origin, "tasks/get", {"taskId": task_id}, rpc_id=2)
                log.append({"step": "poll", "body": polled})
                status = (polled.get("result") or {}).get("status")
                if status == "input_required":
                    paused = polled["result"]
                    break
                time.sleep(0.05)
            assert paused is not None, log[-3:]
            key = next(iter(paused["inputRequests"]))
            updated = _rpc(
                origin,
                "tasks/update",
                {
                    "taskId": task_id,
                    "actor": "reviewer",
                    "inputResponses": {
                        key: {"action": "accept", "content": {"decision": "approve"}}
                    },
                },
                rpc_id=3,
            )
            log.append({"step": "update", "body": updated})
            assert "result" in updated, updated
            final = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                polled = _rpc(origin, "tasks/get", {"taskId": task_id}, rpc_id=4)
                log.append({"step": "final_poll", "body": polled})
                status = (polled.get("result") or {}).get("status")
                if status in {"completed", "failed", "cancelled"}:
                    final = polled["result"]
                    break
                time.sleep(0.05)
            assert final is not None and final.get("status") == "completed", final
    finally:
        coord.shutdown(timeout=2.0)
    # Durable proof the round trip used HTTP JSON-RPC, not TaskService.
    assert any(row["step"] == "create" for row in log)
    assert any(
        (row.get("body") or {}).get("result", {}).get("status") == "input_required" for row in log
    )
    capture = (os.environ.get("READYAGENTS_CAPTURE_MRTR") or "").strip()
    if capture:
        Path(capture).write_text(json.dumps(log, indent=2), encoding="utf-8")
