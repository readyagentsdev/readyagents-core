"""Decider protocol and shared question-shape validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from readyagents.decide.types import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    DecideState,
    Decision,
    Question,
    QuestionType,
)
from readyagents.errors import DecideError

_NOUL_CRITERIA_KEYS = frozenset({"true", "false"})


class Decider(Protocol):
    """Typed-question protocol. Not an LLMProvider — answers are not text."""

    name: str

    def decide(
        self,
        *,
        state: DecideState,
        questions: Mapping[str, Question],
        model: str,
        timeout: float | None = None,
    ) -> Decision: ...


def validate_questions(questions: Mapping[str, Question]) -> None:
    """Raise DecideError for anything the vendor would reject, before egress."""
    if not questions:
        raise DecideError("decider request requires at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise DecideError(f"decider request has {len(questions)} questions; max is {MAX_QUESTIONS}")
    seen: set[str] = set()
    for key, question in questions.items():
        name = str(key)
        if not name or not name.isidentifier():
            raise DecideError(f"question key {name!r} is not a template-safe identifier")
        if name in seen:
            raise DecideError(f"duplicate question key {name!r}")
        seen.add(name)
        if not isinstance(question, Question):
            raise DecideError(f"question {name!r} is not a Question")
        instructions = (question.instructions or "").strip()
        if not instructions:
            raise DecideError(f"question {name!r} requires non-empty instructions")
        qtype = question.type
        criteria = question.criteria
        if qtype == "choice":
            if not isinstance(criteria, Mapping) or isinstance(criteria, (str, bytes)):
                raise DecideError(
                    f"question {name!r}: choice criteria must be a mapping of option to description"
                )
            n = len(criteria)
            if n < 2 or n > MAX_OPTIONS:
                raise DecideError(
                    f"question {name!r}: choice criteria must have 2–{MAX_OPTIONS} options, got {n}"
                )
            for option in criteria:
                if not str(option).strip():
                    raise DecideError(f"question {name!r}: choice option keys must be non-empty")
        elif qtype == "score":
            if not isinstance(criteria, list):
                raise DecideError(
                    f"question {name!r}: score criteria must be an ordered list of levels"
                )
            n = len(criteria)
            if n < 2 or n > MAX_OPTIONS:
                raise DecideError(
                    f"question {name!r}: score criteria must have 2–{MAX_OPTIONS} levels, got {n}"
                )
        elif qtype == "noul":
            if criteria is None:
                continue
            if not isinstance(criteria, Mapping) or isinstance(criteria, (str, bytes)):
                raise DecideError(
                    f"question {name!r}: noul criteria must be omitted or a mapping "
                    "with keys true/false"
                )
            extra = set(str(k) for k in criteria) - _NOUL_CRITERIA_KEYS
            if extra:
                raise DecideError(
                    f"question {name!r}: noul criteria keys must be subset of "
                    f"{{'true', 'false'}}, got extra {sorted(extra)}"
                )
        else:
            raise DecideError(f"question {name!r}: unknown type {qtype!r}")


def questions_from_mapping(raw: Mapping[str, Any]) -> dict[str, Question]:
    """Build ``Question`` objects from a YAML/JSON mapping. Shared by node + classify."""
    out: dict[str, Question] = {}
    for key, spec in raw.items():
        if not isinstance(spec, Mapping):
            raise DecideError(f"question {key!r} must be a mapping")
        qtype = str(spec.get("type") or "").strip()
        if qtype not in {"noul", "choice", "score"}:
            raise DecideError(f"question {key!r}: type must be noul, choice, or score")
        instructions = str(spec.get("instructions") or "")
        criteria = spec.get("criteria")
        if isinstance(criteria, Mapping) and not isinstance(criteria, (str, bytes)):
            criteria = {str(k): str(v) for k, v in criteria.items()}
        elif isinstance(criteria, list):
            criteria = [str(item) for item in criteria]
        elif criteria is not None:
            raise DecideError(f"question {key!r}: criteria must be a mapping, list, or omitted")
        out[str(key)] = Question(
            type=cast(QuestionType, qtype),
            instructions=instructions,
            criteria=criteria,
        )
    validate_questions(out)
    return out
