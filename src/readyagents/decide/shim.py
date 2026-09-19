"""Keyless LLM-backed Decider. Not a Jev replacement; no calibrated confidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from readyagents.decide.base import validate_questions
from readyagents.decide.types import Answer, DecideState, Decision, Question
from readyagents.errors import DecideError
from readyagents.llm.base import Message
from readyagents.workflow.structured import parse_json_payload


class ShimDecider:
    """Structured-output fallback so decide workflows run without a TypeSafe key."""

    name = "shim"

    def __init__(self, llm: Any, *, model: str | None = None) -> None:
        self._llm = llm
        self._model = (model or "").strip() or None

    def decide(
        self,
        *,
        state: DecideState,
        questions: Mapping[str, Question],
        model: str,
        timeout: float | None = None,
    ) -> Decision:
        validate_questions(questions)
        if self._llm is None or not hasattr(self._llm, "complete"):
            raise DecideError("shim decider requires an LLM")
        answering = (model or self._model or getattr(self._llm, "name", None) or "shim").strip()
        prompt = _render_prompt(state, questions)
        try:
            result = self._llm.complete(
                [Message(role="user", content=prompt)],
                model=answering,
            )
        except DecideError:
            raise
        except Exception as exc:
            raise DecideError(f"shim LLM call failed: {type(exc).__name__}") from exc
        text = getattr(result, "text", "") or ""
        try:
            payload = parse_json_payload(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise DecideError("shim output is not JSON") from exc
        if not isinstance(payload, dict):
            raise DecideError("shim output must be a JSON object keyed by question id")
        answers: dict[str, Answer] = {}
        for key, question in questions.items():
            if key not in payload:
                raise DecideError(f"shim output is missing requested answer {key!r}")
            answers[key] = _parse_shim_answer(key, payload[key], question)
        usage = dict(getattr(result, "usage", None) or {})
        return Decision(
            answers=answers,
            model=str(getattr(result, "model", None) or answering),
            decider=self.name,
            usage=usage,
            raw=payload,
        )


def _render_prompt(state: DecideState, questions: Mapping[str, Question]) -> str:
    lines = [
        "Answer the questions about the state.",
        "Return a single JSON object keyed by question id.",
        "Use only declared values. Do not include extra keys.",
        "",
        "Questions:",
    ]
    for key, question in questions.items():
        lines.append(f"- {key} ({question.type}): {question.instructions.strip()}")
        if question.type == "choice" and isinstance(question.criteria, Mapping):
            allowed = ", ".join(str(k) for k in question.criteria)
            lines.append(f"  Allowed values: {allowed}")
        elif question.type == "score" and isinstance(question.criteria, list):
            high = len(question.criteria) - 1
            lines.append(f"  Return a number in [0, {high}]. Levels:")
            for i, label in enumerate(question.criteria):
                lines.append(f"    {i}: {label}")
        elif question.type == "noul":
            lines.append("  Return a number in [0, 1] (probability the statement is true).")
    lines.append("")
    lines.append("State:")
    if isinstance(state, str):
        lines.append(state)
    else:
        lines.append(json.dumps(state, ensure_ascii=False, default=str))
    return "\n".join(lines)


def _parse_shim_answer(key: str, raw: Any, question: Question) -> Answer:
    if question.type == "noul":
        if isinstance(raw, bool):
            value = 1.0 if raw else 0.0
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise DecideError(f"shim answer {key!r} is not a noul number") from exc
        if value < 0.0 or value > 1.0:
            raise DecideError(f"shim answer {key!r} noul {value} is outside [0, 1]")
        return Answer(type="noul", noul=value, confidence=None)
    if question.type == "choice":
        choice = str(raw)
        allowed = set(question.criteria or {}) if isinstance(question.criteria, Mapping) else set()
        if allowed and choice not in allowed:
            raise DecideError(f"shim answer {key!r} value {choice!r} is outside the declared space")
        return Answer(type="choice", choice=choice, confidence=None)
    try:
        score = float(raw)
    except (TypeError, ValueError) as exc:
        raise DecideError(f"shim answer {key!r} is not a score number") from exc
    if isinstance(question.criteria, list):
        high = float(len(question.criteria) - 1)
        if score < 0.0 or score > high:
            raise DecideError(f"shim answer {key!r} score {score} is outside [0, {high}]")
    return Answer(type="score", score=score, confidence=None)
