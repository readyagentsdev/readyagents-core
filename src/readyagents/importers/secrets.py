"""Detect credential *values* in an untrusted export. Never copy them out."""

from __future__ import annotations

import re
from typing import Any

_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_REDACT = "***REDACTED***"


def looks_like_secret(value: str) -> bool:
    return bool(_SECRET_VALUE.search(str(value or "")))


def find_secrets(raw: Any) -> list[str]:
    found: list[str] = []
    _walk(raw, found)
    return found


def _walk(raw: Any, found: list[str]) -> None:
    if isinstance(raw, str):
        if looks_like_secret(raw):
            found.append(raw[:12] + "…")
        return
    if isinstance(raw, dict):
        for item in raw.values():
            _walk(item, found)
        return
    if isinstance(raw, list):
        for item in raw:
            _walk(item, found)


def strip_secrets(raw: Any) -> Any:
    """Return a copy with secret *values* replaced. Names stay."""
    if isinstance(raw, str):
        return _SECRET_VALUE.sub(_REDACT, raw) if looks_like_secret(raw) else raw
    if isinstance(raw, dict):
        return {str(k): strip_secrets(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [strip_secrets(item) for item in raw]
    return raw


def redact_text(text: str) -> str:
    return _SECRET_VALUE.sub(_REDACT, text)
