from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from readyagents.mcp.http import compose_http_app
from readyagents.mcp.protocol import LATEST_PROTOCOL_VERSION, sdk_capability
from readyagents.mcp.server import construct_server, mcp_available

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[misc, assignment]


def _compose(tmp_path: Path):
    server = construct_server(allow_http=False, workspace=tmp_path)
    return compose_http_app(
        server=server,
        coordinator=None,
        token=None,
        bind_host="127.0.0.1",
        bind_port=8765,
    )


def _jsonrpc(response: Any) -> dict[str, Any]:
    ctype = (response.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        data = response.json()
        assert isinstance(data, dict)
        return data
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                data = json.loads(payload)
                if isinstance(data, dict):
                    return data
    raise AssertionError(response.text[:800])


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
def test_legacy_initialize_list_call(tmp_path: Path, version: str) -> None:
    if TestClient is None:
        pytest.skip("starlette not installed")
    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        init = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "legacy", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        payload = _jsonrpc(init)
        assert "result" in payload
        session = init.headers.get("mcp-session-id")
        extra = {}
        if session:
            extra["mcp-session-id"] = session
            extra["mcp-protocol-version"] = version
        listed = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **extra,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200, listed.text
        names = [row["name"] for row in _jsonrpc(listed)["result"]["tools"]]
        assert "calc" in names
        called = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **extra,
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "calc", "arguments": {"expression": "1+1"}},
            },
        )
        assert called.status_code == 200, called.text
        assert "2" in json.dumps(_jsonrpc(called))


def test_2026_tool_call_without_initialize(tmp_path: Path) -> None:
    if TestClient is None:
        pytest.skip("starlette not installed")
    cap = sdk_capability()
    if cap["tier"] != "full":
        pytest.skip("installed SDK does not honour 2026-07-28 tool calls")
    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        called = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "calc",
                "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "calc",
                    "arguments": {"expression": "2+3"},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
        )
        assert called.status_code == 200, called.text
        payload = _jsonrpc(called)
        assert "result" in payload
        result = payload["result"]
        assert result.get("resultType") in {None, "complete"} or "5" in json.dumps(payload)
        assert "5" in json.dumps(payload)


def test_subscriptions_listen_opt_in_and_cap(tmp_path: Path) -> None:
    if TestClient is None:
        pytest.skip("starlette not installed")
    from readyagents.mcp.protocol import honoured_protocol_versions

    if LATEST_PROTOCOL_VERSION not in honoured_protocol_versions():
        pytest.skip("installed SDK does not honour 2026-07-28 subscriptions")
    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        missing = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Mcp-Method": "subscriptions/listen",
                "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "subscriptions/listen",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION}
                },
            },
        )
        assert "error" in _jsonrpc(missing)
        ok = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Mcp-Method": "subscriptions/listen",
                "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "subscriptions/listen",
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION},
                },
            },
        )
        payload = _jsonrpc(ok)
        assert "result" in payload, payload
        meta = payload["result"].get("_meta") or {}
        assert meta.get("io.modelcontextprotocol/subscriptionId")


def test_capability_tier_matches_advertisement(tmp_path: Path) -> None:
    if TestClient is None:
        pytest.skip("starlette not installed")
    cap = sdk_capability()
    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Mcp-Method": "server/discover",
                "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {"io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION}
                },
            },
        )
        if cap["tier"] == "legacy":
            # 2026 is not honoured: either -32022 or a reduced list
            payload = _jsonrpc(resp)
            if "error" in payload:
                assert payload["error"]["code"] == -32022
            else:
                assert LATEST_PROTOCOL_VERSION not in payload["result"]["supportedVersions"]
            return
        assert resp.status_code == 200, resp.text
        versions = _jsonrpc(resp)["result"]["supportedVersions"]
        if cap["tier"] == "full":
            assert LATEST_PROTOCOL_VERSION in versions
        else:
            assert LATEST_PROTOCOL_VERSION not in versions
