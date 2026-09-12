"""Candidate prompts are untrusted data: bounded, never executed as config."""

from __future__ import annotations

import re
from typing import Any

from readyagents.errors import OptimizeRefused
from readyagents.prompts.layout import MAX_PROMPT_CHARS
from readyagents.replay.record import contains_secret
from readyagents.simulate.redact import secret_shaped_values

_CONFIG_MARKERS = (
    re.compile(r"(?m)^nodes:\s*$"),
    re.compile(r"(?m)^start:\s+\S+"),
    re.compile(r"(?m)^\s+type:\s+(agent|tool|condition)\s*$"),
)
_EXEC_MARKERS = (
    re.compile(r"__import__\s*\("),
    re.compile(r"subprocess\."),
    re.compile(r"os\.system\s*\("),
    re.compile(r"eval\s*\("),
    re.compile(r"exec\s*\("),
)


def bound_candidate(text: str) -> str:
    """Store as data. Oversize is refused. Config-shaped text stays a string."""
    if not isinstance(text, str):
        raise OptimizeRefused("candidate prompt must be text", reason="candidate")
    if len(text) > MAX_PROMPT_CHARS:
        raise OptimizeRefused(
            f"candidate prompt exceeds {MAX_PROMPT_CHARS} characters",
            reason="candidate",
        )
    if "\x00" in text:
        raise OptimizeRefused("candidate prompt contains a NUL byte", reason="candidate")
    return text


def is_config_shaped(text: str) -> bool:
    if any(pat.search(text) for pat in _CONFIG_MARKERS):
        return True
    return any(pat.search(text) for pat in _EXEC_MARKERS)


def case_secret_hits(value: Any, secrets: list[str] | None) -> list[str]:
    found: list[str] = []
    extra = list(secrets or [])
    shaped = secret_shaped_values(value)
    extra.extend(shaped)
    found.extend(shaped)
    if extra and contains_secret(value, extra):
        if not found:
            found.append("<secret>")
    return found
