"""CSV, JSON Lines, and Parquet I/O. Streaming reads, CSV injection on write."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from readyagents.errors import (
    PathError,
    TableCapExceeded,
    TableExtraMissing,
    TablePathDenied,
    TableSchemaError,
)
from readyagents.paths import resolve_within
from readyagents.table.part import Column, TablePart
from readyagents.table.schema import coerce_row, infer_columns, parse_declared
from readyagents.table.store import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, TableStore

INJECT_PREFIX = tuple("=+-@")


def confine(path: str | Path, workspace: Path) -> Path:
    try:
        return resolve_within(Path(path), workspace, what="table path")
    except PathError as exc:
        raise TablePathDenied(str(exc)) from exc


def neutralize_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in INJECT_PREFIX:
        return "'" + value
    return value


def read_table(
    source: dict[str, Any] | str,
    store: TableStore,
    *,
    workspace: Path,
    declared: dict[str, Any] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    on_row_error: str = "fail",
) -> TablePart:
    parsed = _parse_source(source)
    kind = parsed["kind"]
    path = confine(parsed["path"], workspace)
    columns_decl = parse_declared(declared)
    policy = str(on_row_error or "fail").strip().lower()
    if kind == "csv":
        rows, names = _iter_csv(path, max_bytes=max_bytes)
        return _ingest(
            store,
            rows,
            names,
            columns_decl,
            max_rows=max_rows,
            max_bytes=max_bytes,
            op="read",
            on_row_error=policy,
        )
    if kind in {"jsonl", "json"}:
        rows, names = _iter_jsonl(path, max_bytes=max_bytes)
        return _ingest(
            store,
            rows,
            names,
            columns_decl,
            max_rows=max_rows,
            max_bytes=max_bytes,
            op="read",
            on_row_error=policy,
        )
    if kind == "parquet":
        return _read_parquet(
            path,
            store,
            declared=columns_decl,
            max_rows=max_rows,
            max_bytes=max_bytes,
            on_row_error=policy,
        )
    raise TablePathDenied(f"unsupported table kind {kind!r}")


def write_table(
    part: TablePart,
    dest: dict[str, Any] | str,
    store: TableStore,
    *,
    workspace: Path,
) -> dict[str, Any]:
    parsed = _parse_source(dest)
    kind = parsed["kind"]
    path = confine(parsed["path"], workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [c.name for c in part.columns]
    if kind == "csv":
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(names)
            for row in store.iter_rows(part.sha256):
                writer.writerow([neutralize_cell(row.get(n)) for n in names])
    elif kind in {"jsonl", "json"}:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in store.iter_rows(part.sha256):
                payload = {n: neutralize_cell(row.get(n)) for n in names}
                fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    elif kind == "parquet":
        _write_parquet(part, path, store)
    else:
        raise TablePathDenied(f"unsupported table kind {kind!r}")
    return {"path": str(path), "kind": kind, "sha256": part.sha256, "row_count": part.row_count}


def _parse_source(source: dict[str, Any] | str) -> dict[str, str]:
    if isinstance(source, str):
        path = source
        suffix = Path(path).suffix.lower()
        kind = {".csv": "csv", ".jsonl": "jsonl", ".json": "jsonl", ".parquet": "parquet"}.get(
            suffix, "csv"
        )
        return {"kind": kind, "path": path}
    kind = str(source.get("kind") or "csv").strip().lower()
    path = str(source.get("path") or "")
    if not path:
        raise TablePathDenied("table source requires a path")
    return {"kind": kind, "path": path}


def _iter_csv(path: Path, *, max_bytes: int) -> tuple[Iterator[dict[str, Any]], list[str]]:
    size = path.stat().st_size
    if size > max_bytes:
        raise TableCapExceeded("bytes", size, max_bytes)

    def rows() -> Iterator[dict[str, Any]]:
        used = 0
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                used += sum(len(str(v or "")) for v in row.values())
                if used > max_bytes:
                    raise TableCapExceeded("bytes", used, max_bytes)
                yield {str(k): v for k, v in row.items() if k is not None}

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
    names = [str(c) for c in header]
    return rows(), names


def _iter_jsonl(path: Path, *, max_bytes: int) -> tuple[Iterator[dict[str, Any]], list[str]]:
    size = path.stat().st_size
    if size > max_bytes:
        raise TableCapExceeded("bytes", size, max_bytes)
    names: list[str] = []

    def rows() -> Iterator[dict[str, Any]]:
        used = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                used += len(line.encode("utf-8"))
                if used > max_bytes:
                    raise TableCapExceeded("bytes", used, max_bytes)
                text = line.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    continue
                for key in payload:
                    if key not in names:
                        names.append(str(key))
                yield {str(k): v for k, v in payload.items()}

    return rows(), names


def _ingest(
    store: TableStore,
    rows: Iterator[dict[str, Any]],
    names: list[str],
    declared: list[Column] | None,
    *,
    max_rows: int,
    max_bytes: int,
    op: str,
    on_row_error: str = "fail",
) -> TablePart:
    policy = str(on_row_error or "fail").strip().lower()
    quarantined: list[dict[str, Any]] = []
    if declared is not None:
        columns = declared
    else:
        buffered = list(rows)
        if len(buffered) > max_rows:
            raise TableCapExceeded("rows", len(buffered), max_rows)
        columns = infer_columns(buffered, names=names or None)
        if names:
            order = [c for n in names for c in columns if c.name == n]
            extra = [c for c in columns if c.name not in names]
            columns = order + extra
        rows = iter(buffered)

    def coerced() -> Iterator[dict[str, Any]]:
        for index, row in enumerate(rows):
            if index + 1 > max_rows:
                raise TableCapExceeded("rows", index + 1, max_rows)
            try:
                yield coerce_row(row, columns, index=index)
            except TableSchemaError as exc:
                if policy == "fail":
                    raise
                if policy == "quarantine":
                    quarantined.append({"row": index, "column": exc.column, "reason": "schema"})
                # skip: drop the row

    part = store.put_rows(columns, coerced(), max_rows=max_rows, max_bytes=max_bytes, op=op)
    if quarantined:
        err_cols = [
            Column(name="row", type="int"),
            Column(name="column", type="str"),
            Column(name="reason", type="str"),
        ]
        errors = store.put_rows(
            err_cols, quarantined, max_rows=max_rows, max_bytes=max_bytes, op="errors"
        )
        part.errors_sha256 = errors.sha256
        part.errors_row_count = errors.row_count
    return part


def _read_parquet(
    path: Path,
    store: TableStore,
    *,
    declared: list[Column] | None,
    max_rows: int,
    max_bytes: int,
    on_row_error: str = "fail",
) -> TablePart:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise TableExtraMissing("parquet", "table") from exc
    table = pq.read_table(path)
    names = list(table.column_names)
    rows = table.to_pylist()
    return _ingest(
        store,
        iter(rows),
        names,
        declared,
        max_rows=max_rows,
        max_bytes=max_bytes,
        op="read",
        on_row_error=on_row_error,
    )


def _write_parquet(part: TablePart, path: Path, store: TableStore) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise TableExtraMissing("parquet", "table") from exc
    rows = list(store.iter_rows(part.sha256))
    names = [c.name for c in part.columns]
    arrays = {n: [neutralize_cell(row.get(n)) for row in rows] for n in names}
    table = pa.table(arrays)
    pq.write_table(table, path)
