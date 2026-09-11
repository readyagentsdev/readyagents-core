"""Complexity-bounded, time-limited regex for contract deny_regex rules."""

from __future__ import annotations

import re
import threading
from typing import Any

MAX_PATTERN_CHARS = 200
MAX_QUANTIFIERS = 8
SEARCH_TIMEOUT_SECONDS = 0.05

_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*?]")


def compile_bounded(pattern: str) -> re.Pattern[str]:
    text = str(pattern)
    if not text:
        raise ValueError("deny_regex is empty")
    if len(text) > MAX_PATTERN_CHARS:
        raise ValueError(f"deny_regex longer than {MAX_PATTERN_CHARS} characters")
    if _NESTED_QUANTIFIER.search(text):
        raise ValueError("deny_regex nested quantifiers are not allowed")
    quantifiers = text.count("*") + text.count("+") + text.count("?")
    if quantifiers > MAX_QUANTIFIERS:
        raise ValueError("deny_regex has too many quantifiers")
    if re.search(r"\{[0-9]{4,}", text):
        raise ValueError("deny_regex repetition bound is too large")
    try:
        return re.compile(text)
    except re.error as exc:
        raise ValueError(f"deny_regex is not a valid pattern: {exc}") from exc


def search_bounded(pattern: str, text: str, *, timeout: float = SEARCH_TIMEOUT_SECONDS) -> bool:
    """Return True if pattern matches. Timeout or error fails closed as a match."""
    compiled = compile_bounded(pattern)
    box: list[Any] = []

    def _run() -> None:
        try:
            box.append(compiled.search(text) is not None)
        except re.error:
            box.append(True)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive() or not box:
        return True
    return bool(box[0])
