"""Claim-to-actor / claim-to-role mapping. Hostile values are inert, never interpolated."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from readyagents.errors import IdentityError

_ACTOR = re.compile(r"^[A-Za-z0-9._%+\-@:]{1,256}$")
_ROLE = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitise_text(value: Any, *, max_len: int = 256) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _CONTROL.sub("", text)
    if ".." in text or "/" in text or "\\" in text:
        return None
    if len(text) > max_len:
        text = text[:max_len]
    return text or None


def map_actor(claims: Mapping[str, Any], *, claim: str) -> str:
    raw = claims.get(claim)
    cleaned = sanitise_text(raw)
    if cleaned is None or not _ACTOR.match(cleaned):
        raise IdentityError(
            f"identity token refused: actor claim '{claim}' is missing or not a safe identifier"
        )
    return cleaned


def map_roles(
    claims: Mapping[str, Any],
    *,
    role_claims: Sequence[str],
    role_map: Mapping[str, str] | None = None,
) -> list[str]:
    """Derive RBAC role names. Unmapped / hostile values are dropped, not errors."""
    mapping = {str(k): str(v) for k, v in dict(role_map or {}).items() if k and v}
    roles: list[str] = []
    seen: set[str] = set()
    for name in role_claims:
        raw = claims.get(name)
        values: list[Any]
        if isinstance(raw, list):
            values = list(raw)
        elif raw is None:
            values = []
        else:
            values = [raw]
        for item in values:
            cleaned = sanitise_text(item, max_len=64)
            if cleaned is None:
                continue
            mapped = mapping.get(cleaned, cleaned if not mapping else None)
            if mapped is None:
                continue
            if not _ROLE.match(mapped):
                continue
            if mapped not in seen:
                seen.add(mapped)
                roles.append(mapped)
    return roles


def sanitise_claims(
    claims: Mapping[str, Any], *, names: Sequence[str]
) -> dict[str, str | int | float | bool | None]:
    out: dict[str, str | int | float | bool | None] = {}
    for name in names:
        if name not in claims:
            continue
        raw = claims[name]
        if isinstance(raw, bool) or raw is None:
            out[name] = raw
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            out[name] = raw
        elif isinstance(raw, list):
            cleaned = [sanitise_text(item, max_len=64) for item in raw[:32]]
            out[name] = ",".join(item for item in cleaned if item)  # type: ignore[assignment]
        else:
            out[name] = sanitise_text(raw)
    return out
