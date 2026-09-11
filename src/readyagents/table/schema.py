"""Column type inference and declared-schema validation. Errors never print values."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from readyagents.errors import TableSchemaError
from readyagents.table.part import COLUMN_TYPES, Column


def parse_declared(raw: Mapping[str, Any] | None) -> list[Column] | None:
    if not raw:
        return None
    out: list[Column] = []
    for name, kind in raw.items():
        token = str(kind).strip().lower()
        if token in {"string", "text"}:
            token = "str"
        if token in {"integer"}:
            token = "int"
        if token in {"number", "double"}:
            token = "float"
        if token in {"boolean"}:
            token = "bool"
        if token not in COLUMN_TYPES:
            token = "str"
        out.append(Column(name=str(name), type=token))
    return out or None


def infer_columns(
    rows: Iterable[Mapping[str, Any]], *, names: list[str] | None = None
) -> list[Column]:
    seen: list[str] = list(names or [])
    kinds: dict[str, set[str]] = {name: set() for name in seen}
    for row in rows:
        for key, value in row.items():
            name = str(key)
            if name not in kinds:
                kinds[name] = set()
                seen.append(name)
            guessed = _guess(value)
            if guessed:
                kinds[name].add(guessed)
    columns: list[Column] = []
    for name in seen:
        found = kinds.get(name) or set()
        if found <= {"int"}:
            token = "int"
        elif found <= {"int", "float"}:
            token = "float"
        elif found <= {"bool"}:
            token = "bool"
        else:
            token = "str"
        columns.append(Column(name=name, type=token))
    return columns


def coerce_row(
    row: Mapping[str, Any],
    columns: list[Column],
    *,
    index: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        if col.name not in row:
            out[col.name] = None
            continue
        try:
            out[col.name] = coerce_value(row.get(col.name), col.type)
        except (TypeError, ValueError) as exc:
            raise TableSchemaError(index, col.name) from exc
    return out


def coerce_value(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    if kind == "str":
        return str(value)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
        raise ValueError("bool")
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError("int")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        text = str(value).strip()
        if text.endswith(".0") and text.replace(".", "", 1).lstrip("-").isdigit():
            return int(float(text))
        return int(text)
    if kind == "float":
        if isinstance(value, bool):
            raise ValueError("float")
        return float(value)
    return value


def _guess(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    text = str(value).strip()
    lowered = text.lower()
    if lowered in {"true", "false", "yes", "no"}:
        return "bool"
    if text.lstrip("-").isdigit():
        return "int"
    try:
        float(text)
        return "float"
    except ValueError:
        return "str"
