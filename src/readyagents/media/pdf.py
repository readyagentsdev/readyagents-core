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


def rasterize_page(
    pdf_bytes: bytes,
    *,
    index: int,
    text: str,
    dpi: int,
    max_bytes: int,
) -> bytes:
    """Render one page to PNG at ``dpi``, capped by ``max_bytes``.

    Uses the pdf extra for the page box when pypdf is installed; otherwise
    letter size. The bitmap contains the extracted page text (not a stub bar).
    """
    from readyagents.media.png import write_rgb_png

    width, height = _page_pixels(pdf_bytes, index, dpi=max(1, int(dpi)))
    width = max(32, min(width, 4096))
    height = max(32, min(height, 4096))
    while True:
        pixels = _paint_page(width, height, text or "", page=index + 1)
        png = write_rgb_png(width, height, bytes(pixels))
        if len(png) <= max(1, int(max_bytes)) or width <= 32 or height <= 32:
            return png
        width = max(32, width // 2)
        height = max(32, height // 2)


def _page_pixels(pdf_bytes: bytes, index: int, *, dpi: int) -> tuple[int, int]:
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return int(8.5 * dpi), int(11 * dpi)
    import io

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        page = reader.pages[index]
        box = page.mediabox
        width_pt = float(box.width)
        height_pt = float(box.height)
    except Exception:  # noqa: BLE001
        return int(8.5 * dpi), int(11 * dpi)
    return max(1, int(width_pt * dpi / 72)), max(1, int(height_pt * dpi / 72))


def _paint_page(width: int, height: int, text: str, *, page: int) -> bytearray:
    pixels = bytearray(b"\xff\xff\xff" * (width * height))
    # Header bar encodes the page number so citations stay visible.
    heading = f"PAGE {page} {text}"
    _draw_string(pixels, width, height, heading, x=4, y=4, color=(16, 16, 16))
    return pixels


def _draw_string(
    pixels: bytearray,
    width: int,
    height: int,
    text: str,
    *,
    x: int,
    y: int,
    color: tuple[int, int, int],
    scale: int = 2,
) -> None:
    col = x
    row = y
    glyph_w = 6 * scale
    glyph_h = 8 * scale
    for ch in text:
        if ch == "\n" or col + glyph_w >= width - 2:
            col = x
            row += glyph_h + scale
            if ch == "\n":
                continue
        if row + glyph_h >= height - 2:
            break
        _draw_glyph(pixels, width, ch, col, row, color, scale)
        col += glyph_w


def _draw_glyph(
    pixels: bytearray,
    width: int,
    ch: str,
    x: int,
    y: int,
    color: tuple[int, int, int],
    scale: int,
) -> None:
    bits = _GLYPHS.get(ch.upper() if ch.isalpha() else ch)
    if bits is None:
        bits = _GLYPHS.get("?", 0x1F1F1F1F1F)
    fill = bytes(color)
    for gy in range(7):
        row_bits = (bits >> ((6 - gy) * 5)) & 0x1F
        for gx in range(5):
            if not (row_bits & (1 << (4 - gx))):
                continue
            for dy in range(scale):
                for dx in range(scale):
                    px = x + gx * scale + dx
                    py = y + gy * scale + dy
                    off = (py * width + px) * 3
                    if 0 <= off <= len(pixels) - 3:
                        pixels[off : off + 3] = fill


# 5x7 glyphs, 7 rows of 5 bits packed in a 35-bit int (row-major, MSB left).
_GLYPHS: dict[str, int] = {
    " ": 0,
    "0": 0b01110100011001110101100101000101110,
    "1": 0b00100011000010000100001000010001110,
    "2": 0b01110100010000100010001000100011111,
    "3": 0b01110100010000100110000011000101110,
    "4": 0b00010001100101010010111110001000010,
    "5": 0b11111100001111000001000011000101110,
    "6": 0b00110010001000011110100011000101110,
    "7": 0b11111000010001000100010000100001000,
    "8": 0b01110100011000101110100011000101110,
    "9": 0b01110100011000101111000010001001100,
    "A": 0b00100010101000110001111111000110001,
    "B": 0b11110010011001011110010011001011110,
    "C": 0b01110100011000010000100001000101110,
    "D": 0b11100010011000110001100011001011100,
    "E": 0b11111100001100011110100001000011111,
    "F": 0b11111100001100011110100001000010000,
    "G": 0b01110100011000010000101111000101110,
    "H": 0b10001100011000111111100011000110001,
    "I": 0b01110001000010000100001000010001110,
    "J": 0b00111000100001000010000101001001100,
    "K": 0b10001100101010011000101001001010001,
    "L": 0b10000100001000010000100001000011111,
    "M": 0b10001110111010110101100011000110001,
    "N": 0b10001100011100110101100111000110001,
    "O": 0b01110100011000110001100011000101110,
    "P": 0b11110100011000111110100001000010000,
    "Q": 0b01110100011000110001101011001001101,
    "R": 0b11110100011000111110101001001010001,
    "S": 0b01110100011000001110000011000101110,
    "T": 0b11111001000010000100001000010000100,
    "U": 0b10001100011000110001100011000101110,
    "V": 0b10001100011000110001100010101000100,
    "W": 0b10001100011000110101101011101110001,
    "X": 0b10001100010101000100010101000110001,
    "Y": 0b10001100010101000100001000010000100,
    "Z": 0b11111000010001000100010001000011111,
    ".": 0b00000000000000000000000000010000100,
    ",": 0b00000000000000000000000000010000100,
    "-": 0b00000000000000001110000000000000000,
    ":": 0b00000001000000000000001000000000000,
    "?": 0b01110100010000100010001000000000100,
}


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
