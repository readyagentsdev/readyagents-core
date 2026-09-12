"""Stable node ids from untrusted titles."""

from __future__ import annotations

import re


def slug(raw: str, *, prefix: str = "n") -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(raw or "").strip())
    text = text.strip("_").lower() or "node"
    if text[0].isdigit():
        text = f"{prefix}_{text}"
    return text[:64]
