"""Frozen declared-action schema, driver protocol, and page snapshot.

A model may choose among these actions; it cannot synthesise others.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from readyagents.errors import BrowserRefused

DECLARED_ACTIONS: frozenset[str] = frozenset(
    {
        "navigate",
        "read",
        "click",
        "type",
        "select",
        "wait_for",
        "screenshot",
        "download",
        "extract",
    }
)
ACTION_ALIASES: dict[str, str] = {"wait-for": "wait_for"}
SIDE_EFFECT_TOKENS: tuple[str, ...] = ("submit", "purchase", "delete", "send")


@dataclass
class PageSnapshot:
    """Driver-returned page. All text fields are untrusted once in run state."""

    url: str
    title: str = ""
    text: str = ""
    hidden_text: str = ""
    alt_text: str = ""
    links: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    subresources: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)
    memory_bytes: int = 0
    elapsed_ms: int = 0
    screenshot: bytes = b""
    download_bytes: bytes = b""
    download_name: str = "download.bin"


@dataclass
class DownloadResult:
    name: str
    data: bytes
    url: str = ""


@runtime_checkable
class BrowserDriver(Protocol):
    """Narrow driver surface. Core never imports Playwright/Selenium/Chromium."""

    def navigate(self, url: str) -> PageSnapshot: ...

    def read(self, selector: str | None = None) -> PageSnapshot: ...

    def click(self, selector: str) -> PageSnapshot: ...

    def type(self, selector: str, text: str) -> PageSnapshot: ...

    def select(self, selector: str, value: str) -> PageSnapshot: ...

    def wait_for(self, selector: str) -> PageSnapshot: ...

    def screenshot(self) -> bytes: ...

    def download(self, selector: str | None, url: str | None) -> DownloadResult: ...

    def current_url(self) -> str: ...

    def fill_credentials(self, host: str, secrets: Mapping[str, str]) -> None: ...

    def scrub_credentials(self) -> None: ...

    def close(self) -> None: ...


def parse_actions(raw: Any) -> list[tuple[str, dict[str, Any]]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BrowserRefused("browser actions must be a list", reason="action")
    return [parse_action(item) for item in raw]


def parse_action(raw: Any) -> tuple[str, dict[str, Any]]:
    """Return ``(op, params)``. Unknown keys are a typed refuse, not a skip."""
    if not isinstance(raw, dict) or not raw:
        raise BrowserRefused("each browser action must be a mapping", reason="action")
    ops: list[tuple[str, Any]] = []
    for key, value in raw.items():
        name = ACTION_ALIASES.get(str(key), str(key))
        if name not in DECLARED_ACTIONS:
            raise BrowserRefused(f"undeclared browser action {key!r}", reason="action")
        ops.append((name, value))
    if len(ops) != 1:
        raise BrowserRefused(
            "each browser action must declare exactly one operation", reason="action"
        )
    op, payload = ops[0]
    return op, _normalize_payload(op, payload)


def _normalize_payload(op: str, payload: Any) -> dict[str, Any]:
    if op == "navigate":
        if isinstance(payload, str):
            return {"url": payload}
        if isinstance(payload, dict) and payload.get("url"):
            return dict(payload)
        raise BrowserRefused("navigate requires a url", reason="action")
    if payload is None:
        return {}
    if isinstance(payload, str):
        if op in {"read", "click", "wait_for", "screenshot", "download", "extract"}:
            key = "path" if op in {"screenshot", "download"} else "selector"
            return {key: payload}
        raise BrowserRefused(f"{op} requires a mapping", reason="action")
    if not isinstance(payload, dict):
        raise BrowserRefused(f"{op} payload must be a mapping", reason="action")
    return dict(payload)


def is_side_effecting(op: str, params: Mapping[str, Any]) -> bool:
    """Submit/purchase/delete/send clicks are gated unless explicitly allowed."""
    if op not in {"click", "type", "select"}:
        return False
    declared = params.get("side_effecting")
    if declared is False:
        return False
    if declared is True:
        return True
    blob = " ".join(
        str(params.get(key) or "") for key in ("selector", "text", "value", "name", "url")
    ).lower()
    return any(token in blob for token in SIDE_EFFECT_TOKENS)
