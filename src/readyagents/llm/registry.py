"""Resolve an LLM provider from settings / model ref."""

from __future__ import annotations

from readyagents.config import Settings, get_settings, require_api_key
from readyagents.errors import LLMError
from readyagents.llm.anthropic_provider import AnthropicProvider
from readyagents.llm.base import LLMProvider, parse_model_ref
from readyagents.llm.bedrock_provider import BedrockProvider
from readyagents.llm.gemini_provider import GeminiProvider
from readyagents.llm.openai_compat import OpenAICompatProvider
from readyagents.llm.openai_provider import OpenAIProvider
from readyagents.llm.vertex_provider import VertexProvider
from readyagents.logging import get_logger

log = get_logger("llm")

_COMPAT_NAMES = {"openai-compat", "openai_compat", "compat", "groq", "ollama"}

# Documented defaults from .env.example / docs/configuration.md
_FALLBACK_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "openai-compat": "llama-3.1-8b-instant",
    "groq": "llama-3.1-8b-instant",
    "ollama": "llama3",
}


def _has_key(settings: Settings, provider_name: str, secrets: object = None) -> bool:
    from readyagents.secrets import secret_for_provider

    if provider_name == "openai":
        name = "openai"
    elif provider_name == "anthropic":
        name = "anthropic"
    elif provider_name == "gemini":
        name = "gemini"
    elif provider_name == "bedrock":
        name = "bedrock"
    elif provider_name == "vertex":
        name = "vertex"
    elif provider_name in _COMPAT_NAMES:
        name = "openai-compat"
    else:
        return False
    return bool(secret_for_provider(name, settings=settings, secrets=secrets))


def _implicit_fallback_ref(settings: Settings, secrets: object = None) -> str | None:
    """First provider that actually has a key. None if BYOK is empty."""
    from readyagents.secrets import secret_for_provider

    if secret_for_provider("openai", settings=settings, secrets=secrets):
        return f"openai:{_FALLBACK_MODELS['openai']}"
    if secret_for_provider("anthropic", settings=settings, secrets=secrets):
        return f"anthropic:{_FALLBACK_MODELS['anthropic']}"
    if secret_for_provider("openai-compat", settings=settings, secrets=secrets):
        if settings.openai_compat_base_url:
            return f"openai-compat:{_FALLBACK_MODELS['openai-compat']}"
        return f"groq:{_FALLBACK_MODELS['groq']}"
    return None


def get_provider(
    model_ref: str | None = None,
    *,
    settings: Settings | None = None,
    implicit: bool = False,
    secrets: object = None,
    offline: bool = False,
) -> tuple[LLMProvider, str]:
    """Return `(provider, model_id)` for a `provider:model` string.

    When ``implicit`` is true (agent node has no ``model:``), a missing key
    for the default provider falls back to whichever BYOK key is set.
    An explicit model ref never falls back.
    Offline mode never constructs a client, reads a key, or imports an SDK.
    """
    if offline:
        raise LLMError(
            "Offline replay cannot construct an LLM provider, read an API key, "
            "or import an optional SDK. Record a cassette with --record."
        )
    settings = settings or get_settings()
    ref = model_ref or settings.default_model
    provider_name, model_id = parse_model_ref(ref)

    if implicit and not _has_key(settings, provider_name, secrets):
        fallback = _implicit_fallback_ref(settings, secrets)
        if fallback:
            new_provider, new_model = parse_model_ref(fallback)
            log.info(
                "No API key for default provider '%s'; using %s:%s",
                provider_name,
                new_provider,
                new_model,
            )
            provider_name, model_id = new_provider, new_model

    if provider_name == "openai":
        key = require_api_key("openai", settings, secrets=secrets)
        return OpenAIProvider(key), model_id

    if provider_name == "anthropic":
        key = require_api_key("anthropic", settings, secrets=secrets)
        return AnthropicProvider(key), model_id

    if provider_name == "gemini":
        key = require_api_key("gemini", settings, secrets=secrets)
        return GeminiProvider(key), model_id

    if provider_name == "bedrock":
        access, secret, region, token = _bedrock_creds(settings, secrets)
        return BedrockProvider(
            access_key=access,
            secret_key=secret,
            region=region,
            session_token=token,
        ), model_id

    if provider_name == "vertex":
        project, location, cred_path = _vertex_creds(settings, secrets)
        return VertexProvider(
            project=project,
            location=location,
            credentials_path=cred_path,
        ), model_id

    if provider_name in _COMPAT_NAMES:
        base = settings.openai_compat_base_url
        if not base:
            if provider_name == "groq":
                base = "https://api.groq.com/openai/v1"
            elif provider_name == "ollama":
                base = "http://127.0.0.1:11434/v1"
            else:
                raise LLMError(
                    "OpenAI-compatible provider requires OPENAI_COMPAT_BASE_URL "
                    "(for example https://api.groq.com/openai/v1 or http://127.0.0.1:11434/v1)."
                )
        from readyagents.sovereign.egress import is_keyless_compat_url

        local = is_keyless_compat_url(base, extra_allow=settings.sovereign_allow_list())
        if local:
            key = settings.api_key_for("openai-compat") or "not-needed"
            if secrets is not None and key == "not-needed":
                from readyagents.secrets import secret_for_provider

                key = secret_for_provider("openai-compat", settings=None, secrets=secrets) or key
        else:
            key = require_api_key("openai-compat", settings, secrets=secrets)
        return OpenAICompatProvider(key, base_url=base), model_id

    raise LLMError(
        f"Unknown LLM provider '{provider_name}'. "
        "Use openai, anthropic, openai-compat (Groq/Ollama), gemini, bedrock, or vertex."
    )


def _bedrock_creds(settings: Settings, secrets: object) -> tuple[str, str, str, str | None]:
    from readyagents.secrets import secret_for_provider

    access = settings.api_key_for("bedrock") or secret_for_provider(
        "bedrock", settings=None, secrets=secrets
    )
    secret = getattr(settings, "aws_secret_access_key", None) or secret_for_provider(
        "bedrock_secret", settings=None, secrets=secrets
    )
    region = (
        getattr(settings, "aws_region", None)
        or secret_for_provider("bedrock_region", settings=None, secrets=secrets)
        or "us-east-1"
    )
    token = getattr(settings, "aws_session_token", None) or secret_for_provider(
        "bedrock_session", settings=None, secrets=secrets
    )
    if not access or not secret:
        raise LLMError(
            "No API key configured for provider 'bedrock'. ReadyAgents is BYOK — "
            "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (and optionally AWS_REGION)."
        )
    return str(access), str(secret), str(region), str(token) if token else None


def _vertex_creds(settings: Settings, secrets: object) -> tuple[str, str, str | None]:
    from readyagents.secrets import secret_for_provider

    project = getattr(settings, "vertex_project", None) or secret_for_provider(
        "vertex", settings=None, secrets=secrets
    )
    location = getattr(settings, "vertex_location", None) or "us-central1"
    cred_path = getattr(settings, "google_application_credentials", None)
    if not project:
        raise LLMError(
            "No API key configured for provider 'vertex'. ReadyAgents is BYOK — "
            "Set VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT."
        )
    return str(project), str(location), str(cred_path) if cred_path else None
