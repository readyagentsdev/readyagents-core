"""Typed media part. State and records store a hash ref, never raw bytes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MEDIA_MARKER = "_media"
KINDS = frozenset({"image", "audio", "video", "document", "page", "binary"})


@dataclass
class MediaPart:
    """Content-addressed media. Bytes live in the hash store, not in run state."""

    kind: str
    mime: str
    sha256: str
    bytes_len: int
    path: str | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    page_index: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata_stripped: bool = True
    redaction: dict[str, Any] | None = None
    tokens: int | None = None
    cost_micros: int | None = None

    def as_ref(self) -> dict[str, Any]:
        """JSON-safe hash reference stored in run state, cassette, and evidence."""
        row: dict[str, Any] = {
            MEDIA_MARKER: True,
            "kind": self.kind,
            "mime": self.mime,
            "sha256": self.sha256,
            "bytes_len": int(self.bytes_len),
            "metadata_stripped": bool(self.metadata_stripped),
        }
        if self.path:
            row["path"] = self.path
        if self.width is not None:
            row["width"] = int(self.width)
        if self.height is not None:
            row["height"] = int(self.height)
        if self.duration_ms is not None:
            row["duration_ms"] = int(self.duration_ms)
        if self.page_index is not None:
            row["page_index"] = int(self.page_index)
        if self.provenance:
            row["provenance"] = dict(self.provenance)
        if self.redaction:
            row["redaction"] = dict(self.redaction)
        if self.tokens is not None:
            row["tokens"] = int(self.tokens)
        if self.cost_micros is not None:
            row["cost_micros"] = int(self.cost_micros)
        return row


def is_media_ref(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and MEDIA_MARKER in text:
            try:
                import json

                value = json.loads(text)
            except (TypeError, ValueError):
                return False
    return isinstance(value, dict) and value.get(MEDIA_MARKER) is True and "sha256" in value


def media_ref(value: Any) -> dict[str, Any] | None:
    if isinstance(value, MediaPart):
        return value.as_ref()
    if is_media_ref(value):
        return dict(value)
    return None


def part_from_mapping(raw: Any) -> MediaPart | None:
    if isinstance(raw, MediaPart):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                import json

                raw = json.loads(text)
            except (TypeError, ValueError):
                return None
    if not is_media_ref(raw):
        return None
    return MediaPart(
        kind=str(raw.get("kind") or "binary"),
        mime=str(raw.get("mime") or "application/octet-stream"),
        sha256=str(raw["sha256"]),
        bytes_len=int(raw.get("bytes_len") or 0),
        path=str(raw["path"]) if raw.get("path") else None,
        width=int(raw["width"]) if raw.get("width") is not None else None,
        height=int(raw["height"]) if raw.get("height") is not None else None,
        duration_ms=int(raw["duration_ms"]) if raw.get("duration_ms") is not None else None,
        page_index=int(raw["page_index"]) if raw.get("page_index") is not None else None,
        provenance=dict(raw.get("provenance") or {}),
        metadata_stripped=bool(raw.get("metadata_stripped", True)),
        redaction=dict(raw["redaction"]) if isinstance(raw.get("redaction"), dict) else None,
        tokens=int(raw["tokens"]) if raw.get("tokens") is not None else None,
        cost_micros=int(raw["cost_micros"]) if raw.get("cost_micros") is not None else None,
    )
