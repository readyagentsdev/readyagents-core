"""Declared media caps. Defaults apply when a workflow omits media: policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_BYTES_PER_PART = 10_000_000
DEFAULT_MAX_RUN_BYTES = 50_000_000
DEFAULT_MAX_WIDTH = 8192
DEFAULT_MAX_HEIGHT = 8192
DEFAULT_MAX_PIXELS = 4096 * 4096
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_DURATION_MS = 10 * 60 * 1000
DEFAULT_MAX_BYTES_PER_PAGE = 2_000_000
DEFAULT_DOWNSCALE_MAX_EDGE = 2048
DEFAULT_DPI = 72
BOMB_PIXELS = 32_000_000
BOMB_RATIO = 1000


@dataclass(frozen=True)
class MediaCaps:
    max_bytes_per_part: int = DEFAULT_MAX_BYTES_PER_PART
    max_run_bytes: int = DEFAULT_MAX_RUN_BYTES
    max_width: int = DEFAULT_MAX_WIDTH
    max_height: int = DEFAULT_MAX_HEIGHT
    max_pixels: int = DEFAULT_MAX_PIXELS
    max_pages: int = DEFAULT_MAX_PAGES
    max_duration_ms: int = DEFAULT_MAX_DURATION_MS
    max_bytes_per_page: int = DEFAULT_MAX_BYTES_PER_PAGE
    downscale_max_edge: int = DEFAULT_DOWNSCALE_MAX_EDGE
    dpi: int = DEFAULT_DPI


def caps_from(workflow: Any = None, render: Any = None) -> MediaCaps:
    """Merge workflow.media policy and a document node's render dict."""
    policy = getattr(workflow, "media", None) if workflow is not None else None
    data = policy.model_dump() if policy is not None and hasattr(policy, "model_dump") else {}
    if isinstance(policy, dict):
        data = dict(policy)
    if isinstance(render, dict):
        data = {**data, **render}
    budget = getattr(workflow, "budget", None) if workflow is not None else None
    extra = getattr(budget, "max_media_bytes", None) if budget is not None else None
    if extra is not None:
        data["max_run_bytes"] = extra
    return MediaCaps(
        max_bytes_per_part=_int(
            data.get("max_bytes_per_part"), DEFAULT_MAX_BYTES_PER_PART, minimum=1
        ),
        max_run_bytes=_int(data.get("max_run_bytes"), DEFAULT_MAX_RUN_BYTES, minimum=1),
        max_width=_int(data.get("max_width"), DEFAULT_MAX_WIDTH, minimum=1),
        max_height=_int(data.get("max_height"), DEFAULT_MAX_HEIGHT, minimum=1),
        max_pixels=_int(data.get("max_pixels"), DEFAULT_MAX_PIXELS, minimum=1),
        max_pages=_int(data.get("max_pages"), DEFAULT_MAX_PAGES, minimum=1),
        max_duration_ms=_int(data.get("max_duration_ms"), DEFAULT_MAX_DURATION_MS, minimum=1),
        max_bytes_per_page=_int(
            data.get("max_bytes_per_page"), DEFAULT_MAX_BYTES_PER_PAGE, minimum=1
        ),
        downscale_max_edge=_int(
            data.get("downscale_max_edge") or data.get("max_edge"),
            DEFAULT_DOWNSCALE_MAX_EDGE,
            minimum=1,
        ),
        dpi=_int(data.get("dpi"), DEFAULT_DPI, minimum=1),
    )


def _int(raw: Any, default: int, *, minimum: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)
