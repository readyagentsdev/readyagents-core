"""Optional pandas backend. Must match stdlib hashes for the same fixtures."""

from __future__ import annotations

from typing import Any

from readyagents.errors import TableExtraMissing
from readyagents.table.expr import eval_predicate, eval_value
from readyagents.table.ops import apply_op as stdlib_apply
from readyagents.table.part import Column, TablePart
from readyagents.table.store import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, TableStore


def available() -> bool:
    try:
        import pandas  # noqa: F401
    except ImportError:
        return False
    return True


def require() -> None:
    if not available():
        raise TableExtraMissing("dataframe", "table")


def apply_op(op: str, store: TableStore, left: TablePart, **kwargs: Any) -> TablePart:
    """Pandas path. Filter/derive reuse the stdlib evaluator so hashes match."""
    require()
    name = str(op or "").strip().lower()
    if name in {"filter", "derive"}:
        return stdlib_apply(op, store, left, **kwargs)
    import pandas as pd

    max_rows = int(kwargs.get("max_rows") or DEFAULT_MAX_ROWS)
    max_bytes = int(kwargs.get("max_bytes") or DEFAULT_MAX_BYTES)
    df = _load(store, left)
    if name == "select":
        names = list(kwargs.get("columns") or list(df.columns))
        df = df.loc[:, names]
        columns = [c for c in left.columns if c.name in names]
        columns = sorted(columns, key=lambda c: names.index(c.name))
    elif name == "sort":
        by = list(kwargs.get("by") or [])
        df = df.sort_values(by=by, ascending=not bool(kwargs.get("descending")), kind="mergesort")
        columns = list(left.columns)
    elif name == "dedupe":
        keys = list(kwargs.get("keys") or [])
        keep = "first" if str(kwargs.get("keep") or "first") == "first" else "last"
        df = df.drop_duplicates(subset=keys, keep=keep)
        columns = list(left.columns)
    elif name == "union":
        right = kwargs.get("right")
        if right is None:
            from readyagents.errors import TableError

            raise TableError("union requires a right table")
        other = _load(store, right)
        df = pd.concat([df, other], ignore_index=True)
        columns = list(left.columns)
    elif name == "join":
        right = kwargs.get("right")
        if right is None:
            from readyagents.errors import TableError

            raise TableError("join requires a right table")
        on = list(kwargs.get("on") or [])
        how = str(kwargs.get("how") or "inner")
        other = _load(store, right)
        left_names = list(df.columns)
        df = df.merge(other, on=on, how=how, suffixes=("", "_r"))
        drop = [c for c in df.columns if c.endswith("_r")]
        if drop:
            df = df.drop(columns=drop)
        right_only = [c for c in right.columns if c.name not in left_names]
        columns = list(left.columns) + right_only
    elif name == "aggregate":
        keys = list(kwargs.get("keys") or [])
        metrics = dict(kwargs.get("metrics") or {})
        grouped = df.groupby(keys, sort=False, dropna=False) if keys else df
        pieces = []
        columns = [Column(name=k, type=_type_of(left, k)) for k in keys]
        used = set(keys)
        for src, spec in metrics.items():
            token = str(spec).lower()
            col_name = "count" if token == "count" and "count" not in used else src
            if token == "count":
                col_name = "count" if "count" not in used else f"{src}_count"
            used.add(col_name)
            if token == "count":
                series = grouped.size() if keys else pd.Series([len(df)])
                kind = "int"
            elif token == "sum":
                series = grouped[src].sum()
                kind = _type_of(left, src)
            elif token == "avg":
                series = grouped[src].mean()
                kind = "float"
            elif token == "min":
                series = grouped[src].min()
                kind = _type_of(left, src)
            elif token == "max":
                series = grouped[src].max()
                kind = _type_of(left, src)
            else:
                from readyagents.errors import TableError

                raise TableError(f"unknown metric {token!r}")
            pieces.append((col_name, series, kind))
            columns.append(Column(name=col_name, type=kind))
        if keys:
            out = grouped.size().rename("_n").reset_index()
            for col_name, series, _kind in pieces:
                aligned = (
                    series.reset_index(name=col_name) if hasattr(series, "reset_index") else series
                )
                if isinstance(aligned, pd.DataFrame):
                    out[col_name] = aligned[col_name]
                else:
                    out[col_name] = aligned
            out = out.drop(columns=["_n"])
            df = out
        else:
            df = pd.DataFrame(
                {
                    name: [series.iloc[0] if hasattr(series, "iloc") else series]
                    for name, series, _ in pieces
                }
            )
    else:
        return stdlib_apply(op, store, left, **kwargs)
    records = _records(df, [c.name for c in columns])
    inputs = [left.sha256]
    right = kwargs.get("right")
    if isinstance(right, TablePart):
        inputs.append(right.sha256)
    return store.put_rows(
        columns,
        records,
        max_rows=max_rows,
        max_bytes=max_bytes,
        op=name,
        input_sha256=inputs,
    )


def _load(store: TableStore, part: TablePart) -> Any:
    import pandas as pd

    rows = list(store.iter_rows(part.sha256))
    names = [c.name for c in part.columns]
    return pd.DataFrame(rows, columns=names)


def _records(df: Any, names: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        row: dict[str, Any] = {}
        for name in names:
            value = rec.get(name)
            if hasattr(value, "item"):
                try:
                    value = value.item()
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(value, float) and value != value:
                value = None
            row[name] = value
        out.append(row)
    return out


def _type_of(part: TablePart, name: str) -> str:
    for col in part.columns:
        if col.name == name:
            return col.type
    return "str"


# Keep unused imports referenced for ruff in optional paths.
_ = (eval_predicate, eval_value)
