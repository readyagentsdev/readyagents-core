from readyagents.mcp.builtin import builtin_tools

__all__ = [
    "builtin_tools",
    "MCPClient",
    "construct_server",
    "mcp_available",
    "streamable_http_app",
    "compose_http_app",
    "serve_streamable_http",
    "resolve_bearer_token",
    "assert_loopback_host",
    "RunCoordinator",
]


def __getattr__(name: str):
    if name == "MCPClient":
        from readyagents.mcp.client import MCPClient

        return MCPClient
    if name in {"construct_server", "mcp_available", "streamable_http_app"}:
        from readyagents.mcp.server import construct_server, mcp_available, streamable_http_app

        return {
            "construct_server": construct_server,
            "mcp_available": mcp_available,
            "streamable_http_app": streamable_http_app,
        }[name]
    if name in {
        "compose_http_app",
        "serve_streamable_http",
        "resolve_bearer_token",
        "assert_loopback_host",
    }:
        from readyagents.mcp import http as mcp_http

        return getattr(mcp_http, name)
    if name == "RunCoordinator":
        from readyagents.mcp.run_api import RunCoordinator

        return RunCoordinator
    raise AttributeError(name)
