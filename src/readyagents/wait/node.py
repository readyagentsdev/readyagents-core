"""type: wait — park as waiting until wake evaluation says otherwise."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents.errors import (
    ApprovalRequired,
    WaitCapExceeded,
    WaitError,
    WaitingRequired,
)
from readyagents.wait.evaluate import WaitOutcome, WaitWorld, evaluate_wait
from readyagents.wait.record import WaitRecord, parse_now, resolve_deadline
from readyagents.workflow.templates import interpolate

DEFAULT_MAX_WAITING = 64


def run_wait_node(node: Any, state: Any, ctx: Any) -> Any:
    if getattr(ctx, "dry_run", False):
        return {"dry_run": True, "type": "wait"}
    now = _now(ctx)
    record = _record_from_state(state, node) or _build_record(node, state, now)
    world = world_from_ctx(ctx, state)
    outcome = evaluate_wait(record, now=now, world=world)
    if outcome.satisfied:
        _clear_wait(state)
        _rebroker(ctx, state, record)
        return _payload(outcome, record, node)
    if outcome.deadline:
        return _deadline(node, state, ctx, record, outcome)
    _park(state, ctx, record, outcome)
    raise WaitingRequired(node.id, state.run_id, state=state, record=record.as_dict())


def _build_record(node: Any, state: Any, now: datetime) -> WaitRecord:
    ns = state.mapping()
    until = interpolate(str(getattr(node, "until", None) or ""), ns).strip()
    if not until:
        raise WaitError(f"wait node '{node.id}' requires until")
    deadline = resolve_deadline(until, now=now)
    created = now.isoformat()
    event = getattr(node, "for_event", None)
    file_spec = getattr(node, "for_file", None)
    run_spec = getattr(node, "for_run", None)
    if isinstance(event, dict):
        event = {k: interpolate(v, ns) if isinstance(v, str) else v for k, v in event.items()}
        if isinstance(event.get("match"), dict):
            event["match"] = {
                k: interpolate(v, ns) if isinstance(v, str) else v
                for k, v in event["match"].items()
            }
    if isinstance(file_spec, dict):
        file_spec = {
            k: interpolate(v, ns) if isinstance(v, str) else v for k, v in file_spec.items()
        }
    if isinstance(run_spec, dict):
        run_spec = {k: interpolate(v, ns) if isinstance(v, str) else v for k, v in run_spec.items()}
    return WaitRecord(
        node_id=node.id,
        until=until,
        deadline_at=deadline.isoformat(),
        on_deadline=str(getattr(node, "on_deadline", None) or "fail").strip().lower(),
        whichever=str(getattr(node, "whichever", None) or "first").strip().lower(),
        for_event=event if isinstance(event, dict) else None,
        for_file=file_spec if isinstance(file_spec, dict) else None,
        for_run=run_spec if isinstance(run_spec, dict) else None,
        default=getattr(node, "default", None),
        escalate_to=list(getattr(node, "escalate_to", None) or []),
        created_at=created,
        dormant_since=created,
        rebroker=True,
    )


def _record_from_state(state: Any, node: Any) -> WaitRecord | None:
    pending = state.pending if isinstance(state.pending, dict) else {}
    raw = pending.get("wait") if pending.get("type") == "wait" else None
    if raw is None and isinstance(state.metadata, dict):
        raw = (state.metadata.get("_wait") or {}).get(node.id)
    if not isinstance(raw, dict):
        return None
    rec = WaitRecord.from_dict(raw)
    rec.node_id = rec.node_id or node.id
    return rec


def _park(state: Any, ctx: Any, record: WaitRecord, outcome: WaitOutcome) -> None:
    _assert_cap(ctx, state)
    from readyagents.wait.compact import compact_results

    compact_results(state)
    if isinstance(state.metadata, dict):
        bucket = state.metadata.setdefault("_wait", {})
        if not isinstance(bucket, dict):
            bucket = {}
            state.metadata["_wait"] = bucket
        bucket[record.node_id] = record.as_dict()
        state.metadata["_wait_rebroker"] = True
        state.metadata.pop("credential_env", None)
        state.metadata.pop("granted_secrets", None)
    state.pending_node = record.node_id
    state.pending = {
        "node_id": record.node_id,
        "type": "wait",
        "wait": record.as_dict(),
        "waiting_for": list(outcome.waiting_for),
        "expires_at": record.deadline_at,
        "wake": f"readyagents wake {state.run_id}",
    }


def _deadline(node: Any, state: Any, ctx: Any, record: WaitRecord, outcome: WaitOutcome) -> Any:
    action = record.on_deadline
    _clear_wait(state)
    _rebroker(ctx, state, record)
    if action == "fail":
        raise WaitError(f"wait '{node.id}' deadline reached")
    if action == "escalate":
        prompt = f"Wait '{node.id}' deadline reached; escalate"
        pause = {
            "on_expire": "fail",
            "escalate_to": list(record.escalate_to),
            "from_wait": True,
        }
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause)
    if action == "branch":
        target = getattr(node, "else_", None) or getattr(node, "then", None)
        return {"reason": "deadline", "action": "branch", "next": target, "payload": record.default}
    return {
        "reason": "deadline",
        "action": "continue",
        "next": getattr(node, "next", None),
        "payload": record.default,
    }


def _payload(outcome: WaitOutcome, record: WaitRecord, node: Any) -> dict[str, Any]:
    return {
        "reason": outcome.reason,
        "payload": outcome.payload,
        "deadline_at": record.deadline_at,
        "next": getattr(node, "next", None),
    }


def _clear_wait(state: Any) -> None:
    if isinstance(state.metadata, dict):
        state.metadata.pop("_wait", None)
    # keep _wait_rebroker until tools run


def _rebroker(ctx: Any, state: Any, record: WaitRecord) -> None:
    if not record.rebroker:
        return
    if hasattr(ctx, "credential_env"):
        ctx.credential_env = None
    if hasattr(ctx, "last_credential_kind"):
        ctx.last_credential_kind = None
    if isinstance(state.metadata, dict):
        state.metadata["_wait_rebroker"] = True
        state.metadata["credentials_epoch"] = record.dormant_since or record.created_at


def _assert_cap(ctx: Any, state: Any) -> None:
    raw = getattr(ctx, "max_waiting", None)
    limit = DEFAULT_MAX_WAITING if raw is None else int(raw)
    counter = getattr(ctx, "waiting_count", None)
    if callable(counter):
        used = int(counter())
    elif counter is not None:
        used = int(counter)
    else:
        used = _count_waiting_others(ctx, state)
    if used >= limit:
        raise WaitCapExceeded(used, limit)


def _count_waiting_others(ctx: Any, state: Any) -> int:
    current = getattr(state, "run_id", None)
    store = getattr(ctx, "run_store", None)
    if store is not None:
        try:
            from readyagents.run_store import RunQuery

            return sum(
                1 for item in store.list(RunQuery(status="waiting")) if item.state.run_id != current
            )
        except Exception:  # noqa: BLE001
            return 0
    pin = getattr(ctx, "pin_home", None)
    if pin is None:
        return 0
    from readyagents.workflow.state import list_runs

    runs_dir = Path(pin) / "runs"
    if not runs_dir.is_dir():
        return 0
    return sum(
        1 for row in list_runs(runs_dir) if row.status == "waiting" and row.run_id != current
    )


def _now(ctx: Any) -> datetime:
    clock = getattr(ctx, "clock", None)
    if callable(clock):
        return parse_now(clock())
    if clock is not None:
        return parse_now(clock)
    return datetime.now(UTC)


def world_from_ctx(ctx: Any, state: Any) -> WaitWorld:
    injected = getattr(ctx, "wait_world", None)
    if isinstance(injected, WaitWorld):
        return injected
    from readyagents.wait.world import load_world

    return load_world(ctx, state)
