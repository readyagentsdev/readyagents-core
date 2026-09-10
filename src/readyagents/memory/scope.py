"""Explicit memory scopes. Traversal, wildcards, and kind-switching are refused."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import fnmatch

from readyagents.errors import MemoryScopeError

SCOPE_KINDS = ("workflow", "ns", "subject")
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def parse_scope(raw: str) -> tuple[str, str]:
    text = str(raw or "")
    if text != text.strip():
        raise MemoryScopeError("memory scope has surrounding whitespace")
    text = text.strip()
    if not text:
        raise MemoryScopeError("memory scope is empty")
    if _CONTROL.search(text):
        raise MemoryScopeError("memory scope contains control characters")
    if ".." in text or "/" in text or "\\" in text:
        raise MemoryScopeError("memory scope refuses path traversal")
    if "*" in text or "?" in text or "[" in text or "]" in text:
        raise MemoryScopeError("memory scope refuses wildcards")
    if text.count(":") != 1:
        raise MemoryScopeError("memory scope must be exactly kind:value")
    kind, value = text.split(":", 1)
    kind = kind.strip().lower()
    value = value.strip()
    if kind not in SCOPE_KINDS:
        raise MemoryScopeError(f"memory scope kind must be workflow, ns, or subject, not {kind!r}")
    if not _VALUE.fullmatch(value):
        raise MemoryScopeError("memory scope value is not a safe token")
    return kind, value


def format_scope(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def validate_scope(
    rendered: str,
    *,
    pattern: str | None = None,
    allowed: Sequence[str] | None = None,
    workflow_name: str | None = None,
) -> str:
    """Validate an already-interpolated scope against the declared pattern."""
    kind, value = parse_scope(rendered)
    scope = format_scope(kind, value)
    if kind == "workflow" and workflow_name and value != str(workflow_name):
        raise MemoryScopeError(
            f"workflow scope {scope!r} does not match this workflow {workflow_name!r}"
        )
    if pattern:
        _assert_declared_pattern(pattern)
        if not fnmatch(scope, pattern):
            raise MemoryScopeError(f"memory scope {scope!r} does not match declared pattern")
    if allowed:
        if not any(fnmatch(scope, item) for item in allowed):
            raise MemoryScopeError(f"memory scope {scope!r} is not on the workflow allow-list")
    return scope


def _assert_declared_pattern(pattern: str) -> None:
    text = str(pattern or "").strip()
    if not text or _CONTROL.search(text):
        raise MemoryScopeError("memory scope_pattern is invalid")
    if ".." in text or "/" in text or "\\" in text:
        raise MemoryScopeError("memory scope_pattern refuses path traversal")
    if text.count(":") != 1:
        raise MemoryScopeError("memory scope_pattern must be kind:value")
    kind, _rest = text.split(":", 1)
    if kind.strip().lower() not in SCOPE_KINDS:
        raise MemoryScopeError("memory scope_pattern kind is not workflow, ns, or subject")


def subject_scope(subject: str) -> str:
    kind, value = parse_scope(f"subject:{str(subject).strip()}")
    if kind != "subject":
        raise MemoryScopeError("subject sweep requires a subject token")
    return format_scope(kind, value)
