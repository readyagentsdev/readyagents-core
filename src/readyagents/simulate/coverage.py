"""Honest coverage: declared vs reached. No completeness percentage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.simulate.generate import _walk
from readyagents.simulate.layout import KIND_APPROVAL, KIND_CONDITION, KIND_ERROR, KIND_FOREACH
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


@dataclass
class CoveragePoint:
    kind: str
    id: str
    reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "reached": self.reached}


@dataclass
class CoverageReport:
    points: list[CoveragePoint] = field(default_factory=list)

    def mark(self, kind: str, ident: str) -> None:
        for point in self.points:
            if point.kind == kind and point.id == ident:
                point.reached = True
                return

    def reached(self) -> list[CoveragePoint]:
        return [p for p in self.points if p.reached]

    def unreached(self) -> list[CoveragePoint]:
        return [p for p in self.points if not p.reached]

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": len(self.points),
            "reached": [p.as_dict() for p in self.reached()],
            "unreached": [p.as_dict() for p in self.unreached()],
        }


def declared_coverage(workflow: WorkflowSpec) -> CoverageReport:
    points: list[CoveragePoint] = []
    for node in _walk(workflow.nodes):
        kind = str(node.type)
        if kind == "condition":
            points.append(CoveragePoint(KIND_CONDITION, f"{node.id}:then"))
            points.append(CoveragePoint(KIND_CONDITION, f"{node.id}:else"))
        elif kind == "approval":
            points.append(CoveragePoint(KIND_APPROVAL, f"{node.id}:approve"))
            points.append(CoveragePoint(KIND_APPROVAL, f"{node.id}:reject"))
        elif kind == "foreach":
            points.append(CoveragePoint(KIND_FOREACH, f"{node.id}:empty"))
            points.append(CoveragePoint(KIND_FOREACH, f"{node.id}:items"))
        points.append(CoveragePoint(KIND_ERROR, f"{node.id}:error"))
    return CoverageReport(points=points)


def apply_run(
    report: CoverageReport,
    state: RunState,
    *,
    decisions: dict[str, str] | None = None,
) -> None:
    ran = {row.node_id for row in state.results}
    votes = dict(decisions or {})
    for point in report.points:
        ident = point.id
        node_id, _, suffix = ident.partition(":")
        if point.kind == KIND_CONDITION:
            if suffix == "then" and node_id in ran:
                # Reached if the condition node ran and then-branch successor ran.
                point.reached = True if _then_taken(state, node_id) else point.reached
            elif suffix == "else" and node_id in ran:
                point.reached = True if _else_taken(state, node_id) else point.reached
        elif point.kind == KIND_APPROVAL:
            vote = str(votes.get(node_id) or "").lower()
            if suffix == "approve" and vote in {"approve", "approved", "yes"}:
                point.reached = True
            if suffix == "reject" and vote in {"reject", "rejected", "no"}:
                point.reached = True
        elif point.kind == KIND_FOREACH:
            if suffix == "empty" and node_id in ran and _foreach_empty(state, node_id):
                point.reached = True
            if suffix == "items" and node_id in ran and not _foreach_empty(state, node_id):
                point.reached = True
        elif point.kind == KIND_ERROR:
            if state.status in {"failed", "error"} and _is_failed_node(state, node_id):
                point.reached = True


def _then_taken(state: RunState, node_id: str) -> bool:
    return _branch_flag(state, node_id, want=True)


def _else_taken(state: RunState, node_id: str) -> bool:
    return _branch_flag(state, node_id, want=False)


def _branch_flag(state: RunState, node_id: str, *, want: bool) -> bool:
    for row in state.results:
        if row.node_id != node_id:
            continue
        out = row.output
        if isinstance(out, dict) and "matched" in out:
            return bool(out.get("matched")) is want
        if isinstance(out, dict) and "next" in out:
            nxt = str(out.get("next") or "")
            return (nxt != "else" and want) or (nxt == "else" and not want)
    return False


def _foreach_empty(state: RunState, node_id: str) -> bool:
    for row in state.results:
        if row.node_id != node_id:
            continue
        out = row.output
        if isinstance(out, dict) and "count" in out:
            try:
                return int(out["count"]) == 0
            except (TypeError, ValueError):
                return False
        if isinstance(out, list):
            return len(out) == 0
    return True


def _error_mentions(state: RunState, node_id: str) -> bool:
    blob = " ".join(str(item) for item in (state.errors or []))
    return node_id in blob


def _failed_node_id(state: RunState) -> str | None:
    if state.pending_node:
        return state.pending_node
    for row in reversed(state.results):
        if row.status in {"failed", "error"} or row.error:
            return row.node_id
    return None


def _is_failed_node(state: RunState, node_id: str) -> bool:
    failed = _failed_node_id(state)
    if failed:
        return failed == node_id
    return _error_mentions(state, node_id)
