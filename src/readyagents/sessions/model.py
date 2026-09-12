"""Frozen session object, turn record, and lifecycle states."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

from readyagents.workflow.state import utc_now

LIFECYCLE = frozenset({"active", "awaiting_user", "awaiting_human_agent", "closed", "expired"})
TURN_ROLES = frozenset({"user", "assistant", "human_agent", "system"})


def new_session_id() -> str:
    """Unguessable id that is also a legal memory-scope value."""
    return "s" + secrets.token_hex(16)


def hash_token(token: str, session_id: str) -> str:
    raw = f"{token}\n{session_id}".encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass
class Turn:
    turn_id: str
    run_id: str
    role: str
    text: str
    status: str = "ok"
    node_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    superseded: bool = False
    signature_status: str = "unsigned"
    actor: str | None = None
    role_name: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None and v != "" and v != {}}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Turn:
        return cls(
            turn_id=str(raw.get("turn_id") or ""),
            run_id=str(raw.get("run_id") or ""),
            role=str(raw.get("role") or "user"),
            text=str(raw.get("text") or ""),
            status=str(raw.get("status") or "ok"),
            node_id=str(raw["node_id"]) if raw.get("node_id") else None,
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
            superseded=bool(raw.get("superseded")),
            signature_status=str(raw.get("signature_status") or "unsigned"),
            actor=str(raw["actor"]) if raw.get("actor") else None,
            role_name=str(raw["role_name"]) if raw.get("role_name") else None,
            usage=dict(raw.get("usage") or {}),
            latency_ms=int(raw["latency_ms"]) if raw.get("latency_ms") is not None else None,
        )


@dataclass
class Session:
    session_id: str
    workflow: str
    source: str = ""
    status: str = "active"
    turns: list[Turn] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    working: dict[str, Any] = field(default_factory=dict)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    pending_run_id: str | None = None
    pending_node: str | None = None
    token_hash: str | None = None
    memory_scope: str = ""
    promote_to: str | None = None
    created_at: str = ""
    updated_at: str = ""
    deadline_at: str | None = None
    on_expire: str = "close"
    max_turns: int | None = None
    turn_timeout_seconds: float | None = None
    last_turn_at: str | None = None
    spent_tokens: int = 0
    spent_cost_micros: int = 0
    max_tokens: int | None = None
    max_cost_micros: int | None = None
    compaction: dict[str, Any] | None = None
    turn_started_mono: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workflow": self.workflow,
            "source": self.source,
            "status": self.status,
            "turns": [t.as_dict() for t in self.turns],
            "history": list(self.history),
            "working": dict(self.working),
            "dropped": list(self.dropped),
            "pending_run_id": self.pending_run_id,
            "pending_node": self.pending_node,
            "token_hash": self.token_hash,
            "memory_scope": self.memory_scope,
            "promote_to": self.promote_to,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
            "on_expire": self.on_expire,
            "max_turns": self.max_turns,
            "turn_timeout_seconds": self.turn_timeout_seconds,
            "last_turn_at": self.last_turn_at,
            "spent_tokens": self.spent_tokens,
            "spent_cost_micros": self.spent_cost_micros,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "compaction": dict(self.compaction) if self.compaction else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        turns = [Turn.from_dict(t) for t in list(raw.get("turns") or []) if isinstance(t, dict)]
        return cls(
            session_id=str(raw.get("session_id") or ""),
            workflow=str(raw.get("workflow") or ""),
            source=str(raw.get("source") or ""),
            status=str(raw.get("status") or "active"),
            turns=turns,
            history=[dict(h) for h in list(raw.get("history") or []) if isinstance(h, dict)],
            working=dict(raw.get("working") or {}),
            dropped=[dict(h) for h in list(raw.get("dropped") or []) if isinstance(h, dict)],
            pending_run_id=str(raw["pending_run_id"]) if raw.get("pending_run_id") else None,
            pending_node=str(raw["pending_node"]) if raw.get("pending_node") else None,
            token_hash=str(raw["token_hash"]) if raw.get("token_hash") else None,
            memory_scope=str(raw.get("memory_scope") or ""),
            promote_to=str(raw["promote_to"]) if raw.get("promote_to") else None,
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            deadline_at=str(raw["deadline_at"]) if raw.get("deadline_at") else None,
            on_expire=str(raw.get("on_expire") or "close"),
            max_turns=int(raw["max_turns"]) if raw.get("max_turns") is not None else None,
            turn_timeout_seconds=(
                float(raw["turn_timeout_seconds"])
                if raw.get("turn_timeout_seconds") is not None
                else None
            ),
            last_turn_at=str(raw["last_turn_at"]) if raw.get("last_turn_at") else None,
            spent_tokens=int(raw.get("spent_tokens") or 0),
            spent_cost_micros=int(raw.get("spent_cost_micros") or 0),
            max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") is not None else None,
            max_cost_micros=(
                int(raw["max_cost_micros"]) if raw.get("max_cost_micros") is not None else None
            ),
            compaction=dict(raw["compaction"]) if isinstance(raw.get("compaction"), dict) else None,
        )

    def touch(self) -> None:
        self.updated_at = utc_now()
