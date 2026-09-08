from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import MCPError
from readyagents.mcp.http import (
    assert_loopback_host,
    compose_http_app,
    resolve_bearer_token,
    serve_streamable_http,
)
from readyagents.mcp.server import construct_server, mcp_available

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

runner = CliRunner()

TOKEN = "s3cret-readyagents-mcp-bearer-test-token"
_EXPECTED_TOOLS = (
    "now",
    "calc",
    "json_get",
    "json_set",
    "json_merge",
    "list_dir",
    "read_file",
    "write_file",
    "http_get",
    "run_workflow",
)


def _mcp_headers(token: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


def _mcp_body(response: Any) -> dict[str, Any]:
    ctype = (response.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        data = response.json()
        if isinstance(data, dict):
            return data
        raise AssertionError(f"expected JSON object, got {data!r}")
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            data = json.loads(payload)
            if isinstance(data, dict):
                return data
    raise AssertionError(f"no MCP payload in {ctype!r}: {text[:800]!r}")


def _compose(tmp_path: Path, *, token: str | None = TOKEN):
    server = construct_server(allow_http=False, workspace=tmp_path)
    return compose_http_app(
        server=server,
        coordinator=None,
        token=token,
        bind_host="127.0.0.1",
        bind_port=8765,
    )


def test_mcp_serve_default_still_stdio(monkeypatch) -> None:
    called = {"n": 0}

    def fake_serve() -> None:
        called["n"] += 1

    monkeypatch.setattr("readyagents.mcp.server.serve_stdio", fake_serve)
    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert called["n"] == 1


def test_construct_server_tool_set_unchanged(tmp_path: Path) -> None:
    server = construct_server(allow_http=False, workspace=tmp_path)
    by_name = {t.name: t for t in server._tool_manager.list_tools()}
    for expected in _EXPECTED_TOOLS:
        assert expected in by_name


def test_streamable_http_initialize_list_and_call(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        init = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        payload = _mcp_body(init)
        assert "result" in payload
        session = init.headers.get("mcp-session-id")
        assert session
        extra = {"mcp-session-id": session, "mcp-protocol-version": "2025-03-26"}
        note = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN, extra),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert note.status_code in {200, 202}, note.text
        listed = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN, extra),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200, listed.text
        tools = _mcp_body(listed)
        names = [row["name"] for row in tools["result"]["tools"]]
        assert "now" in names
        assert "calc" in names
        called = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN, extra),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "calc", "arguments": {"expression": "2 + 2"}},
            },
        )
        assert called.status_code == 200, called.text
        result = _mcp_body(called)
        assert "4" in json.dumps(result)


