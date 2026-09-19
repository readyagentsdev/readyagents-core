"""Typed decision request/response values for System One deciders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

QuestionType = Literal["noul", "choice", "score"]
DecideState = str | Mapping[str, Any] | list[str]

# Vendor caps, see .tmp/jev-implement/00-jev-research.md §3
MAX_OPTIONS = 255
MAX_QUESTIONS = 64  # ours, not the vendor's: a sanity cap


@dataclass(frozen=True)
class Question:
    """One typed judgment to make about a state."""

    type: QuestionType
    instructions: str
    # choice -> {option_key: description}
    # score  -> ordered list of level descriptions
    # noul   -> optional {"true": ..., "false": ...}
    criteria: Mapping[str, str] | list[str] | None = None

    def wire(self) -> dict[str, Any]:
        """Vendor-neutral JSON body fragment."""
        out: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = (
                list(self.criteria) if isinstance(self.criteria, list) else dict(self.criteria)
            )
        return out


@dataclass(frozen=True)
class Answer:
    """One typed answer. Exactly one of noul/choice/score is meaningful."""

    type: QuestionType
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    legend: dict[str, str] = field(default_factory=dict)

    @property
    def value(self) -> Any:
        """The single value a template or condition should see."""
        if self.type == "noul":
            return self.noul
        if self.type == "choice":
            return self.choice
        return self.score

    def effective_confidence(self, *, threshold: float = 0.5) -> float:
        """Confidence as a 0..1 margin. Derived when the vendor omits it.

        This is a MARGIN, not P(correct). See 00-jev-research.md §5.
        """
        if self.confidence is not None:
            return float(self.confidence)
        if self.type == "noul" and self.noul is not None:
            v = float(self.noul)
            if v >= threshold:
                return (v - threshold) / (1.0 - threshold) if threshold < 1.0 else 1.0
            return (threshold - v) / threshold if threshold > 0.0 else 1.0
        return 0.0


@dataclass(frozen=True)
class Decision:
    """A full decider response."""

    answers: dict[str, Answer]
    model: str
    decider: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    def low_confidence_keys(self, min_confidence: float | None) -> list[str]:
        """Question keys that fall below ``min_confidence``.

        The shim has no calibrated probability: when a threshold is set, every
        answer is treated as low-confidence so the workflow degrades toward a
        human, never past them.
        """
        if min_confidence is None:
            return []
        if self.decider == "shim":
            return list(self.answers)
        return [
            key
            for key, answer in self.answers.items()
            if answer.effective_confidence() < float(min_confidence)
        ]

    def as_dict(self, *, min_confidence: float | None = None) -> dict[str, Any]:
        """Stable, JSON-safe projection: node output, cassette entry, audit."""
        answers: dict[str, Any] = {}
        for key, answer in self.answers.items():
            payload: dict[str, Any] = {
                "type": answer.type,
                "value": answer.value,
                "confidence": answer.effective_confidence(),
                "probabilities": dict(answer.probabilities),
                "legend": dict(answer.legend),
            }
            if answer.type == "noul":
                payload["noul"] = answer.noul
            elif answer.type == "choice":
                payload["choice"] = answer.choice
            else:
                payload["score"] = answer.score
            answers[key] = payload
        return {
            "decider": self.decider,
            "model": self.model,
            "answers": answers,
            "min_confidence": min_confidence,
            "low_confidence": self.low_confidence_keys(min_confidence),
            "usage": dict(self.usage),
        }
