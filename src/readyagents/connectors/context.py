"""ConnectorContext: the only allowed path to HTTP, secrets, caps, and the limiter."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from readyagents.connectors.spec import ConnectorSpec, HttpResponse
from readyagents.credentials.broker import current_granted
from readyagents.errors import ConnectorAuthError, ConnectorCapError, ToolError

_CURRENT: ContextVar[ConnectorContext | None] = ContextVar("connector_ctx", default=None)

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RETRY_WAIT = 30.0
_IN_HTTP: ContextVar[bool] = ContextVar("connector_in_http", default=False)


def current_context() -> ConnectorContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise ToolError("connector context is missing; call through dispatch_tool")
    return ctx


def bind_context(ctx: ConnectorContext) -> Any:
    return _CURRENT.set(ctx)


def reset_context(token: Any) -> None:
    _CURRENT.reset(token)


def in_context_http() -> bool:
    return bool(_IN_HTTP.get())


class RateLimiter:
    def __init__(self, requests: int, window_seconds: float) -> None:
        self.requests = max(1, int(requests))
        self.window = max(0.001, float(window_seconds))
        self._hits: list[float] = []

    def check(self) -> None:
        now = time.monotonic()
        cutoff = now - self.window
        self._hits = [stamp for stamp in self._hits if stamp >= cutoff]
        if len(self._hits) >= self.requests:
            raise ConnectorCapError("connector rate limit exceeded")
        self._hits.append(now)


class ConnectorContext:
    """Granted secrets, pinned HTTP, caps, limiter, fixtures, idempotency."""

    def __init__(
        self,
        spec: ConnectorSpec,
        *,
        workspace: Path | None = None,
        fixtures: Any = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
        state: Any = None,
        node_id: str | None = None,
        redactor: Any = None,
    ) -> None:
        self.spec = spec
        self.workspace = Path(workspace) if workspace is not None else Path.cwd()
        self.fixtures = fixtures
        self.max_bytes = int(max_bytes)
        self.max_pages = int(max_pages)
        self.state = state
        self.node_id = node_id
        self.redactor = redactor
        self._limiter: RateLimiter | None = None
        if spec.rate_limit is not None:
            self._limiter = RateLimiter(spec.rate_limit.requests, spec.rate_limit.window_seconds)
        self._idempotency = _load_idempotency(state, spec.name)

    def secret(self, name: str) -> str:
        granted = current_granted()
        if name not in granted:
            raise ConnectorAuthError(f"secret {name!r} was not granted to this connector")
        return granted[name]

    def granted_secrets(self) -> Mapping[str, str]:
        return current_granted()

    def assert_destination(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if not host:
            raise ToolError("connector URL must include a host")
        allowed = _destination_hosts(self.spec.destinations)
        if "local" in allowed:
            raise ToolError(f"connector {self.spec.name!r} does not allow HTTP destinations")
        if host not in allowed and not any(
            host == item or host.endswith("." + item) for item in allowed
        ):
            raise ToolError(f"connector {self.spec.name!r} destination {host!r} is not declared")
        return host

    def http(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        json_body: Any = None,
        retry: bool = True,
    ) -> HttpResponse:
        if self._limiter is not None:
            self._limiter.check()
        self.assert_destination(url)
        token = _IN_HTTP.set(True)
        try:
            from readyagents.connectors.sdk import exchange

            return exchange(
                self,
                method,
                url,
                headers=headers,
                body=body,
                json_body=json_body,
                retry=retry,
            )
        finally:
            _IN_HTTP.reset(token)

    def idempotent(self, key: str, produce: Callable[[], Any]) -> Any:
        if not key:
            return produce()
        if key in self._idempotency:
            return self._idempotency[key]
        result = produce()
        self._idempotency[key] = result
        _store_idempotency(self.state, self.spec.name, self._idempotency)
        return result

    def cap_body(self, data: bytes, *, what: str = "response") -> bytes:
        if len(data) > self.max_bytes:
            raise ConnectorCapError(f"connector {what} exceeded {self.max_bytes} bytes")
        return data


def _destination_hosts(destinations: tuple[str, ...]) -> set[str]:
    hosts: set[str] = set()
    for raw in destinations:
        text = str(raw).strip().lower()
        if not text:
            continue
        if text == "local":
            hosts.add("local")
            continue
        if "://" in text:
            host = urlparse(text).hostname or ""
        else:
            host = text.split("/", 1)[0].split(":", 1)[0]
        host = host.strip(".").lower()
        if host:
            hosts.add(host)
    return hosts


def _load_idempotency(state: Any, name: str) -> dict[str, Any]:
    meta = getattr(state, "metadata", None) if state is not None else None
    if not isinstance(meta, dict):
        return {}
    bucket = meta.get("connector_idempotency")
    if not isinstance(bucket, dict):
        return {}
    existing = bucket.get(name)
    return dict(existing) if isinstance(existing, dict) else {}


def _store_idempotency(state: Any, name: str, mapping: Mapping[str, Any]) -> None:
    if state is None or not isinstance(getattr(state, "metadata", None), dict):
        return
    bucket = state.metadata.setdefault("connector_idempotency", {})
    if isinstance(bucket, dict):
        bucket[name] = dict(mapping)
