"""OpenAI Chat Completions provider."""

from __future__ import annotations

import asyncio
from typing import Any

from readyagents.errors import LLMError, missing_extra_message
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.tool_calls import (
    messages_to_openai,
    openai_tools_payload,
    tool_calls_from_openai_message,
)


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url

    def _client_kwargs(self) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        return client_kwargs

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai(messages),
        }
        openai_tools = openai_tools_payload(tools)
        if openai_tools:
            payload["tools"] = openai_tools
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return payload

    def _from_response(self, response: Any, *, model: str) -> CompletionResult:
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        usage: dict[str, Any] = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return CompletionResult(
            text=text,
            model=model,
            raw=response,
            usage=usage,
            tool_calls=tool_calls_from_openai_message(choice.message),
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
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(missing_extra_message("OpenAI", "openai")) from exc
        try:
            client = OpenAI(**self._client_kwargs())
            response = client.chat.completions.create(
                **self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            )
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_rate_limit(exc)
            raise LLMError(f"OpenAI request failed: {exc}") from exc

    async def complete_async(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            return await asyncio.to_thread(
                self.complete, messages, model=model, tools=tools, **kwargs
            )
        try:
            client = AsyncOpenAI(**self._client_kwargs())
            response = await client.chat.completions.create(
                **self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            )
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_rate_limit(exc)
            raise LLMError(f"OpenAI request failed: {exc}") from exc

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        on_token: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(missing_extra_message("OpenAI", "openai")) from exc
        try:
            client = OpenAI(**self._client_kwargs())
            payload = self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            payload["stream"] = True
            payload.setdefault("stream_options", {"include_usage": True})
            parts: list[str] = []
            usage: dict[str, Any] = {}
            tool_acc: list[Any] = []
            for chunk in client.chat.completions.create(**payload):
                if getattr(chunk, "usage", None):
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None) or ""
                if piece:
                    parts.append(piece)
                    if on_token is not None:
                        on_token(piece)
                calls = getattr(delta, "tool_calls", None)
                if calls:
                    tool_acc.extend(calls)
            text = "".join(parts)
            from readyagents.llm.tool_calls import tool_calls_from_openai_message

            fake = type("Msg", (), {"content": text, "tool_calls": tool_acc or None})()
            return CompletionResult(
                text=text,
                model=model,
                raw=None,
                usage=usage,
                tool_calls=tool_calls_from_openai_message(fake),
            )
        except LLMError:
            raise
        except Exception:
            return self.complete(messages, model=model, tools=tools, **kwargs)

    def _note_rate_limit(self, exc: BaseException) -> None:
        from readyagents.workflow.governor import (
            looks_like_rate_limit,
            notify_retry_after,
            retry_after_seconds_from,
        )

        if not looks_like_rate_limit(exc):
            return
        notify_retry_after(
            self.name,
            exc,
            seconds=retry_after_seconds_from(exc) or 1.0,
        )
