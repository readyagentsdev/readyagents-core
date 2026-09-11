"""type: document — PDF to ordered page parts (text, image, citable page index)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import MediaError, MediaMalformed
from readyagents.media.caps import caps_from
from readyagents.media.cost import note_media_spend
from readyagents.media.ingest import ingest_bytes, ingest_path, store_from
from readyagents.media.part import is_media_ref, part_from_mapping
from readyagents.media.pdf import extract_with_pypdf, is_pdf, split_simple_pdf
from readyagents.media.png import write_rgb_png
from readyagents.media.redact import maybe_redact
from readyagents.workflow.templates import interpolate, lookup


def run_document_node(node: Any, state: Any, ctx: Any) -> Any:
    caps = caps_from(getattr(ctx, "workflow", None), getattr(node, "render", None))
    if getattr(ctx, "offline", False) and getattr(ctx, "cassette", None) is not None:
        return _replay(node, ctx)
    source = _resolve_source(node, state, ctx)
    pages = _split(source, caps=caps)
    out: list[dict[str, Any]] = []
    parts = []
    for row in pages:
        image = _page_image(row["page"], row.get("text") or "", caps=caps)
        ingested = ingest_bytes(
            image,
            ctx=ctx,
            caps=caps,
            source="document",
            kind="page",
            mime="image/png",
            page_index=row["index"],
        )
        ingested = maybe_redact(ingested, ctx=ctx, node=node)
        parts.append(ingested)
        out.append(
            {
                "index": row["index"],
                "page": row["page"],
                "text": row.get("text") or "",
                "image": ingested.as_ref(),
            }
        )
    note_media_spend(state, parts)
    result = out
    _record(node, ctx, result)
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "page_count": len(out)}
    return result


def _resolve_source(node: Any, state: Any, ctx: Any) -> bytes:
    ns = state.mapping()
    raw = getattr(node, "source", None)
    if not raw:
        raise MediaError(f"Node '{node.id}': document nodes require 'source'")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            path = stripped[2:-2].strip().split("|", 1)[0].strip()
            value = lookup(ns, path)
        else:
            value = interpolate(stripped, ns)
    else:
        value = raw
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif is_media_ref(value) or hasattr(value, "sha256"):
        part = part_from_mapping(value)
        if part is None:
            raise MediaMalformed("document source is not a media part")
        data = store_from(ctx).get(part.sha256)
    elif isinstance(value, str) and value.strip():
        part = ingest_path(
            value.strip(),
            ctx=ctx,
            workspace=getattr(ctx, "workflow_dir", None) or Path.cwd(),
            caps=caps_from(getattr(ctx, "workflow", None), getattr(node, "render", None)),
            source="document",
        )
        data = store_from(ctx).get(part.sha256)
    else:
        raise MediaMalformed("document source did not resolve to a PDF")
    if not is_pdf(data):
        raise MediaMalformed("not a PDF")
    size_cap = caps_from(getattr(ctx, "workflow", None), getattr(node, "render", None))
    if len(data) > size_cap.max_bytes_per_part:
        from readyagents.errors import MediaSizeExceeded

        raise MediaSizeExceeded(len(data), size_cap.max_bytes_per_part)
    return data


def _split(data: bytes, *, caps: Any) -> list[dict[str, Any]]:
    extra = extract_with_pypdf(data, max_pages=caps.max_pages)
    if extra is not None:
        return extra
    return split_simple_pdf(data, max_pages=caps.max_pages)


def _page_image(page: int, text: str, *, caps: Any) -> bytes:
    """Deterministic page PNG. Plumbing, not a renderer accuracy claim."""
    width = 64
    height = 64
    pixels = bytearray(width * height * 3)
    # white background
    for i in range(0, len(pixels), 3):
        pixels[i : i + 3] = b"\xff\xff\xff"
    # unique bar from page index (citable, content-addressed)
    tone = 32 + (int(page) * 17) % 200
    for x in range(width):
        for y in range(8):
            off = (y * width + x) * 3
            pixels[off] = tone
            pixels[off + 1] = (tone * 3) % 256
            pixels[off + 2] = 80
    _ = (text, caps)
    return write_rgb_png(width, height, bytes(pixels))


def _record(node: Any, ctx: Any, output: Any) -> None:
    cassette = getattr(ctx, "cassette", None)
    if cassette is None or not getattr(ctx, "recording", False):
        return
    if hasattr(cassette, "record_document"):
        cassette.record_document(node_id=node.id, output=output)


def _replay(node: Any, ctx: Any) -> Any:
    cassette = ctx.cassette
    if hasattr(cassette, "replay_document"):
        return cassette.replay_document(node_id=node.id)
    raise MediaError(f"Node '{node.id}': cassette has no document entry")
