from __future__ import annotations

import json

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import MCPError

runner = CliRunner()


def test_probe_cli_success(monkeypatch) -> None:
    def fake_probe(url: str, *, timeout: float = 10.0) -> dict:
        assert "example" in url or url
        return {
            "url": url if url.startswith("http") else "http://127.0.0.1:8765/mcp",
            "negotiated": "server/discover",
            "protocol_versions": ["2026-07-28", "2025-11-25", "2025-06-18"],
            "extensions": ["io.modelcontextprotocol/tasks"],
            "server_info": {"name": "readyagents", "version": "0.10.0"},
        }

    monkeypatch.setattr("readyagents.mcp.probe.probe_server", fake_probe)
    result = runner.invoke(app, ["mcp", "probe", "http://127.0.0.1:8765/mcp", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["command"] == "mcp probe"
    assert "2026-07-28" in data["protocol_versions"]
    assert "io.modelcontextprotocol/tasks" in data["extensions"]


def test_probe_cli_failure(monkeypatch) -> None:
    def boom(url: str, *, timeout: float = 10.0) -> dict:
        raise MCPError("mcp probe failed: connection refused")

    monkeypatch.setattr("readyagents.mcp.probe.probe_server", boom)
    result = runner.invoke(app, ["mcp", "probe", "http://127.0.0.1:1/mcp", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["error"] == "MCPError"


def test_probe_url_normalization() -> None:
    from readyagents.mcp.probe import _mcp_url

    assert _mcp_url("http://127.0.0.1:8765").endswith("/mcp")
    assert _mcp_url("http://127.0.0.1:8765/mcp").endswith("/mcp")
