"""CLI group: knowledge (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import (
    ConfigError,
    ReadyAgentsError,
)

knowledge_app = typer.Typer(
    help="Ingest, sync, cite, and forget knowledge documents. Foreground only.",
    no_args_is_help=True,
)


@knowledge_app.command("list")
def knowledge_list_cmd(
    scope: str | None = typer.Option(None, "--scope"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List ingested documents in a scope. Offline. No embedder."""
    from readyagents.config import get_settings
    from readyagents.knowledge.ingest import _by_document
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        grouped = _by_document(store.list(scope=scope))
        rows = [
            {
                "document_id": doc,
                "versions": sorted(
                    {str((rec.metadata or {}).get("document_version") or "") for rec in recs}
                ),
                "chunks": len(recs),
                "scope": recs[0].scope if recs else scope,
            }
            for doc, recs in sorted(grouped.items())
        ]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("knowledge list", ok=True, documents=rows, count=len(rows)))
        return
    if not rows:
        console.print("(no knowledge documents)")
        return
    table = Table(title="Knowledge")
    table.add_column("document_id")
    table.add_column("chunks")
    table.add_column("scope")
    for row in rows:
        table.add_row(str(row["document_id"]), str(row["chunks"]), str(row.get("scope") or ""))
    console.print(table)


@knowledge_app.command("show")
def knowledge_show_cmd(
    document_id: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show chunks for one document id."""
    from readyagents.config import get_settings
    from readyagents.knowledge.cite import citation_from_record
    from readyagents.knowledge.ingest import _by_document
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope

    settings = get_settings()
    try:
        validate_scope(scope)
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        recs = _by_document(store.list(scope=scope)).get(document_id) or []
        store.close()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    rows = [{"text": rec.text, "citation": citation_from_record(rec)} for rec in recs]
    if as_json:
        _print_json(
            _json_envelope(
                "knowledge show", ok=True, document_id=document_id, chunks=rows, count=len(rows)
            )
        )
        return
    if not rows:
        console.print("(no chunks)")
        return
    for row in rows:
        cite = row.get("citation") or {}
        console.print(f"{cite.get('range')}  {str(row.get('text') or '')[:120]}")


@knowledge_app.command("cite")
def knowledge_cite_cmd(
    citation: str = typer.Argument(
        ..., help="JSON citation or document_id@version#bytes:start-end"
    ),
    scope: str = typer.Option(..., "--scope"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Resolve a citation to the exact source span. Same scope gate as retrieval."""
    from readyagents.config import get_settings
    from readyagents.knowledge.cite import resolve_citation
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        resolved = resolve_citation(store, citation, scope=scope)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge cite", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("knowledge cite", ok=True, **resolved))
        return
    console.print(resolved["text"])


@knowledge_app.command("forget")
def knowledge_forget_cmd(
    document_id: str = typer.Argument(...),
    scope: str = typer.Option(..., "--scope"),
    yes: bool = typer.Option(False, "--yes"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove every chunk, vector, and cite target for a document id."""
    from readyagents.config import get_settings
    from readyagents.knowledge.forget import forget_document
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope

    if not yes:
        extra = ConfigError("knowledge forget requires --yes")
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge forget", ok=False, error="ConfigError", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    settings = get_settings()
    try:
        validate_scope(scope)
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        removed = forget_document(store, scope=scope, document_id=document_id)
        store.close()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge forget", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("knowledge forget", ok=True, removed=removed))
        return
    console.print(f"removed {removed}")


@knowledge_app.command("sync")
def knowledge_sync_cmd(
    path: Path = typer.Argument(..., help="Workflow YAML with type: ingest, or a directory."),
    node_id: str | None = typer.Option(None, "--node"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Re-walk ingest sources. Foreground. Reports added/updated/unchanged/removed."""
    from readyagents.config import get_settings
    from readyagents.knowledge.sync import sync_workflow
    from readyagents.workflow.runner import load_workflow

    settings = get_settings()
    try:
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}:
            workflow = load_workflow(path)
            if dry_run:
                names = [n.id for n in workflow.nodes if str(n.type) == "ingest"]
                report = {
                    "dry_run": True,
                    "nodes": names,
                    "added": 0,
                    "updated": 0,
                    "unchanged": 0,
                    "removed": 0,
                }
            else:
                report = sync_workflow(
                    workflow,
                    home=settings.home_path(),
                    workspace=path.parent,
                    node_id=node_id,
                    backend=settings.memory_store,
                )
        else:
            extra = ConfigError("knowledge sync requires a workflow YAML with type: ingest")
            raise extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "knowledge sync", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("knowledge sync", ok=True, **report))
        return
    console.print(
        f"added={report.get('added', 0)} updated={report.get('updated', 0)} "
        f"unchanged={report.get('unchanged', 0)} removed={report.get('removed', 0)}"
    )
