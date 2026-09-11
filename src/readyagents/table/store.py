"""Content-addressed table store. Canonical JSONL, confined, never rows in records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.errors import PathError, TableCapExceeded, TableError, TablePathDenied
from readyagents.paths import resolve_within
from readyagents.permissions import restrict_file
from readyagents.table.part import Column, TablePart

DEFAULT_MAX_ROWS = 100_000
DEFAULT_MAX_BYTES = 32_000_000
HEADER_KEY = "_table"


def encode_row(row: dict[str, Any], columns: list[Column]) -> bytes:
    payload = {col.name: row.get(col.name) for col in columns}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def encode_header(columns: list[Column]) -> bytes:
    payload = {
        HEADER_KEY: True,
        "columns": [col.as_dict() for col in columns],
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class TableStore:
    """``root / aa / sha256`` JSONL tables plus header metadata."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        digest = str(sha256).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise TableError(f"invalid table hash: {sha256!r}")
        dest = self.root / digest[:2] / digest
        try:
            return resolve_within(dest, self.root, what="table blob")
        except PathError as exc:
            raise TablePathDenied(str(exc)) from exc

    def has(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put_rows(
        self,
        columns: list[Column],
        rows: Iterable[dict[str, Any]],
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        op: str | None = None,
        input_sha256: list[str] | None = None,
    ) -> TablePart:
        tmp = self.root / f".tmp-{uuid4().hex}"
        hasher = hashlib.sha256()
        used = 0
        count = 0
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tmp.open("wb") as fh:
                header = encode_header(columns)
                fh.write(header)
                hasher.update(header)
                used += len(header)
                for row in rows:
                    count += 1
                    if count > max_rows:
                        raise TableCapExceeded("rows", count, max_rows)
                    line = encode_row(row, columns)
                    used += len(line)
                    if used > max_bytes:
                        raise TableCapExceeded("bytes", used, max_bytes)
                    fh.write(line)
                    hasher.update(line)
            digest = hasher.hexdigest()
            dest = self.path_for(digest)
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                restrict_file(tmp)
                tmp.replace(dest)
                restrict_file(dest)
            else:
                tmp.unlink(missing_ok=True)
            return TablePart(
                sha256=digest,
                row_count=count,
                bytes_len=used,
                columns=list(columns),
                path=str(dest),
                op=op,
                input_sha256=list(input_sha256 or []),
            )
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def iter_rows(self, sha256: str) -> Iterator[dict[str, Any]]:
        path = self.path_for(sha256)
        if not path.is_file():
            raise TableError(f"table blob not found: {sha256}")
        with path.open("r", encoding="utf-8") as fh:
            header = fh.readline()
            if HEADER_KEY not in header:
                raise TableError("table blob is missing a header")
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                yield json.loads(text)

    def columns_of(self, sha256: str) -> list[Column]:
        path = self.path_for(sha256)
        if not path.is_file():
            raise TableError(f"table blob not found: {sha256}")
        with path.open("r", encoding="utf-8") as fh:
            raw = json.loads(fh.readline() or "{}")
        columns = []
        for item in raw.get("columns") or []:
            columns.append(
                Column(name=str(item.get("name") or ""), type=str(item.get("type") or "str"))
            )
        return columns

    def part_for(self, sha256: str) -> TablePart:
        path = self.path_for(sha256)
        if not path.is_file():
            raise TableError(f"table blob not found: {sha256}")
        columns = self.columns_of(sha256)
        size = path.stat().st_size
        count = 0
        for _ in self.iter_rows(sha256):
            count += 1
        return TablePart(
            sha256=sha256,
            row_count=count,
            bytes_len=int(size),
            columns=columns,
            path=str(path),
        )
