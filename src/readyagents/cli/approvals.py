"""CLI group: approvals (split from cli.py; see H-05)."""

from __future__ import annotations

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

approvals_app = typer.Typer(
    help="Queue of paused gates.",
    no_args_is_help=True,
)


@approvals_app.command("serve")
def approvals_serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. v0.9 rejects non-loopback binds.",
    ),
    port: int = typer.Option(
        8766,
        "--port",
        min=1,
        max=65535,
        help="Bind port (default 8766).",
    ),
    token_env: str = typer.Option(
        "READYAGENTS_APPROVAL_UI_SECRET",
        "--token-env",
        help="Env var holding the UI HMAC secret. Never pass the secret as a flag.",
    ),
    session_ttl: int = typer.Option(
        1800,
        "--session-ttl",
        min=30,
        max=86400,
        help="Session cookie TTL in seconds (default 1800).",
    ),
    action_ttl: int = typer.Option(
        300,
        "--action-ttl",
        min=10,
        max=3600,
        help="One-use action token TTL in seconds (default 300).",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        envvar="READYAGENTS_ACTOR",
        help="Actor id for RBAC checks.",
    ),
    no_open: bool = typer.Option(
        True,
        "--no-open",
        help="Do not launch a browser (default). Print the bootstrap URL on stderr.",
    ),
) -> None:
    """Foreground localhost approval page. Stops when this process stops."""
    try:
        from readyagents.approvals.server import serve_approvals

        serve_approvals(
            host=host,
            port=port,
            token_env=token_env,
            session_ttl=float(session_ttl),
            action_ttl=float(action_ttl),
            actor=actor,
            no_open=no_open,
        )
    except ReadyAgentsError as exc:
        _fail(exc)


@approvals_app.command("list")
def approvals_list_cmd(
    role: str | None = typer.Option(None, "--role", help="Filter to this approver role."),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    expiring_within: str | None = typer.Option(
        None, "--expiring-within", help="Duration like 1h; omit to list all pending."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List paused approval gates this caller may see. Unauthorized looks empty."""
    from readyagents.approvals.queue import list_approvals
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.run_store.base import RunQuery

    try:
        settings = get_settings()
        store = open_run_store(settings)
        try:
            found = [item.state for item in store.list(RunQuery(status="paused", limit=256))]
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        rows = list_approvals(
            found,
            actor=actor,
            role=role,
            expiring_within=expiring_within,
            home=settings.home_path(),
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "approvals list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("approvals list", ok=True, approvals=rows))
        return
    if not rows:
        console.print("no pending approvals")
        return
    for row in rows:
        console.print(
            f"{row['run_id']} node={row['node_id']} "
            f"votes={row['approvals_received']}/{row['approvals_required']} "
            f"expires={row['expires_at'] or '-'} status={row['status']}"
        )
