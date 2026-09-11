"""AWS Bedrock Converse provider (optional extra ``bedrock``)."""

from __future__ import annotations

from typing import Any

from readyagents.errors import LLMError, ReadyAgentsError, missing_extra_message
from readyagents.llm.base import CompletionResult, Message, ToolCall
from readyagents.llm.tool_calls import parse_json_arguments


class BedrockProvider:
    name = "bedrock"

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        region: str,
        session_token: str | None = None,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._session_token = session_token

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            import boto3
        except (ImportError, AttributeError) as exc:
            raise LLMError(missing_extra_message("Bedrock", "bedrock")) from exc
        if boto3 is None:
            raise LLMError(missing_extra_message("Bedrock", "bedrock"))
        try:
            client_kwargs: dict[str, Any] = {
                "service_name": "bedrock-runtime",
                "region_name": self._region,
                "aws_access_key_id": self._access_key,
                "aws_secret_access_key": self._secret_key,
            }
            if self._session_token:
                client_kwargs["aws_session_token"] = self._session_token
            client = boto3.client(**client_kwargs)
            payload = self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            response = client.converse(**payload)
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_rate_limit(exc)
            raise LLMError(f"Bedrock request failed: {exc}") from exc

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
            import boto3
        except (ImportError, AttributeError) as exc:
            raise LLMError(missing_extra_message("Bedrock", "bedrock")) from exc
        if boto3 is None:
            raise LLMError(missing_extra_message("Bedrock", "bedrock"))
        try:
            client = boto3.client(
                "bedrock-runtime",
                region_name=self._region,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                **({"aws_session_token": self._session_token} if self._session_token else {}),
            )
            payload = self._payload(messages, model=model, tools=tools, kwargs=kwargs)
            stream_fn = getattr(client, "converse_stream", None)
            if not callable(stream_fn):
                return self.complete(messages, model=model, tools=tools, **kwargs)
            parts: list[str] = []
            last: Any = None
            streamed = stream_fn(**payload)
            events = streamed.get("stream") if isinstance(streamed, dict) else streamed
            for event in events or []:
                last = event
                delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
                piece = delta.get("text") or ""
                if piece:
                    parts.append(piece)
                    if on_token is not None:
                        on_token(piece)
            if parts:
                return CompletionResult(text="".join(parts).strip(), model=model, raw=last)
            return self.complete(messages, model=model, tools=tools, **kwargs)
        except ReadyAgentsError:
            raise
        except Exception:
            return self.complete(messages, model=model, tools=tools, **kwargs)

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        system, chat = _bedrock_messages(messages)
        payload: dict[str, Any] = {"modelId": model, "messages": chat}
        if system:
            payload["system"] = system
        tool_cfg = _bedrock_tools(tools)
        if tool_cfg:
            payload["toolConfig"] = tool_cfg
        if kwargs.get("response_mime_type") == "application/json" or kwargs.get("structured"):
            payload.setdefault("inferenceConfig", {})["temperature"] = 0
        return payload

    def _from_response(self, response: Any, *, model: str) -> CompletionResult:
        data = response if isinstance(response, dict) else {}
        output = data.get("output") or {}
        message = output.get("message") or {}
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("text"):
                text_parts.append(str(block["text"]))
            tool = block.get("toolUse")
            if isinstance(tool, dict):
                calls.append(
                    ToolCall(
                        id=str(tool.get("toolUseId") or tool.get("name") or "call"),
                        name=str(tool.get("name") or ""),
                        arguments=parse_json_arguments(tool.get("input") or {}),
                    )
                )
        usage_raw = data.get("usage") or {}
        usage = {
            "prompt_tokens": usage_raw.get("inputTokens"),
            "completion_tokens": usage_raw.get("outputTokens"),
            "total_tokens": usage_raw.get("totalTokens"),
        }
        return CompletionResult(
            text="".join(text_parts).strip(),
            model=model,
            raw=response,
            usage=usage,
            tool_calls=calls,
        )

    def _note_rate_limit(self, exc: BaseException) -> None:
        from readyagents.workflow.governor import (
            looks_like_rate_limit,
            notify_retry_after,
            retry_after_seconds_from,
        )

        if not looks_like_rate_limit(exc):
            return
        notify_retry_after(self.name, exc, seconds=retry_after_seconds_from(exc) or 1.0)


def _bedrock_messages(messages: list[Message]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    system: list[dict[str, str]] = []
    chat: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system.append({"text": message.content or ""})
            continue
        role = "assistant" if message.role == "assistant" else "user"
        chat.append({"role": role, "content": [{"text": message.content or ""}]})
    return system, chat


def _bedrock_tools(tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not tools:
        return None
    specs: list[dict[str, Any]] = []
    for spec in tools:
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else spec
        name = fn.get("name") or spec.get("name")
        if not name:
            continue
        specs.append(
            {
                "toolSpec": {
                    "name": name,
                    "description": fn.get("description") or spec.get("description") or "",
                    "inputSchema": {
                        "json": fn.get("parameters")
                        or spec.get("schema")
                        or {"type": "object", "properties": {}},
                    },
                }
            }
        )
    if not specs:
        return None
    return {"tools": specs}
