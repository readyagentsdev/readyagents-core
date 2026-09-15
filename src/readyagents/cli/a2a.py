"""CLI group: a2a (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import ReadyAgentsError

a2a_app = typer.Typer(
    help="Serve a workflow as an A2A agent, print its card, or probe a remote card.",
    no_args_is_help=True,
)


@a2a_app.command("serve")
def a2a_serve_cmd(
    path: Path = _WORKFLOW_ARG,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Loopback by default.",
        envvar="READYAGENTS_A2A_HOST",
    ),
    port: int = typer.Option(
        8770,
        "--port",
        min=1,
        max=65535,
        help="Bind port.",
        envvar="READYAGENTS_A2A_PORT",
    ),
    allow_public_bind: bool = typer.Option(
        False,
        "--allow-public-bind",
        help="Allow a non-loopback bind. Prints a warning. You own the exposure.",
    ),
    token_env: str = typer.Option(
        "READYAGENTS_A2A_TOKEN",
        "--token-env",
        help="Env var holding the bearer token. There is no token-value CLI flag.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print bind + card JSON, then serve."),
) -> None:
    """Expose one workflow as an A2A agent (loopback). Foreground; stops when it stops."""
    from readyagents.a2a.card import load_workflow_card
    from readyagents.a2a.server import serve_a2a

    try:
        card = load_workflow_card(path, url=f"http://{host}:{int(port)}")
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a serve",
                    ok=True,
                    host=host,
                    port=int(port),
                    url=card.get("url"),
                    card=card,
                )
            )
        serve_a2a(
            path,
            host=host,
            port=port,
            allow_public_bind=allow_public_bind,
            token_env=token_env,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a serve",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


@a2a_app.command("card")
def a2a_card_cmd(
    path: Path = _WORKFLOW_ARG,
    url: str = typer.Option(
        "http://127.0.0.1:8770",
        "--url",
        help="Canonical agent URL written into the card.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the card JSON to FILE."),
    as_json: bool = typer.Option(False, "--json", help="Print the card as a JSON envelope."),
) -> None:
    """Generate a deterministic Agent Card from a workflow. No network."""
    from readyagents.a2a.card import load_workflow_card

    try:
        card = load_workflow_card(path, url=url)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a card",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if out is not None:
        out.write_text(
            json.dumps(card, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if as_json:
        _print_json(_json_envelope("a2a card", ok=True, card=card))
        return
    if out is not None:
        console.print(f"wrote {out}")
        return
    _print_json(card)


@a2a_app.command("probe")
def a2a_probe_cmd(
    url: str = typer.Argument(..., help="Remote A2A agent origin or card URL."),
    as_json: bool = typer.Option(False, "--json", help="Print the probe envelope as JSON."),
) -> None:
    """Fetch and validate a remote Agent Card. Read-only. Never prints secret values."""
    import os

    from readyagents.a2a.card import card_digest, card_signature_status
    from readyagents.a2a.client import fetch_agent_card

    token = (os.environ.get("READYAGENTS_A2A_TOKEN") or "").strip() or None
    card_secret = (os.environ.get("READYAGENTS_A2A_CARD_SECRET") or "").strip() or None
    try:
        card = fetch_agent_card(url, token=token)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a probe",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    url=url,
                )
            )
        else:
            err_console.print(f"[red]{type(extra).__name__}[/red]: {extra}")
        raise typer.Exit(code=1) from extra
    schemes = card.get("securitySchemes")
    redacted_schemes: dict[str, Any] = {}
    if isinstance(schemes, dict):
        for name, spec in schemes.items():
            if isinstance(spec, dict):
                redacted_schemes[str(name)] = {
                    "type": spec.get("type"),
                    "scheme": spec.get("scheme"),
                }
            else:
                redacted_schemes[str(name)] = {"type": None}
    extra = card.get("readyagents") if isinstance(card.get("readyagents"), dict) else {}
    caps = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
    report = {
        "name": card.get("name"),
        "url": card.get("url"),
        "version": card.get("version"),
        "protocolVersion": card.get("protocolVersion"),
        "capabilities": caps,
        "securitySchemes": redacted_schemes,
        "digest": card_digest(card),
        "signatureStatus": card_signature_status(card, secret=card_secret),
        "approvalCapable": extra.get("approvalCapable") if isinstance(extra, dict) else None,
        "streaming": bool(caps.get("streaming")) if isinstance(caps, dict) else False,
    }
    if as_json:
        _print_json(_json_envelope("a2a probe", ok=True, **report))
        return
    console.print(f"name: {report['name']}")
    console.print(f"url: {report['url']}")
    console.print(f"protocolVersion: {report['protocolVersion']}")
    console.print(f"approvalCapable: {report['approvalCapable']}")
    console.print(f"streaming: {report['streaming']}")
    console.print(f"digest: {report['digest']}")
    console.print(f"signatureStatus: {report['signatureStatus']}")
