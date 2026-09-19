"""Resolve ``decider:model`` into ``(Decider, model_id)``."""

from __future__ import annotations

from typing import Any, cast

from readyagents.config import Settings, get_settings, require_api_key
from readyagents.decide.base import Decider
from readyagents.decide.jev import (
    DEFAULT_BASE_URL,
    DEFAULT_PATH,
    PINNED_JEV_MODEL,
    JevDecider,
    warn_unpinned_jev,
)
from readyagents.decide.shim import ShimDecider
from readyagents.errors import DecideError, LLMError
from readyagents.logging import get_logger

log = get_logger("decide")


def parse_decider_ref(ref: str) -> tuple[str, str]:
    """Split ``jev``, ``jev:jev-1.13.0``, ``shim``, ``shim:openai:gpt-4o-mini``."""
    text = (ref or "").strip()
    if not text:
        raise DecideError("Empty decider reference")
    if ":" in text:
        name, model = text.split(":", 1)
        return name.strip().lower(), model.strip()
    return text.lower(), ""


def get_decider(
    ref: str | None = None,
    *,
    settings: Settings | None = None,
    secrets: object = None,
    offline: bool = False,
    llm: Any = None,
    min_confidence: float | None = None,
) -> tuple[Decider, str]:
    """Resolve ``decider:model`` into (Decider, model_id).

    Offline mode never constructs a client or reads an API key.
    An explicit ``jev`` never falls back to ``shim``.
    """
    if offline:
        raise DecideError(
            "Offline replay cannot construct a decider, read an API key, or open a "
            "socket. Record a cassette with --record."
        )
    settings = settings or get_settings()
    name, model_id, implicit = _resolve_name(ref, settings=settings, secrets=secrets)
    if name == "jev":
        model_id = model_id or PINNED_JEV_MODEL
        warn_unpinned_jev(model_id, min_confidence)
        try:
            key = require_api_key("typesafe", settings, secrets=secrets)
        except LLMError as exc:
            raise DecideError(str(exc)) from exc
        return (
            JevDecider(
                key,
                base_url=str(getattr(settings, "typesafe_base_url", None) or DEFAULT_BASE_URL),
                path=str(getattr(settings, "typesafe_systemone_path", None) or DEFAULT_PATH),
            ),
            model_id,
        )
    if name == "shim":
        if implicit:
            log.info("No TypeSafe API key; using shim decider")
        return ShimDecider(llm, model=model_id or None), model_id or (
            getattr(llm, "name", None) or "shim"
        )
    raise DecideError(
        f"Unknown decider '{name}'. Use jev or shim (optionally jev:<model> / shim:<llm>)."
    )


def _has_typesafe_key(settings: Settings, secrets: object = None) -> bool:
    from readyagents.secrets import secret_for_provider

    return bool(secret_for_provider("typesafe", settings=settings, secrets=cast(Any, secrets)))


def _resolve_name(
    ref: str | None,
    *,
    settings: Settings,
    secrets: object,
) -> tuple[str, str, bool]:
    text = (ref or "").strip()
    if not text:
        if _has_typesafe_key(settings, secrets):
            return "jev", PINNED_JEV_MODEL, False
        return "shim", "", True
    name, model_id = parse_decider_ref(text)
    if name == "jev" and not model_id:
        model_id = PINNED_JEV_MODEL
    return name, model_id, False
