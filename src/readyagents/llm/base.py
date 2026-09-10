"""Thin LLM provider interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    """One model-requested tool invocation (id + name + JSON arguments)."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class CompletionResult:
    text: str
    model: str
    raw: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult: ...


def parse_model_ref(ref: str) -> tuple[str, str]:
    """Split `provider:model` (or bare model → openai)."""
    ref = ref.strip()
    if not ref:
        raise ValueError("Empty model reference")
    if ":" in ref:
        provider, model = ref.split(":", 1)
        return provider.strip().lower(), model.strip()
    return "openai", ref
