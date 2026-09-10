"""Authenticated HTTP, Retry-After, pagination, size caps. Stdlib only."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urljoin

from readyagents import __version__
from readyagents.connectors.spec import HttpResponse
from readyagents.errors import ConnectorCapError, ToolError
from readyagents.mcp.builtin import _assert_public_http_url, _resolve_public_ips

_TIMEOUT = 20.0


def parse_retry_after(raw: str | None) -> float | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        return None


def iter_cursor_pages(
    fetch: Callable[[str | None], Mapping[str, Any]],
    *,
    cursor_key: str = "cursor",
    next_key: str = "next_cursor",
    max_pages: int = 10,
) -> Iterator[Mapping[str, Any]]:
    cursor: str | None = None
    for _ in range(max(1, int(max_pages))):
        page = fetch(cursor)
        yield page
        nxt = page.get(next_key)
        if nxt is None:
            nxt = page.get(cursor_key)
        if not nxt:
            return
        cursor = str(nxt)
    raise ConnectorCapError(f"pagination exceeded {max_pages} pages")


def iter_token_pages(
    fetch: Callable[[str | None], Mapping[str, Any]],
    *,
    token_key: str = "page_token",
    next_key: str = "next_page_token",
    max_pages: int = 10,
) -> Iterator[Mapping[str, Any]]:
    return iter_cursor_pages(fetch, cursor_key=token_key, next_key=next_key, max_pages=max_pages)


def exchange(
    ctx: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    json_body: Any = None,
    retry: bool = True,
) -> HttpResponse:
    payload = body
    req_headers = {str(k): str(v) for k, v in dict(headers or {}).items()}
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    if ctx.fixtures is not None:
        resp = ctx.fixtures.exchange(method, url, body=payload)
        ctx.cap_body(resp.body)
        return resp
    max_tries = DEFAULT_RETRIES if retry else 1
    last: HttpResponse | None = None
    current = url
    for attempt in range(max_tries):
        last = _once(ctx, method, current, headers=req_headers, body=payload)
        if last.status in {429, 503}:
            wait = parse_retry_after(last.headers.get("Retry-After"))
            if wait is None:
                wait = min(2**attempt, 8)
            from readyagents.workflow.governor import notify_retry_after

            spec = getattr(ctx, "spec", None)
            provider = str(getattr(spec, "name", None) or "connector")
            notify_retry_after(provider, last.headers, seconds=float(wait))
            if attempt + 1 < max_tries:
                time.sleep(min(float(wait), 30.0))
                continue
        if last.status in {301, 302, 303, 307, 308}:
            location = last.headers.get("Location") or last.headers.get("location")
            if location:
                current = urljoin(current, location)
                ctx.assert_destination(current)
                continue
        ctx.cap_body(last.body)
        return last
    assert last is not None
    ctx.cap_body(last.body)
    return last


DEFAULT_RETRIES = 3


def _once(
    ctx: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpResponse:
    parsed = _assert_public_http_url(url, kind="connector")
    host = parsed.hostname
    assert host is not None
    ips = _resolve_public_ips(host, kind="connector")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    last_err: Exception | None = None
    for ip in ips:
        try:
            status, raw, hdrs = _http_exchange(
                parsed.scheme, host, ip, port, path, method=method, body=body, headers=headers
            )
            return HttpResponse(status=status, headers=hdrs, body=raw, url=url)
        except (TimeoutError, OSError) as extra:
            last_err = extra
    raise ToolError(f"connector HTTP failed: {last_err}") from last_err


def _http_exchange(
    scheme: str,
    hostname: str,
    ip: str,
    port: int,
    path: str,
    *,
    method: str,
    body: bytes | None,
    headers: Mapping[str, str],
) -> tuple[int, bytes, dict[str, str]]:
    timeout = _TIMEOUT
    req_headers = {"User-Agent": f"readyagents/{__version__}"}
    req_headers.update(headers)
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
        conn.request(method.upper(), path, body=body, headers=req_headers)
        resp = conn.getresponse()
        from readyagents.connectors.context import DEFAULT_MAX_BYTES

        payload = resp.read(DEFAULT_MAX_BYTES + 1)
        hdrs = {k: v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()
