"""Declared staleness threshold. Refuses; does not guess quality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from readyagents.errors import KnowledgeStale
from readyagents.knowledge.cite import META_INGESTED


def oldest_ingested(store: Any, scope: str) -> str | None:
    records = store.list(scope=scope)
    stamps: list[str] = []
    for rec in records:
        stamp = str((rec.metadata or {}).get(META_INGESTED) or rec.created_at or "")
        if stamp:
            stamps.append(stamp)
    if not stamps:
        return None
    stamps.sort()
    return stamps[0]


def newest_ingested(store: Any, scope: str) -> str | None:
    records = store.list(scope=scope)
    stamps: list[str] = []
    for rec in records:
        stamp = str((rec.metadata or {}).get(META_INGESTED) or rec.created_at or "")
        if stamp:
            stamps.append(stamp)
    if not stamps:
        return None
    stamps.sort()
    return stamps[-1]


def assert_fresh(store: Any, scope: str, spec: dict[str, Any] | str) -> None:
    max_age = spec.get("max_age") if isinstance(spec, dict) else spec
    if not max_age:
        return
    from readyagents.approvals.gate import parse_expires_in

    seconds = parse_expires_in(str(max_age))
    oldest = oldest_ingested(store, scope)
    if oldest is None:
        return
    parsed = _parse_ts(oldest)
    if parsed is None:
        return
    age = datetime.now(UTC) - parsed
    if age > timedelta(seconds=seconds):
        raise KnowledgeStale(oldest, str(max_age))


def _parse_ts(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
