"""Secrets-manager hooks. Env / `.env` remains the default BYOK path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from readyagents.config import Settings
from readyagents.errors import LLMError


@runtime_checkable
class SecretsBackend(Protocol):
    """Resolve a secret by name (usually an env-style key like ``OPENAI_API_KEY``)."""

    name: str

    def get(self, key: str) -> str | None: ...


class MappingSecrets:
    """In-process / pack test backend. Not a vendor SDK."""

    name = "mapping"

    def __init__(self, values: Mapping[str, str], *, name: str = "mapping") -> None:
        self.name = name
        self._values = {str(k): str(v) for k, v in values.items() if v is not None}

    def get(self, key: str) -> str | None:
        value = self._values.get(key)
        if value is None or not str(value).strip():
            return None
        return str(value)


_PROVIDER_KEYS = {
    "openai": ("OPENAI_API_KEY", "READYAGENTS_OPENAI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY", "READYAGENTS_ANTHROPIC_API_KEY"),
    "openai-compat": (
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_COMPAT_API_KEY",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
    ),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "READYAGENTS_GEMINI_API_KEY"),
    "bedrock": ("AWS_ACCESS_KEY_ID", "READYAGENTS_AWS_ACCESS_KEY_ID"),
    "bedrock_secret": ("AWS_SECRET_ACCESS_KEY", "READYAGENTS_AWS_SECRET_ACCESS_KEY"),
    "bedrock_region": ("AWS_REGION", "AWS_DEFAULT_REGION", "BEDROCK_REGION"),
    "bedrock_session": ("AWS_SESSION_TOKEN",),
    "vertex": (
        "VERTEX_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "READYAGENTS_VERTEX_PROJECT",
    ),
}


def lookup_secret(
    key: str,
    backends: Sequence[SecretsBackend] | SecretsBackend | None,
) -> str | None:
    if not backends:
        return None
    items = as_backends(backends)
    if not items and isinstance(backends, SecretsBackend):
        items = [backends]
    for backend in items:
        getter = getattr(backend, "get", None)
        if not callable(getter):
            continue
        try:
            value = getter(key)
        except Exception:  # noqa: BLE001
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def secret_for_provider(
    provider: str,
    *,
    settings: Settings | None = None,
    secrets: Sequence[SecretsBackend] | SecretsBackend | None = None,
) -> str | None:
    """Settings/env first (BYOK default), then secrets-manager hooks."""
    if settings is not None:
        from_settings = settings.api_key_for(provider)
        if from_settings:
            return from_settings
    names = _PROVIDER_KEYS.get(provider.lower()) or (provider.upper() + "_API_KEY",)
    for name in names:
        found = lookup_secret(name, secrets)
        if found:
            return found
    return None


def require_secret(
    provider: str,
    *,
    settings: Settings | None = None,
    secrets: Sequence[SecretsBackend] | None = None,
) -> str:
    """Like ``require_api_key`` but consults secrets backends after env."""
    from readyagents.config import get_settings, require_api_key

    settings = settings or get_settings()
    found = secret_for_provider(provider, settings=settings, secrets=secrets)
    if found:
        return found
    # Reuse the BYOK error wording when nothing is configured.
    try:
        return require_api_key(provider, settings)
    except LLMError:
        raise


def as_backends(raw: Any) -> list[SecretsBackend]:
    if raw is None:
        return []
    if isinstance(raw, SecretsBackend):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [item for item in raw if isinstance(item, SecretsBackend)]
    return []
