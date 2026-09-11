"""Engine-enforced media redaction. Applied before persist, record, or send."""

from __future__ import annotations

from typing import Any

from readyagents.errors import MediaError, missing_extra_message
from readyagents.media.part import MediaPart, is_media_ref, part_from_mapping
from readyagents.media.png import fill_box, is_png, read_rgb_png, write_rgb_png


def spec_from_node(node: Any) -> tuple[list[dict[str, Any]], list[str]]:
    raw = getattr(node, "media_redact", None) or getattr(node, "redact", None)
    if not isinstance(raw, dict):
        return [], []
    regions = [row for row in list(raw.get("regions") or []) if isinstance(row, dict)]
    classes = [str(item) for item in list(raw.get("classes") or [])]
    return regions, classes


def spec_from_ctx_node(ctx: Any, node: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    regions, classes = spec_from_node(node) if node is not None else ([], [])
    policy = getattr(getattr(ctx, "workflow", None), "media", None)
    extra = getattr(policy, "redact", None) if policy is not None else None
    if isinstance(extra, dict):
        extra_regions = list(extra.get("regions") or [])
        regions = regions + [row for row in extra_regions if isinstance(row, dict)]
        classes = classes + [str(item) for item in list(extra.get("classes") or [])]
    return regions, classes


def apply_redaction_bytes(
    data: bytes,
    *,
    ctx: Any,
    regions: list[dict[str, Any]] | None = None,
    classes: list[str] | None = None,
) -> tuple[bytes, dict[str, Any] | None]:
    """Box declared/detected regions in memory. Does not persist."""
    wanted_regions = list(regions or [])
    wanted_classes = [str(item).strip().lower() for item in (classes or []) if str(item).strip()]
    detector = getattr(ctx, "redact_detector", None)
    if wanted_classes:
        if detector is None:
            try:
                detector = _pillow_detector()
            except Exception as exc:  # noqa: BLE001
                raise MediaError(missing_extra_message("image detection", "image")) from exc
        found = detector(None, data) or []
        for item in found:
            if not isinstance(item, dict):
                continue
            klass = str(item.get("class") or item.get("kind") or "").strip().lower()
            if klass and klass not in wanted_classes:
                continue
            wanted_regions.append(item)
    if not wanted_regions:
        return data, None
    if not is_png(data):
        raise MediaError(missing_extra_message("image", "image"))
    width, height, pixels = read_rgb_png(data)
    applied: list[dict[str, Any]] = []
    for region in wanted_regions:
        x = int(region.get("x") or 0)
        y = int(region.get("y") or 0)
        w = int(region.get("width") or region.get("w") or 0)
        h = int(region.get("height") or region.get("h") or 0)
        fill_box(width, height, pixels, x=x, y=y, w=w, h=h)
        applied.append(
            {
                "x": x,
                "y": y,
                "width": w,
                "height": h,
                "class": str(region.get("class") or "declared"),
            }
        )
    boxed = write_rgb_png(width, height, bytes(pixels))
    return boxed, {"regions": applied, "classes": wanted_classes}


def redact_part(
    part: MediaPart,
    *,
    ctx: Any,
    regions: list[dict[str, Any]] | None = None,
    classes: list[str] | None = None,
) -> MediaPart:
    """Redact then persist the boxed bytes only; purge the unredacted original."""
    from readyagents.media.ingest import ingest_bytes, store_from

    store = store_from(ctx)
    data = store.get(part.sha256)
    boxed, record = apply_redaction_bytes(data, ctx=ctx, regions=regions, classes=classes)
    if record is None:
        return part
    old = part.sha256
    redacted = ingest_bytes(
        boxed,
        ctx=ctx,
        source=str((part.provenance or {}).get("source") or "redact"),
        kind=part.kind,
        mime="image/png",
        page_index=part.page_index,
        redact=False,
    )
    redacted.redaction = record
    redacted.width = part.width
    redacted.height = part.height
    if redacted.sha256 != old:
        _purge_original(ctx, old, redacted)
    return redacted


def maybe_redact(part: MediaPart, *, ctx: Any, node: Any = None) -> MediaPart:
    regions, classes = spec_from_ctx_node(ctx, node)
    if not regions and not classes:
        return part
    return redact_part(part, ctx=ctx, regions=regions, classes=classes)


def _purge_original(ctx: Any, old_sha: str, replacement: MediaPart) -> None:
    from readyagents.media.ingest import forget_cassette_blob, store_from

    store = store_from(ctx)
    store.delete(old_sha)
    cassette = getattr(ctx, "cassette", None)
    if cassette is not None:
        forget_cassette_blob(cassette, old_sha)
    state = getattr(ctx, "usage_state", None)
    if state is not None:
        _rewrite_state_sha(state, old_sha, replacement)


def _rewrite_state_sha(state: Any, old_sha: str, replacement: MediaPart) -> None:
    new_ref = replacement.as_ref()

    def walk(value: Any) -> Any:
        if is_media_ref(value) and str(value.get("sha256") or "") == old_sha:
            return dict(new_ref)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    if getattr(state, "node_outputs", None) is not None:
        state.node_outputs = walk(state.node_outputs)
    if getattr(state, "output_keys", None) is not None:
        state.output_keys = walk(state.output_keys)
    if getattr(state, "inputs", None) is not None:
        state.inputs = walk(state.inputs)
    for result in getattr(state, "results", None) or []:
        object.__setattr__(result, "output", walk(getattr(result, "output", None)))
    meta = getattr(state, "metadata", None)
    if isinstance(meta, dict):
        state.metadata = walk(meta)


def _pillow_detector() -> Any:
    raise MediaError(missing_extra_message("image detection", "image"))


def as_part(value: Any) -> MediaPart | None:
    return part_from_mapping(value)
