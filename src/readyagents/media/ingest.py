"""Ingest untrusted media: size caps before decode, strip metadata, hash-store."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from readyagents.errors import (
    MediaBomb,
    MediaBudgetExceeded,
    MediaDimensionExceeded,
    MediaMalformed,
    MediaSizeExceeded,
    PathError,
)
from readyagents.media.caps import BOMB_PIXELS, BOMB_RATIO, MediaCaps, caps_from
from readyagents.media.cost import account_part
from readyagents.media.jpeg import is_jpeg, jpeg_dimensions, strip_jpeg_metadata
from readyagents.media.part import MediaPart
from readyagents.media.png import is_png, png_dimensions, strip_png_metadata
from readyagents.media.store import MediaStore
from readyagents.media.wav import check_duration, is_wav, wav_duration_ms
from readyagents.paths import resolve_within

_CURRENT_CTX: ContextVar[Any] = ContextVar("readyagents_media_ctx", default=None)


def bind_run_ctx(ctx: Any) -> Any:
    return _CURRENT_CTX.set(ctx)


def reset_run_ctx(token: Any) -> None:
    if token is not None:
        _CURRENT_CTX.reset(token)


def current_ctx() -> Any:
    return _CURRENT_CTX.get()


_PDF_MAGIC = b"%PDF"
_SNIFF: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"%PDF", "document", "application/pdf"),
    (b"RIFF", "audio", "audio/wav"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"ftyp", "video", "video/mp4"),
]


def store_from(ctx: Any) -> MediaStore:
    if ctx is None:
        ctx = current_ctx()
    existing = getattr(ctx, "media_store", None) if ctx is not None else None
    if existing is not None:
        return existing
    home = getattr(ctx, "pin_home", None) if ctx is not None else None
    if home is None:
        from readyagents.config import get_settings

        home = get_settings().home_path()
    store = MediaStore(Path(home) / "media")
    cassette = getattr(ctx, "cassette", None) if ctx is not None else None
    blobs = getattr(cassette, "media_blobs", None) if cassette is not None else None
    if isinstance(blobs, dict):
        for digest, data in blobs.items():
            if isinstance(data, (bytes, bytearray)):
                store.put(bytes(data), sha256=str(digest))
    if ctx is not None:
        ctx.media_store = store
    return store


def ingest_bytes(
    data: bytes,
    *,
    ctx: Any,
    caps: MediaCaps | None = None,
    source: str = "ingest",
    kind: str | None = None,
    mime: str | None = None,
    page_index: int | None = None,
) -> MediaPart:
    caps = caps or caps_from(getattr(ctx, "workflow", None))
    if len(data) > caps.max_bytes_per_part:
        raise MediaSizeExceeded(len(data), caps.max_bytes_per_part)
    if not data:
        raise MediaMalformed("empty media")
    sniffed_kind, sniffed_mime = sniff(data)
    kind = kind or sniffed_kind
    mime = mime or sniffed_mime
    width = height = duration_ms = None
    cleaned = data
    if is_png(data):
        width, height = png_dimensions(data)
        _check_pixels(width, height, len(data), caps)
        cleaned = strip_png_metadata(data)
    elif is_jpeg(data):
        width, height = jpeg_dimensions(data)
        _check_pixels(width, height, len(data), caps)
        cleaned = strip_jpeg_metadata(data)
    elif is_wav(data):
        duration_ms = wav_duration_ms(data)
        check_duration(duration_ms, caps.max_duration_ms)
        kind = "audio"
        mime = "audio/wav"
    elif data.lstrip().startswith(_PDF_MAGIC):
        kind = "document"
        mime = "application/pdf"
        from readyagents.media.pdf import count_pages

        pages = count_pages(data)
        if pages < 1:
            raise MediaMalformed("malformed PDF container")
    elif sniffed_kind == "video":
        kind = "video"
    elif kind == "image":
        raise MediaMalformed("malformed image container")
    store = store_from(ctx)
    if store.used_bytes() + len(cleaned) > caps.max_run_bytes:
        raise MediaBudgetExceeded(store.used_bytes() + len(cleaned), caps.max_run_bytes)
    digest = hashlib.sha256(cleaned).hexdigest()
    store.put(cleaned, sha256=digest)
    cassette = getattr(ctx, "cassette", None)
    if cassette is not None:
        remember_cassette_blob(cassette, digest, cleaned)
    part = MediaPart(
        kind=kind,
        mime=mime,
        sha256=digest,
        bytes_len=len(cleaned),
        path=str(store.path_for(digest)),
        width=width,
        height=height,
        duration_ms=duration_ms,
        page_index=page_index,
        provenance={"trust": "untrusted", "source": source},
        metadata_stripped=True,
    )
    return account_part(part)


def ingest_path(
    path: Path | str,
    *,
    ctx: Any,
    workspace: Path | str | None = None,
    caps: MediaCaps | None = None,
    source: str = "file",
) -> MediaPart:
    fallback = getattr(ctx, "workflow_dir", None) or "."
    root = Path(workspace) if workspace is not None else Path(fallback)
    try:
        resolved = resolve_within(path, root, must_exist=True, what="media source")
    except PathError as exc:
        raise MediaMalformed(str(exc)) from exc
    size = resolved.stat().st_size
    caps = caps or caps_from(getattr(ctx, "workflow", None))
    if size > caps.max_bytes_per_part:
        raise MediaSizeExceeded(int(size), caps.max_bytes_per_part)
    data = resolved.read_bytes()
    return ingest_bytes(data, ctx=ctx, caps=caps, source=source)


def sniff(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG"):
        return "image", "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image", "image/jpeg"
    if data.lstrip().startswith(b"%PDF"):
        return "document", "application/pdf"
    if is_wav(data):
        return "audio", "audio/wav"
    if data.startswith(b"GIF8"):
        return "image", "image/gif"
    if len(data) > 8 and data[4:8] == b"ftyp":
        return "video", "video/mp4"
    return "binary", "application/octet-stream"


def remember_cassette_blob(cassette: Any, sha256: str, data: bytes) -> None:
    blobs = getattr(cassette, "media_blobs", None)
    if blobs is None:
        cassette.media_blobs = {}
        blobs = cassette.media_blobs
    blobs[sha256] = data


def _check_pixels(width: int, height: int, nbytes: int, caps: MediaCaps) -> None:
    pixels = width * height
    if pixels >= BOMB_PIXELS or (pixels > caps.max_pixels and nbytes * BOMB_RATIO < pixels):
        raise MediaBomb(
            f"media decompression bomb refused: {width}x{height} from {nbytes} compressed bytes"
        )
    if width > caps.max_width or height > caps.max_height or pixels > caps.max_pixels:
        raise MediaDimensionExceeded(
            width,
            height,
            limit_width=caps.max_width,
            limit_height=caps.max_height,
        )
