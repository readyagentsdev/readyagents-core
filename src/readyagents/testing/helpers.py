"""Helpers that wrap the same engine path the CLI uses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from readyagents.decide.base import validate_questions
from readyagents.decide.types import Answer, DecideState, Decision, Question
from readyagents.errors import DecideError, LLMError
from readyagents.llm.base import CompletionResult, Message, ToolCall
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


class FakeDecider:
    """In-process Decider. Queue decisions or errors; never opens a socket."""

    name = "fake"

    def __init__(self, *, model: str = "fake") -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[Decision | BaseException | Mapping[str, Any]] = []
        self._model = model

    def enqueue(
        self,
        decision: Decision | Mapping[str, Any] | None = None,
        *,
        error: BaseException | None = None,
    ) -> FakeDecider:
        if error is not None:
            self._queue.append(error)
        elif decision is not None:
            self._queue.append(decision)
        return self

    def decide(
        self,
        *,
        state: DecideState,
        questions: Mapping[str, Question],
        model: str,
        timeout: float | None = None,
    ) -> Decision:
        validate_questions(questions)
        self.calls.append(
            {"state": state, "questions": dict(questions), "model": model, "timeout": timeout}
        )
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, Decision):
                return item
            return _decision_from_mapping(item, questions, model=model or self._model)
        return _default_decision(questions, model=model or self._model)


def _default_decision(questions: Mapping[str, Question], *, model: str) -> Decision:
    answers: dict[str, Answer] = {}
    for key, question in questions.items():
        if question.type == "choice" and isinstance(question.criteria, Mapping):
            choice = next(iter(question.criteria))
            answers[key] = Answer(type="choice", choice=str(choice), confidence=1.0)
        elif question.type == "score":
            answers[key] = Answer(type="score", score=0.0, confidence=1.0)
        else:
            answers[key] = Answer(type="noul", noul=0.9, confidence=0.8)
    return Decision(answers=answers, model=model, decider="fake")


def _decision_from_mapping(
    raw: Mapping[str, Any], questions: Mapping[str, Question], *, model: str
) -> Decision:
    answers: dict[str, Answer] = {}
    blob = raw.get("answers") if isinstance(raw.get("answers"), Mapping) else raw
    if not isinstance(blob, Mapping):
        raise DecideError("fake decision answers must be a mapping")
    for key, question in questions.items():
        payload = blob.get(key)
        if isinstance(payload, Answer):
            answers[key] = payload
            continue
        if question.type == "choice":
            if isinstance(payload, str):
                choice = payload
            elif isinstance(payload, Mapping):
                choice = str(payload.get("choice"))
            else:
                choice = str(payload)
            answers[key] = Answer(type="choice", choice=choice, confidence=1.0)
        elif question.type == "score":
            if isinstance(payload, (int, float)):
                score = float(payload)
            elif isinstance(payload, Mapping):
                score = float(payload.get("score") or 0)
            else:
                score = 0.0
            answers[key] = Answer(type="score", score=score, confidence=1.0)
        else:
            if isinstance(payload, (int, float)):
                noul = float(payload)
            elif isinstance(payload, Mapping):
                noul = float(payload.get("noul") or 0)
            else:
                noul = 0.0
            answers[key] = Answer(type="noul", noul=noul, confidence=1.0)
    return Decision(
        answers=answers,
        model=str(raw.get("model") or model),
        decider=str(raw.get("decider") or "fake"),
        usage=dict(raw.get("usage") or {}),
    )


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
        self.calls.append({"model": model, "messages": messages, "tools": tools, **kwargs})
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
    replies = ctx_kwargs.pop("converse_replies", None)
    ctx = ExecutionContext(
        workflow,
        tools or ToolRegistry(),
        llm=llm,
        default_model=ctx_kwargs.pop("default_model", workflow.default_model or "mock:test"),
        decisions=decisions,
        **ctx_kwargs,
    )
    if replies:
        ctx.converse_replies = {str(k): str(v) for k, v in dict(replies).items()}
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
