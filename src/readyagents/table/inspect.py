"""Inspect a stored table without dumping every cell."""

from __future__ import annotations

from typing import Any

from readyagents.table.part import TablePart
from readyagents.table.store import TableStore


def schema_payload(part: TablePart) -> dict[str, Any]:
    return {
        "sha256": part.sha256,
        "row_count": part.row_count,
        "bytes_len": part.bytes_len,
        "columns": [col.as_dict() for col in part.columns],
    }


def head_payload(part: TablePart, store: TableStore, *, n: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(n), 50))
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(store.iter_rows(part.sha256)):
        if index >= limit:
            break
        rows.append(row)
    payload = schema_payload(part)
    payload["head"] = rows
    payload["head_n"] = len(rows)
    return payload


def stats_payload(part: TablePart, store: TableStore) -> dict[str, Any]:
    nulls = {col.name: 0 for col in part.columns}
    count = 0
    for row in store.iter_rows(part.sha256):
        count += 1
        for col in part.columns:
            if row.get(col.name) is None or row.get(col.name) == "":
                nulls[col.name] += 1
    payload = schema_payload(part)
    payload["row_count"] = count
    payload["nulls"] = nulls
    payload["non_null"] = {name: count - n for name, n in nulls.items()}
    return payload
