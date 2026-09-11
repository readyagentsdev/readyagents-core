"""Official ``io.modelcontextprotocol/tasks`` projection over the durable run record.

``taskId`` **is** the ReadyAgents ``run_id``. No second identifier. No prefix lookup.
``/runs`` is a deprecated alias of this same coordinator — not a second state machine.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from readyagents.audit import append_audit_event
from readyagents.decisions.signing import verify_signed_body
from readyagents.errors import (
    AuthorizationError,
    ConfigError,
    HttpRequestError,
    RunConflict,
    TaskStateError,
)
from readyagents.mcp.protocol import (
    POLL_INTERVAL_MS,
    PROMPT_MAX_CHARS,
    REASON_MAX_CHARS,
    RESULT_TYPE_COMPLETE,
    RESULT_TYPE_TASK,
    sanitize_prompt,
)
from readyagents.workflow.state import RunState

_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_STATUS_MAP = {
    "queued": "working",
    "running": "working",
    "cancel_requested": "working",
    "paused": "input_required",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_APPROVE = "approve"
_REJECT = "reject"
_INPUT_KEY_PREFIX = "readyagents.approval"
_NOT_FOUND_MESSAGE = "Task not found"


def task_id_from_run_id(run_id: str) -> str:
    return run_id


def require_task_id(task_id: Any) -> str:
    """Exact 32-char hex. Prefix-shaped and unknown ids share one not-found body."""
    if not isinstance(task_id, str) or not _RUN_ID_RE.fullmatch(task_id):
        raise ConfigError(_NOT_FOUND_MESSAGE)
    return task_id


def map_run_status(status: str | None) -> str:
    if not status:
        return "working"
    return _STATUS_MAP.get(str(status), "working")


def occurrence_for(state: RunState, gate_id: str) -> int:
    """0-based count of prior resolved approvals for ``gate_id`` on this run."""
    n = 0
    for row in state.results:
        if row.node_id == gate_id and row.type == "approval" and row.status == "ok":
            n += 1
    applied = (
        (state.metadata.get("mcp_input_responses") or {})
        if isinstance(state.metadata, dict)
        else {}
    )
    if isinstance(applied, Mapping):
        prefix = f"{_INPUT_KEY_PREFIX}.{state.run_id}.{gate_id}."
        for key in applied:
            text = str(key)
            if text.startswith(prefix):
                suffix = text[len(prefix) :]
                if suffix.isdigit():
                    n = max(n, int(suffix) + 1)
    foreach = state.metadata.get("_foreach") if isinstance(state.metadata, dict) else None
    pending = state.pending_node
    if isinstance(foreach, Mapping) and pending and pending in foreach:
        prior = foreach.get(pending)
        if isinstance(prior, list):
            n = max(n, len(prior))
    return n


def gate_id_for(state: RunState) -> str | None:
    if isinstance(state.pending, Mapping):
        node = state.pending.get("node_id")
        if isinstance(node, str) and node.strip():
            return node.strip()
    if isinstance(state.pending_node, str) and state.pending_node.strip():
        return state.pending_node.strip()
    return None


def input_request_key(run_id: str, gate_id: str, occurrence: int) -> str:
    """Unique over the task lifetime. Grammar: readyagents.approval.{run_id}.{gate_id}.{n}."""
    return f"{_INPUT_KEY_PREFIX}.{run_id}.{gate_id}.{int(occurrence)}"


def current_input_request_key(state: RunState) -> str | None:
    if map_run_status(state.status) != "input_required":
        return None
    stored = None
    if isinstance(state.pending, Mapping):
        stored = state.pending.get("input_request_key")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    gate_id = gate_id_for(state)
    if not gate_id:
        return None
    return input_request_key(state.run_id, gate_id, occurrence_for(state, gate_id))


def _approval_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["decision"],
        "properties": {
            "decision": {"type": "string", "enum": [_APPROVE, _REJECT]},
            "reason": {"type": "string", "maxLength": REASON_MAX_CHARS},
        },
    }


def input_requests_for(state: RunState, *, redactor: Any | None = None) -> dict[str, Any]:
    if map_run_status(state.status) != "input_required":
        return {}
    gate_id = gate_id_for(state)
    if not gate_id:
        return {}
    key = current_input_request_key(state)
    if not key:
        return {}
    raw_prompt = ""
    if isinstance(state.pending, Mapping):
        raw_prompt = str(state.pending.get("prompt") or "")
    if not raw_prompt:
        raw_prompt = f"Approve node '{gate_id}'?"
    message = sanitize_prompt(raw_prompt, redactor=redactor, limit=PROMPT_MAX_CHARS)
    return {
        key: {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": _approval_schema(),
            },
        }
    }


def project_task(
    state: RunState,
    *,
    redactor: Any | None = None,
    include_result: bool = True,
) -> dict[str, Any]:
    """Pure projection. Does not persist. ``ttlMs`` is JSON null (until ``runs gc``)."""
    status = map_run_status(state.status)
    created = state.started_at
    updated = state.finished_at or state.started_at
    body: dict[str, Any] = {
        "resultType": RESULT_TYPE_COMPLETE,
        "taskId": state.run_id,
        "status": status,
        "createdAt": created,
        "lastUpdatedAt": updated,
        "ttlMs": None,
        "pollIntervalMs": POLL_INTERVAL_MS,
    }
    if status == "input_required":
        body["inputRequests"] = input_requests_for(state, redactor=redactor)
    elif status == "completed" and include_result:
        record = state.to_record()
        body["result"] = {
            "content": [{"type": "text", "text": json.dumps(record, ensure_ascii=False)}],
            "isError": False,
        }
    elif status == "failed":
        message = "; ".join(state.errors) if state.errors else "run failed"
        body["error"] = {"code": -32603, "message": message}
        if state.errors:
            body["statusMessage"] = sanitize_prompt(message, redactor=redactor, limit=500)
    return body


def create_task_result(state: RunState) -> dict[str, Any]:
    projected = project_task(state, include_result=False)
    projected["resultType"] = RESULT_TYPE_TASK
    projected.pop("inputRequests", None)
    projected.pop("result", None)
    projected.pop("error", None)
    return projected


def parse_input_responses(raw: Any) -> list[tuple[str, str, str | None]]:
    """Return ``(key, decision, reason)`` tuples. Reject is first-class."""
    if not isinstance(raw, Mapping) or not raw:
        raise HttpRequestError("inputResponses must be a non-empty JSON object")
    parsed: list[tuple[str, str, str | None]] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise HttpRequestError("inputResponses keys must be strings")
        decision, reason = _decision_from_response(value)
        parsed.append((key.strip(), decision, reason))
    return parsed


def _decision_from_response(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, Mapping):
        raise HttpRequestError("each input response must be a JSON object")
    action = str(value.get("action") or "accept").strip().lower()
    content = value.get("content")
    if content is None:
        content = value
    if not isinstance(content, Mapping):
        raise HttpRequestError("input response content must be a JSON object")
    raw_decision = content.get("decision")
    if isinstance(raw_decision, str):
        decision = raw_decision.strip().lower()
    elif action in {"decline", "cancel", "reject"}:
        decision = _REJECT
    else:
        decision = ""
    if decision not in {_APPROVE, _REJECT}:
        if action in {"decline", "cancel", "reject"}:
            decision = _REJECT
        else:
            raise HttpRequestError('decision must be exactly "approve" or "reject"')
    reason = content.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise HttpRequestError("reason must be a string")
        if len(reason) > REASON_MAX_CHARS:
            raise HttpRequestError(f"reason must be at most {REASON_MAX_CHARS} characters")
    if action in {"decline", "cancel"} and decision == _APPROVE:
        raise HttpRequestError("declined elicitation cannot carry decision approve")
    return decision, reason


def canonical_decision_bytes(
    *,
    run_id: str,
    node_id: str,
    decision: str,
    actor: str | None,
    input_request_key: str,
) -> bytes:
    payload = {
        "run_id": run_id,
        "node_id": node_id,
        "decision": decision,
        "actor": actor,
        "input_request_key": input_request_key,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class TaskService:
    """JSON-RPC tasks/* handlers. One coordinator, shared with ``/runs``."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def _redactor(self) -> Any:
        return getattr(self.coordinator, "_redactor", None)

    def _audit(self, event: str, **fields: Any) -> None:
        settings = getattr(self.coordinator, "settings", None)
        if settings is None:
            return
        try:
            audit_dir = settings.audit_dir()
        except Exception:  # noqa: BLE001
            return
        payload = {"event": event, **fields}
        redactor = self._redactor()
        if redactor is not None:
            payload = redactor.redact(payload)
        append_audit_event(audit_dir, payload)

    def _load(self, task_id: str) -> RunState:
        ident = require_task_id(task_id)
        try:
            return self.coordinator._load_exact(ident)
        except ConfigError as exc:
            text = str(exc).lower()
            if "not found" in text or "invalid run id" in text:
                raise ConfigError(_NOT_FOUND_MESSAGE) from exc
            raise
        except HttpRequestError as exc:
            raise ConfigError(_NOT_FOUND_MESSAGE) from exc

    def create_from_workflow(
        self,
        *,
        path: str,
        inputs: Mapping[str, Any] | None = None,
        actor: str | None = None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        trace_context: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": path,
            "inputs": dict(inputs or {}),
            "dry_run": bool(dry_run),
        }
        if actor is not None:
            payload["actor"] = actor
        handle = self.coordinator.start_run(payload, idempotency_key=idempotency_key)
        run_id = str(handle["run_id"])
        state = self._load(run_id)
        if trace_context:
            meta = dict(state.metadata)
            meta["trace_context"] = dict(trace_context)
            state.metadata = meta
            persist = getattr(self.coordinator, "_persist", None)
            if callable(persist):
                persist(state)
            state = self._load(run_id)
        # Durable-before-create-result: get must resolve for this id.
        _ = self.get(run_id)
        return create_task_result(state)

    def get(self, task_id: str, *, actor: str | None = None) -> dict[str, Any]:
        ident = require_task_id(task_id)
        try:
            self._authorize_read(ident, actor)
            state = self._load(ident)
        except (ConfigError, HttpRequestError, AuthorizationError):
            raise ConfigError(_NOT_FOUND_MESSAGE) from None
        state = self._ensure_pending_key(state)
        return project_task(state, redactor=self._redactor())

    def _ensure_pending_key(self, state: RunState) -> RunState:
        if map_run_status(state.status) != "input_required":
            return state
        pending = dict(state.pending or {}) if isinstance(state.pending, Mapping) else {}
        stored = pending.get("input_request_key")
        if isinstance(stored, str) and stored.strip():
            return state
        gate_id = gate_id_for(state)
        if not gate_id:
            return state
        pending["input_request_key"] = input_request_key(
            state.run_id, gate_id, occurrence_for(state, gate_id)
        )
        state.pending = pending
        persist = getattr(self.coordinator, "_persist", None)
        if callable(persist):
            persist(state)
        return state

    def cancel(
        self, task_id: str, *, actor: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        ident = require_task_id(task_id)
        try:
            self._load(ident)
        except ConfigError:
            raise ConfigError(_NOT_FOUND_MESSAGE) from None
        payload: dict[str, Any] = {}
        if actor is not None:
            payload["actor"] = actor
        if reason is not None:
            payload["reason"] = reason
        self.coordinator.cancel(ident, payload or None)
        return {"resultType": RESULT_TYPE_COMPLETE}

    def update(
        self,
        task_id: str,
        *,
        input_responses: Any,
        actor: str | None = None,
        signature: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        ident = require_task_id(task_id)
        try:
            state = self._load(ident)
        except ConfigError:
            raise ConfigError(_NOT_FOUND_MESSAGE) from None
        parsed = parse_input_responses(input_responses)
        if len(parsed) != 1:
            raise HttpRequestError("exactly one input response is required")
        key, decision, _reason = parsed[0]
        applied = _applied_decision(state, key)
        if applied is not None:
            if applied == decision:
                if map_run_status(state.status) != "input_required":
                    return {"resultType": RESULT_TYPE_COMPLETE}
                # Recorded but still paused: resume rather than no-op.
            else:
                self._audit(
                    "decision_conflict",
                    run_id=ident,
                    node_id=state.pending_node,
                    decision=decision,
                    actor=actor,
                    input_request_key=key,
                )
                raise RunConflict("conflicting input response for this input request key")
        if map_run_status(state.status) != "input_required":
            state = self._load(ident)
            applied = _applied_decision(state, key)
            if applied == decision:
                return {"resultType": RESULT_TYPE_COMPLETE}
            raise TaskStateError(
                f"Task {ident} is not input_required (status={map_run_status(state.status)})",
                run_id=ident,
            )
        state = self._ensure_pending_key(state)
        expected = current_input_request_key(state)
        if expected is None or key != expected:
            state = self._load(ident)
            applied = _applied_decision(state, key)
            if applied == decision:
                return {"resultType": RESULT_TYPE_COMPLETE}
            raise HttpRequestError("inputResponses key is not currently outstanding")
        node_id = state.pending_node
        if not isinstance(node_id, str) or not node_id.strip():
            raise TaskStateError(f"Task {ident} has no pending node", run_id=ident)
        node_id = node_id.strip()
        body = canonical_decision_bytes(
            run_id=ident,
            node_id=node_id,
            decision=decision,
            actor=actor,
            input_request_key=key,
        )
        if secret:
            try:
                verify_signed_body(secret, body, signature)
            except ValueError as exc:
                self._audit(
                    "decision_refused",
                    run_id=ident,
                    node_id=node_id,
                    decision=decision,
                    actor=actor,
                    reason=str(exc),
                    input_request_key=key,
                )
                raise AuthorizationError(actor, decision, node_id) from exc
        try:
            self.coordinator.decide(
                ident,
                {"node_id": node_id, "decision": decision, "actor": actor},
                input_request_key=key,
            )
        except RunConflict:
            # Resume can finish between the input_required check and decide.
            state = self._load(ident)
            applied = _applied_decision(state, key)
            if applied == decision and map_run_status(state.status) != "input_required":
                return {"resultType": RESULT_TYPE_COMPLETE}
            raise
        except AuthorizationError as exc:
            self._audit(
                "decision_refused",
                run_id=ident,
                node_id=node_id,
                decision=decision,
                actor=actor,
                reason=str(exc),
                input_request_key=key,
            )
            raise
        return {"resultType": RESULT_TYPE_COMPLETE}

    def _authorize_read(self, run_id: str, actor: str | None) -> None:
        authorizer = getattr(self.coordinator, "_authorizer", None)
        if authorizer is None:
            return
        check = getattr(authorizer, "check", None)
        if not callable(check):
            return
        check(actor, "read", run_id)


def _applied_decision(state: RunState, key: str) -> str | None:
    bucket = state.metadata.get("mcp_input_responses") if isinstance(state.metadata, dict) else None
    if not isinstance(bucket, Mapping):
        return None
    value = bucket.get(key)
    return str(value) if value in {_APPROVE, _REJECT} else None