def test_mcp_missing_bearer_is_401(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/mcp",
            headers=_mcp_headers(None, {"X-Request-Id": "req-missing-auth"}),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert resp.status_code == 401
    assert "bearer" in (resp.headers.get("www-authenticate") or "").lower()
    assert "no-store" in (resp.headers.get("cache-control") or "").lower()
    assert resp.headers.get("x-request-id") == "req-missing-auth"
    assert TOKEN not in (resp.text or "")


def test_mcp_wrong_bearer_is_401(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/mcp",
            headers=_mcp_headers("definitely-not-the-token"),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert resp.status_code == 401
    assert "bearer" in (resp.headers.get("www-authenticate") or "").lower()
    assert TOKEN not in (resp.text or "")


def test_runs_missing_and_wrong_bearer_is_401(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        missing = client.post("/runs", json={"path": "examples/calc_pipeline.yaml"})
        wrong = client.post(
            "/runs",
            headers=_mcp_headers("nope"),
            json={"path": "examples/calc_pipeline.yaml"},
        )
    assert missing.status_code == 401
    assert "bearer" in (missing.headers.get("www-authenticate") or "").lower()
    assert wrong.status_code == 401
    assert TOKEN not in (missing.text or "") + (wrong.text or "")


def test_token_not_in_response_bodies_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with caplog.at_level(logging.DEBUG):
        with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
            missing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            wrong = client.post(
                "/mcp",
                headers=_mcp_headers("wrong-token"),
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            okish = client.post(
                "/mcp",
                headers=_mcp_headers(TOKEN),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
            )
    blob = "".join(
        [
            missing.text or "",
            wrong.text or "",
            okish.text or "",
            caplog.text,
        ]
    )
    assert TOKEN not in blob


def test_bad_host_rejected_without_success(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://evil.example:8765") as client:
        resp = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
    assert resp.status_code in {400, 421}
    assert resp.status_code != 200
    body = resp.text or ""
    assert "result" not in body or '"ok": false' in body.lower() or '"ok":false' in body.lower()


def test_bad_origin_rejected(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    asgi = _compose(tmp_path)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        resp = client.post(
            "/mcp",
            headers=_mcp_headers(TOKEN, {"Origin": "http://evil.example"}),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
    assert resp.status_code == 403
    assert resp.status_code != 200


def test_mcp_serve_help_has_streamable_http_not_token_flag() -> None:
    result = runner.invoke(app, ["mcp", "serve", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = result.stdout
    assert "--transport" in text
    assert "streamable-http" in text
    assert "--token-env" in text
    stripped = text.replace("--token-env", "")
    assert "--token" not in stripped


def test_assert_loopback_host_rejects_non_loopback() -> None:
    assert assert_loopback_host("127.0.0.1") == "127.0.0.1"
    assert assert_loopback_host("localhost").lower() == "localhost"
    assert assert_loopback_host("::1") == "::1"
    for bad in ("0.0.0.0", "::", "8.8.8.8", "", "example.com"):
        with pytest.raises(MCPError, match="loopback"):
            assert_loopback_host(bad)
    with pytest.raises(MCPError, match="loopback"):
        serve_streamable_http(
            host="0.0.0.0",
            port=8765,
            auth_mode="token",
            token_env="READYAGENTS_MCP_TOKEN",
            max_concurrent_runs=4,
            max_pending_runs=32,
        )


def test_generated_token_has_urlsafe_32_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READYAGENTS_MCP_TOKEN_UNSET", raising=False)
    token = resolve_bearer_token(
        auth_mode="token",
        token_env="READYAGENTS_MCP_TOKEN_UNSET",
        bind_host="127.0.0.1",
    )
    assert token is not None
    assert len(token) >= len(secrets.token_urlsafe(32))


def test_auth_none_only_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READYAGENTS_MCP_TOKEN", raising=False)
    assert (
        resolve_bearer_token(
            auth_mode="none", token_env="READYAGENTS_MCP_TOKEN", bind_host="127.0.0.1"
        )
        is None
    )
    assert (
        resolve_bearer_token(
            auth_mode="none", token_env="READYAGENTS_MCP_TOKEN", bind_host="localhost"
        )
        is None
    )
    with pytest.raises(MCPError):
        resolve_bearer_token(
            auth_mode="none", token_env="READYAGENTS_MCP_TOKEN", bind_host="0.0.0.0"
        )
    with pytest.raises(MCPError):
        resolve_bearer_token(
            auth_mode="none", token_env="READYAGENTS_MCP_TOKEN", bind_host="1.2.3.4"
        )


def test_mcp_serve_http_flags_require_streamable_http(monkeypatch) -> None:
    called = {"n": 0}

    def fake_serve() -> None:
        called["n"] += 1

    monkeypatch.setattr("readyagents.mcp.server.serve_stdio", fake_serve)
    result = runner.invoke(app, ["mcp", "serve", "--host", "127.0.0.1"])
    assert result.exit_code != 0
    text = result.stdout + result.stderr
    assert "HTTP" in text
    assert called["n"] == 0


def test_mcp_serve_streamable_http_invokes(monkeypatch) -> None:
    called: dict[str, Any] = {}

    def fake_serve(**kwargs: Any) -> None:
        called.update(kwargs)

    monkeypatch.setattr("readyagents.mcp.http.serve_streamable_http", fake_serve)
    result = runner.invoke(app, ["mcp", "serve", "--transport", "streamable-http"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert called.get("host") == "127.0.0.1"
    assert called.get("port") == 8765
    assert called.get("auth_mode") == "token"


def test_mcp_serve_invalid_transport() -> None:
    result = runner.invoke(app, ["mcp", "serve", "--transport", "sse"])
    assert result.exit_code != 0
    assert "transport" in (result.stdout + result.stderr).lower()
