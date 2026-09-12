"""Fidelity report: translated / approximated / unsupported. Structural only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MUST_TEST = (
    "This is a structural translation only, not behavioural equivalence. "
    "The result must be tested before use."
)


@dataclass
class NodeReport:
    id: str
    kind: str
    title: str
    status: str
    reason: str
    nearest: str | None = None
    structural: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "reason": self.reason,
            "nearest": self.nearest,
            "structural": self.structural,
        }


@dataclass
class FidelityReport:
    source: str
    name: str
    nodes: list[NodeReport] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statement: str = MUST_TEST

    @property
    def coverage(self) -> float:
        if not self.nodes:
            return 0.0
        ok = sum(1 for item in self.nodes if item.status in {"translated", "approximated"})
        return round(100.0 * ok / len(self.nodes), 1)

    def as_dict(self) -> dict[str, Any]:
        counts = {
            "translated": sum(1 for n in self.nodes if n.status == "translated"),
            "approximated": sum(1 for n in self.nodes if n.status == "approximated"),
            "unsupported": sum(1 for n in self.nodes if n.status == "unsupported"),
        }
        return {
            "source": self.source,
            "name": self.name,
            "coverage_percent": self.coverage,
            "counts": counts,
            "nodes": [item.as_dict() for item in self.nodes],
            "follow_ups": list(self.follow_ups),
            "warnings": list(self.warnings),
            "statement": self.statement,
            "equivalence": False,
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Import fidelity report ({self.source})",
            "",
            MUST_TEST,
            "",
            f"Workflow: {self.name}",
            f"Coverage: {self.coverage}% "
            f"(translated/approximated over {len(self.nodes)} source nodes)",
            "",
            "| Node | Kind | Status | Reason |",
            "| --- | --- | --- | --- |",
        ]
        for item in self.nodes:
            reason = item.reason.replace("|", "/")
            lines.append(f"| {item.id} | {item.kind} | {item.status} | {reason} |")
        if self.follow_ups:
            lines.extend(["", "## Manual follow-ups", ""])
            for row in self.follow_ups:
                lines.append(f"- {row}")
        if self.warnings:
            lines.extend(["", "## Warnings", ""])
            for row in self.warnings:
                lines.append(f"- {row}")
        lines.append("")
        return "\n".join(lines)
