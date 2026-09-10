"""A2A client: card fetch, task submit/poll. SSRF pin; no creds on cross-host redirect."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from typing import Any
from urllib.parse import urljoin, urlparse

from readyagents.a2a.card import WELL_KNOWN_ALIAS, WELL_KNOWN_CARD, validate_card
from readyagents.errors import A2ACardError, A2AError, ToolError
from readyagents.mcp.builtin import _assert_public_http_url, _resolve_public_ips
from readyagents.mcp.protocol import sanitize_prompt

MAX_MESSAGE_CHARS = 8_000
MAX_REDIRECTS = 5
_TIMEOUT = 20.0

# In-process fixture hook. Production never sets this. When set, SSRF pinning
# is skipped because no sockets are opened.
A2AExchange = Callable[..., tuple[int, bytes, dict[str, str], str | None]]
_transport: ContextVar[A2AExchange | None] = ContextVar("readyagents_a2a_transport", default=None)


def use_transport(exchange: A2AExchange) -> Token[A2AExchange | None]:
    """Install an in-process HTTP exchange (tests). Not a public network bypass."""
    return _transport.set(exchange)


def reset_transport(token: Token[A2AExchange | None]) -> None:
    _transport.reset(token)


def card_urls(agent_url: str) -> list[str]:
    base = str(agent_url).rstrip("/")
    if base.endswith(WELL_KNOWN_CARD) or base.endswith(WELL_KNOWN_ALIAS):
        return [base]
    return [base + WELL_KNOWN_CARD, base + WELL_KNOWN_ALIAS]


def rpc_url(agent_url: str) -> str:
    """JSON-RPC POST target. Card ``url`` is the endpoint; ``/`` is the default path."""
    raw = str(agent_url).strip()
    parsed = urlparse(raw)
    if parsed.path in {"", "/"}:
        return raw.rstrip("/") + "/"
    return raw.rstrip("/")


def fetch_agent_card(agent_url: str, *, token: str | None = None) -> dict[str, Any]:
    last: Exception | None = None
    for url in card_urls(agent_url):
        try:
            status, body, _hdrs = request_json(
                url, method="GET", token=token, accept_redirects=True
            )
            if status >= 400:
                last = A2ACardError(f"agent card HTTP {status}")
                continue
            return validate_card(body)
        except (A2ACardError, ToolError, A2AError) as extra:
            last = extra
    raise last if last is not None else A2ACardError("agent card fetch failed")


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
    token: str | None = None,
    accept_redirects: bool = True,
    timeout: float = _TIMEOUT,
) -> tuple[int, Any, dict[str, str]]:
    """HTTP JSON with public-IP SSRF pin. Authorization never follows a new host."""
    current = url
    original_host = (urlparse(url).hostname or "").lower()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    custom = _transport.get()
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        host = parsed.hostname
        if host is None or not str(host).strip():
            raise ToolError("a2a: URL must include a host")
        headers = {"Accept": "application/json", "User-Agent": "readyagents-a2a"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        this_host = host.lower()
        if token and this_host == original_host:
            headers["Authorization"] = f"Bearer {token}"
        if custom is None:
            pinned = _assert_public_http_url(current, kind="a2a")
            assert pinned.hostname is not None
            ips = _resolve_public_ips(pinned.hostname, kind="a2a")
            port = pinned.port or (443 if pinned.scheme == "https" else 80)
            last_err: Exception | None = None
            status = 0
            raw = b""
            hdrs = {}
            location = None
            for ip in ips:
                try:
                    status, raw, hdrs, location = _exchange(
                        pinned.scheme,
                        pinned.hostname,
                        ip,
                        port,
                        pinned.path or "/",
                        pinned.query,
                        method=method,
                        body=payload,
                        headers=headers,
                        timeout=timeout,
                    )
                    last_err = None
                    break
                except (TimeoutError, OSError, ToolError) as extra:
                    last_err = extra
            if last_err is not None:
                raise ToolError(f"a2a HTTP failed: {last_err}") from last_err
        else:
            status, raw, hdrs, location = custom(
                current,
                method=method,
                body=payload,
                headers=headers,
                timeout=timeout,
            )
        if accept_redirects and status in {301, 302, 303, 307, 308} and location:
            nxt = urljoin(current, location)
            next_host = (urlparse(nxt).hostname or "").lower()
            if next_host != original_host:
                token = None
            current = nxt
            if method.upper() == "POST" and status in {301, 302, 303}:
                method = "GET"
                payload = None
            continue
        if raw and len(raw) > 1_000_000:
            raise A2AError("a2a response exceeds size cap")
        data: Any = None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = sanitize_prompt(
                    raw.decode("utf-8", errors="replace"), limit=MAX_MESSAGE_CHARS
                )
        return status, data, hdrs
    raise A2AError("a2a too many redirects")


def _exchange(
    scheme: str,
    hostname: str,
    ip: str,
    port: int,
    path: str,
    query: str,
    *,
    method: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, dict[str, str], str | None]:
    import http.client
    import socket
    import ssl

    target = path or "/"
    if query:
        target = f"{target}?{query}"
    if scheme == "https":
        ctx = ssl.create_default_context()
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            hostname, port, timeout=timeout, context=ctx
        )

        def connect() -> None:
            sock = socket.create_connection((ip, port), timeout)
            conn.sock = ctx.wrap_socket(sock, server_hostname=hostname)

        conn.connect = connect  # type: ignore[method-assign]
    else:
        conn = http.client.HTTPConnection(hostname, port, timeout=timeout)

        def connect() -> None:
            conn.sock = socket.create_connection((ip, port), timeout)

        conn.connect = connect  # type: ignore[method-assign]
    try:
        conn.request(method.upper(), target, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(1_000_001)
        hdrs = {k: v for k, v in resp.getheaders()}
        return resp.status, raw, hdrs, resp.getheader("Location")
    finally:
        conn.close()


def jsonrpc_call(
    agent_url: str,
    method: str,
    params: Mapping[str, Any],
    *,
    token: str | None = None,
    rpc_id: int = 1,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": method,
        "params": dict(params),
    }
    status, data, _hdrs = request_json(
        rpc_url(agent_url),
        method="POST",
        body=payload,
        token=token,
        accept_redirects=True,
    )
    if status >= 400:
        raise A2AError(f"a2a RPC HTTP {status}")
    if not isinstance(data, Mapping):
        raise A2AError("a2a RPC response is not JSON")
    if data.get("error"):
        err = data["error"]
        raise A2AError(str(err.get("message") or err) if isinstance(err, Mapping) else str(err))
    result = data.get("result")
    if not isinstance(result, Mapping):
        raise A2AError("a2a RPC missing result")
    return dict(result)


def poll_task(
    agent_url: str,
    task_id: str,
    *,
    token: str | None = None,
    timeout_seconds: float = 120.0,
    interval: float = 0.05,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = jsonrpc_call(agent_url, "tasks/get", {"id": task_id}, token=token)
        raw = ((last.get("status") or {}) if isinstance(last.get("status"), Mapping) else {}).get(
            "state"
        )
        state = str(raw or "").strip().lower().replace("_", "-")
        if state == "cancelled":
            state = "canceled"
        if state in {"completed", "failed", "canceled", "input-required"}:
            return last
        time.sleep(interval)
    raise A2AError("a2a task timed out")
