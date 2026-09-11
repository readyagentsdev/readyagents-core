"""Attach declared media to an agent request. Capability-checked before spend."""

from __future__ import annotations

from typing import Any

from readyagents.errors import CapabilityError, MediaError, TemplateError
from readyagents.llm.base import Message
from readyagents.llm.capabilities import assert_capable, load_capability_matrix
from readyagents.media.caps import caps_from
from readyagents.media.cost import note_media_spend
from readyagents.media.ingest import ingest_bytes, ingest_path, store_from
from readyagents.media.part import MediaPart, is_media_ref, media_ref, part_from_mapping
from readyagents.media.png import is_png, read_rgb_png, resize_nearest, write_rgb_png
from readyagents.media.redact import maybe_redact
from readyagents.workflow.templates import interpolate, interpolate_value, lookup


def attach_media(
    node: Any,
    state: Any,
    ctx: Any,
    messages: list[Message],
) -> list[Message]:
    """Validate + attach. No-op (same list) when the node declares no media."""
    declared = list(getattr(node, "media", None) or [])
    if not declared:
        return messages
    refs = resolve_media_list(declared, state, ctx, node=node)
    if not refs:
        raise MediaError(f"Node '{getattr(node, 'id', '?')}': media list resolved empty")
    model = getattr(node, "model", None) or getattr(ctx, "default_model", None) or ""
    matrix = getattr(ctx, "capability_matrix", None) or load_capability_matrix()
    ctx.capability_matrix = matrix
    try:
        assert_capable(str(model), {"media": True}, matrix=matrix)
    except CapabilityError:
        raise
    ceiling = caps_from(getattr(ctx, "workflow", None)).downscale_max_edge
    attached: list[dict[str, Any]] = []
    parts: list[MediaPart] = []
    for ref in refs:
        part = part_from_mapping(ref)
        if part is None:
            continue
        part = maybe_redact(part, ctx=ctx, node=node)
        part = downscale_part(part, ctx=ctx, max_edge=ceiling)
        attached.append(part.as_ref())
        parts.append(part)
    note_media_spend(state, parts)
    if not messages:
        return messages
    out = list(messages)
    last = out[-1]
    out[-1] = Message(
        role=last.role,
        content=last.content,
        tool_calls=list(last.tool_calls or []),
        tool_call_id=last.tool_call_id,
        name=last.name,
        media=attached,
    )
    return out


def resolve_media_list(
    declared: list[Any],
    state: Any,
    ctx: Any,
    *,
    node: Any = None,
) -> list[dict[str, Any]]:
    ns = state.mapping() if hasattr(state, "mapping") else {}
    workspace = getattr(ctx, "workflow_dir", None)
    found: list[dict[str, Any]] = []
    for item in declared:
        value = interpolate_value(item, ns) if isinstance(item, (dict, list)) else None
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith("{{") and stripped.endswith("}}"):
                path = stripped[2:-2].strip().split("|", 1)[0].strip()
                try:
                    value = lookup(ns, path)
                except Exception as exc:  # noqa: BLE001
                    raise TemplateError(str(exc)) from exc
            else:
                value = interpolate(item, ns)
        if isinstance(value, list):
            for inner in value:
                ref = _coerce_ref(inner, ctx=ctx, workspace=workspace, node=node)
                if ref is not None:
                    found.append(ref)
            continue
        ref = _coerce_ref(value, ctx=ctx, workspace=workspace, node=node)
        if ref is not None:
            found.append(ref)
    return found


def downscale_part(part: MediaPart, *, ctx: Any, max_edge: int) -> MediaPart:
    width = part.width or 0
    height = part.height or 0
    if width <= 0 or height <= 0:
        return part
    edge = max(width, height)
    if edge <= max_edge:
        return part
    scale = max_edge / float(edge)
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    store = store_from(ctx)
    data = store.get(part.sha256)
    if not is_png(data):
        from readyagents.errors import missing_extra_message

        raise MediaError(missing_extra_message("image", "image"))
    src_w, src_h, pixels = read_rgb_png(data)
    resized = write_rgb_png(new_w, new_h, resize_nearest(src_w, src_h, bytes(pixels), new_w, new_h))
    scaled = ingest_bytes(
        resized,
        ctx=ctx,
        source=str((part.provenance or {}).get("source") or "downscale"),
        kind=part.kind,
        mime="image/png",
        page_index=part.page_index,
    )
    scaled.redaction = part.redaction
    return scaled


def _coerce_ref(value: Any, *, ctx: Any, workspace: Any, node: Any) -> dict[str, Any] | None:
    if isinstance(value, MediaPart):
        return maybe_redact(value, ctx=ctx, node=node).as_ref()
    if is_media_ref(value):
        part = part_from_mapping(value)
        if part is None:
            return dict(value)
        return maybe_redact(part, ctx=ctx, node=node).as_ref()
    if isinstance(value, dict) and "image" in value and is_media_ref(value.get("image")):
        return _coerce_ref(value.get("image"), ctx=ctx, workspace=workspace, node=node)
    if isinstance(value, (bytes, bytearray)):
        part = ingest_bytes(bytes(value), ctx=ctx, source="bytes")
        return maybe_redact(part, ctx=ctx, node=node).as_ref()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith("{") or "\n" in text[:8]:
            return None
        part = ingest_path(text, ctx=ctx, workspace=workspace, source="file")
        return maybe_redact(part, ctx=ctx, node=node).as_ref()
    return media_ref(value)
