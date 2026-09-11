"""Stdlib simple-PDF page split. Not a general codec; pypdf is the optional extra."""

from __future__ import annotations

import re
from typing import Any

from readyagents.errors import MediaMalformed, MediaPageLimitExceeded, missing_extra_message

_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?!\s*s)\b")
_TEXT = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj")
_HEX_TEXT = re.compile(rb"<(?:[0-9A-Fa-f]{2})+>\s*Tj")


def is_pdf(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF")


def count_pages(data: bytes) -> int:
    if not is_pdf(data):
        raise MediaMalformed("not a PDF")
    return len(_PAGE_OBJ.findall(data))


def split_simple_pdf(data: bytes, *, max_pages: int) -> list[dict[str, Any]]:
    """Ordered page dicts with extracted Tj text. Caps before walking contents."""
    if not is_pdf(data):
        raise MediaMalformed("not a PDF")
    if b"stream" not in data and b"/Type" not in data:
        raise MediaMalformed("malformed PDF container")
    used = count_pages(data)
    if used < 1:
        # /Type/Page without whitespace
        used = len(re.findall(rb"/Type\s*/Page(?!s)", data))
    if used < 1:
        raise MediaMalformed("PDF has no pages")
    if used > max_pages:
        raise MediaPageLimitExceeded(used, max_pages)
    texts = _page_texts(data, used)
    pages: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        pages.append({"index": index, "page": index + 1, "text": text})
    return pages


def build_simple_pdf(texts: list[str]) -> bytes:
    """Deterministic uncompressed PDF used by tests and the stdlib splitter."""
    if not texts:
        raise MediaMalformed("PDF has no pages")
    objs: list[bytes] = []
    n = len(texts)
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii"))
    content_ids = [3 + n + i for i in range(n)]
    for i, text in enumerate(texts):
        cid = content_ids[i]
        escaped = _escape(text)
        stream = f"BT /F1 12 Tf 24 720 Td ({escaped}) Tj ET".encode("latin-1", "replace")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> >>".encode(
                "ascii"
            )
        )
        header = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
        objs.append(header + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    return _assemble(objs)


def extract_with_pypdf(data: bytes, *, max_pages: int) -> list[dict[str, Any]] | None:
    """Optional extra. Returns None when pypdf is absent (caller may use simple path)."""
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return None
    import io

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        used = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise MediaMalformed(f"malformed PDF container: {exc}") from exc
    if used > max_pages:
        raise MediaPageLimitExceeded(used, max_pages)
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        pages.append({"index": index, "page": index + 1, "text": text})
    return pages


def require_pdf_extra() -> None:
    try:
        import pypdf  # noqa: F401
    except ImportError as exc:
        from readyagents.errors import MediaError

        raise MediaError(missing_extra_message("PDF", "pdf")) from exc


def _page_texts(data: bytes, count: int) -> list[str]:
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, flags=re.DOTALL)
    texts = [_text_from_stream(s) for s in streams]
    if len(texts) >= count:
        return texts[:count]
    joined = _text_from_stream(data)
    if count == 1:
        return [joined]
    return (texts + [""] * count)[:count]


def _text_from_stream(stream: bytes) -> str:
    parts: list[str] = []
    for match in _TEXT.finditer(stream):
        raw = match.group(0)
        inner = raw[1 : raw.rfind(b")")]
        parts.append(
            inner.replace(b"\\(", b"(")
            .replace(b"\\)", b")")
            .replace(b"\\\\", b"\\")
            .decode("latin-1", "replace")
        )
    return " ".join(parts).strip()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _assemble(objs: list[bytes]) -> bytes:
    offsets = [0]
    body = bytearray(b"%PDF-1.4\n")
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body.extend(f"{i} 0 obj\n".encode("ascii"))
        body.extend(obj)
        if not obj.endswith(b"\n"):
            body.extend(b"\n")
        body.extend(b"endobj\n")
    xref_at = len(body)
    count = len(objs) + 1
    xref = [f"xref\n0 {count}\n".encode("ascii"), b"0000000000 65535 f \n"]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode("ascii"))
    body.extend(b"".join(xref))
    body.extend(
        f"trailer << /Size {count} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
    )
    return bytes(body)
