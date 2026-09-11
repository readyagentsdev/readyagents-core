"""Typed table part. State and records store a hash ref, never the rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TABLE_MARKER = "_table"
COLUMN_TYPES = frozenset({"int", "float", "str", "bool"})


@dataclass(frozen=True)
class Column:
    name: str
    type: str = "str"

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}


@dataclass
class TablePart:
    """Content-addressed table. Rows live on disk, not in run state or cassettes."""

    sha256: str
    row_count: int
    bytes_len: int
    columns: list[Column] = field(default_factory=list)
    path: str | None = None
    errors_sha256: str | None = None
    errors_row_count: int = 0
    input_sha256: list[str] = field(default_factory=list)
    op: str | None = None

    def as_ref(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            TABLE_MARKER: True,
            "sha256": self.sha256,
            "row_count": int(self.row_count),
            "bytes_len": int(self.bytes_len),
            "columns": [col.as_dict() for col in self.columns],
        }
        if self.path:
            row["path"] = self.path
        if self.errors_sha256:
            row["errors_sha256"] = self.errors_sha256
            row["errors_row_count"] = int(self.errors_row_count)
        if self.input_sha256:
            row["input_sha256"] = list(self.input_sha256)
        if self.op:
            row["op"] = self.op
        return row


def is_table_ref(value: Any) -> bool:
    if isinstance(value, TablePart):
        return True
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and TABLE_MARKER in text:
            try:
                import json

                value = json.loads(text)
            except (TypeError, ValueError):
                return False
    return isinstance(value, dict) and value.get(TABLE_MARKER) is True and "sha256" in value


def part_from_mapping(raw: Any) -> TablePart | None:
    if isinstance(raw, TablePart):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                import json

                raw = json.loads(text)
            except (TypeError, ValueError):
                return None
    if not is_table_ref(raw) or isinstance(raw, TablePart):
        return raw if isinstance(raw, TablePart) else None
    columns = []
    for item in raw.get("columns") or []:
        if isinstance(item, dict) and item.get("name"):
            kind = str(item.get("type") or "str").strip().lower()
            if kind not in COLUMN_TYPES:
                kind = "str"
            columns.append(Column(name=str(item["name"]), type=kind))
        elif isinstance(item, str) and item:
            columns.append(Column(name=item, type="str"))
    inputs = raw.get("input_sha256") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    return TablePart(
        sha256=str(raw["sha256"]),
        row_count=int(raw.get("row_count") or 0),
        bytes_len=int(raw.get("bytes_len") or 0),
        columns=columns,
        path=str(raw["path"]) if raw.get("path") else None,
        errors_sha256=str(raw["errors_sha256"]) if raw.get("errors_sha256") else None,
        errors_row_count=int(raw.get("errors_row_count") or 0),
        input_sha256=[str(x) for x in inputs],
        op=str(raw["op"]) if raw.get("op") else None,
    )
