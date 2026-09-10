"""Connect to MCP servers declared in a workflow (optional extra)."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from readyagents.errors import MCPError, missing_extra_message
from readyagents.tools import FunctionTool, Tool
from readyagents.workflow.schema import MCPServerSpec

# Stdio children get a narrow env. API keys are not inherited unless the
# workflow sets them under mcp_servers.<name>.env.
_PASSTHROUGH_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
    }
)


def mcp_child_env(spec: MCPServerSpec) -> dict[str, str]:
    """Env mapping passed to an MCP stdio subprocess."""
    env = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV}
    if spec.env:
        env.update(spec.env)
    return env


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


def _resolve_mcp_cwd(spec: MCPServerSpec, workspace: Path) -> Path:
    """Use the run workspace, or confine an explicit cwd under it, before spawn."""
    from readyagents.paths import resolve_within

    raw = (spec.cwd or "").strip()
    if not raw:
        return resolve_within(".", workspace, what="MCP cwd")
    return resolve_within(raw, workspace, what="MCP cwd")


_SHELL_SHIMS = frozenset({".cmd", ".bat", ".ps1", ".command"})
_SHELL_EXES = frozenset(
    {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "command.com"}
)


def _reject_shell_stdio_command(command: str) -> None:
    """Fail closed: do not spawn cmd/bat/ps1 shims (quoting would change)."""
    name = Path(command).name.lower()
    suffix = Path(command).suffix.lower()
    if suffix in _SHELL_SHIMS or name in _SHELL_EXES:
        raise MCPError(
            "MCP stdio command cannot be a Windows shell shim "
            f"({command!r}); use a real executable without shell interpretation"
        )


def _schema_from_mcp_tool(item: Any) -> dict[str, Any]:
    raw: Any = None
    if isinstance(item, dict):
        raw = item.get("inputSchema", item.get("input_schema"))
    else:
        raw = getattr(item, "inputSchema", None)
        if raw is None:
            raw = getattr(item, "input_schema", None)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if not content:
        return str(result)
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
        else:
            text = getattr(block, "text", None)
        if text is not None:
            texts.append(str(text))
    return "\n".join(texts) if texts else str(result)


class _AsyncLoop:
    """Background asyncio loop so MCP stdio sessions outlive a single coroutine."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="readyagents-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise MCPError("MCP event loop failed to start")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def run(self, coro: Any, *, timeout: float = 60) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()
            raise MCPError("MCP operation timed out") from None

    def stop(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


class MCPClient:
    """Long-lived stdio MCP sessions, one child per ``mcp_servers`` name."""

    def __init__(self, servers: dict[str, MCPServerSpec], workspace: Path) -> None:
        self._servers = dict(servers)
        self._workspace = Path(workspace)
        # Confine every declared cwd before any subprocess is spawned.
        self._cwds = {
            name: _resolve_mcp_cwd(spec, self._workspace) for name, spec in self._servers.items()
        }
        self._tools: dict[str, Tool] | None = None
        self._sessions: dict[str, Any] = {}
        self._stack: AsyncExitStack | None = None
        self._loop: _AsyncLoop | None = None
        self._lock = threading.Lock()

    def tools(self) -> dict[str, Tool]:
        if not self._servers:
            return {}
        with self._lock:
            if self._tools is None:
                self._tools = self._connect_locked()
            return self._tools

    def close(self) -> None:
        with self._lock:
            self._shutdown_locked()

    def _connect_locked(self) -> dict[str, Tool]:
        if not mcp_available():
            names = ", ".join(self._servers)
            raise MCPError(
                f"Workflow declares MCP servers ({names}) but {missing_extra_message('MCP', 'mcp')}"
            )
        self._loop = _AsyncLoop()
        try:
            return self._loop.run(self._connect())
        except BaseException:
            self._shutdown_locked()
            raise

    def _shutdown_locked(self) -> None:
        loop = self._loop
        if loop is not None:
            try:
                if self._stack is not None:
                    loop.run(self._stack.aclose(), timeout=15)
            except Exception:  # noqa: BLE001
                pass
            loop.stop()
        self._loop = None
        self._stack = None
        self._sessions.clear()
        self._tools = None

    async def _connect(self) -> dict[str, Tool]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        tools: dict[str, Tool] = {}
        try:
            for name, spec in self._servers.items():
                _reject_shell_stdio_command(spec.command)
                params = StdioServerParameters(
                    command=spec.command,
                    args=spec.args,
                    env=mcp_child_env(spec),
                    cwd=str(self._cwds[name]),
                )
                try:
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await _negotiate_session(session)
                    listed = await session.list_tools()
                except MCPError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise MCPError(f"Failed to connect to MCP server '{name}': {exc}") from exc
                self._sessions[name] = session
                for item in listed.tools:
                    tool_name = item.name
                    qualified = f"{name}.{tool_name}"
                    desc = item.description or ""
                    tools[qualified] = FunctionTool(
                        name=qualified,
                        description=desc or f"MCP tool {tool_name} from {name}",
                        handler=self._handler(name, tool_name),
                        schema=_schema_from_mcp_tool(item),
                    )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return tools

    def _handler(self, server: str, tool_name: str) -> Any:
        def handler(**kwargs: Any) -> Any:
            return self._call(server, tool_name, kwargs)

        return handler

    def _call(self, server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            loop = self._loop
            session = self._sessions.get(server)
        if loop is None or session is None:
            raise MCPError(f"MCP server '{server}' is not connected")
        try:
            return loop.run(self._call_on(session, tool_name, arguments))
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"MCP tool '{tool_name}' failed: {exc}") from exc

    async def _call_on(self, session: Any, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            result = await session.call_tool(tool_name, arguments)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"MCP tool '{tool_name}' failed: {exc}") from exc
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise MCPError(f"MCP tool '{tool_name}' returned an error: {result}")
        result_type = getattr(result, "result_type", None) or getattr(result, "resultType", None)
        if result_type is None and isinstance(result, dict):
            result_type = result.get("resultType")
        if result_type == "input_required":
            raise MCPError(
                f"MCP tool '{tool_name}' returned input_required; "
                "this client does not retry with inputResponses"
            )
        return _result_text(result)


async def _negotiate_session(session: Any) -> None:
    """Prefer ``server/discover``; fall back to legacy ``initialize``."""
    discover = getattr(session, "discover", None)
    if callable(discover):
        try:
            await discover()
            return
        except Exception:  # noqa: BLE001
            pass
    initialize = getattr(session, "initialize", None)
    if callable(initialize):
        await initialize()
        return
    raise MCPError("MCP session provides neither server/discover nor initialize")
