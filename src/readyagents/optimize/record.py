"""Candidate / iteration / report records. ``ok`` is popped by the CLI envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.optimize.layout import SCHEMA_RUN, STOP_COMPLETE


@dataclass
class ScoreSnapshot:
    passed: int = 0
    failed: int = 0
    total: int = 0
    pass_rate: float = 0.0
    spend_usd: float = 0.0
    failed_names: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "pass_rate": self.pass_rate,
            "spend_usd": self.spend_usd,
            "failed_names": list(self.failed_names),
            "reasons": dict(self.reasons),
            "failures": list(self.failures),
        }


@dataclass
class CandidateRecord:
    prompt_id: str
    version: int
    content_hash: str
    text: str
    train_score: float
    held_out_score: float | None = None
    regressions: list[str] = field(default_factory=list)
    adopted: bool = False
    config_shaped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "text": self.text,
            "train_score": self.train_score,
            "held_out_score": self.held_out_score,
            "regressions": list(self.regressions),
            "adopted": self.adopted,
            "config_shaped": self.config_shaped,
        }


@dataclass
class IterationRecord:
    index: int
    train: dict[str, Any] = field(default_factory=dict)
    held_out: dict[str, Any] = field(default_factory=dict)
    spend_usd: float = 0.0
    candidates: list[CandidateRecord] = field(default_factory=list)
    promoted: bool = False
    regressions: list[str] = field(default_factory=list)
    blocked_cases: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "train": dict(self.train),
            "held_out": dict(self.held_out),
            "spend_usd": self.spend_usd,
            "candidates": [row.as_dict() for row in self.candidates],
            "promoted": self.promoted,
            "regressions": list(self.regressions),
            "blocked_cases": list(self.blocked_cases),
        }


@dataclass
class OptimizeReport:
    ok: bool
    stop_reason: str = STOP_COMPLETE
    promoted: bool = False
    prompt_id: str = ""
    node_id: str = ""
    active_version: int = 0
    content_hash: str = ""
    min_improvement: float = 0.0
    baseline_score: float = 0.0
    best_score: float = 0.0
    delta: float = 0.0
    spend_usd: float = 0.0
    held_out: dict[str, Any] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    iterations: list[IterationRecord] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    blocked_cases: list[dict[str, str]] = field(default_factory=list)
    stop: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": SCHEMA_RUN,
            "stop_reason": self.stop_reason,
            "promoted": self.promoted,
            "prompt_id": self.prompt_id,
            "node_id": self.node_id,
            "active_version": self.active_version,
            "content_hash": self.content_hash,
            "min_improvement": self.min_improvement,
            "baseline_score": self.baseline_score,
            "best_score": self.best_score,
            "delta": self.delta,
            "spend_usd": self.spend_usd,
            "held_out": dict(self.held_out),
            "regressions": list(self.regressions),
            "iterations": [row.as_dict() for row in self.iterations],
            "approval": None if self.approval is None else dict(self.approval),
            "blocked_cases": list(self.blocked_cases),
        }
