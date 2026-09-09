from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.mcp.http import compose_http_app
from readyagents.mcp.protocol import (
    LATEST_PROTOCOL_VERSION,
    TASKS_EXTENSION,
    honoured_protocol_versions,
    sdk_capability,
)
from readyagents.mcp.server import construct_server, mcp_available

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[misc, assignment]


def _rpc_headers(
    method: str, *, name: str | None = None, version: str = LATEST_PROTOCOL_VERSION
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": version,
    }
    if version == LATEST_PROTOCOL_VERSION:
        headers["Mcp-Method"] = method
        if name:
            headers["Mcp-Name"] = name
    return headers


def _meta(version: str = LATEST_PROTOCOL_VERSION, *, tasks: bool = True) -> dict[str, Any]:
    caps: dict[str, Any] = {}
    if tasks:
        caps["extensions"] = {TASKS_EXTENSION: {}}
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": caps,
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
    }


def _body(response: Any) -> dict[str, Any]:
    ctype = (response.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        data = response.json()
        assert isinstance(data, dict)
        return data
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data
    raise AssertionError(f"no JSON-RPC body: {text[:800]!r}")


@pytest.fixture
def discover_client(tmp_path: Path):
    if TestClient is None:
        pytest.skip("starlette not installed")
    server = construct_server(allow_http=False, workspace=tmp_path)
    app_asgi = compose_http_app(
        server=server,
        coordinator=None,
        token=None,
        bind_host="127.0.0.1",
        bind_port=8765,
    )
    with TestClient(app_asgi, base_url="http://127.0.0.1:8765") as client:
        yield client, tmp_path


def test_discover_http_no_run_no_pack(discover_client) -> None:
    client, tmp_path = discover_client
    resp = client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": "d1",
            "method": "server/discover",
            "params": {},
        },
    )
    assert resp.status_code == 200, resp.text
    payload = _body(resp)
    result = payload["result"]
    assert result["resultType"] == "complete"
    assert "supportedVersions" in result
    assert isinstance(result["supportedVersions"], list)
    cap = sdk_capability()
    if cap["tier"] == "full":
        assert LATEST_PROTOCOL_VERSION in result["supportedVersions"]
        assert TASKS_EXTENSION in (result.get("capabilities") or {}).get("extensions", {})
    else:
        assert LATEST_PROTOCOL_VERSION not in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "readyagents"
    runs = tmp_path / ".readyagents" / "runs"
    assert not runs.exists() or not any(runs.glob("*.json"))


def test_discover_out_of_range_version() -> None:
    if TestClient is None:
        pytest.skip("starlette not installed")
    from pathlib import Path

    tmp = Path.cwd()
    server = construct_server(allow_http=False, workspace=tmp)
    asgi = compose_http_app(
        server=server, coordinator=None, token=None, bind_host="127.0.0.1", bind_port=8765
    )
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/mcp",
            headers=_rpc_headers("server/discover"),
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "server/discover",
                "params": {"_meta": _meta("1900-01-01")},
            },
        )
    assert resp.status_code == 400
    payload = _body(resp)
    assert payload["error"]["code"] == -32022
    assert "supported" in payload["error"]["data"]


def test_mcp_serve_json_envelope(monkeypatch) -> None:
    monkeypatch.setattr("readyagents.mcp.server.serve_stdio", lambda: None)
    result = CliRunner().invoke(app, ["mcp", "serve", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["command"] == "mcp serve"
    assert data["transport"] == "stdio"
    assert "protocol_versions" in data
    assert "sdk" in data
    assert "/runs" in data["deprecated_endpoints"]


def test_header_mismatch_rejected_before_dispatch(discover_client) -> None:
    if LATEST_PROTOCOL_VERSION not in honoured_protocol_versions():
        pytest.skip("installed SDK does not honour 2026-07-28 headers")
    client, _tmp = discover_client
    resp = client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Mcp-Method": "tools/call",
            "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": _meta()},
        },
    )
    assert resp.status_code == 400
    payload = _body(resp)
    assert payload["error"]["code"] == -32020


def test_header_mismatch_tools_call_rejected_before_sdk(discover_client) -> None:
    if LATEST_PROTOCOL_VERSION not in honoured_protocol_versions():
        pytest.skip("installed SDK does not honour 2026-07-28 headers")
    client, _tmp = discover_client
    resp = client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Mcp-Method": "server/discover",
            "Mcp-Name": "calc",
            "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
        },
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "calc",
                "arguments": {"expression": "1+1"},
                "_meta": _meta(),
            },
        },
    )
    assert resp.status_code == 400, resp.text
    payload = _body(resp)
    assert payload["error"]["code"] == -32020
    assert "4" not in json.dumps(payload.get("result") or {})
