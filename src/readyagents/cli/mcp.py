"""CLI group: mcp (split from cli.py; see H-05)."""

from __future__ import annotations

import sys

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.config import DEFAULT_MCP_TOKEN_ENV
from readyagents.errors import (
    MCPError,
    ReadyAgentsError,
)

mcp_app = typer.Typer(help="Run ReadyAgents as an MCP server.", no_args_is_help=True)


@mcp_app.command("serve")
def mcp_serve(
    ctx: typer.Context,
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="stdio (default) or streamable-http.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. HTTP only. v0.9 rejects non-loopback binds.",
        envvar="READYAGENTS_MCP_HTTP_HOST",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        min=1,
        max=65535,
        help="Bind port. HTTP only.",
        envvar="READYAGENTS_MCP_HTTP_PORT",
    ),
    auth: str = typer.Option(
        "token",
        "--auth",
        help="token (default) or none. none is loopback-only and warns.",
    ),
    token_env: str = typer.Option(
        DEFAULT_MCP_TOKEN_ENV,
        "--token-env",
        help="Env var holding the bearer token. HTTP only.",
    ),
    max_concurrent_runs: int = typer.Option(
        4,
        "--max-concurrent-runs",
        help="In-process executor cap. HTTP only.",
        envvar="READYAGENTS_MCP_MAX_CONCURRENT_RUNS",
        min=1,
    ),
    max_pending_runs: int = typer.Option(
        32,
        "--max-pending-runs",
        help="Pending-run queue cap. HTTP only.",
        envvar="READYAGENTS_MCP_MAX_PENDING_RUNS",
        min=1,
    ),
    approval_ui: bool = typer.Option(
        False,
        "--approval-ui",
        help="Mount the localhost approval UI on this HTTP process. HTTP only.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print protocol versions, extensions, and SDK pin as JSON, then serve.",
    ),
) -> None:
    """Expose builtin tools (and run_workflow) over MCP stdio or Streamable HTTP."""
    mode = (transport or "stdio").strip().lower()
    http_flags = (
        "--host",
        "--port",
        "--auth",
        "--token-env",
        "--max-concurrent-runs",
        "--max-pending-runs",
        "--approval-ui",
    )
    http_params = (
        "host",
        "port",
        "auth",
        "token_env",
        "max_concurrent_runs",
        "max_pending_runs",
        "approval_ui",
    )
    if mode == "stdio":
        from_argv = any(
            arg == flag or arg.startswith(f"{flag}=") for arg in sys.argv for flag in http_flags
        )
        from_cli = any(
            getattr(ctx.get_parameter_source(name), "name", None) == "COMMANDLINE"
            for name in http_params
        )
        passed = from_argv or from_cli
        if passed:
            _fail(
                MCPError(
                    "HTTP flags (--host, --port, --auth, --token-env, "
                    "--max-concurrent-runs, --max-pending-runs, --approval-ui) "
                    "are only valid with --transport streamable-http"
                )
            )
        if as_json:
            from readyagents.mcp.protocol import serve_json_envelope

            _print_json(serve_json_envelope(transport="stdio"))
        try:
            from readyagents.mcp.server import serve_stdio

            serve_stdio()
        except ReadyAgentsError as exc:
            _fail(exc)
        return
    if mode != "streamable-http":
        _fail(MCPError(f"Unknown --transport '{transport}'. Use stdio or streamable-http."))
    if as_json:
        from readyagents.mcp.protocol import serve_json_envelope

        _print_json(serve_json_envelope(transport="http"))
    try:
        from readyagents.mcp.http import serve_streamable_http

        serve_streamable_http(
            host=host,
            port=port,
            auth_mode=auth,
            token_env=token_env,
            max_concurrent_runs=max_concurrent_runs,
            max_pending_runs=max_pending_runs,
            approval_ui=approval_ui,
        )
    except ReadyAgentsError as err:
        _fail(err)


@mcp_app.command("probe")
def mcp_probe(
    url: str = typer.Argument(..., help="Remote MCP HTTP origin or /mcp URL."),
    as_json: bool = typer.Option(False, "--json", help="Print the probe envelope as JSON."),
) -> None:
    """Read-only diagnostic: call server/discover (then initialize). Never calls a tool."""
    from readyagents.mcp.probe import probe_server

    try:
        report = probe_server(url)
    except ReadyAgentsError as err:
        if as_json:
            _print_json(
                _json_envelope(
                    "mcp probe",
                    ok=False,
                    error=type(err).__name__,
                    message=str(err),
                    url=url,
                )
            )
        else:
            err_console.print(f"[red]{type(err).__name__}[/red]: {err}")
        raise typer.Exit(code=1) from err
    if as_json:
        _print_json(_json_envelope("mcp probe", ok=True, **report))
        return
    versions = ", ".join(report.get("protocol_versions") or []) or "(none)"
    extensions = ", ".join(report.get("extensions") or []) or "(none)"
    console.print(f"url: {report.get('url')}")
    console.print(f"protocol_versions: {versions}")
    console.print(f"extensions: {extensions}")
    console.print(f"negotiated: {report.get('negotiated')}")
