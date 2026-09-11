"""type: table — read/write and the eight deterministic ops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import TableError
from readyagents.table.io import read_table, write_table
from readyagents.table.ops import OPS, apply_op
from readyagents.table.part import TablePart, is_table_ref, part_from_mapping
from readyagents.table.store import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, TableStore
from readyagents.workflow.templates import interpolate, lookup

_TABLE_OPS = frozenset({"read", "write", *OPS})


def store_from(ctx: Any) -> TableStore:
    existing = getattr(ctx, "table_store", None) if ctx is not None else None
    if existing is not None:
        return existing
    home = getattr(ctx, "pin_home", None) if ctx is not None else None
    if home is None:
        from readyagents.config import get_settings

        home = get_settings().home_path()
    store = TableStore(Path(home) / "tables")
    if ctx is not None:
        ctx.table_store = store
    return store


def run_table_node(node: Any, state: Any, ctx: Any) -> Any:
    op = str(getattr(node, "op", None) or "").strip().lower()
    if op not in _TABLE_OPS:
        raise TableError(f"table op must be read, write, or {', '.join(OPS)}, not {op!r}")
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "op": op}
    if getattr(ctx, "offline", False) and getattr(ctx, "cassette", None) is not None:
        replayed = _replay(node, ctx)
        if replayed is not None:
            return replayed
    store = store_from(ctx)
    max_rows, max_bytes = _caps(node)
    if op == "read":
        declared = getattr(node, "column_schema", None)
        source = node.source
        if isinstance(source, str):
            source = _render_source(source, state)
        elif isinstance(source, dict):
            ns = state.mapping()
            source = {
                key: interpolate(val, ns) if isinstance(val, str) else val
                for key, val in source.items()
            }
        part = read_table(
            source,
            store,
            workspace=Path(getattr(ctx, "workflow_dir", None) or Path.cwd()),
            declared=declared,
            max_rows=max_rows,
            max_bytes=max_bytes,
            on_row_error=str(getattr(node, "on_row_error", None) or "fail"),
        )
        _record(node, ctx, part)
        return part.as_ref()
    left = resolve_part(node.source, state, ctx, store)
    if op == "write":
        dest = getattr(node, "path", None) or node.source
        if isinstance(dest, str):
            dest = interpolate(dest, state.mapping())
        result = write_table(
            left,
            dest if not isinstance(dest, str) else dest,
            store,
            workspace=Path(getattr(ctx, "workflow_dir", None) or Path.cwd()),
        )
        _record(node, ctx, result)
        return result
    right = None
    raw_right = getattr(node, "right", None)
    if raw_right:
        right = resolve_part(raw_right, state, ctx, store)
    derive = getattr(node, "derive", None)
    part = apply_op(
        op,
        store,
        left,
        right=right,
        columns=list(getattr(node, "columns", None) or []) or None,
        when=getattr(node, "when", None),
        keys=list(getattr(node, "keys", None) or []) or None,
        keep=str(getattr(node, "keep", None) or "first"),
        how=str(getattr(node, "how", None) or "inner"),
        on=list(getattr(node, "on", None) or []) or None,
        metrics=dict(getattr(node, "metrics", None) or {}) or None,
        by=list(getattr(node, "by", None) or []) or None,
        descending=bool(getattr(node, "descending", False)),
        derive=dict(derive) if isinstance(derive, dict) else None,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )
    _record(node, ctx, part)
    return part.as_ref()


def resolve_part(raw: Any, state: Any, ctx: Any, store: TableStore) -> TablePart:
    value = _resolve_value(raw, state)
    part = part_from_mapping(value)
    if part is not None:
        if not part.columns:
            loaded = store.part_for(part.sha256)
            part.columns = loaded.columns
            part.row_count = loaded.row_count
            part.bytes_len = loaded.bytes_len
        return part
    if isinstance(value, str) and value.strip():
        return read_table(
            value.strip(),
            store,
            workspace=Path(getattr(ctx, "workflow_dir", None) or Path.cwd()),
        )
    raise TableError("table source did not resolve to a table")


def _resolve_value(raw: Any, state: Any) -> Any:
    ns = state.mapping()
    if raw is None:
        raise TableError("table node requires 'source'")
    if isinstance(raw, dict):
        if is_table_ref(raw):
            return raw
        return {
            key: interpolate(val, ns) if isinstance(val, str) else val for key, val in raw.items()
        }
    text = str(raw).strip()
    if text.startswith("{{") and text.endswith("}}"):
        path = text[2:-2].strip().split("|", 1)[0].strip()
        return lookup(ns, path)
    if is_table_ref(text):
        return text
    return interpolate(text, ns)


def _render_source(raw: str, state: Any) -> str:
    return interpolate(raw, state.mapping())


def _caps(node: Any) -> tuple[int, int]:
    limits = dict(getattr(node, "limits", None) or {})
    max_rows = int(limits.get("max_rows") or DEFAULT_MAX_ROWS)
    max_bytes = int(limits.get("max_bytes") or DEFAULT_MAX_BYTES)
    return max(1, max_rows), max(1, max_bytes)


def _record(node: Any, ctx: Any, output: Any) -> None:
    cassette = getattr(ctx, "cassette", None)
    if cassette is None or not getattr(ctx, "recording", False):
        return
    if hasattr(cassette, "record_table"):
        cassette.record_table(
            node_id=node.id, output=output if not isinstance(output, TablePart) else output.as_ref()
        )


def _replay(node: Any, ctx: Any) -> Any:
    cassette = ctx.cassette
    if hasattr(cassette, "replay_table"):
        return cassette.replay_table(node_id=node.id)
    return None
