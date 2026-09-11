"""Google Gemini provider (optional extra ``gemini``)."""

from __future__ import annotations

from typing import Any

from readyagents.errors import LLMError, ReadyAgentsError, missing_extra_message
from readyagents.llm.base import CompletionResult, Message, ToolCall
from readyagents.llm.tool_calls import parse_json_arguments


def _load_genai() -> Any:
    try:
        from google import genai
    except (ImportError, AttributeError) as exc:
        raise LLMError(missing_extra_message("Gemini", "gemini")) from exc
    if genai is None or not hasattr(genai, "Client"):
        raise LLMError(missing_extra_message("Gemini", "gemini"))
    return genai


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        genai = _load_genai()
        try:
            client = genai.Client(api_key=self._api_key)
            payload = self._payload(messages, tools=tools, kwargs=kwargs)
            response = client.models.generate_content(model=model, **payload)
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_rate_limit(exc)
            raise LLMError(f"Gemini request failed: {exc}") from exc

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        on_token: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        genai = _load_genai()
        try:
            client = genai.Client(api_key=self._api_key)
            payload = self._payload(messages, tools=tools, kwargs=kwargs)
            parts: list[str] = []
            stream_fn = getattr(client.models, "generate_content_stream", None)
            if not callable(stream_fn):
                return self.complete(messages, model=model, tools=tools, **kwargs)
            last = None
            for chunk in stream_fn(model=model, **payload):
                last = chunk
                piece = getattr(chunk, "text", None) or ""
                if piece:
                    parts.append(piece)
                    if on_token is not None:
                        on_token(piece)
            if parts:
                return CompletionResult(text="".join(parts).strip(), model=model, raw=last)
            return self._from_response(last, model=model)
        except ReadyAgentsError:
            raise
        except Exception:
            return self.complete(messages, model=model, tools=tools, **kwargs)

    def _payload(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        system, contents = _gemini_contents(messages)
        payload: dict[str, Any] = {"contents": contents}
        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system
        decls = _gemini_tools(tools)
        if decls:
            config["tools"] = [{"function_declarations": decls}]
        if kwargs.get("response_mime_type"):
            config["response_mime_type"] = kwargs["response_mime_type"]
        elif kwargs.get("structured"):
            config["response_mime_type"] = "application/json"
        if config:
            payload["config"] = config
        return payload

    def _from_response(self, response: Any, *, model: str) -> CompletionResult:
        text = (getattr(response, "text", None) or "").strip()
        usage: dict[str, Any] = {}
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage = {
                "prompt_tokens": getattr(meta, "prompt_token_count", None)
                or getattr(meta, "input_tokens", None),
                "completion_tokens": getattr(meta, "candidates_token_count", None)
                or getattr(meta, "output_tokens", None),
            }
        calls = _tool_calls_from_gemini(response)
        if not text and calls:
            text = ""
        return CompletionResult(text=text, model=model, raw=response, usage=usage, tool_calls=calls)

    def _note_rate_limit(self, exc: BaseException) -> None:
        from readyagents.workflow.governor import (
            looks_like_rate_limit,
            notify_retry_after,
            retry_after_seconds_from,
        )

        if not looks_like_rate_limit(exc):
            return
        notify_retry_after(self.name, exc, seconds=retry_after_seconds_from(exc) or 1.0)


def _gemini_contents(messages: list[Message]) -> tuple[str, list[Any]]:
    system_parts: list[str] = []
    contents: list[Any] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(message.content or "")
            continue
        role = "user" if message.role in {"user", "tool"} else "model"
        from readyagents.media.payload import gemini_parts

        contents.append({"role": role, "parts": gemini_parts(message)})
    return "\n".join(part for part in system_parts if part), contents


def _gemini_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for spec in tools:
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else spec
        name = fn.get("name") or spec.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": fn.get("description") or spec.get("description") or "",
                "parameters": fn.get("parameters")
                or spec.get("schema")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def _tool_calls_from_gemini(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fn = getattr(part, "function_call", None)
            if fn is None:
                continue
            name = str(getattr(fn, "name", "") or "")
            args = parse_json_arguments(getattr(fn, "args", None) or {})
            calls.append(ToolCall(id=name or "call", name=name, arguments=args))
    return calls
