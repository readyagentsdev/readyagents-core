"""Per-node decision records. A projection over the run record and audit — not a second store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import NodeResult, RunState

_APPROVE = {"approve", "approved", "yes", "true", "accept", "ok"}
_REJECT = {"reject", "rejected", "deny", "denied", "no", "false"}


@dataclass
class HumanReview:
    actor: str | None = None
    role: str | None = None
    timestamp: str | None = None
    decision: str | None = None
    outcome: str | None = None
    signature_status: str = "n/a"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionRecord:
    node_id: str
    node_type: str
    status: str
    sequence: int
    occurrence: int = 0
    attempts: int = 1
    started_at: str | None = None
    finished_at: str | None = None
    inputs_in_scope: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    prompt: str | None = None
    model: str | None = None
    model_version: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost_micros: int | None = None
    rationale: Any = None
    human: HumanReview | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.human is None:
            data["human"] = None
        return data


def project_decisions(
    state: RunState,
    workflow: WorkflowSpec | None = None,
    audit: list[dict[str, Any]] | None = None,
    *,
    redactor: Any = None,
) -> list[DecisionRecord]:
    """Build one DecisionRecord per executed node result."""
    nodes = workflow.node_map() if workflow is not None else {}
    audit = list(audit or [])
    counts: dict[str, int] = {}
    records: list[DecisionRecord] = []
    for index, result in enumerate(state.results):
        counts[result.node_id] = counts.get(result.node_id, 0) + 1
        spec = nodes.get(result.node_id)
        prompt = getattr(spec, "prompt", None) if spec is not None else None
        model = getattr(spec, "model", None) if spec is not None else None
        if not model and workflow is not None:
            model = workflow.default_model
        rationale_key = getattr(spec, "rationale_key", None) if spec is not None else None
        rationale = None
        if rationale_key:
            if isinstance(result.output, dict) and rationale_key in result.output:
                rationale = result.output.get(rationale_key)
            elif result.node_id == rationale_key:
                rationale = result.output
            elif rationale_key in state.output_keys:
                rationale = state.output_keys.get(rationale_key)
        usage = dict(result.usage or {})
        cost = usage.get("cost_micros")
        human = _human_for(result, audit)
        record = DecisionRecord(
            node_id=result.node_id,
            node_type=result.type,
            status=result.status,
            sequence=index,
            occurrence=counts[result.node_id] - 1,
            attempts=result.attempts,
            started_at=result.started_at or None,
            finished_at=result.finished_at or None,
            inputs_in_scope=_inputs_in_scope(state, spec),
            output=result.output,
            prompt=prompt,
            model=model,
            model_version=_model_version(usage),
            tool_calls=list(result.tool_rounds or []),
            usage=usage,
            cost_micros=int(cost) if cost is not None else None,
            rationale=rationale,
            human=human,
        )
        records.append(_maybe_redact(record, redactor))
    return records


def _inputs_in_scope(state: RunState, spec: Any) -> dict[str, Any]:
    scoped = dict(state.inputs)
    if spec is None:
        return scoped
    prompt = str(getattr(spec, "prompt", "") or "")
    system = str(getattr(spec, "system", "") or "")
    blob = prompt + " " + system + " " + str(getattr(spec, "template", "") or "")
    for key, value in state.output_keys.items():
        if key and f"{{{{{key}}}}}" in blob:
            scoped[key] = value
    return scoped


def _human_for(result: NodeResult, audit: list[dict[str, Any]]) -> HumanReview | None:
    if result.type not in {"approval", "tool"} and result.status != "paused":
        events = [
            e
            for e in audit
            if e.get("node_id") == result.node_id and e.get("event") in {"decision", "paused"}
        ]
        if result.type != "approval" and not events:
            return None
    events = [
        e
        for e in audit
        if e.get("node_id") == result.node_id and e.get("event") in {"decision", "paused"}
    ]
    if result.type != "approval" and not events:
        return None
    decision_events = [e for e in events if e.get("event") == "decision"]
    row = decision_events[-1] if decision_events else (events[-1] if events else None)
    if row is None and result.type != "approval":
        return None
    raw = str((row or {}).get("decision") or "").strip().lower()
    if raw in _APPROVE:
        outcome = "confirm"
        decision = "approve"
    elif raw in _REJECT:
        outcome = "override"
        decision = "reject"
    elif raw:
        outcome = "override"
        decision = raw
    else:
        outcome = None
        decision = None
    signed = (row or {}).get("signed")
    if signed is True:
        signature_status = "signed"
    elif signed is False:
        signature_status = "unsigned"
    else:
        signature_status = "n/a"
    return HumanReview(
        actor=(row or {}).get("actor"),
        role=(row or {}).get("role") or (row or {}).get("actor_role"),
        timestamp=(row or {}).get("ts"),
        decision=decision,
        outcome=outcome,
        signature_status=signature_status,
    )


def _model_version(usage: dict[str, int]) -> str | None:
    _ = usage
    return None


def _maybe_redact(record: DecisionRecord, redactor: Any) -> DecisionRecord:
    if redactor is None:
        return record
    record.inputs_in_scope = redactor.redact(record.inputs_in_scope)
    record.output = redactor.redact(record.output)
    record.prompt = redactor.redact(record.prompt) if record.prompt is not None else None
    record.rationale = redactor.redact(record.rationale)
    record.tool_calls = redactor.redact(record.tool_calls)
    return record
