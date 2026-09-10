"""Time-bounded, single-hop, revocable delegation under ``$READYAGENTS_HOME``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.approvals.gate import format_clock, normalize_actor, parse_clock
from readyagents.atomic import atomic_write_text
from readyagents.errors import ConfigError, TrustError
from readyagents.permissions import restrict_dir, restrict_file

STORE_NAME = "delegations.json"
STORE_VERSION = 1


@dataclass
class Delegation:
    id: str
    from_actor: str
    to_actor: str
    until: str
    scope: str | None = None
    revoked: bool = False
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from": self.from_actor,
            "to": self.to_actor,
            "until": self.until,
            "scope": self.scope,
            "revoked": self.revoked,
            "created_at": self.created_at,
        }


def store_path(home: Path | str | None = None) -> Path:
    if home is None:
        from readyagents.config import get_settings

        base = get_settings().home_path()
    else:
        base = Path(home)
    return Path(base) / STORE_NAME


def load_delegations(
    path: Path | str | None = None, *, home: Path | str | None = None
) -> list[Delegation]:
    dest = Path(path) if path is not None else store_path(home)
    if not dest.exists():
        return []
    if not dest.is_file():
        raise TrustError(
            f"delegations store is not a file: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as extra:
        raise TrustError(
            f"delegations store unreadable: {dest}",
            artifact=str(dest),
            reason="malformed",
        ) from extra
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        raise TrustError(
            f"delegations store malformed: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise TrustError(
            f"delegations store malformed: {dest}",
            artifact=str(dest),
            reason="malformed",
        )
    out: list[Delegation] = []
    for row in items:
        if not isinstance(row, dict):
            raise TrustError(
                f"delegations store malformed: {dest}",
                artifact=str(dest),
                reason="malformed",
            )
        out.append(
            Delegation(
                id=str(row.get("id") or ""),
                from_actor=str(row.get("from") or ""),
                to_actor=str(row.get("to") or ""),
                until=str(row.get("until") or ""),
                scope=str(row["scope"]) if row.get("scope") else None,
                revoked=bool(row.get("revoked")),
                created_at=str(row.get("created_at") or ""),
            )
        )
    return out


def save_delegations(
    items: list[Delegation], *, home: Path | str | None = None, path: Path | str | None = None
) -> Path:
    dest = Path(path) if path is not None else store_path(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    restrict_dir(dest.parent)
    payload = json.dumps(
        {"version": STORE_VERSION, "items": [item.as_dict() for item in items]},
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_text(dest, payload + "\n", encoding="utf-8", newline="\n", restrict=True)
    restrict_file(dest)
    return dest


def add_delegation(
    *,
    from_actor: str,
    to_actor: str,
    until: str,
    scope: str | None = None,
    home: Path | str | None = None,
    now: datetime | None = None,
) -> Delegation:
    source = from_actor.strip()
    dest = to_actor.strip()
    if not source or not dest:
        raise ConfigError("delegate requires --from and --to")
    if normalize_actor(source) == normalize_actor(dest):
        raise ConfigError("self-delegation is refused")
    until_stamp = parse_clock(until)
    if until_stamp is None:
        raise ConfigError(f"delegation --until is not a timestamp: {until}")
    stamp = now or datetime.now(UTC)
    if until_stamp <= stamp:
        raise ConfigError("delegation --until must be in the future")
    items = load_delegations(home=home)
    scope_norm = scope.strip() if scope else None
    source_n = normalize_actor(source)
    dest_n = normalize_actor(dest)
    for item in items:
        if item.revoked:
            continue
        end = parse_clock(item.until)
        if end is None or end <= stamp:
            continue
        if normalize_actor(item.to_actor) == source_n:
            raise ConfigError("delegation chains are refused")
        same_pair = (
            normalize_actor(item.from_actor) == source_n
            and normalize_actor(item.to_actor) == dest_n
        )
        if same_pair:
            if _same_scope(item.scope, scope_norm) or item.scope is None:
                raise ConfigError("an overlapping delegation already exists")
            if scope_norm is None:
                raise ConfigError("delegation must not widen scope")
    entry = Delegation(
        id=uuid4().hex,
        from_actor=source,
        to_actor=dest,
        until=format_clock(until_stamp),
        scope=scope_norm,
        created_at=format_clock(stamp),
    )
    items.append(entry)
    save_delegations(items, home=home)
    return entry


def revoke_delegation(delegation_id: str, *, home: Path | str | None = None) -> Delegation:
    token = delegation_id.strip()
    items = load_delegations(home=home)
    found = None
    for item in items:
        if item.id == token:
            item.revoked = True
            found = item
            break
    if found is None:
        raise ConfigError(f"delegation not found: {token}")
    save_delegations(items, home=home)
    return found


def find_delegation(
    *,
    actor: str | None,
    roles: list[str] | None = None,
    home: Path | str | None = None,
    now: datetime | None = None,
) -> Delegation | None:
    """Active delegation *to* actor, checked at decision time."""
    dest = normalize_actor(actor)
    if not dest:
        return None
    stamp = now or datetime.now(UTC)
    wanted = {normalize_actor(item) for item in (roles or []) if item}
    for item in load_delegations(home=home):
        if item.revoked:
            continue
        if normalize_actor(item.to_actor) != dest:
            continue
        until = parse_clock(item.until)
        if until is None or until <= stamp:
            continue
        if item.scope and wanted and normalize_actor(item.scope) not in wanted:
            continue
        return item
    return None


def _same_scope(left: str | None, right: str | None) -> bool:
    if not left and not right:
        return True
    if not left or not right:
        return False
    return normalize_actor(left) == normalize_actor(right)
