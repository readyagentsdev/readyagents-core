"""Lazy wake. Core starts no timer, watcher, or daemon."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from readyagents.wait.evaluate import evaluate_wait
from readyagents.wait.node import _now, _rebroker, world_from_ctx
from readyagents.wait.record import WaitRecord
from readyagents.workflow.runner import resume_run
from readyagents.workflow.state import RunState


def wait_record_of(state: RunState) -> WaitRecord | None:
    pending = state.pending if isinstance(state.pending, dict) else {}
    if pending.get("type") == "wait" and isinstance(pending.get("wait"), dict):
        return WaitRecord.from_dict(pending["wait"])
    bucket = (state.metadata or {}).get("_wait") if isinstance(state.metadata, dict) else None
    if isinstance(bucket, dict) and bucket:
        first = next(iter(bucket.values()))
        if isinstance(first, dict):
            return WaitRecord.from_dict(first)
    return None


def peek_wait(state: RunState, ctx: Any) -> dict[str, Any]:
    record = wait_record_of(state)
    if record is None:
        return {"waiting": False}
    outcome = evaluate_wait(record, now=_now(ctx), world=world_from_ctx(ctx, state))
    return {
        "waiting": True,
        "satisfied": outcome.satisfied,
        "deadline": outcome.deadline,
        "waiting_for": outcome.waiting_for,
        "expires_at": record.deadline_at,
        "node_id": record.node_id,
        "on_deadline": record.on_deadline,
    }


def wake_one(
    run_id: str,
    *,
    settings: Any,
    ctx_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from readyagents.run_store import open_run_store

    store = open_run_store(settings)
    try:
        state = store.get(run_id, allow_prefix=True).state
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    if state.status != "waiting":
        return {
            "run_id": state.run_id,
            "status": state.status,
            "woke": False,
            "reason": "not_waiting",
        }
    extras = dict(ctx_extras or {})
    workspace = Path.cwd()
    if isinstance(state.metadata, dict) and state.metadata.get("workspace"):
        workspace = Path(str(state.metadata.get("workspace")))
    fake = SimpleNamespace(
        clock=extras.get("clock"),
        wait_world=extras.get("wait_world"),
        pin_home=extras.get("pin_home") or settings.home_path(),
        workflow_dir=workspace,
        credential_env=None,
        last_credential_kind=None,
    )
    peek = peek_wait(state, fake)
    if not peek.get("satisfied") and not peek.get("deadline"):
        return {
            "run_id": state.run_id,
            "status": "waiting",
            "woke": False,
            "reason": "still_waiting",
            **{k: peek[k] for k in ("waiting_for", "expires_at") if k in peek},
        }
    record = wait_record_of(state)
    if record is not None:
        _rebroker(fake, state, record)
    resumed = resume_run(
        state.run_id, settings=settings, **{k: v for k, v in extras.items() if k in _RESUME_KEYS}
    )
    return {
        "run_id": resumed.run_id,
        "status": resumed.status,
        "woke": resumed.status != "waiting",
        "reason": "evaluated",
    }


_RESUME_KEYS = {
    "path",
    "inputs",
    "dry_run",
    "persist",
    "llm",
    "actor",
    "store",
}


def wake_all(*, settings: Any, ctx_extras: dict[str, Any] | None = None) -> dict[str, Any]:
    from readyagents.run_store import RunQuery, open_run_store

    store = open_run_store(settings)
    try:
        found = [item.state for item in store.list(RunQuery(status="waiting"))]
        if not found:
            found = [
                item.state for item in store.list(RunQuery()) if item.state.status == "waiting"
            ]
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    rows = []
    for state in found:
        rows.append(wake_one(state.run_id, settings=settings, ctx_extras=ctx_extras))
    return {"runs": rows, "woke": sum(1 for row in rows if row.get("woke"))}
