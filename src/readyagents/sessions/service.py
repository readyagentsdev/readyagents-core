"""Start, reply, close, barge-in, replay, freeze. Turns are ordinary runs."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.approvals.gate import parse_expires_in
from readyagents.config import Settings, get_settings
from readyagents.errors import (
    ConverseRequired,
    SessionBoundBudget,
    SessionBoundDeadline,
    SessionBoundTimeout,
    SessionBoundTurns,
    SessionExpired,
    SessionRefused,
)
from readyagents.sessions.model import Session, Turn, hash_token, new_session_id
from readyagents.sessions.store import SessionStore
from readyagents.workflow.state import utc_now

_INFLIGHT: dict[str, Any] = {}


def start_session(
    path: Path | str,
    *,
    inputs: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
    token: str | None = None,
    store: SessionStore | None = None,
    clock: Any = None,
    persist: bool = True,
    **run_kwargs: Any,
) -> Session:
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    from readyagents.workflow.runner import load_workflow, run_workflow_file

    source = Path(path)
    workflow = load_workflow(source)
    spec = getattr(workflow, "conversation", None)
    now = _now(clock)
    session = Session(
        session_id=new_session_id(),
        workflow=workflow.name,
        source=str(source),
        status="active",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        memory_scope="",
        on_expire=_on_expire(spec),
    )
    session.memory_scope = f"session:{session.session_id}"
    if token:
        session.token_hash = hash_token(token, session.session_id)
    _apply_spec(session, spec, now)
    store.save(session)
    t0 = time.monotonic()
    from readyagents.workflow.state import RunState

    init = RunState.start(
        workflow.name,
        dict(inputs or {}),
        metadata={"session_id": session.session_id, "session_history": list(session.history)},
    )
    try:
        state = run_workflow_file(
            source,
            inputs=dict(inputs or {}),
            settings=settings,
            persist=persist,
            initial_state=init,
            **run_kwargs,
        )
        _finish_run(session, state, say=None, latency_ms=_ms(t0), clock=clock)
    except ConverseRequired as exc:
        _park(session, exc, latency_ms=_ms(t0), clock=clock)
    store.save(session)
    return session


def reply_session(
    session_id: str,
    text: str,
    *,
    token: str | None = None,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    actor: str | None = None,
    role: str | None = None,
    signature_status: str = "unsigned",
    clock: Any = None,
    persist: bool = True,
    **run_kwargs: Any,
) -> Session:
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    try:
        _expire_or_bound(session, clock=clock)
    except (
        SessionBoundDeadline,
        SessionBoundTimeout,
        SessionBoundTurns,
        SessionBoundBudget,
        SessionExpired,
    ):
        store.save(session)
        raise
    if session.status in {"closed", "expired"}:
        raise SessionRefused(f"session is {session.status}", reason=session.status)
    if not session.pending_run_id or not session.pending_node:
        raise SessionRefused("session is not awaiting a reply", reason="state")
    user_role = "human_agent" if session.status == "awaiting_human_agent" else "user"
    _append_user(session, text, role=user_role, actor=actor, role_name=role, sig=signature_status)
    _compact(session)
    t0 = time.monotonic()
    from readyagents.workflow.runner import resume_run

    try:
        state = resume_run(
            session.pending_run_id,
            settings=settings,
            persist=persist,
            decisions={session.pending_node: text},
            actor=actor,
            vote_signature_status=signature_status,
            **run_kwargs,
        )
        _finish_run(session, state, say=None, latency_ms=_ms(t0), clock=clock)
    except ConverseRequired as exc:
        _park(session, exc, latency_ms=_ms(t0), clock=clock)
    store.save(session)
    return session


def close_session(
    session_id: str,
    *,
    token: str | None = None,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    promote: str | None = None,
) -> Session:
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    if promote and promote != session.promote_to:
        raise SessionRefused("undeclared memory promotion", reason="promote")
    _clear_or_promote(session, settings=settings, promote=promote or session.promote_to)
    session.status = "closed"
    session.pending_run_id = None
    session.pending_node = None
    store.save(session)
    return session


def barge_in(
    session_id: str,
    text: str,
    *,
    token: str | None = None,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    clock: Any = None,
    **run_kwargs: Any,
) -> Session:
    handle = _INFLIGHT.get(session_id)
    if handle is not None and hasattr(handle, "request"):
        handle.request(actor="user", reason="barge-in")
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    for turn in reversed(session.turns):
        if turn.role == "assistant" and not turn.superseded:
            turn.superseded = True
            turn.status = "superseded"
            break
    session.touch()
    store.save(session)
    if session.pending_run_id:
        return reply_session(
            session_id, text, token=token, settings=settings, store=store, clock=clock, **run_kwargs
        )
    return session


def register_inflight(session_id: str, token: Any) -> None:
    _INFLIGHT[session_id] = token


def clear_inflight(session_id: str) -> None:
    _INFLIGHT.pop(session_id, None)


def replay_session(
    session_id: str,
    *,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    from readyagents.workflow.runner import replay_run

    turns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for turn in session.turns:
        row = turn.as_dict()
        if turn.run_id and turn.run_id not in seen:
            seen.add(turn.run_id)
            try:
                state = replay_run(turn.run_id, settings=settings, offline=True)
                row["replay_status"] = state.status
            except Exception as extra:  # noqa: BLE001
                row["replay_status"] = "miss"
                row["replay_error"] = str(extra)
        turns.append(row)
    return {"session_id": session.session_id, "status": session.status, "turns": turns}


def freeze_session(
    session_id: str,
    *,
    out_dir: Path | str,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    token: str | None = None,
) -> Path:
    settings = settings or get_settings()
    store = store or SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    from readyagents.paths import resolve_within

    dest = resolve_within(out_dir, settings.workspace_path(), what="session freeze")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "session.json").write_text(
        json.dumps(session.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    source = Path(session.source) if session.source else None
    cases: list[dict[str, Any]] = []
    if source and source.is_file():
        rel = source.name
        target = dest / rel
        if not target.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        decisions: dict[str, str] = {}
        for turn in session.turns:
            if turn.role in {"user", "human_agent"} and turn.node_id:
                decisions[turn.node_id] = turn.text
        cases.append(
            {
                "name": f"session-{session.session_id[:8]}",
                "workflow": rel,
                "decisions": decisions,
                "expect_status": "paused" if session.status.startswith("awaiting") else "succeeded",
            }
        )
    suite = {"cases": cases}
    (dest / "eval.yaml").write_text(
        json.dumps(suite, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return dest


def list_sessions(*, settings: Settings | None = None, limit: int = 50) -> list[Session]:
    return SessionStore(settings=settings).list(limit=limit)


def show_session(
    session_id: str, *, settings: Settings | None = None, token: str | None = None
) -> Session:
    store = SessionStore(settings=settings)
    session = store.get(session_id)
    _bind(session, token)
    return session


def _apply_spec(session: Session, spec: Any, now: datetime) -> None:
    if spec is None:
        return
    session.max_turns = getattr(spec, "max_turns", None)
    timeout = getattr(spec, "turn_timeout", None)
    if timeout:
        session.turn_timeout_seconds = parse_expires_in(str(timeout))
    deadline = getattr(spec, "deadline", None)
    if deadline:
        seconds = parse_expires_in(str(deadline))
        session.deadline_at = (now + timedelta(seconds=seconds)).isoformat()
    session.on_expire = _on_expire(spec)
    budget = getattr(spec, "budget", None)
    if budget is not None:
        session.max_tokens = getattr(budget, "max_tokens", None)
        cost = getattr(budget, "max_cost_usd", None)
        if cost is not None:
            session.max_cost_micros = int(float(cost) * 1_000_000)
    compaction = getattr(spec, "compaction", None)
    if isinstance(compaction, dict):
        session.compaction = dict(compaction)
    memory = getattr(spec, "memory", None)
    if isinstance(memory, dict) and memory.get("promote"):
        session.promote_to = str(memory.get("promote"))


def _on_expire(spec: Any) -> str:
    raw = str(getattr(spec, "on_expire", None) or "close").strip().lower()
    if raw not in {"close", "fail", "handoff"}:
        return "close"
    return raw


def _park(session: Session, exc: ConverseRequired, *, latency_ms: int, clock: Any) -> None:
    now = _now(clock)
    session.pending_run_id = exc.run_id
    session.pending_node = exc.node_id
    session.status = "awaiting_human_agent" if exc.mode == "human_agent" else "awaiting_user"
    session.last_turn_at = now.isoformat()
    state = getattr(exc, "state", None)
    run_id = exc.run_id
    usage = dict(getattr(state, "usage", None) or {})
    session.spent_tokens += int(usage.get("total_tokens") or 0)
    session.spent_cost_micros += int(usage.get("cost_micros") or 0)
    turn = Turn(
        turn_id=uuid4().hex,
        run_id=run_id,
        role="assistant",
        text=exc.say,
        status="paused",
        node_id=exc.node_id,
        started_at=session.last_turn_at,
        finished_at=session.last_turn_at,
        latency_ms=latency_ms,
        usage=usage,
    )
    session.turns.append(turn)
    session.history.append({"role": "assistant", "text": exc.say, "trust": "trusted"})
    if state is not None and isinstance(getattr(state, "metadata", None), dict):
        state.metadata["session_id"] = session.session_id
        state.metadata["session_history"] = list(session.history)


def _finish_run(
    session: Session, state: Any, *, say: str | None, latency_ms: int, clock: Any
) -> None:
    now = _now(clock)
    session.last_turn_at = now.isoformat()
    usage = dict(getattr(state, "usage", None) or {})
    session.spent_tokens += int(usage.get("total_tokens") or 0)
    session.spent_cost_micros += int(usage.get("cost_micros") or 0)
    text = say or ""
    if not text:
        last = None
        if getattr(state, "results", None):
            last = state.results[-1]
        if last is not None and isinstance(last.output, dict):
            text = str(last.output.get("say") or last.output.get("text") or "")
    session.turns.append(
        Turn(
            turn_id=uuid4().hex,
            run_id=state.run_id,
            role="assistant",
            text=text,
            status=state.status,
            started_at=session.last_turn_at,
            finished_at=now.isoformat(),
            latency_ms=latency_ms,
            usage=usage,
        )
    )
    if text:
        session.history.append({"role": "assistant", "text": text, "trust": "trusted"})
    session.pending_run_id = None
    session.pending_node = None
    session.status = "closed" if state.status == "succeeded" else "active"
    if isinstance(getattr(state, "output_keys", None), dict):
        session.working.update(state.output_keys)


def _append_user(
    session: Session,
    text: str,
    *,
    role: str,
    actor: str | None,
    role_name: str | None,
    sig: str,
) -> None:
    session.turns.append(
        Turn(
            turn_id=uuid4().hex,
            run_id=session.pending_run_id or "",
            role=role,
            text=text,
            status="ok",
            node_id=session.pending_node,
            started_at=utc_now(),
            finished_at=utc_now(),
            actor=actor,
            role_name=role_name,
            signature_status=sig,
        )
    )
    session.history.append(
        {
            "role": role,
            "text": text,
            "trust": "untrusted",
            "attributed": True,
            "actor": actor,
            "role_name": role_name,
        }
    )


def _compact(session: Session) -> None:
    spec = session.compaction or {}
    cap = spec.get("max_turns") or spec.get("keep")
    if not cap:
        return
    keep = int(cap)
    if keep < 1 or len(session.history) <= keep:
        return
    dropped = session.history[:-keep]
    session.history = session.history[-keep:]
    session.dropped.extend(dropped)


def _expire_or_bound(session: Session, *, clock: Any) -> None:
    now = _now(clock)
    if session.deadline_at:
        try:
            deadline = datetime.fromisoformat(session.deadline_at.replace("Z", "+00:00"))
        except ValueError:
            deadline = None
        if deadline is not None and now >= deadline:
            _apply_expiry(session)
            if session.on_expire == "handoff":
                return
            raise SessionBoundDeadline("session deadline exceeded")
    if session.turn_timeout_seconds and session.last_turn_at:
        try:
            last = datetime.fromisoformat(session.last_turn_at.replace("Z", "+00:00"))
        except ValueError:
            last = None
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed > float(session.turn_timeout_seconds):
                raise SessionBoundTimeout("session turn timeout exceeded")
    if session.max_turns is not None and len(session.turns) >= int(session.max_turns):
        raise SessionBoundTurns("session max turns exceeded")
    if session.max_tokens is not None and session.spent_tokens > int(session.max_tokens):
        raise SessionBoundBudget("session budget exceeded")
    if session.max_cost_micros is not None and session.spent_cost_micros > int(
        session.max_cost_micros
    ):
        raise SessionBoundBudget("session budget exceeded")


def _apply_expiry(session: Session) -> None:
    action = session.on_expire or "close"
    if action == "handoff":
        session.status = "awaiting_human_agent"
        return
    if action == "fail":
        session.status = "expired"
        raise SessionExpired("session expired")
    session.status = "expired"


def _bind(session: Session, token: str | None) -> None:
    if not session.token_hash:
        return
    if not token or hash_token(token, session.session_id) != session.token_hash:
        raise SessionRefused("session not found", reason="missing")


def _clear_or_promote(session: Session, *, settings: Settings, promote: str | None) -> None:
    from readyagents.memory.protocol import open_memory_store

    store = open_memory_store(settings.home_path())
    if promote:
        from readyagents.memory.scope import validate_scope

        dest = validate_scope(promote)
        for rec in store.read(session.memory_scope):
            store.write(
                rec.__class__(
                    id=rec.id,
                    scope=dest,
                    text=rec.text,
                    metadata=dict(rec.metadata),
                    created_at=rec.created_at,
                    expires_at=rec.expires_at,
                    source_run_id=rec.source_run_id,
                    provenance=rec.provenance,
                )
            )
    if session.memory_scope:
        store.forget(scope=session.memory_scope)
    store.close()


def _now(clock: Any) -> datetime:
    if callable(clock):
        value = clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
    return datetime.now(UTC)


def _ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))
