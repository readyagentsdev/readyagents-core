"""Load stored media bytes into vendor request payloads. Text-only path unchanged."""

from __future__ import annotations

import base64
from typing import Any

from readyagents.media.ingest import current_ctx, store_from
from readyagents.media.part import part_from_mapping


def load_media_bytes(ref: Any, *, store: Any = None) -> tuple[str, bytes]:
    """Return (mime, bytes) for a media ref. Bytes come from the hash store."""
    part = part_from_mapping(ref)
    if part is None:
        raise ValueError("not a media part")
    blob_store = store if store is not None else store_from(current_ctx())
    return part.mime or "application/octet-stream", blob_store.get(part.sha256)


def openai_content(message: Any, *, store: Any = None) -> str | list[dict[str, Any]]:
    """OpenAI Chat Completions content: string when no media, parts when attached."""
    media = list(getattr(message, "media", None) or [])
    text = getattr(message, "content", None) or ""
    if not media:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for ref in media:
        mime, data = load_media_bytes(ref, store=store)
        b64 = base64.b64encode(data).decode("ascii")
        if mime.startswith("audio/"):
            fmt = "wav" if "wav" in mime else mime.split("/", 1)[-1]
            blocks.append({"type": "input_audio", "input_audio": {"data": b64, "format": fmt}})
        else:
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
    return blocks


def anthropic_content(message: Any, *, store: Any = None) -> str | list[dict[str, Any]]:
    media = list(getattr(message, "media", None) or [])
    text = getattr(message, "content", None) or ""
    if not media:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for ref in media:
        mime, data = load_media_bytes(ref, store=store)
        b64 = base64.b64encode(data).decode("ascii")
        if mime.startswith("image/"):
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                }
            )
        else:
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                }
            )
    return blocks


def gemini_parts(message: Any, *, store: Any = None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = getattr(message, "content", None) or ""
    if text:
        parts.append({"text": text})
    for ref in getattr(message, "media", None) or []:
        mime, data = load_media_bytes(ref, store=store)
        parts.append(
            {
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
        )
    return parts or [{"text": ""}]


def bedrock_content(message: Any, *, store: Any = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = getattr(message, "content", None) or ""
    if text:
        blocks.append({"text": text})
    for ref in getattr(message, "media", None) or []:
        mime, data = load_media_bytes(ref, store=store)
        if mime.startswith("image/"):
            fmt = mime.split("/", 1)[-1].replace("jpeg", "jpeg")
            if fmt == "jpg":
                fmt = "jpeg"
            blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
        elif mime.startswith("audio/"):
            fmt = "wav" if "wav" in mime else mime.split("/", 1)[-1]
            blocks.append({"audio": {"format": fmt, "source": {"bytes": data}}})
        else:
            blocks.append({"document": {"format": "pdf", "source": {"bytes": data}}})
    return blocks or [{"text": ""}]


def vertex_inline_parts(message: Any, *, store: Any = None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    text = getattr(message, "content", None) or ""
    if text:
        parts.append({"text": text})
    for ref in getattr(message, "media", None) or []:
        mime, data = load_media_bytes(ref, store=store)
        parts.append({"mime_type": mime, "data": data})
    return parts
