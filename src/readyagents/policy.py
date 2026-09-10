"""RBAC hooks and PII redaction. No control plane — local policy only."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from readyagents.errors import AuthorizationError

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_SK_KEY = re.compile(r"sk-[A-Za-z0-9]{8,}")
_ASSIGNED_SECRET = re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[=:]\s*\S+")

DEFAULT_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (_EMAIL, _SK_KEY, _ASSIGNED_SECRET)
REDACTED = "[redacted]"


@runtime_checkable
class Authorizer(Protocol):
    """Allow or deny an action. Raise ``AuthorizationError`` to deny."""

    def check(self, actor: str | None, action: str, resource: str) -> None: ...


class AllowAll:
    """Default authorizer — core stays open unless a pack/hook is installed."""

    def check(self, actor: str | None, action: str, resource: str) -> None:
        return None


class CallbackAuthorizer:
    """Wrap a ``(actor, action, resource) -> bool`` callback."""

    def __init__(
        self,
        allowed: Callable[[str | None, str, str], bool],
        *,
        name: str = "callback",
    ) -> None:
        self._allowed = allowed
        self.name = name

    def check(self, actor: str | None, action: str, resource: str) -> None:
        if not self._allowed(actor, action, resource):
            raise AuthorizationError(actor, action, resource)


class CompositeAuthorizer:
    """Every registered authorizer must allow (AND)."""

    def __init__(self, parts: Sequence[Authorizer]) -> None:
        self.parts = [p for p in parts if p is not None]

    def check(self, actor: str | None, action: str, resource: str) -> None:
        for part in self.parts:
            part.check(actor, action, resource)

    def roles_for(self, actor: str | None) -> list[str]:
        out: list[str] = []
        for part in self.parts:
            fn = getattr(part, "roles_for", None)
            if callable(fn):
                out.extend(str(item) for item in list(fn(actor) or []))
        return out


def resolve_authorizer(raw: Any) -> Authorizer:
    if raw is None:
        return AllowAll()
    if isinstance(raw, CompositeAuthorizer):
        return raw
    if hasattr(raw, "check") and callable(raw.check):
        return raw
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return CompositeAuthorizer(list(raw))
    return AllowAll()


class Redactor:
    """Mask emails, vendor-style keys, assignment secrets, and extra literals."""

    def __init__(
        self,
        *,
        patterns: Sequence[str] | None = None,
        literals: Sequence[str] | None = None,
        replacement: str = REDACTED,
    ) -> None:
        compiled: list[re.Pattern[str]] = list(DEFAULT_REDACT_PATTERNS)
        for raw in patterns or []:
            text = str(raw).strip()
            if not text:
                continue
            compiled.append(re.compile(text))
        self._patterns = compiled
        self._literals = [str(item) for item in (literals or []) if str(item)]
        self.replacement = replacement

    def redact_text(self, text: str) -> str:
        out = text
        for lit in self._literals:
            if lit:
                out = out.replace(lit, self.replacement)
        for pat in self._patterns:
            out = pat.sub(self.replacement, out)
        return out

    def redact(self, value: Any) -> Any:
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {str(k): self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, bytes):
            try:
                return self.redact_text(value.decode("utf-8"))
            except UnicodeDecodeError:
                return self.replacement
        return value


def redactor_from_settings(
    *,
    enabled: bool,
    patterns: Sequence[str] | None = None,
    literals: Sequence[str] | None = None,
) -> Redactor | None:
    if not enabled:
        return None
    return Redactor(patterns=patterns, literals=literals)
