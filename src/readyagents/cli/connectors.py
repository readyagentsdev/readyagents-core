"""CLI group: connectors (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from readyagents.cli._common import (
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import ReadyAgentsError

connectors_app = typer.Typer(
    help="List, show, and test installed connectors (small catalog by design).",
    no_args_is_help=True,
)


@connectors_app.command("list")
def connectors_list_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """List installed connectors (schemas, auth names, destinations). No secret values."""
    from readyagents.connectors.catalog import list_payload, redact_catalog

    try:
        payload = redact_catalog(list_payload())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors list", ok=True, **payload))
        return
    table = Table(title="Connectors")
    table.add_column("Name")
    table.add_column("Side effects")
    table.add_column("Auth")
    table.add_column("Destinations")
    for row in payload["connectors"]:
        table.add_row(
            str(row["name"]),
            str(row["side_effects"]),
            str(row.get("auth") or "none"),
            ", ".join(row.get("destinations") or ()) or "—",
        )
    console.print(table)


@connectors_app.command("show")
def connectors_show_cmd(
    name: str = typer.Argument(..., help="Connector name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one connector's declaration. Secret names only, never values."""
    from readyagents.connectors.catalog import redact_catalog, show_payload

    try:
        payload = redact_catalog(show_payload(name))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors show", ok=True, **payload))
        return
    console.print(f"[bold]{payload['name']}[/bold] {payload.get('version')}")
    console.print(payload.get("description") or "")
    console.print(f"destinations: {', '.join(payload.get('destinations') or ())}")
    auth = payload.get("auth")
    kind = auth.get("kind") if isinstance(auth, dict) else auth
    console.print(f"auth: {kind}")
    console.print(f"idempotent: {payload.get('idempotent')} key={payload.get('idempotency_key')}")
    console.print(
        f"side_effects: {payload.get('side_effects')} determinism: {payload.get('determinism')}"
    )


@connectors_app.command("test")
def connectors_test_cmd(
    name: str = typer.Argument(..., help="Connector name."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Offline fixture directory."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the conformance harness offline. Exit 0 if the connector passes."""
    from readyagents.config import get_settings
    from readyagents.connectors.catalog import test_payload

    try:
        payload = test_payload(name, fixtures=fixtures, workspace=get_settings().workspace_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors test", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors test", ok=payload["ok"], **payload))
    else:
        if payload["ok"]:
            console.print(f"[green]{name} passed conformance[/green]")
        else:
            err_console.print(f"[red]{name} failed conformance[/red]")
            for row in payload["failures"]:
                err_console.print(f"  {row['check']}: {row['message']}")
    if not payload["ok"]:
        raise typer.Exit(code=1)
