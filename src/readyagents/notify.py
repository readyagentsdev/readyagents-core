"""Outbound pause notify. Core never listens; packs may receive the webhook."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin

from readyagents import __version__
from readyagents.mcp.builtin import (
    _MAX_HTTP_REDIRECTS,
    _assert_public_http_url,
    _http_exchange,
    _resolve_public_ips,
)

_KIND = "notify"


def post_json(url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    current = url.strip() if isinstance(url, str) else ""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"readyagents/{__version__}",
    }
    assertion = _workload_bearer(current)
    if assertion:
        headers["Authorization"] = assertion
    for _ in range(_MAX_HTTP_REDIRECTS + 1):
        parsed = _assert_public_http_url(current, kind=_KIND)
        assert parsed.hostname is not None
        ips = _resolve_public_ips(parsed.hostname, kind=_KIND)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        last_err: Exception | None = None
        status = 0
        location: str | None = None
        for ip in ips:
            try:
                status, _body, location = _http_exchange(
                    parsed.scheme,
                    parsed.hostname,
                    ip,
                    port,
                    path,
                    method="POST",
                    body=data,
                    headers=headers,
                    timeout=timeout,
                )
                last_err = None
                break
            except (TimeoutError, OSError) as exc:
                last_err = exc
        if last_err is not None:
            raise last_err
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        if status >= 400:
            raise OSError(f"HTTP Error {status}")
        return
    raise OSError("too many redirects")


def _workload_bearer(url: str) -> str | None:
    """Attach a short-lived workload JWT when configured. Fail closed on errors."""
    from readyagents.identity.workload import sign_assertion, workload_configured

    if not workload_configured():
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        audience = f"{parsed.scheme}://{parsed.netloc}"
    else:
        audience = "readyagents"
    token = sign_assertion(audience=audience)
    return f"Bearer {token}"
