"""Stable node ids from untrusted titles."""

from __future__ import annotations

import re

from readyagents.importers.secrets import looks_like_secret

_SECRET_MUTATION = re.compile(
    r"(sk[-_][A-Za-z0-9]{8,}|ghp[-_][A-Za-z0-9]{8,}|AKIA[0-9A-Z]{8,})",
    re.I,
)


def secret_shaped(value: str) -> bool:
    """True if value looks like a secret, including hyphen/underscore mutation."""
    text = str(value or "")
    if looks_like_secret(text) or looks_like_secret(text.replace("_", "-")):
        return True
    return bool(_SECRET_MUTATION.search(text))


def slug(raw: str, *, prefix: str = "n") -> str:
    if secret_shaped(raw):
        return f"{prefix}_redacted"
    text = re.sub(r"[^A-Za-z0-9]+", "_", str(raw or "").strip())
    text = text.strip("_").lower() or "node"
    if secret_shaped(text):
        return f"{prefix}_redacted"
    if text[0].isdigit():
        text = f"{prefix}_{text}"
    return text[:64]
