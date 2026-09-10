"""Anthropic Messages provider."""

from __future__ import annotations

import asyncio
from typing import Any

from readyagents.errors import LLMError, missing_extra_message
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.tool_calls import (
    anthropic_tools_payload,
    messages_to_anthropic,
    tool_calls_from_anthropic_content,
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        system, chat = messages_to_anthropic(messages)
        if not chat:
            raise LLMError("Anthropic requires at least one non-system message")
        payload: dict[str, Any] = {"model": model, "messages": chat, "max_tokens": 4096}
        if system:
            payload["system"] = system
        anth_tools = anthropic_tools_payload(tools)
        if anth_tools:
            payload["tools"] = anth_tools
        payload.update({k: v for k, v in kwargs.items() if v is not None and k != "max_tokens"})
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]
        return payload

    def _from_response(self, response: Any, *, model: str) -> CompletionResult:
        parts = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        usage: dict[str, Any] = {}
        if getattr(response, "usage", None):
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
            }
        return CompletionResult(
            text="".join(parts).strip(),
            model=model,
            raw=response,
            usage=usage,
            tool_calls=tool_calls_from_anthropic_content(response.content),
        )

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMError(missing_extra_message("Anthropic", "anthropic")) from exc
        try:
            client = Anthropic(api_key=self._api_key)
            response = client.messages.create(
                **self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            )
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Anthropic request failed: {exc}") from exc

    async def complete_async(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            return await asyncio.to_thread(
                self.complete, messages, model=model, tools=tools, **kwargs
            )
        try:
            client = AsyncAnthropic(api_key=self._api_key)
            response = await client.messages.create(
                **self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            )
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Anthropic request failed: {exc}") from exc
