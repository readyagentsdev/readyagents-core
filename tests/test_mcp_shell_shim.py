"""MCP stdio must refuse Windows shell shims and never spawn with shell=True."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from readyagents.errors import MCPError
from readyagents.mcp import client as mcp_client
from readyagents.mcp.client import MCPClient, _reject_shell_stdio_command
from readyagents.workflow.schema import MCPServerSpec


@pytest.mark.parametrize(
    "command",
    [
        "run.cmd",
        "setup.bat",
        "tool.ps1",
        "cmd.exe",
        "cmd",
        "/Windows/System32/cmd.exe",
        "powershell.exe",
        "powershell",
        "pwsh.exe",
        "pwsh",
        "helper.CMD",
        "Launch.BAT",
        "script.PS1",
        "./wrap.cmd",
        "bin/tool.bat",
    ],
)
def test_reject_shell_stdio_command(command: str) -> None:
    with pytest.raises(MCPError, match="shell shim"):
        _reject_shell_stdio_command(command)


@pytest.mark.parametrize("command", ["python", "node", "/usr/bin/env", "mcp-server-fs"])
def test_reject_shell_stdio_allows_real_executables(command: str) -> None:
    _reject_shell_stdio_command(command)


def test_mcp_client_rejects_bat_before_spawn(tmp_path: Path) -> None:
    spec = MCPServerSpec(command="evil.bat", args=["/c", "echo"])
    client = MCPClient({"shim": spec}, tmp_path)
    with pytest.raises(MCPError, match="shell shim"):
        client.tools()


def test_mcp_client_rejects_cmd_exe(tmp_path: Path) -> None:
    spec = MCPServerSpec(command="cmd.exe", args=["/c", "dir"])
    client = MCPClient({"shim": spec}, tmp_path)
    with pytest.raises(MCPError, match="shell shim"):
        client.tools()


def test_mcp_client_rejects_ps1(tmp_path: Path) -> None:
    spec = MCPServerSpec(command="tools/run.ps1", args=[])
    client = MCPClient({"shim": spec}, tmp_path)
    with pytest.raises(MCPError, match="shell shim"):
        client.tools()


def test_stdio_connect_never_uses_shell_true() -> None:
    """Guard: stdio spawn path must not introduce shell=True."""
    connect_src = inspect.getsource(MCPClient._connect)
    module_src = inspect.getsource(mcp_client)
    assert "shell=True" not in connect_src
    assert "shell=True" not in module_src
    assert "shell = True" not in module_src
    # StdioServerParameters is used without a shell flag.
    assert "StdioServerParameters" in connect_src
    assert "_reject_shell_stdio_command(spec.command)" in connect_src
