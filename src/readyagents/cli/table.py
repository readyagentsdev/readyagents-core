"""CLI group: table (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

table_app = typer.Typer(
    help="Inspect intermediate tables (head, schema, stats). Never dumps every cell.",
    no_args_is_help=True,
)


@table_app.command("schema")
def table_schema_cmd(
    path: Path = typer.Argument(..., help="Table file (csv/jsonl) or stored hash path."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print column names, types, and row count. No cell values."""
    from readyagents.table.inspect import schema_payload

    try:
        part, store = _load_table_arg(path)
        payload = schema_payload(part)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "table schema", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("table schema", ok=True, **payload))
        return
    console.print(f"rows={payload['row_count']} bytes={payload['bytes_len']}")
    for col in payload["columns"]:
        console.print(f"  {col['name']}: {col['type']}")


@table_app.command("head")
def table_head_cmd(
    path: Path = typer.Argument(..., help="Table file (csv/jsonl) or stored hash path."),
    n: int = typer.Option(5, "--n", help="Row cap (1–50)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print schema plus the first N rows. Does not dump the whole table."""
    from readyagents.table.inspect import head_payload

    try:
        part, store = _load_table_arg(path)
        payload = head_payload(part, store, n=n)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "table head", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("table head", ok=True, **payload))
        return
    console.print(f"rows={payload['row_count']} showing={payload['head_n']}")
    for row in payload["head"]:
        console.print(json.dumps(row, ensure_ascii=False))


@table_app.command("stats")
def table_stats_cmd(
    path: Path = typer.Argument(..., help="Table file (csv/jsonl) or stored hash path."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print row count and per-column null counts. No cell values."""
    from readyagents.table.inspect import stats_payload

    try:
        part, store = _load_table_arg(path)
        payload = stats_payload(part, store)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "table stats", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("table stats", ok=True, **payload))
        return
    console.print(f"rows={payload['row_count']}")
    for name, n_null in (payload.get("nulls") or {}).items():
        console.print(f"  {name}: nulls={n_null} non_null={payload['non_null'][name]}")


def _load_table_arg(path: Path):
    from readyagents.config import get_settings
    from readyagents.table.io import read_table
    from readyagents.table.store import TableStore

    settings = get_settings()
    store = TableStore(settings.home_path() / "tables")
    token = str(path)
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token.lower()):
        return store.part_for(token.lower()), store
    workspace = Path.cwd()
    part = read_table(str(path), store, workspace=workspace)
    return part, store
