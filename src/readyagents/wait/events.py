"""Signed, audited, bounded, idempotent wait events. No listener in core."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.decisions.signing import sign_body, verify_signed_body
from readyagents.errors import WaitEventRefused
from readyagents.permissions import restrict_file
from readyagents.workflow.state import utc_now

MAX_EVENT_BYTES = 64_000
EVENTS_NAME = "wait-events.jsonl"


def event_id(name: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"name": name, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def accept_event(
    home: Path,
    *,
    name: str,
    payload: dict[str, Any] | None,
    secret: str | None,
    signature: str | None,
    actor: str | None = None,
    auditor: Any = None,
) -> dict[str, Any]:
    body = json.dumps(
        {"name": name, "payload": payload or {}}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > MAX_EVENT_BYTES:
        _audit(auditor, "event_refused", name=name, reason="too_large")
        raise WaitEventRefused("event payload exceeds bound")
    if not secret:
        _audit(auditor, "event_refused", name=name, reason="unsigned")
        raise WaitEventRefused("unsigned event: no signing secret configured")
    try:
        verify_signed_body(secret, body, signature)
    except ValueError as exc:
        _audit(auditor, "event_refused", name=name, reason="unsigned")
        raise WaitEventRefused(str(exc)) from exc
    eid = event_id(name, payload or {})
    existing = list_events(home)
    if any(item.get("event_id") == eid for item in existing):
        _audit(auditor, "event_idempotent", name=name, event_id=eid, actor=actor)
        return {"ok": True, "idempotent": True, "event_id": eid, "name": name}
    row = {
        "event_id": eid,
        "name": name,
        "payload": payload or {},
        "actor": actor,
        "ts": utc_now(),
    }
    _append(home, row)
    _audit(auditor, "event_accepted", name=name, event_id=eid, actor=actor)
    return {
        "ok": True,
        "idempotent": False,
        "event_id": eid,
        "name": name,
        "payload": row["payload"],
    }


def list_events(home: Path) -> list[dict[str, Any]]:
    path = Path(home) / EVENTS_NAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def sign_event(secret: str, name: str, payload: dict[str, Any] | None) -> str:
    body = json.dumps(
        {"name": name, "payload": payload or {}}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sign_body(secret, body)


def _append(home: Path, row: dict[str, Any]) -> None:
    path = Path(home) / EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = path.read_text(encoding="utf-8") if path.is_file() else ""
    line = json.dumps(row, ensure_ascii=False) + "\n"
    atomic_write_text(path, prior + line, encoding="utf-8", newline="\n", restrict=True)
    restrict_file(path)


def _audit(auditor: Any, event: str, **fields: Any) -> None:
    if callable(auditor):
        auditor(event, **fields)
