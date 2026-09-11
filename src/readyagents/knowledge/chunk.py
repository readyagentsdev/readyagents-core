"""Declared chunk strategies. Boundaries only — not a quality claim."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

STRATEGIES = frozenset({"fixed", "paragraph", "heading", "row_group"})
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP = 120
DEFAULT_ROW_GROUP = 20
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    text: str
    start: int
    end: int
    heading_path: str = ""
    range_kind: str = "bytes"


def chunk_text(
    text: str,
    *,
    strategy: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
    row_group: int = DEFAULT_ROW_GROUP,
) -> list[Chunk]:
    kind = (strategy or "paragraph").strip().lower().replace("-", "_")
    if kind not in STRATEGIES:
        raise ValueError(f"unknown chunk strategy {strategy!r}")
    cap = max(32, int(max_chars or DEFAULT_MAX_CHARS))
    ov = max(0, min(int(overlap or 0), cap - 1))
    if kind == "fixed":
        return _fixed(text, cap, ov)
    if kind == "paragraph":
        return _paragraph(text, cap, ov)
    if kind == "heading":
        return _heading(text, cap, ov)
    return _row_group(text, cap, max(1, int(row_group or DEFAULT_ROW_GROUP)))


def _byte_span(text: str, start_char: int, end_char: int) -> tuple[int, int]:
    start = len(text[:start_char].encode("utf-8"))
    end = len(text[:end_char].encode("utf-8"))
    return start, end


def _fixed(text: str, max_chars: int, overlap: int) -> list[Chunk]:
    if not text:
        return []
    step = max(1, max_chars - overlap)
    out: list[Chunk] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(n, i + max_chars)
        piece = text[i:j]
        start, end = _byte_span(text, i, j)
        out.append(Chunk(text=piece, start=start, end=end))
        if j >= n:
            break
        i += step
    return out


def _paragraph(text: str, max_chars: int, overlap: int) -> list[Chunk]:
    if not text:
        return []
    out: list[Chunk] = []
    idx = 0
    for block in re.split(r"\n\s*\n", text):
        piece = block.strip("\n")
        if not piece.strip():
            continue
        start_char = text.find(block, idx)
        if start_char < 0:
            start_char = idx
        idx = start_char + len(block)
        if len(piece) > max_chars:
            for chunk in _fixed(piece, max_chars, overlap):
                base, _end = _byte_span(text, start_char, start_char + len(piece))
                out.append(
                    Chunk(
                        text=chunk.text,
                        start=base + chunk.start,
                        end=base + (chunk.end - chunk.start),
                    )
                )
            continue
        start, end = _byte_span(text, start_char, start_char + len(piece))
        out.append(Chunk(text=piece, start=start, end=end))
    return out or _fixed(text, max_chars, overlap)


def _heading(text: str, max_chars: int, overlap: int) -> list[Chunk]:
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, int, str]] = []
    buf: list[str] = []
    buf_at = 0
    pos = 0
    current_path = ""

    def flush() -> None:
        nonlocal buf, buf_at
        body = "".join(buf)
        if body.strip():
            sections.append((current_path, buf_at, body))
        buf = []

    for line in lines:
        match = _HEADING.match(line.rstrip("\n"))
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(item[1] for item in stack)
            buf_at = pos
            buf = [line]
        else:
            if not buf:
                buf_at = pos
            buf.append(line)
        pos += len(line)
    flush()
    out: list[Chunk] = []
    for path, start_char, body in sections:
        if len(body) > max_chars:
            for chunk in _fixed(body, max_chars, overlap):
                start, end = _byte_span(text, start_char, start_char + len(body))
                out.append(
                    Chunk(
                        text=chunk.text,
                        start=start + chunk.start,
                        end=start + (chunk.end - chunk.start),
                        heading_path=path,
                    )
                )
            continue
        # Do not split markdown table rows: if a table is present, keep lines intact.
        start, end = _byte_span(text, start_char, start_char + len(body))
        out.append(Chunk(text=body, start=start, end=end, heading_path=path))
    return out or _fixed(text, max_chars, overlap)


def _row_group(text: str, max_chars: int, rows_per_group: int) -> list[Chunk]:
    if not text.strip():
        return []
    sample = text.lstrip()
    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    data = rows[1:] if len(rows) > 1 else []
    if not data:
        blob = _join([header], dialect)
        start, end = _byte_span(text, 0, len(text))
        return [Chunk(text=blob, start=start, end=end)]
    out: list[Chunk] = []
    i = 0
    # Reconstruct approximate char offsets by walking the original text lines.
    lines = text.splitlines(keepends=True)
    # header is line 0
    while i < len(data):
        group: list[list[str]] = []
        while i < len(data) and (
            not group or len(_join([header] + group + [data[i]], dialect)) <= max_chars
        ):
            group.append(data[i])
            i += 1
            if len(group) >= rows_per_group:
                break
        if not group:
            group = [data[i]]
            i += 1
        blob = _join([header] + group, dialect)
        # Byte range: header line + grouped data lines (1-based in file).
        start_line = 1 + (i - len(group))
        end_line = 1 + i
        start_char = sum(len(line) for line in lines[:start_line])
        end_char = (
            sum(len(line) for line in lines[: end_line + 1])
            if False
            else sum(len(line) for line in lines[: 1 + i])
        )
        # include header in range start 0
        start, end = _byte_span(
            text, 0 if start_line <= 1 else start_char, max(end_char, start_char + 1)
        )
        _ = start_line
        out.append(Chunk(text=blob, start=start, end=end))
    return out


def _join(rows: list[list[str]], dialect: Any) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, dialect)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()
