"""CLI group: serve (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _json_envelope,
    _print_json,
)

serve_app = typer.Typer(
    help="Foreground loopback surfaces. Not a hosted product.",
    no_args_is_help=True,
)


@serve_app.command("chat")
def serve_chat_cmd(
    path: Path = _WORKFLOW_ARG,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host. Loopback by default."),
    port: int = typer.Option(8795, "--port", min=1, max=65535),
    allow_public_bind: bool = typer.Option(
        False,
        "--allow-public-bind",
        help="Allow a non-loopback bind. Prints a warning. You own the exposure.",
    ),
    token_env: str = typer.Option(
        "READYAGENTS_CHAT_TOKEN",
        "--token-env",
        help="Env var holding the bearer token. There is no token-value CLI flag.",
    ),
    widget: bool = typer.Option(False, "--widget", help="Serve bundled vanilla widget assets."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Foreground loopback chat endpoint. Not a hosted product, no widget CDN."""
    from readyagents.sessions.chat import PUBLIC_WARN, serve_chat

    if as_json:
        _print_json(
            _json_envelope(
                "serve chat",
                ok=True,
                host=host,
                port=int(port),
                warning=PUBLIC_WARN if allow_public_bind else None,
            )
        )
    serve_chat(
        path,
        host=host,
        port=int(port),
        token_env=token_env,
        allow_public_bind=allow_public_bind,
        widget=widget,
    )
