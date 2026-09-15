"""CLI group: triggers (split from cli.py; see H-05)."""

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

triggers_app = typer.Typer(
    help="Inspect declared triggers, dry-run mappings, list events. Core starts no listener.",
    no_args_is_help=True,
)


@triggers_app.command("list")
def triggers_list(
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List declared triggers. Core starts no listener."""
    from readyagents.triggers.inspect import list_triggers
    from readyagents.workflow.runner import load_workflow

    try:
        workflow = load_workflow(path)
        rows = list_triggers(workflow)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "triggers list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("triggers list", ok=True, triggers=rows))
        return
    if not rows:
        console.print("No triggers declared.")
        return
    for row in rows:
        console.print(
            f"name: {row['name']}  kind: {row['kind']}  "
            f"signature: {row['require_signature']}  concurrency: {row['concurrency']}"
        )


@triggers_app.command("show")
def triggers_show(
    name: str = typer.Argument(..., help="Trigger name."),
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one declared trigger contract."""
    from readyagents.triggers.inspect import show_trigger
    from readyagents.workflow.runner import load_workflow

    try:
        workflow = load_workflow(path)
        row = show_trigger(workflow, name)
    except KeyError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "triggers show", ok=False, error="unknown_trigger", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]unknown trigger[/red] {name}")
        raise typer.Exit(code=1) from extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "triggers show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("triggers show", ok=True, trigger=row))
        return
    console.print(json.dumps(row, indent=2, ensure_ascii=False))


@triggers_app.command("test")
def triggers_test(
    name: str = typer.Argument(..., help="Trigger name."),
    path: Path = _WORKFLOW_ARG,
    payload: Path | None = typer.Option(None, "--payload", help="JSON payload file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Dry-run an event against a trigger mapping. Does not start a run."""
    from readyagents.config import get_settings
    from readyagents.triggers.inspect import test_trigger
    from readyagents.workflow.runner import load_workflow

    body: dict[str, Any] = {}
    if payload is not None:
        loaded = json.loads(payload.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise typer.Exit(code=1)
        body = loaded
    try:
        workflow = load_workflow(path)
        report = test_trigger(workflow, name, body, home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "triggers test", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("triggers test", ok=True, **report))
        return
    console.print(f"action={report.get('action')} reason={report.get('reason')}")
    console.print(f"inputs={report.get('inputs')}")


@triggers_app.command("events")
def triggers_events(
    as_json: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List recent trigger outcomes and dead letters."""
    from readyagents.config import get_settings
    from readyagents.triggers.inspect import list_dead_letters, list_events

    settings = get_settings()
    rows = list_events(settings.home_path(), limit=limit)
    letters = list_dead_letters(settings.home_path())
    if as_json:
        _print_json(_json_envelope("triggers events", ok=True, events=rows, dead_letters=letters))
        return
    console.print(f"events: {len(rows)}  dead_letters: {len(letters)}")
    for row in rows:
        console.print(
            f"  {row.get('action')} trigger={row.get('trigger')} reason={row.get('reason')}"
        )
