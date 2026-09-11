"""Deterministic JSON repairs. Run before any model call."""

from __future__ import annotations

import json
import re
from typing import Any

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_fences(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def strip_trailing_commas(text: str) -> str:
    prev = text
    for _ in range(8):
        nxt = _TRAILING_COMMA.sub(r"\1", prev)
        if nxt == prev:
            return nxt
        prev = nxt
    return prev


def wrap_object(text: str, schema: dict[str, Any] | None) -> str:
    if not schema or schema.get("type") != "object":
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        return text
    required = [str(item) for item in (schema.get("required") or []) if item]
    if len(required) == 1:
        return json.dumps({required[0]: data}, ensure_ascii=False)
    return json.dumps({"value": data}, ensure_ascii=False)


def deterministic_repair(text: str, schema: dict[str, Any] | None) -> str:
    """Fences, trailing commas, then a single wrapping object. No model."""
    repaired = strip_fences(text)
    repaired = strip_trailing_commas(repaired)
    return wrap_object(repaired, schema)
