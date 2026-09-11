"""Stdlib JPEG plumbing: SOF dimensions and metadata strip. No decode."""

from __future__ import annotations

from readyagents.errors import MediaMalformed

_STANDALONE = frozenset({0xD8, 0xD9, 0x01} | set(range(0xD0, 0xD8)))
_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB})
_DROP = frozenset({0xE1, 0xE2, 0xED, 0xFE})  # EXIF, ICC, IPTC, COM


def is_jpeg(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8")


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not is_jpeg(data):
        raise MediaMalformed("not a JPEG")
    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            raise MediaMalformed("JPEG marker")
        marker = data[i + 1]
        if marker in _STANDALONE:
            if marker == 0xD9:
                break
            i += 2
            continue
        if i + 4 > len(data):
            raise MediaMalformed("truncated JPEG")
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2 or i + 2 + seglen > len(data):
            raise MediaMalformed("truncated JPEG segment")
        if marker in _SOF:
            block = data[i + 4 : i + 2 + seglen]
            if len(block) < 5:
                raise MediaMalformed("truncated JPEG SOF")
            height = int.from_bytes(block[1:3], "big")
            width = int.from_bytes(block[3:5], "big")
            if width < 1 or height < 1:
                raise MediaMalformed("JPEG has invalid dimensions")
            return width, height
        if marker == 0xDA:
            break
        i += 2 + seglen
    raise MediaMalformed("JPEG missing SOF")


def strip_jpeg_metadata(data: bytes) -> bytes:
    if not is_jpeg(data):
        raise MediaMalformed("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i = 2
    while i + 1 < len(data):
        if data[i] != 0xFF:
            out.extend(data[i:])
            break
        marker = data[i + 1]
        if marker == 0xD9:
            out.extend(data[i : i + 2])
            break
        if marker == 0xDA:
            out.extend(data[i:])
            break
        if marker in _STANDALONE:
            out.extend(data[i : i + 2])
            i += 2
            continue
        if i + 4 > len(data):
            out.extend(data[i:])
            break
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        nxt = i + 2 + seglen
        if marker in _DROP:
            i = nxt
            continue
        out.extend(data[i:nxt])
        i = nxt
    return bytes(out)
