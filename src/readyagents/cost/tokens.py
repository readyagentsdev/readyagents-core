"""Deterministic token counting. tiktoken is an optional extra, never required."""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4
HEURISTIC_BAND = 0.50
MEASURED_BAND = 0.10


def count_tokens(text: str) -> tuple[int, bool]:
    """Return ``(tokens, measured)``. ``measured`` is True when tiktoken ran."""
    raw = text or ""
    encoder = _tiktoken_encoder()
    if encoder is not None:
        try:
            return max(0, len(encoder.encode(raw))), True
        except Exception:  # noqa: BLE001
            pass
    return heuristic_tokens(raw), False


def heuristic_tokens(text: str) -> int:
    """``max(1, len // 4)`` for non-empty text; 0 for empty. Same idea as dry-run."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _tiktoken_encoder() -> Any | None:
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        return None
