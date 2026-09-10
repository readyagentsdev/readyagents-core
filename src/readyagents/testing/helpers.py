"""Helpers that wrap the same engine path the CLI uses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from readyagents.errors import LLMError
from readyagents.llm.base import CompletionResult, Message, ToolCall
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


class ScriptedLLM:
    """In-process LLM stand-in. Queue completions or errors per model id."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._by_model: dict[str, list[CompletionResult | BaseException]] = {}
        self._queue: list[CompletionResult | BaseException] = []

    def enqueue(
        self,
        text: str = "ok",
        *,
        model: str | None = None,
        usage: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        tool_calls: Sequence[ToolCall] | None = None,
    ) -> ScriptedLLM:
        item: CompletionResult | BaseException
        if error is not None:
            item = error
        else:
            item = CompletionResult(
                text=text,
                model=model or "scripted",
                usage=dict(usage or {}),
                tool_calls=list(tool_calls or []),
            )
        if model:
            self._by_model.setdefault(model, []).append(item)
        else:
            self._queue.append(item)
        return self

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        item: CompletionResult | BaseException | None = None
        bucket = self._by_model.get(model)
        if bucket:
            item = bucket.pop(0)
        elif self._queue:
            item = self._queue.pop(0)
        if item is None:
            return CompletionResult(text="ok", model=model, usage={})
        if isinstance(item, BaseException):
            raise item
        if not item.model:
            item.model = model
        return item

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: Any = None,
        on_token: Any = None,
        **kwargs: Any,
    ) -> CompletionResult:
        result = self.complete(messages, model=model, tools=tools, **kwargs)
        text = result.text or ""
        if on_token is not None and text:
            step = 4 if len(text) > 4 else 1
            for i in range(0, len(text), step):
                on_token(text[i : i + step])
        return result


def run_workflow_spec(
    spec: Mapping[str, Any] | WorkflowSpec,
    *,
    inputs: Mapping[str, Any] | None = None,
    llm: Any | None = None,
    tools: ToolRegistry | None = None,
    decisions: Mapping[str, str] | None = None,
    **ctx_kwargs: Any,
) -> RunState:
    """Validate a workflow mapping and run it through ``run_workflow``."""
    workflow = spec if isinstance(spec, WorkflowSpec) else WorkflowSpec.model_validate(dict(spec))
    merged = dict(workflow.input_defaults())
    if inputs:
        merged.update(inputs)
    ctx = ExecutionContext(
        workflow,
        tools or ToolRegistry(),
        llm=llm,
        default_model=ctx_kwargs.pop("default_model", workflow.default_model or "mock:test"),
        decisions=decisions,
        **ctx_kwargs,
    )
    return run_workflow(workflow, merged, ctx)


def run_workflow_file_test(
    path: Path | str,
    *,
    inputs: Mapping[str, Any] | None = None,
    llm: Any | None = None,
    settings: Any | None = None,
    persist: bool = False,
    decisions: Mapping[str, str] | None = None,
    extra_tools: ToolRegistry | None = None,
    **kwargs: Any,
) -> RunState:
    """``run_workflow_file`` with test-friendly defaults (no persist)."""
    return run_workflow_file(
        path,
        inputs=inputs,
        llm=llm,
        settings=settings,
        persist=persist,
        decisions=decisions,
        extra_tools=extra_tools,
        **kwargs,
    )


def fail(message: str) -> LLMError:
    return LLMError(message)
