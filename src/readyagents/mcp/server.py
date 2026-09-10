"""Expose ReadyAgents builtin tools (and run-workflow) as an MCP server."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from readyagents.config import MAX_HTTP_BODY_BYTES, get_settings
from readyagents.errors import MCPError, missing_extra_message
from readyagents.mcp.builtin import builtin_tools
from readyagents.mcp.protocol import (
    LIST_CACHE_SCOPE,
    LIST_CACHE_TTL_MS,
    TASKS_EXTENSION,
    discover_result,
    honoured_protocol_versions,
    package_version,
)


def mcp_available() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


def _create_server(name: str, **kwargs: Any) -> Any:
    try:
        from mcp.server import MCPServer

        return _instantiate_server(MCPServer, name, kwargs)
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return _instantiate_server(FastMCP, name, kwargs)
    except ImportError as exc:
        raise MCPError(
            "This version of the mcp package does not provide MCPServer or FastMCP."
        ) from exc


def _instantiate_server(cls: Any, name: str, kwargs: dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(cls.__init__)
        params = signature.parameters
    except (TypeError, ValueError):
        return cls(name)
    accepted = {key: value for key, value in kwargs.items() if key in params}
    try:
        return cls(name, **accepted) if accepted else cls(name)
    except TypeError:
        return cls(name)


def _cache_hints() -> Any:
    try:
        from mcp.server.caching import CacheHint
    except ImportError:
        return None
    hint = CacheHint(ttl_ms=LIST_CACHE_TTL_MS, scope=LIST_CACHE_SCOPE)
    return {
        "tools/list": hint,
        "server/discover": hint,
        "resources/list": hint,
        "resources/read": hint,
        "prompts/list": hint,
        "resources/templates/list": hint,
    }


def construct_server(*, allow_http: bool | None = None, workspace: Path | None = None) -> Any:
    """Build an MCP server that exposes builtin tools. Does not start a transport."""
    if not mcp_available():
        raise MCPError(missing_extra_message("MCP", "mcp"))

    settings = get_settings()
    allow = settings.allow_http if allow_http is None else allow_http
    root = workspace or settings.workspace_path()
    tools = {t.name: t for t in builtin_tools(allow_http=allow, workspace=root)}
    extra: dict[str, Any] = {
        "instructions": "ReadyAgents local one-shot workflow engine plus MCP toolkit.",
        "version": package_version(),
    }
    hints = _cache_hints()
    if hints is not None:
        extra["cache_hints"] = hints
    server = _create_server("readyagents", **extra)
    _register_server_tools(server, tools, workspace=Path(root))
    _advertise_tasks_extension(server)
    return server


def _advertise_tasks_extension(server: Any) -> None:
    honoured = honoured_protocol_versions()
    if "2026-07-28" not in honoured:
        return
    low = getattr(server, "_lowlevel_server", server)
    extensions = getattr(low, "extensions", None)
    if isinstance(extensions, dict):
        extensions[TASKS_EXTENSION] = {}
    add = getattr(low, "add_request_handler", None)
    if not callable(add):
        return
    try:
        import mcp_types as types
    except ImportError:
        return

    async def on_discover(ctx: Any, params: Any) -> Any:
        payload = discover_result(honoured)
        try:
            return types.DiscoverResult.model_validate(payload)
        except Exception:  # noqa: BLE001
            return payload

    try:
        add("server/discover", types.RequestParams, on_discover)
    except Exception:  # noqa: BLE001
        return


def serve_stdio(*, allow_http: bool | None = None, workspace: Path | None = None) -> None:
    """Run a stdio MCP server exposing builtin tools."""
    construct_server(allow_http=allow_http, workspace=workspace).run(transport="stdio")


def streamable_http_app(
    server: Any,
    *,
    host: str,
    port: int,
    max_body_bytes: int = MAX_HTTP_BODY_BYTES,
) -> Any:
    """Return the SDK Streamable HTTP Starlette app mounted at /mcp. Feature-detect."""
    method = getattr(server, "streamable_http_app", None)
    if method is None:
        raise MCPError(
            "This mcp package does not support Streamable HTTP. "
            "Upgrade with: pip install 'mcp>=2' or pip install 'readyagentsdev[mcp]'"
        )
    kwargs: dict[str, Any] = {
        "streamable_http_path": "/mcp",
        "host": host,
        "max_request_body_size": max_body_bytes,
    }
    security = _transport_security_settings(host, port)
    if security is not None:
        kwargs["transport_security"] = security
    # Keep SDK default response mode so Accept can negotiate JSON vs SSE.
    kwargs.pop("json_response", None)
    if not callable(method):
        return method
    return _call_supported_kwargs(method, kwargs)


def _transport_security_settings(host: str, port: int) -> Any | None:
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        return None
    try:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_dns_allowed_hosts(host, port),
            allowed_origins=_dns_allowed_origins(host, port),
        )
    except Exception:  # noqa: BLE001
        return None


def _dns_bind_names(host: str) -> list[str]:
    names: list[str] = []
    for item in (host, "127.0.0.1", "localhost", "::1"):
        if item and item not in names:
            names.append(item)
    return names


def _host_header_variants(name: str, port: int) -> list[str]:
    variants: list[str] = []

    def add(value: str) -> None:
        if value not in variants:
            variants.append(value)

    ipv6 = ":" in name and not name.startswith("[")
    bracketed = f"[{name}]" if ipv6 else name
    add(name)
    add(bracketed)
    add(f"{bracketed}:{port}")
    if not ipv6:
        add(f"{name}:{port}")
    add(f"{name}:*")
    add(f"{bracketed}:*")
    return variants


def _dns_allowed_hosts(host: str, port: int) -> list[str]:
    hosts: list[str] = []
    for name in _dns_bind_names(host):
        for variant in _host_header_variants(name, port):
            if variant not in hosts:
                hosts.append(variant)
    return hosts


def _dns_allowed_origins(host: str, port: int) -> list[str]:
    origins: list[str] = []
    for name in _dns_bind_names(host):
        ipv6 = ":" in name and not name.startswith("[")
        shown = f"[{name}]" if ipv6 else name
        for value in (f"http://{shown}:{port}", f"http://{shown}:*"):
            if value not in origins:
                origins.append(value)
    return origins


def _call_supported_kwargs(method: Any, kwargs: dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(method)
        params = signature.parameters
    except (TypeError, ValueError):
        return method()
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        accepted = dict(kwargs)
    else:
        accepted = {key: value for key, value in kwargs.items() if key in params}
    accepted.pop("json_response", None)
    try:
        return method(**accepted) if accepted else method()
    except TypeError:
        return method()


def _register_server_tools(server: Any, tools: dict[str, Any], *, workspace: Path) -> None:
    root = Path(workspace).resolve()

    @server.tool(name="now", description=tools["now"].description)
    def now() -> str:
        return str(tools["now"].run())

    @server.tool(name="calc", description=tools["calc"].description)
    def calc(expression: str) -> str:
        return str(tools["calc"].run(expression=expression))

    @server.tool(name="json_get", description=tools["json_get"].description)
    def json_get(data: str, path: str) -> str:
        import json

        result = tools["json_get"].run(data=data, path=path)
        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    @server.tool(name="json_set", description=tools["json_set"].description)
    def json_set(data: str, path: str, value: str) -> str:
        import json

        result = tools["json_set"].run(data=data, path=path, value=value)
        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    @server.tool(name="json_merge", description=tools["json_merge"].description)
    def json_merge(data: str, path: str, value: str) -> str:
        import json

        result = tools["json_merge"].run(data=data, path=path, value=value)
        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    @server.tool(name="http_get", description=tools["http_get"].description)
    def http_get(url: str) -> str:
        return str(tools["http_get"].run(url=url))

    @server.tool(name="list_dir", description=tools["list_dir"].description)
    def list_dir(path: str = ".", include_hidden: bool = False, max_entries: int = 200) -> str:
        import json

        result = tools["list_dir"].run(
            path=path, include_hidden=include_hidden, max_entries=max_entries
        )
        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    @server.tool(name="read_file", description=tools["read_file"].description)
    def read_file(path: str) -> str:
        return str(tools["read_file"].run(path=path))

    @server.tool(name="write_file", description=tools["write_file"].description)
    def write_file(path: str, content: str) -> str:
        return str(tools["write_file"].run(path=path, content=content))

    @server.tool()
    def run_workflow(path: str, inputs_json: str = "{}") -> str:
        """Run a ReadyAgents workflow file under the server workspace."""
        import json

        from readyagents.config import get_settings
        from readyagents.errors import ConfigError
        from readyagents.workflow.runner import confine_under, run_workflow_file

        data = json.loads(inputs_json) if inputs_json else {}
        if not isinstance(data, dict):
            raise ValueError("inputs_json must be a JSON object")
        try:
            wf_path = confine_under(path, root, what="workflow")
        except ConfigError as exc:
            raise MCPError(str(exc)) from exc
        bound = get_settings().model_copy(update={"workspace": Path(root)})
        state = run_workflow_file(wf_path, inputs=data, settings=bound)
        return json.dumps(state.to_record(), ensure_ascii=False)
