"""Per-part token and cost estimates. Governance accounting, not a price quote."""

from __future__ import annotations

from typing import Any

from readyagents.media.part import MediaPart, is_media_ref, part_from_mapping

TILE = 512
TOKENS_PER_TILE = 170
TOKENS_PER_AUDIO_SECOND = 10
TOKENS_PER_PAGE = 80
MICROS_PER_TOKEN = 50  # $0.00005 — recorded estimate, not a vendor rate


def estimate_tokens(part: MediaPart) -> int:
    if part.kind in {"image", "page"}:
        width = int(part.width or TILE)
        height = int(part.height or TILE)
        tiles = max(1, (max(1, width) + TILE - 1) // TILE) * max(
            1, (max(1, height) + TILE - 1) // TILE
        )
        return tiles * TOKENS_PER_TILE
    if part.kind == "audio":
        seconds = max(1, int((part.duration_ms or 1000) / 1000))
        return seconds * TOKENS_PER_AUDIO_SECOND
    if part.kind == "document":
        return TOKENS_PER_PAGE
    return max(1, int(part.bytes_len or 0) // 1024)


def estimate_cost_micros(tokens: int) -> int:
    return max(0, int(tokens) * MICROS_PER_TOKEN)


def account_part(part: MediaPart) -> MediaPart:
    tokens = estimate_tokens(part)
    part.tokens = tokens
    part.cost_micros = estimate_cost_micros(tokens)
    return part


def note_media_spend(state: Any, parts: list[Any]) -> None:
    """Fold per-part tokens/cost into run usage and metadata.media_parts."""
    rows: list[dict[str, Any]] = []
    tokens = 0
    cost = 0
    nbytes = 0
    for raw in parts:
        part = part_from_mapping(raw) if not isinstance(raw, MediaPart) else raw
        if part is None and is_media_ref(raw):
            part = part_from_mapping(raw)
        if part is None:
            continue
        if part.tokens is None:
            account_part(part)
        rows.append(
            {
                "sha256": part.sha256,
                "kind": part.kind,
                "tokens": int(part.tokens or 0),
                "cost_micros": int(part.cost_micros or 0),
                "bytes_len": int(part.bytes_len),
            }
        )
        tokens += int(part.tokens or 0)
        cost += int(part.cost_micros or 0)
        nbytes += int(part.bytes_len)
    if not rows:
        return
    meta = getattr(state, "metadata", None)
    if isinstance(meta, dict):
        existing = list(meta.get("media_parts") or [])
        existing.extend(rows)
        meta["media_parts"] = existing
    if hasattr(state, "add_usage"):
        state.add_usage(media_tokens=tokens, media_cost_micros=cost, media_bytes=nbytes)
    if hasattr(state, "note_node_usage"):
        state.note_node_usage(
            {"media_tokens": tokens, "media_cost_micros": cost, "media_bytes": nbytes}
        )
