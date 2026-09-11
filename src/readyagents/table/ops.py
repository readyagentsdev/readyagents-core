"""Stdlib implementations of the eight deterministic table ops."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from readyagents.errors import TableError
from readyagents.table.expr import eval_predicate, eval_value
from readyagents.table.part import Column, TablePart
from readyagents.table.store import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, TableStore

OPS = ("select", "filter", "join", "aggregate", "sort", "dedupe", "union", "derive")


def apply_op(
    op: str,
    store: TableStore,
    left: TablePart,
    *,
    right: TablePart | None = None,
    columns: list[str] | None = None,
    when: str | None = None,
    keys: list[str] | None = None,
    keep: str = "first",
    how: str = "inner",
    on: list[str] | None = None,
    metrics: dict[str, str] | None = None,
    by: list[str] | None = None,
    descending: bool = False,
    derive: dict[str, Any] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TablePart:
    name = str(op or "").strip().lower()
    if name not in OPS:
        raise TableError(f"unknown table op {op!r}")
    fn = {
        "select": _select,
        "filter": _filter,
        "join": _join,
        "aggregate": _aggregate,
        "sort": _sort,
        "dedupe": _dedupe,
        "union": _union,
        "derive": _derive,
    }[name]
    return fn(
        store,
        left,
        right=right,
        columns=columns,
        when=when,
        keys=keys,
        keep=keep,
        how=how,
        on=on,
        metrics=metrics,
        by=by,
        descending=descending,
        derive=derive,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def _put(
    store: TableStore,
    columns: list[Column],
    rows: Iterable[dict[str, Any]],
    *,
    op: str,
    inputs: list[str],
    max_rows: int,
    max_bytes: int,
) -> TablePart:
    return store.put_rows(
        columns,
        rows,
        max_rows=max_rows,
        max_bytes=max_bytes,
        op=op,
        input_sha256=inputs,
    )


def _select(store, left, **kw) -> TablePart:
    names = list(kw.get("columns") or [c.name for c in left.columns])
    lookup = {c.name: c for c in left.columns}
    missing = [n for n in names if n not in lookup]
    if missing:
        raise TableError(f"unknown column {missing[0]!r}")
    columns = [Column(name=n, type=lookup[n].type) for n in names]
    rows = ({n: row.get(n) for n in names} for row in store.iter_rows(left.sha256))
    return _put(
        store,
        columns,
        rows,
        op="select",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _filter(store, left, **kw) -> TablePart:
    expr = str(kw.get("when") or "").strip()
    if not expr:
        raise TableError("filter requires 'when'")
    rows = (row for row in store.iter_rows(left.sha256) if eval_predicate(expr, row))
    return _put(
        store,
        list(left.columns),
        rows,
        op="filter",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _join(store, left, **kw) -> TablePart:
    right: TablePart | None = kw.get("right")
    if right is None:
        raise TableError("join requires a right table")
    on = list(kw.get("on") or [])
    if not on:
        raise TableError("join requires 'on' keys")
    how = str(kw.get("how") or "inner").strip().lower()
    if how not in {"inner", "left"}:
        raise TableError("join how must be inner or left")
    right_index: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in store.iter_rows(right.sha256):
        right_index[_key(row, on)].append(row)
    left_names = [c.name for c in left.columns]
    right_names = [c.name for c in right.columns if c.name not in left_names]
    columns = list(left.columns) + [c for c in right.columns if c.name not in left_names]

    def rows() -> Iterable[dict[str, Any]]:
        for row in store.iter_rows(left.sha256):
            matches = right_index.get(_key(row, on)) or []
            if not matches:
                if how == "left":
                    merged = {n: row.get(n) for n in left_names}
                    for name in right_names:
                        merged[name] = None
                    yield merged
                continue
            for other in matches:
                merged = {n: row.get(n) for n in left_names}
                for name in right_names:
                    merged[name] = other.get(name)
                yield merged

    return _put(
        store,
        columns,
        rows(),
        op="join",
        inputs=[left.sha256, right.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _aggregate(store, left, **kw) -> TablePart:
    keys = list(kw.get("keys") or [])
    metrics = dict(kw.get("metrics") or {})
    if not metrics:
        raise TableError("aggregate requires 'metrics'")
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in store.iter_rows(left.sha256):
        groups[_key(row, keys)].append(row)
    metric_cols: list[tuple[str, str, str, str]] = []
    used = set(keys)
    for name, spec in metrics.items():
        token = str(spec).lower()
        if token == "count":
            col_name = "count" if "count" not in used else f"{name}_count"
        else:
            col_name = name
        used.add(col_name)
        kind = "int" if token == "count" else ("float" if token == "avg" else _type_of(left, name))
        metric_cols.append((col_name, token, name, kind))
    columns = [Column(name=k, type=_type_of(left, k)) for k in keys]
    columns.extend(Column(name=n, type=kind) for n, _t, _s, kind in metric_cols)

    def rows() -> Iterable[dict[str, Any]]:
        for key, items in groups.items():
            row = {k: v for k, v in zip(keys, key, strict=True)}
            for col_name, token, src, _kind in metric_cols:
                row[col_name] = _metric(items, src, token)
            yield row

    return _put(
        store,
        columns,
        rows(),
        op="aggregate",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _sort(store, left, **kw) -> TablePart:
    by = list(kw.get("by") or [])
    if not by:
        raise TableError("sort requires 'by'")
    descending = bool(kw.get("descending"))
    rows = list(store.iter_rows(left.sha256))
    if len(rows) > kw["max_rows"]:
        from readyagents.errors import TableCapExceeded

        raise TableCapExceeded("rows", len(rows), kw["max_rows"])
    rows.sort(key=lambda r: tuple(_sort_key(r.get(c)) for c in by), reverse=descending)
    return _put(
        store,
        list(left.columns),
        rows,
        op="sort",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _dedupe(store, left, **kw) -> TablePart:
    keys = list(kw.get("keys") or [])
    if not keys:
        raise TableError("dedupe requires 'keys'")
    keep = str(kw.get("keep") or "first").strip().lower()
    if keep not in {"first", "last"}:
        raise TableError("dedupe keep must be first or last")
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for row in store.iter_rows(left.sha256):
        key = _key(row, keys)
        if key not in seen:
            order.append(key)
            seen[key] = row
        elif keep == "last":
            seen[key] = row
    rows = (seen[k] for k in order)
    return _put(
        store,
        list(left.columns),
        rows,
        op="dedupe",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _union(store, left, **kw) -> TablePart:
    right: TablePart | None = kw.get("right")
    if right is None:
        raise TableError("union requires a right table")
    left_names = [c.name for c in left.columns]
    right_names = [c.name for c in right.columns]
    if left_names != right_names:
        raise TableError("union requires matching columns")

    def rows() -> Iterable[dict[str, Any]]:
        yield from store.iter_rows(left.sha256)
        yield from store.iter_rows(right.sha256)

    return _put(
        store,
        list(left.columns),
        rows(),
        op="union",
        inputs=[left.sha256, right.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _derive(store, left, **kw) -> TablePart:
    spec = dict(kw.get("derive") or {})
    name = str(spec.get("name") or "").strip()
    expr = str(spec.get("expr") or spec.get("when") or "").strip()
    if not name or not expr:
        raise TableError("derive requires {name, expr}")
    if any(c.name == name for c in left.columns):
        columns = list(left.columns)
    else:
        columns = list(left.columns) + [Column(name=name, type="float")]

    def rows() -> Iterable[dict[str, Any]]:
        for row in store.iter_rows(left.sha256):
            out = dict(row)
            out[name] = eval_value(expr, row)
            yield out

    return _put(
        store,
        columns,
        rows(),
        op="derive",
        inputs=[left.sha256],
        max_rows=kw["max_rows"],
        max_bytes=kw["max_bytes"],
    )


def _key(row: dict[str, Any], keys: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in keys)


def _type_of(part: TablePart, name: str) -> str:
    for col in part.columns:
        if col.name == name:
            return col.type
    return "str"


def _metric(rows: list[dict[str, Any]], src: str, token: str) -> Any:
    if token == "count":
        return len(rows)
    values = [row.get(src) for row in rows if row.get(src) is not None]
    if not values:
        return None
    if token == "sum":
        return sum(values)
    if token == "avg":
        return float(sum(values)) / len(values)
    if token == "min":
        return min(values)
    if token == "max":
        return max(values)
    raise TableError(f"unknown metric {token!r}")


def _sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, "")
    return (0, value)
