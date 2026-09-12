"""Correction object, consent record, and export row schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.feedback.layout import HUMAN, SCHEMA_CORRECTION, SCHEMA_EXPORT, SCHEMA_STATS


@dataclass
class Correction:
    id: str
    run_id: str
    node_id: str
    decision_id: str
    kind: str = HUMAN
    original: str = ""
    diff: list[dict[str, Any]] = field(default_factory=list)
    role: str = ""
    reason: str = ""
    rating: int | None = None
    label: str | None = None
    signal: str | None = None
    model: str = ""
    ts: str = ""
    consent_scope: str = ""
    workflow: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_CORRECTION,
            "id": self.id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "decision_id": self.decision_id,
            "kind": self.kind,
            "original": self.original,
            "diff": list(self.diff),
            "role": self.role,
            "reason": self.reason,
            "rating": self.rating,
            "label": self.label,
            "signal": self.signal,
            "model": self.model,
            "ts": self.ts,
            "consent_scope": self.consent_scope,
            "workflow": self.workflow,
            "inputs": dict(self.inputs),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Correction:
        return cls(
            id=str(data.get("id") or ""),
            run_id=str(data.get("run_id") or ""),
            node_id=str(data.get("node_id") or ""),
            decision_id=str(data.get("decision_id") or ""),
            kind=str(data.get("kind") or HUMAN),
            original=str(data.get("original") or ""),
            diff=[dict(row) for row in (data.get("diff") or []) if isinstance(row, dict)],
            role=str(data.get("role") or ""),
            reason=str(data.get("reason") or ""),
            rating=int(data["rating"]) if data.get("rating") is not None else None,
            label=str(data["label"]) if data.get("label") is not None else None,
            signal=str(data["signal"]) if data.get("signal") else None,
            model=str(data.get("model") or ""),
            ts=str(data.get("ts") or ""),
            consent_scope=str(data.get("consent_scope") or ""),
            workflow=str(data.get("workflow") or ""),
            inputs=dict(data.get("inputs") or {}),
            source=str(data.get("source") or ""),
        )


@dataclass
class ExportReport:
    ok: bool
    format: str
    written: int = 0
    excluded: int = 0
    excluded_unconsented: int = 0
    excluded_secret: int = 0
    path: str = ""
    warned: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": SCHEMA_EXPORT,
            "format": self.format,
            "written": self.written,
            "excluded": self.excluded,
            "excluded_unconsented": self.excluded_unconsented,
            "excluded_secret": self.excluded_secret,
            "path": self.path,
            "warned": self.warned,
        }


@dataclass
class StatsReport:
    ok: bool
    by: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    sample_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": SCHEMA_STATS,
            "by": self.by,
            "sample_size": self.sample_size,
            "n": self.sample_size,
            "rows": list(self.rows),
            "significance": None,
        }
