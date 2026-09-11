"""Google Vertex AI provider (optional extra ``vertex``)."""

from __future__ import annotations

from typing import Any

from readyagents.errors import LLMError, ReadyAgentsError, missing_extra_message
from readyagents.llm.base import CompletionResult, Message, ToolCall
from readyagents.llm.tool_calls import parse_json_arguments


class VertexProvider:
    name = "vertex"

    def __init__(
        self,
        *,
        project: str,
        location: str = "us-central1",
        credentials_path: str | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._credentials_path = credentials_path

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel
        except (ImportError, AttributeError) as exc:
            raise LLMError(missing_extra_message("Vertex", "vertex")) from exc
        if vertexai is None or GenerativeModel is None:
            raise LLMError(missing_extra_message("Vertex", "vertex"))
        try:
            init_kwargs: dict[str, Any] = {"project": self._project, "location": self._location}
            if self._credentials_path:
                init_kwargs["credentials"] = _load_vertex_credentials(self._credentials_path)
            vertexai.init(**init_kwargs)
            llm = GenerativeModel(model)
            contents = _vertex_contents(messages)
            gen_kwargs: dict[str, Any] = {}
            decls = _vertex_tools(tools)
            if decls:
                gen_kwargs["tools"] = decls
            if kwargs.get("structured") or kwargs.get("response_mime_type") == "application/json":
                gen_kwargs["generation_config"] = {"response_mime_type": "application/json"}
            response = llm.generate_content(contents, **gen_kwargs)
            return self._from_response(response, model=model)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_rate_limit(exc)
            raise LLMError(f"Vertex request failed: {exc}") from exc

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
            import vertexai
            from vertexai.generative_models import GenerativeModel
        except (ImportError, AttributeError) as exc:
            raise LLMError(missing_extra_message("Vertex", "vertex")) from exc
        if vertexai is None or GenerativeModel is None:
            raise LLMError(missing_extra_message("Vertex", "vertex"))
        try:
            vertexai.init(project=self._project, location=self._location)
            llm = GenerativeModel(model)
            contents = _vertex_contents(messages)
            parts: list[str] = []
            last = None
            for chunk in llm.generate_content(contents, stream=True):
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

    def _from_response(self, response: Any, *, model: str) -> CompletionResult:
        text = (getattr(response, "text", None) or "").strip()
        usage: dict[str, Any] = {}
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage = {
                "prompt_tokens": getattr(meta, "prompt_token_count", None),
                "completion_tokens": getattr(meta, "candidates_token_count", None),
            }
        return CompletionResult(
            text=text,
            model=model,
            raw=response,
            usage=usage,
            tool_calls=_tool_calls_from_vertex(response),
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


def _load_vertex_credentials(path: str) -> Any:
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise LLMError(missing_extra_message("Vertex", "vertex")) from exc
    return service_account.Credentials.from_service_account_file(path)


def _vertex_contents(messages: list[Message]) -> list[str]:
    parts: list[str] = []
    for message in messages:
        role = message.role or "user"
        parts.append(f"{role}: {message.content or ''}")
    return parts


def _vertex_tools(tools: list[dict[str, Any]] | None) -> list[Any]:
    if not tools:
        return []
    try:
        from vertexai.generative_models import FunctionDeclaration, Tool
    except ImportError:
        return []
    decls: list[Any] = []
    for spec in tools:
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else spec
        name = fn.get("name") or spec.get("name")
        if not name:
            continue
        decls.append(
            FunctionDeclaration(
                name=name,
                description=fn.get("description") or spec.get("description") or "",
                parameters=fn.get("parameters")
                or spec.get("schema")
                or {"type": "object", "properties": {}},
            )
        )
    if not decls:
        return []
    return [Tool(function_declarations=decls)]


def _tool_calls_from_vertex(response: Any) -> list[ToolCall]:
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
            calls.append(
                ToolCall(
                    id=name or "call",
                    name=name,
                    arguments=parse_json_arguments(getattr(fn, "args", None) or {}),
                )
            )
    return calls
