"""Pure start decision: start, return existing, or refuse. No listener."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.approvals.gate import parse_expires_in
from readyagents.decisions.signing import verify_signed_body
from readyagents.errors import TemplateError, TriggerCeiling, TriggerRefused
from readyagents.triggers.caps import RateLimiter, refuse_before_parse
from readyagents.triggers.store import (
    ConcurrencyGate,
    DeadLetterLog,
    EventLog,
    IdempotencyStore,
    TriggerSpend,
)
from readyagents.workflow.schema import TriggerSpec, WorkflowSpec
from readyagents.workflow.templates import interpolate

SOURCE_KINDS = frozenset({"webhook", "file", "queue", "schedule"})


@dataclass
class TriggerDecision:
    action: str
    reason: str
    run_id: str | None = None
    state: Any = None
    trigger: str | None = None
    event_id: str | None = None
    digest: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    dead_letter_id: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        row = {
            "action": self.action,
            "reason": self.reason,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "event_id": self.event_id,
            "digest": self.digest,
            "inputs": dict(self.inputs),
            "dead_letter_id": self.dead_letter_id,
            "provenance": dict(self.provenance),
        }
        if self.state is not None and hasattr(self.state, "status"):
            row["status"] = self.state.status
        return row


def event_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def event_id_for(trigger: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(
        {"trigger": trigger, "payload": payload}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def decide_trigger(
    workflow: WorkflowSpec,
    *,
    trigger_name: str,
    raw: bytes | str | dict[str, Any],
    source_kind: str,
    signature: str | None = None,
    secret: str | None = None,
    home: Path | None = None,
    clock: Any = None,
    auditor: Any = None,
    store: IdempotencyStore | None = None,
    gate: ConcurrencyGate | None = None,
    spend: TriggerSpend | None = None,
    limiter: RateLimiter | None = None,
    letters: DeadLetterLog | None = None,
    events: EventLog | None = None,
    starter: Any = None,
    dry_run: bool = False,
    persist: bool = True,
    settings: Any = None,
) -> TriggerDecision:
    """Start a run, return the original, or refuse with a typed reason."""
    home_path = Path(home) if home is not None else Path(".")
    from readyagents.triggers.runtime import gate_for, limiter_for, spend_for, store_for

    store = store or store_for(home_path, clock=clock)
    gate = gate or gate_for(home_path)
    spend = spend or spend_for(home_path)
    letters = letters or DeadLetterLog(home_path)
    events = events or EventLog(home_path)
    limiter = limiter if limiter is not None else limiter_for(home_path, clock=clock)
    try:
        trigger = _find(workflow, trigger_name)
    except TriggerRefused as extra:
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        letter_id = letters.record(
            reason=extra.reason,
            trigger=trigger_name,
            digest=None,
            event_id=None,
            raw=raw_text[:2000],
        )
        events.record(
            {
                "action": "refuse",
                "reason": extra.reason,
                "trigger": trigger_name,
                "dead_letter_id": letter_id,
            }
        )
        _audit(auditor, "trigger_refused", trigger=trigger_name, reason=extra.reason)
        return TriggerDecision(
            action="refuse",
            reason=extra.reason,
            trigger=trigger_name,
            dead_letter_id=letter_id,
        )

    def _refuse(
        message: str, reason: str, digest: str | None = None, raw_text: str = ""
    ) -> TriggerDecision:
        _audit(auditor, "trigger_refused", trigger=trigger_name, reason=reason)
        letter_id = letters.record(
            reason=reason,
            trigger=trigger_name,
            digest=digest,
            event_id=None,
            raw=raw_text,
        )
        events.record(
            {
                "action": "refuse",
                "reason": reason,
                "trigger": trigger_name,
                "dead_letter_id": letter_id,
            }
        )
        return TriggerDecision(
            action="refuse",
            reason=reason,
            trigger=trigger_name,
            digest=digest,
            dead_letter_id=letter_id,
        )

    try:
        body, payload, digest = _parse(
            raw,
            trigger=trigger_name,
            limiter=limiter,
        )
    except TriggerRefused as exc:
        raw_text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        return _refuse(str(exc), exc.reason, digest=None, raw_text=raw_text[:2000])

    raw_text = body.decode("utf-8", errors="replace")
    kind = str(source_kind or "").strip().lower()
    if kind not in SOURCE_KINDS:
        return _refuse("unknown source kind", "source", digest, raw_text)
    if kind != trigger.accepts.kind:
        return _refuse(
            f"source {kind} does not match trigger {trigger.accepts.kind}",
            "kind",
            digest,
            raw_text,
        )
    try:
        _check_payload_schema(trigger, payload)
    except TriggerRefused as exc:
        return _refuse(str(exc), exc.reason, digest, raw_text)

    eid = event_id_for(trigger.name, payload)
    try:
        mapped = _map_inputs(trigger, payload)
        idem_key = interpolate(trigger.idempotency_key, {"event": payload, **mapped})
    except TemplateError as extra:
        return _refuse(str(extra), "mapping", digest, raw_text)
    if not str(idem_key).strip():
        return _refuse("idempotency key interpolated empty", "idempotency", digest, raw_text)

    if dry_run:
        events.record({"action": "test", "trigger": trigger.name, "event_id": eid})
        return TriggerDecision(
            action="test",
            reason="mapped",
            trigger=trigger.name,
            event_id=eid,
            digest=digest,
            inputs=mapped,
        )

    try:
        _verify_signature(trigger, body, signature=signature, secret=secret, auditor=auditor)
    except TriggerRefused as extra:
        return _refuse(str(extra), extra.reason, digest, raw_text)

    window_s = parse_expires_in(trigger.idempotency_window)
    composite = f"{workflow.name}:{trigger.name}:{idem_key}"
    with store.lock_for(composite):
        existing = store.lookup(composite, window_s=window_s)
        if existing:
            _audit(auditor, "trigger_existing", trigger=trigger.name, run_id=existing)
            events.record(
                {
                    "action": "existing",
                    "trigger": trigger.name,
                    "run_id": existing,
                    "event_id": eid,
                }
            )
            return TriggerDecision(
                action="existing",
                reason="idempotent",
                run_id=existing,
                trigger=trigger.name,
                event_id=eid,
                digest=digest,
                inputs=mapped,
                provenance=_provenance(trigger, eid, digest, kind),
            )
        try:
            _check_budget(trigger, spend)
        except TriggerRefused as exc:
            return _refuse(str(exc), exc.reason, digest, raw_text)
        try:
            slot = gate.try_enter(trigger.name, trigger.concurrency, on_ceiling=trigger.on_ceiling)
        except TriggerCeiling as extra:
            letter_id = letters.record(
                reason="ceiling",
                trigger=trigger.name,
                digest=digest,
                event_id=eid,
                raw=raw_text,
                extra={"action": extra.action},
            )
            events.record(
                {
                    "action": extra.action,
                    "reason": "ceiling",
                    "trigger": trigger.name,
                    "dead_letter_id": letter_id,
                }
            )
            _audit(auditor, "trigger_ceiling", trigger=trigger.name, action=extra.action)
            return TriggerDecision(
                action=extra.action,
                reason="ceiling",
                trigger=trigger.name,
                event_id=eid,
                digest=digest,
                dead_letter_id=letter_id,
            )
        if slot == "defer":
            queued = gate.defer(
                trigger.name,
                {"raw": raw_text, "event_id": eid, "digest": digest},
            )
            letter_id = letters.record(
                reason="deferred",
                trigger=trigger.name,
                digest=digest,
                event_id=eid,
                raw=raw_text,
                extra={"queued": queued},
            )
            events.record(
                {
                    "action": "defer",
                    "reason": "ceiling",
                    "trigger": trigger.name,
                    "dead_letter_id": letter_id,
                }
            )
            return TriggerDecision(
                action="defer",
                reason="ceiling",
                trigger=trigger.name,
                event_id=eid,
                digest=digest,
                dead_letter_id=letter_id,
            )
        provenance = _provenance(trigger, eid, digest, kind)
        try:
            state = _start(
                workflow,
                mapped,
                provenance=provenance,
                trigger=trigger,
                starter=starter,
                persist=persist,
                settings=settings,
                home=home_path,
            )
        except Exception as extra:
            gate.leave(trigger.name)
            return _refuse(str(extra), "start", digest, raw_text)
        gate.leave(trigger.name)
        run_id = getattr(state, "run_id", None)
        store.put(composite, str(run_id or ""), trigger=trigger.name)
        if trigger.budget and trigger.budget.max_cost_usd:
            spend.add(trigger.name, usd=0.0)
        _audit(auditor, "trigger_started", trigger=trigger.name, run_id=run_id)
        events.record(
            {"action": "start", "trigger": trigger.name, "run_id": run_id, "event_id": eid}
        )
        return TriggerDecision(
            action="start",
            reason="started",
            run_id=str(run_id or ""),
            state=state,
            trigger=trigger.name,
            event_id=eid,
            digest=digest,
            inputs=mapped,
            provenance=provenance,
        )


def replay_dead_letter(
    workflow: WorkflowSpec,
    letter_id: str,
    *,
    home: Path,
    source_kind: str | None = None,
    **kwargs: Any,
) -> TriggerDecision:
    letters = kwargs.pop("letters", None) or DeadLetterLog(home)
    row = letters.get(letter_id)
    if row is None:
        raise TriggerRefused(f"dead letter not found: {letter_id}", reason="missing")
    raw = str(row.get("raw") or "").encode("utf-8")
    kind = source_kind or str(row.get("source_kind") or "")
    if not kind:
        trigger = _find(workflow, str(row.get("trigger") or kwargs.get("trigger_name") or ""))
        kind = trigger.accepts.kind
    return decide_trigger(
        workflow,
        trigger_name=str(row.get("trigger") or ""),
        raw=raw,
        source_kind=kind,
        home=home,
        letters=letters,
        **kwargs,
    )


def _find(workflow: WorkflowSpec, name: str) -> TriggerSpec:
    for item in workflow.triggers:
        if item.name == name:
            return item
    raise TriggerRefused(f"unknown trigger '{name}'", reason="unknown_trigger")


def _parse(
    raw: bytes | str | dict[str, Any],
    *,
    trigger: str,
    limiter: RateLimiter,
) -> tuple[bytes, dict[str, Any], str]:
    if isinstance(raw, dict):
        body = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        refuse_before_parse(body, trigger=trigger, limiter=limiter)
        return body, dict(raw), event_digest(body)
    if isinstance(raw, str):
        body = raw.encode("utf-8")
    else:
        body = raw
    refuse_before_parse(body, trigger=trigger, limiter=limiter)
    try:
        loaded = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as extra:
        raise TriggerRefused("event is not JSON", reason="parse") from extra
    if not isinstance(loaded, dict):
        raise TriggerRefused("event payload must be an object", reason="parse")
    return body, loaded, event_digest(body)


def _check_payload_schema(trigger: TriggerSpec, payload: dict[str, Any]) -> None:
    schema = trigger.accepts.payload_schema
    if not schema:
        return
    required = schema.get("required") or []
    missing = [key for key in required if key not in payload]
    if missing:
        raise TriggerRefused(
            f"payload missing required fields: {', '.join(missing)}", reason="schema"
        )


def _verify_signature(
    trigger: TriggerSpec,
    body: bytes,
    *,
    signature: str | None,
    secret: str | None,
    auditor: Any,
) -> None:
    if not trigger.require_signature:
        return
    if not secret:
        _audit(auditor, "trigger_refused", trigger=trigger.name, reason="unsigned")
        raise TriggerRefused("unsigned event: no signing secret configured", reason="unsigned")
    try:
        verify_signed_body(secret, body, signature)
    except ValueError as extra:
        _audit(auditor, "trigger_refused", trigger=trigger.name, reason="unsigned")
        raise TriggerRefused(str(extra), reason="unsigned") from extra


def _map_inputs(trigger: TriggerSpec, payload: dict[str, Any]) -> dict[str, Any]:
    ns = {"event": payload}
    return {key: interpolate(template, ns) for key, template in trigger.inputs.items()}


def _check_budget(trigger: TriggerSpec, spend: TriggerSpend) -> None:
    budget = trigger.budget
    if budget is None:
        return
    if budget.max_cost_usd is not None and spend.used_usd(trigger.name) >= budget.max_cost_usd:
        raise TriggerRefused("per-trigger budget exceeded", reason="budget")
    if budget.max_tokens is not None and spend.used_tokens(trigger.name) >= budget.max_tokens:
        raise TriggerRefused("per-trigger token budget exceeded", reason="budget")


def _provenance(trigger: TriggerSpec, eid: str, digest: str, kind: str) -> dict[str, Any]:
    return {
        "kind": "trigger",
        "trigger": trigger.name,
        "event_id": eid,
        "digest": digest,
        "source_kind": kind,
    }


def _start(
    workflow: WorkflowSpec,
    inputs: dict[str, Any],
    *,
    provenance: dict[str, Any],
    trigger: TriggerSpec,
    starter: Any,
    persist: bool,
    settings: Any,
    home: Path,
) -> Any:
    if starter is not None:
        return starter(
            workflow,
            inputs,
            provenance=provenance,
            trigger=trigger,
            persist=persist,
            settings=settings,
        )
    from readyagents.triggers.start import start_triggered_run

    return start_triggered_run(
        workflow,
        inputs,
        provenance=provenance,
        trigger=trigger,
        persist=persist,
        settings=settings,
        home=home,
    )


def _audit(auditor: Any, event: str, **fields: Any) -> None:
    if callable(auditor):
        auditor(event, **fields)
