"""Engine-enforced media redaction. Applied before persist, record, or send."""

from __future__ import annotations

from typing import Any

from readyagents.errors import MediaError, missing_extra_message
from readyagents.media.ingest import ingest_bytes, store_from
from readyagents.media.part import MediaPart, part_from_mapping
from readyagents.media.png import fill_box, is_png, read_rgb_png, write_rgb_png


def redact_part(
    part: MediaPart,
    *,
    ctx: Any,
    regions: list[dict[str, Any]] | None = None,
    classes: list[str] | None = None,
) -> MediaPart:
    """Blur/box declared regions and opt-in detected classes. Records what was removed."""
    wanted_regions = list(regions or [])
    wanted_classes = [str(item).strip().lower() for item in (classes or []) if str(item).strip()]
    detector = getattr(ctx, "redact_detector", None)
    detected: list[dict[str, Any]] = []
    if wanted_classes:
        if detector is None:
            try:
                detector = _pillow_detector()
            except Exception as exc:  # noqa: BLE001
                raise MediaError(missing_extra_message("image detection", "image")) from exc
        store = store_from(ctx)
        raw = store.get(part.sha256)
        found = detector(part, raw) or []
        for item in found:
            if not isinstance(item, dict):
                continue
            klass = str(item.get("class") or item.get("kind") or "").strip().lower()
            if klass and klass not in wanted_classes:
                continue
            detected.append(item)
            wanted_regions.append(item)
    if not wanted_regions:
        return part
    store = store_from(ctx)
    data = store.get(part.sha256)
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
    redacted = ingest_bytes(
        boxed,
        ctx=ctx,
        source=str((part.provenance or {}).get("source") or "redact"),
        kind=part.kind,
        mime="image/png",
        page_index=part.page_index,
    )
    redacted.redaction = {"regions": applied, "classes": wanted_classes}
    redacted.width = width
    redacted.height = height
    return redacted


def spec_from_node(node: Any) -> tuple[list[dict[str, Any]], list[str]]:
    raw = getattr(node, "media_redact", None) or getattr(node, "redact", None)
    if not isinstance(raw, dict):
        return [], []
    regions = [row for row in list(raw.get("regions") or []) if isinstance(row, dict)]
    classes = [str(item) for item in list(raw.get("classes") or [])]
    return regions, classes


def maybe_redact(part: MediaPart, *, ctx: Any, node: Any = None) -> MediaPart:
    regions, classes = spec_from_node(node) if node is not None else ([], [])
    policy = getattr(getattr(ctx, "workflow", None), "media", None)
    if policy is not None:
        extra = getattr(policy, "redact", None)
        if isinstance(extra, dict):
            extra_regions = list(extra.get("regions") or [])
            regions = regions + [row for row in extra_regions if isinstance(row, dict)]
            classes = classes + [str(item) for item in list(extra.get("classes") or [])]
    if not regions and not classes:
        return part
    return redact_part(part, ctx=ctx, regions=regions, classes=classes)


def _pillow_detector() -> Any:
    raise MediaError(missing_extra_message("image detection", "image"))


def as_part(value: Any) -> MediaPart | None:
    return part_from_mapping(value)
