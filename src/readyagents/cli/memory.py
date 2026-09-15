"""CLI group: memory (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import (
    ConfigError,
    ReadyAgentsError,
)

memory_app = typer.Typer(
    help="Inspect, search, forget, and export the local memory store.",
    no_args_is_help=True,
)


@memory_app.command("list")
def memory_list_cmd(
    scope: str | None = typer.Option(None, "--scope", help="Limit to one explicit scope."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List local memory records. Offline. No secret values beyond stored text."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        records = store.list(scope=scope)
        rows = [rec.as_dict() for rec in records]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("memory list", ok=True, records=rows, count=len(rows)))
        return
    if not rows:
        console.print("(no memory records)")
        return
    table = Table(title="Memory")
    table.add_column("id")
    table.add_column("scope")
    table.add_column("created")
    for row in rows:
        table.add_row(str(row["id"])[:12], str(row["scope"]), str(row.get("created_at") or ""))
    console.print(table)


@memory_app.command("show")
def memory_show_cmd(
    record_id: str = typer.Argument(..., help="Memory record id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one local memory record."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        rec = store.get(record_id).as_dict()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("memory show", ok=True, record=rec))
        return
    _print_json(rec)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(..., help="Keyword query."),
    scope: str = typer.Option(..., "--scope", help="Explicit scope to search."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """BM25 search one local scope. Offline."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope

    settings = get_settings()
    try:
        validate_scope(scope)
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        hits = store.search(scope, query)
        rows = [{"score": hit.score, **hit.record.as_dict()} for hit in hits]
        store.close()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory search", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory search", ok=True, hits=rows, count=len(rows)))
        return
    if not rows:
        console.print("(no hits)")
        return
    for row in rows:
        console.print(f"{row['id'][:12]}  {row['scope']}  {row.get('text', '')[:80]}")


@memory_app.command("forget")
def memory_forget_cmd(
    scope: str | None = typer.Option(None, "--scope", help="Scope to erase."),
    subject: str | None = typer.Option(None, "--subject", help="Subject token to sweep."),
    record_id: str | None = typer.Option(None, "--id", help="Single record id."),
    yes: bool = typer.Option(False, "--yes", help="Required. Forgetting is irreversible."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Erase records, index entries, and vectors. Audited. Requires --yes."""
    from readyagents.audit import make_auditor
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import parse_scope, validate_scope

    if not yes:
        extra = ConfigError("memory forget requires --yes")
        if as_json:
            _print_json(
                _json_envelope("memory forget", ok=False, error="ConfigError", message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if not scope and not subject and not record_id:
        extra = ConfigError("memory forget requires --scope, --subject, or --id")
        if as_json:
            _print_json(
                _json_envelope("memory forget", ok=False, error="ConfigError", message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    settings = get_settings()
    try:
        if scope:
            validate_scope(scope)
        if subject:
            parse_scope(f"subject:{subject}")
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        removed = store.forget(scope=scope, record_id=record_id, subject=subject)
        store.close()
        auditor = make_auditor(settings.audit_dir())
        auditor(
            "memory_forget",
            scope=scope,
            subject=subject,
            record_id=record_id,
            removed=removed,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory forget", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory forget", ok=True, removed=removed))
        return
    console.print(f"removed {removed}")


@memory_app.command("export")
def memory_export_cmd(
    out: Path = typer.Option(..., "--out", help="Workspace-relative export path."),
    scope: str | None = typer.Option(None, "--scope", help="Limit to one scope."),
    yes: bool = typer.Option(False, "--yes", help="Confirm writing a sensitive file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write records as JSON. Confined, warned, audited. Vectors omitted."""
    from readyagents.audit import make_auditor
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope
    from readyagents.workflow.runner import confine_under

    settings = get_settings()
    try:
        if scope:
            validate_scope(scope)
        dest = confine_under(out, settings.workspace_path(), what="memory export")
        if dest.exists() and not yes:
            raise ConfigError("memory export refuses to overwrite without --yes")
        if not yes:
            err_console.print(
                f"Warning: writing a sensitive memory export to {dest}. Pass --yes to confirm."
            )
            raise ConfigError("memory export requires --yes")
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        records = [rec.as_dict() for rec in store.list(scope=scope)]
        store.close()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps({"records": records}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        auditor = make_auditor(settings.audit_dir())
        auditor("memory_export", path=str(dest), scope=scope, count=len(records))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory export", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory export", ok=True, path=str(dest), count=len(records)))
        return
    console.print(f"wrote {dest} ({len(records)} records)")
