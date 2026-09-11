"""Stdlib PNG plumbing: header, metadata strip, nearest-neighbor resize, box fill."""

from __future__ import annotations

import struct
import zlib
from typing import Any

from readyagents.errors import MediaMalformed

PNG_SIG = b"\x89PNG\r\n\x1a\n"
_TEXT_CHUNKS = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME", b"iCCP"})


def is_png(data: bytes) -> bool:
    return data.startswith(PNG_SIG)


def png_dimensions(data: bytes) -> tuple[int, int]:
    if not is_png(data) or len(data) < 33:
        raise MediaMalformed("not a PNG")
    length = int.from_bytes(data[8:12], "big")
    if data[12:16] != b"IHDR" or length < 13:
        raise MediaMalformed("PNG missing IHDR")
    width, height = struct.unpack(">II", data[16:24])
    if width < 1 or height < 1:
        raise MediaMalformed("PNG has invalid dimensions")
    return int(width), int(height)


def strip_png_metadata(data: bytes) -> bytes:
    if not is_png(data):
        raise MediaMalformed("not a PNG")
    out = bytearray(PNG_SIG)
    for kind, payload in _chunks(data):
        if kind in _TEXT_CHUNKS:
            continue
        out.extend(_chunk(kind, payload))
    return bytes(out)


def write_rgb_png(width: int, height: int, pixels: bytes) -> bytes:
    """Uncompressed-filter RGB8 PNG. ``pixels`` is width*height*3 bytes, row-major."""
    if width < 1 or height < 1:
        raise MediaMalformed("PNG has invalid dimensions")
    expected = width * height * 3
    if len(pixels) != expected:
        raise MediaMalformed("PNG pixel buffer size mismatch")
    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        rows.extend(pixels[y * stride : (y + 1) * stride])
    compressed = zlib.compress(bytes(rows), 9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = bytearray(PNG_SIG)
    out.extend(_chunk(b"IHDR", ihdr))
    out.extend(_chunk(b"IDAT", compressed))
    out.extend(_chunk(b"IEND", b""))
    return bytes(out)


def read_rgb_png(data: bytes) -> tuple[int, int, bytearray]:
    """Decode color-type 2 (RGB8) PNGs. Other types raise MediaMalformed."""
    width, height, bit_depth, color_type = _ihdr_fields(data)
    if bit_depth != 8 or color_type != 2:
        raise MediaMalformed("PNG color type is not RGB8 (install readyagentsdev[image])")
    raw = zlib.decompress(_idat(data))
    stride = width * 3
    pixels = bytearray(width * height * 3)
    src = 0
    for y in range(height):
        if src >= len(raw):
            raise MediaMalformed("truncated PNG IDAT")
        filter_id = raw[src]
        src += 1
        row = raw[src : src + stride]
        if len(row) < stride:
            raise MediaMalformed("truncated PNG row")
        src += stride
        if filter_id == 0:
            decoded = bytearray(row)
        elif filter_id == 1:
            decoded = _unfilter_sub(row, 3)
        elif filter_id == 2:
            prior = pixels[(y - 1) * stride : y * stride] if y else bytes(stride)
            decoded = _unfilter_up(row, prior)
        else:
            raise MediaMalformed(f"unsupported PNG filter {filter_id}")
        pixels[y * stride : (y + 1) * stride] = decoded
    return width, height, pixels


def resize_nearest(width: int, height: int, pixels: bytes, new_w: int, new_h: int) -> bytes:
    if new_w < 1 or new_h < 1:
        raise MediaMalformed("invalid resize target")
    out = bytearray(new_w * new_h * 3)
    for y in range(new_h):
        src_y = min(height - 1, y * height // new_h)
        for x in range(new_w):
            src_x = min(width - 1, x * width // new_w)
            s = (src_y * width + src_x) * 3
            d = (y * new_w + x) * 3
            out[d : d + 3] = pixels[s : s + 3]
    return bytes(out)


def fill_box(
    width: int,
    height: int,
    pixels: bytearray,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int] = (0, 0, 0),
) -> None:
    x0 = max(0, int(x))
    y0 = max(0, int(y))
    x1 = min(width, x0 + max(0, int(w)))
    y1 = min(height, y0 + max(0, int(h)))
    fill = bytes(color)
    for yy in range(y0, y1):
        start = (yy * width + x0) * 3
        for xx in range(x1 - x0):
            off = start + xx * 3
            pixels[off : off + 3] = fill


def _ihdr_fields(data: bytes) -> tuple[int, int, int, int]:
    if not is_png(data) or len(data) < 33:
        raise MediaMalformed("not a PNG")
    if data[12:16] != b"IHDR":
        raise MediaMalformed("PNG missing IHDR")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return int(width), int(height), int(bit_depth), int(color_type)


def _chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not is_png(data):
        raise MediaMalformed("not a PNG")
    chunks: list[tuple[bytes, bytes]] = []
    i = 8
    while i + 12 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        kind = data[i + 4 : i + 8]
        start = i + 8
        end = start + length
        if end + 4 > len(data):
            raise MediaMalformed("truncated PNG chunk")
        payload = data[start:end]
        crc = int.from_bytes(data[end : end + 4], "big")
        expect = zlib.crc32(kind + payload) & 0xFFFFFFFF
        if crc != expect:
            raise MediaMalformed("PNG CRC mismatch")
        chunks.append((kind, payload))
        i = end + 4
        if kind == b"IEND":
            break
    if not chunks or chunks[-1][0] != b"IEND":
        raise MediaMalformed("PNG missing IEND")
    return chunks


def _idat(data: bytes) -> bytes:
    parts = [payload for kind, payload in _chunks(data) if kind == b"IDAT"]
    if not parts:
        raise MediaMalformed("PNG missing IDAT")
    return b"".join(parts)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _unfilter_sub(row: bytes, bpp: int) -> bytearray:
    out = bytearray(row)
    for i in range(bpp, len(out)):
        out[i] = (out[i] + out[i - bpp]) & 0xFF
    return out


def _unfilter_up(row: bytes, prior: Any) -> bytearray:
    out = bytearray(len(row))
    for i, value in enumerate(row):
        out[i] = (value + prior[i]) & 0xFF
    return out
