"""Per-call secret grant/scrub at the tool-dispatch seam."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from readyagents.credentials.policy import ToolGrant
from readyagents.secrets import lookup_secret
from readyagents.workflow.state import utc_now

_GRANTED: ContextVar[Mapping[str, str] | None] = ContextVar("ra_granted_secrets", default=None)


@dataclass(frozen=True)
class GrantedSecret:
    name: str
    value: str
    kind: str  # "static" | "minted"
    expires_at: str | None = None


def current_granted() -> Mapping[str, str]:
    """Secrets granted to the in-flight tool. Empty outside a broker call."""
    return dict(_GRANTED.get() or {})


def parse_ttl(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip().lower()
    if text.endswith("s") and text[:-1].isdigit():
        return int(text[:-1])
    if text.endswith("m") and text[:-1].isdigit():
        return int(text[:-1]) * 60
    if text.endswith("h") and text[:-1].isdigit():
        return int(text[:-1]) * 3600
    if text.isdigit():
        return int(text)
    return None


def materialise(
    grant: ToolGrant,
    backends: Any,
    *,
    mint: bool = True,
) -> list[GrantedSecret]:
    ttl = parse_ttl(grant.ttl)
    out: list[GrantedSecret] = []
    for name in grant.secrets:
        minted = _try_mint(backends, name, ttl) if mint else None
        if minted is not None:
            out.append(minted)
            continue
        value = lookup_secret(name, backends)
        if value is None:
            value = os.environ.get(name)
        if not value:
            continue
        out.append(GrantedSecret(name=name, value=value, kind="static"))
    return out


def credential_kind(granted: list[GrantedSecret]) -> str | None:
    if not granted:
        return None
    if all(item.kind == "minted" for item in granted):
        return "minted"
    return "static"


@contextmanager
def scoped_env(
    granted: list[GrantedSecret],
    *,
    managed: set[str],
) -> Iterator[Mapping[str, str]]:
    """Strip managed names from os.environ except those granted to this tool."""
    mapping = {item.name: item.value for item in granted}
    token = _GRANTED.set(mapping)
    saved: dict[str, str | None] = {}
    try:
        for name in managed:
            saved[name] = os.environ.get(name)
            if name in mapping:
                os.environ[name] = mapping[name]
            elif name in os.environ:
                del os.environ[name]
        yield mapping
    finally:
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        _GRANTED.reset(token)


def minimal_child_env(granted: Mapping[str, str] | None = None) -> dict[str, str]:
    """Passthrough plus explicitly granted values. No inherited secret dump."""
    keep = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USER",
        "LOGNAME",
        "SHELL",
    }
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update(dict(granted or {}))
    return env


def audit_payload(
    event: str,
    *,
    tool: str,
    node_id: str,
    run_id: str | None,
    names: list[str],
    kind: str | None,
    decision: str = "allow",
) -> dict[str, Any]:
    """Grant/use/denial fields. Never the secret value."""
    return {
        "event": event,
        "tool": tool,
        "node_id": node_id,
        "run_id": run_id,
        "scope": list(names),
        "credential_kind": kind,
        "decision": decision,
        "ts": utc_now(),
    }


def _try_mint(backends: Any, name: str, ttl: int | None) -> GrantedSecret | None:
    from readyagents.secrets import as_backends

    for backend in as_backends(backends):
        fn: Callable[..., Any] | None = getattr(backend, "mint", None)
        if not callable(fn):
            continue
        try:
            raw = fn(name, ttl_seconds=ttl)
        except TypeError:
            try:
                raw = fn(name)
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
        if raw is None:
            continue
        if isinstance(raw, GrantedSecret):
            return raw
        if isinstance(raw, Mapping) and raw.get("value"):
            kind = str(raw.get("kind") or "minted")
            if kind == "static":
                return GrantedSecret(
                    name=name, value=str(raw["value"]), kind="static", expires_at=None
                )
            return GrantedSecret(
                name=name,
                value=str(raw["value"]),
                kind="minted",
                expires_at=raw.get("expires_at"),  # type: ignore[arg-type]
            )
        text = str(raw).strip()
        if text:
            return GrantedSecret(name=name, value=text, kind="minted")
    return None


def wrap_runner(
    runner: Callable[[], Any],
    *,
    granted: list[GrantedSecret],
    managed: set[str],
) -> Callable[[], Any]:
    def _run() -> Any:
        with scoped_env(granted, managed=managed):
            return runner()

    return _run
