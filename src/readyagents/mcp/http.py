"""Loopback Streamable HTTP composition: auth, DNS-rebinding, body limit."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import ipaddress
import json
import os
import secrets
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from readyagents.config import (
    DEFAULT_MCP_TOKEN_ENV,
    LOOPBACK_HOSTS,
    MAX_CONCURRENT_RUNS_HARD,
    MAX_HTTP_BODY_BYTES,
    MAX_PENDING_RUNS_HARD,
    get_settings,
)
from readyagents.errors import MCPError, missing_extra_message
from readyagents.mcp.dispatch import McpRpcSurface, ProtocolDispatchMiddleware
from readyagents.mcp.server import construct_server, streamable_http_app

_SCOPE_REQUEST_ID = "readyagents.request_id"
_MAX_REQUEST_ID_LEN = 128


def assert_loopback_host(host: str) -> str:
    """Reject non-loopback bind hosts. Allow 127.0.0.1, localhost, and ::1."""
    if host is None or not str(host).strip():
        raise MCPError("MCP HTTP bind host must be loopback, not empty.")
    raw = str(host).strip()
    if raw.startswith("[") and raw.endswith("]") and len(raw) > 2:
        raw = raw[1:-1]
    lowered = raw.lower()
    if lowered in {"0.0.0.0", "::", "[::]"}:
        raise MCPError(
            f"MCP HTTP bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        )
    if lowered in LOOPBACK_HOSTS:
        return raw
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise MCPError(
            f"MCP HTTP bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        ) from exc
    if ip.is_unspecified or not ip.is_loopback:
        raise MCPError(
            f"MCP HTTP bind host '{host}' is not loopback. Use 127.0.0.1, localhost, or ::1."
        )
    return raw


def resolve_bearer_token(*, auth_mode: str, token_env: str, bind_host: str) -> str | None:
    """Return a bearer token, or None when ``--auth none`` on loopback."""
    mode = (auth_mode or "").strip().lower()
    env_name = (token_env or DEFAULT_MCP_TOKEN_ENV).strip() or DEFAULT_MCP_TOKEN_ENV
    bind_host = assert_loopback_host(bind_host)
    if mode == "none":
        return None
    if mode != "token":
        raise MCPError("Invalid --auth value. Use token or none.")
    existing = os.environ.get(env_name)
    if existing is not None and existing.strip():
        return existing.strip()
    return secrets.token_urlsafe(32)


def compose_http_app(
    *,
    server: Any,
    coordinator: Any = None,
    token: str | None,
    bind_host: str,
    bind_port: int,
    max_body_bytes: int = MAX_HTTP_BODY_BYTES,
    approval_app: Any = None,
) -> Any:
    """SDK Streamable HTTP app plus /runs routes, wrapped in pure ASGI middleware."""
    bind_host = assert_loopback_host(bind_host)
    if not (1 <= int(bind_port) <= 65535):
        raise MCPError("MCP HTTP port must be between 1 and 65535.")
    secret = token.strip() if isinstance(token, str) and token.strip() else None
    mcp_app = streamable_http_app(
        server, host=bind_host, port=int(bind_port), max_body_bytes=max_body_bytes
    )
    router = getattr(mcp_app, "router", None)
    routes = getattr(router, "routes", None)
    if routes is not None:
        extra = _run_routes(coordinator)
        if approval_app is not None:
            extra[0:0] = _approval_mount_routes(approval_app)
        routes[0:0] = extra
    skip_prefixes = ("/approvals",) if approval_app is not None else ()
    settings = get_settings()
    bound = getattr(coordinator, "settings", None) or settings
    max_streams = 4
    if coordinator is not None:
        max_streams = max(1, int(getattr(coordinator, "max_concurrent_runs", 4) or 4))
    surface = McpRpcSurface(
        coordinator=coordinator,
        max_streams=max_streams,
        decision_secret=getattr(bound, "decision_secret", None),
        default_actor=getattr(bound, "actor", None),
    )
    dispatched = ProtocolDispatchMiddleware(mcp_app, surface)
    # Do not wrap in a second Starlette: StreamableHTTPSessionManager.run() is once-only.
    return CacheControlMiddleware(
        AuthMiddleware(
            HostOriginMiddleware(
                BodyLimitMiddleware(dispatched, max_body_bytes=max_body_bytes),
                bind_host=bind_host,
                bind_port=int(bind_port),
            ),
            token=secret,
            skip_prefixes=skip_prefixes,
        )
    )


def serve_streamable_http(
    *,
    host: str,
    port: int,
    auth_mode: str,
    token_env: str,
    max_concurrent_runs: int,
    max_pending_runs: int,
    allow_http: bool | None = None,
    workspace: Path | None = None,
    approval_ui: bool = False,
) -> None:
    """Foreground loopback Streamable HTTP server. Blocking. Not started on import."""
    coordinator: Any = None
    try:
        host = assert_loopback_host(host)
        if not (1 <= int(port) <= 65535):
            raise MCPError("MCP HTTP port must be between 1 and 65535.")
        max_concurrent_runs = max(1, min(int(max_concurrent_runs), MAX_CONCURRENT_RUNS_HARD))
        max_pending_runs = max(1, min(int(max_pending_runs), MAX_PENDING_RUNS_HARD))
        mode = (auth_mode or "").strip().lower()
        env_name = (token_env or DEFAULT_MCP_TOKEN_ENV).strip() or DEFAULT_MCP_TOKEN_ENV
        prior = (os.environ.get(env_name) or "").strip()
        token = resolve_bearer_token(auth_mode=mode, token_env=env_name, bind_host=host)
        if mode == "token" and not prior:
            print("Generated MCP bearer token (shown once):", file=sys.stderr)
            print(token, file=sys.stderr)
        elif mode == "none":
            print(
                "Warning: MCP HTTP authentication is disabled (--auth none). "
                "Loopback-only; do not expose this process.",
                file=sys.stderr,
            )
        settings = get_settings()
        root = Path(workspace) if workspace is not None else settings.workspace_path()
        server = construct_server(allow_http=allow_http, workspace=root)
        coordinator = _try_run_coordinator(
            workspace=root,
            max_concurrent_runs=max_concurrent_runs,
            max_pending_runs=max_pending_runs,
        )
        approval_app = None
        if approval_ui:
            from readyagents.approvals.app import compose_approval_app
            from readyagents.approvals.tokens import TokenService

            ui_tokens = TokenService()
            approval_app = compose_approval_app(
                tokens=ui_tokens,
                coordinator=coordinator,
                bind_host=host,
                bind_port=int(port),
                settings=settings,
            )
            bootstrap = ui_tokens.issue_bootstrap()
            print(
                "Approval UI bootstrap URL (stderr only; single-use):",
                file=sys.stderr,
            )
            print(f"http://{host}:{int(port)}/approvals?token={bootstrap}", file=sys.stderr)
        app = compose_http_app(
            server=server,
            coordinator=coordinator,
            token=token,
            bind_host=host,
            bind_port=int(port),
            max_body_bytes=MAX_HTTP_BODY_BYTES,
            approval_app=approval_app,
        )
        try:
            import uvicorn
        except ImportError as exc:
            raise MCPError(missing_extra_message("MCP", "mcp")) from exc
        uvicorn.run(app, host=host, port=int(port), log_level="warning")
    finally:
        shutdown = getattr(coordinator, "shutdown", None) if coordinator is not None else None
        if callable(shutdown):
            shutdown()


def _approval_mount_routes(approval_app: Any) -> list[Any]:
    try:
        from starlette.routing import Route
    except ImportError:
        return []

    async def _dispatch(request: Any) -> Any:
        from starlette.responses import Response

        header_items = list(request.headers.items())
        body = await request.body()
        query = dict(request.query_params)
        response = approval_app.handle(
            request.method,
            request.url.path,
            query=query,
            headers=header_items,
            body=body,
        )
        headers = {k: v for k, v in response.headers}
        return Response(response.body, status_code=response.status, headers=headers)

    paths = [
        "/approvals",
        "/approvals/",
        "/approvals/api/runs",
        "/approvals/assets/app.js",
        "/approvals/assets/style.css",
        "/approvals/assets/index.html",
    ]
    routes = [Route(path, endpoint=_dispatch, methods=["GET", "POST", "HEAD"]) for path in paths]
    routes.append(
        Route(
            "/approvals/api/runs/{run_id}/decide",
            endpoint=_dispatch,
            methods=["GET", "POST"],
        )
    )
    return routes


def _run_routes(coordinator: Any) -> list[Any]:
    if coordinator is None:
        return []
    try:
        from readyagents.mcp.run_api import build_run_routes
    except ImportError:
        return []
    return list(build_run_routes(coordinator))


def _try_run_coordinator(
    *,
    workspace: Path | None,
    max_concurrent_runs: int,
    max_pending_runs: int,
) -> Any | None:
    try:
        from readyagents.mcp.run_api import RunCoordinator
    except ImportError:
        return None
    desired = {
        "settings": get_settings(),
        "workspace": workspace,
        "max_concurrent_runs": max_concurrent_runs,
        "max_pending_runs": max_pending_runs,
    }
    try:
        signature = inspect.signature(RunCoordinator)
    except (TypeError, ValueError):
        return RunCoordinator()
    params = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return RunCoordinator(**desired)
    accepted = {key: value for key, value in desired.items() if key in params}
    return RunCoordinator(**accepted)


def _header(scope: dict[str, Any], name: str) -> str | None:
    key = name.lower().encode("latin-1")
    for raw_key, raw_value in scope.get("headers") or ():
        if raw_key.lower() == key:
            return raw_value.decode("latin-1")
    return None


def _header_values(scope: dict[str, Any], name: str) -> list[str]:
    key = name.lower().encode("latin-1")
    values: list[str] = []
    for raw_key, raw_value in scope.get("headers") or ():
        if raw_key.lower() == key:
            values.append(raw_value.decode("latin-1"))
    return values


def _parse_request_id(scope: dict[str, Any]) -> str:
    raw = _header(scope, "x-request-id")
    if raw is None:
        return uuid.uuid4().hex
    value = raw.strip()
    if (
        1 <= len(value) <= _MAX_REQUEST_ID_LEN
        and value.isascii()
        and value.isprintable()
        and all(ch not in value for ch in " \t\r\n")
    ):
        return value
    return uuid.uuid4().hex


def _ensure_request_id(scope: dict[str, Any]) -> str:
    existing = scope.get(_SCOPE_REQUEST_ID)
    if isinstance(existing, str) and existing:
        return existing
    request_id = _parse_request_id(scope)
    scope[_SCOPE_REQUEST_ID] = request_id
    return request_id


def _error_payload(*, error: str, message: str, request_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "message": message,
        "run_id": None,
        "request_id": request_id,
    }


async def _send_json(
    send: Any,
    *,
    status: int,
    payload: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_id = str(payload.get("request_id") or "")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if request_id:
        headers.append((b"x-request-id", request_id.encode("utf-8", errors="replace")))
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _canonical_host(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]") and len(value) > 2:
        value = value[1:-1]
    if value == "0:0:0:0:0:0:0:1":
        return "::1"
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def _split_host_port(value: str) -> tuple[str, int | None]:
    text = value.strip()
    if not text:
        raise ValueError("empty host")
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ValueError("bad ipv6 host")
        host = text[1:end]
        rest = text[end + 1 :]
        if not rest:
            return host, None
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        raise ValueError("bad ipv6 host port")
    if text.count(":") == 1:
        host, port_s = text.rsplit(":", 1)
        if port_s.isdigit():
            return host, int(port_s)
    return text, None


def _hostname_allowed(hostname: str, bind_host: str) -> bool:
    got = _canonical_host(hostname)
    allowed = {_canonical_host(bind_host)}
    for item in LOOPBACK_HOSTS:
        allowed.add(_canonical_host(item))
    return got in allowed


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    provided = parts[1].strip()
    return provided or None


def _tokens_match(provided: str, expected: str) -> bool:
    got = hashlib.sha256(provided.encode("utf-8")).digest()
    want = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(got, want)


def bearer_authorized(*, authorization: str | None, token: str | None) -> bool:
    """True when no token is configured, or the Authorization header matches."""
    if token is None:
        return True
    provided = _extract_bearer(authorization)
    if provided is None:
        return False
    return _tokens_match(provided, token)


class BodyLimitMiddleware:
    """Reject oversized HTTP bodies with 413 before the inner app."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _ensure_request_id(scope)
        declared = _header(scope, "content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                await _send_json(
                    send,
                    status=400,
                    payload=_error_payload(
                        error="BadRequest",
                        message="Invalid Content-Length header.",
                        request_id=request_id,
                    ),
                )
                return
            if size < 0:
                await _send_json(
                    send,
                    status=400,
                    payload=_error_payload(
                        error="BadRequest",
                        message="Invalid Content-Length header.",
                        request_id=request_id,
                    ),
                )
                return
            if size > self.max_body_bytes:
                await _send_json(
                    send,
                    status=413,
                    payload=_error_payload(
                        error="PayloadTooLarge",
                        message="Request body exceeds the maximum allowed size.",
                        request_id=request_id,
                    ),
                )
                return
        method = str(scope.get("method") or "").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        received = bytearray()
        saw_request = False
        complete = False
        trailing: dict[str, Any] | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                trailing = message
                break
            saw_request = True
            chunk = message.get("body", b"") or b""
            if len(received) + len(chunk) > self.max_body_bytes:
                await _send_json(
                    send,
                    status=413,
                    payload=_error_payload(
                        error="PayloadTooLarge",
                        message="Request body exceeds the maximum allowed size.",
                        request_id=request_id,
                    ),
                )
                return
            received.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        cached: deque[dict[str, Any]] = deque()
        if saw_request:
            cached.append(
                {
                    "type": "http.request",
                    "body": bytes(received),
                    "more_body": not complete,
                }
            )
        if trailing is not None:
            cached.append(trailing)

        async def replay() -> dict[str, Any]:
            if cached:
                return cached.popleft()
            return await receive()

        await self.app(scope, replay, send)


class HostOriginMiddleware:
    """Reject bad Host/Origin before the inner app (DNS-rebinding defense)."""

    def __init__(self, app: Any, *, bind_host: str, bind_port: int) -> None:
        self.app = app
        self.bind_host = bind_host
        self.bind_port = bind_port

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _ensure_request_id(scope)
        hosts = _header_values(scope, "host")
        if not hosts:
            await _send_json(
                send,
                status=400,
                payload=_error_payload(
                    error="InvalidHost",
                    message="Missing Host header.",
                    request_id=request_id,
                ),
            )
            return
        if len(hosts) > 1 or not self._host_ok(hosts[0]):
            await _send_json(
                send,
                status=421,
                payload=_error_payload(
                    error="InvalidHost",
                    message="Invalid Host header.",
                    request_id=request_id,
                ),
            )
            return
        origins = _header_values(scope, "origin")
        if len(origins) > 1 or (origins and not self._origin_ok(origins[0])):
            await _send_json(
                send,
                status=403,
                payload=_error_payload(
                    error="Forbidden",
                    message="Invalid Origin header.",
                    request_id=request_id,
                ),
            )
            return
        await self.app(scope, receive, send)

    def _host_ok(self, host_header: str) -> bool:
        try:
            hostname, port = _split_host_port(host_header)
        except ValueError:
            return False
        if not hostname or not _hostname_allowed(hostname, self.bind_host):
            return False
        if port is not None and port != self.bind_port:
            return False
        return True

    def _origin_ok(self, origin: str) -> bool:
        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return False
        if parsed.path not in {"", "/"}:
            return False
        if parsed.params or parsed.query or parsed.fragment:
            return False
        if parsed.username or parsed.password:
            return False
        hostname = parsed.hostname
        if not hostname or not _hostname_allowed(hostname, self.bind_host):
            return False
        port = parsed.port if parsed.port is not None else 80
        return port == self.bind_port


class AuthMiddleware:
    """Require Authorization: Bearer when a token is configured."""

    def __init__(
        self,
        app: Any,
        *,
        token: str | None,
        skip_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.token = token
        self.skip_prefixes = skip_prefixes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self.token is None:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if any(path == prefix or path.startswith(prefix + "/") for prefix in self.skip_prefixes):
            await self.app(scope, receive, send)
            return
        request_id = _ensure_request_id(scope)
        if not bearer_authorized(
            authorization=_header(scope, "authorization"),
            token=self.token,
        ):
            await _send_json(
                send,
                status=401,
                payload=_error_payload(
                    error="Unauthorized",
                    message="Authorization required.",
                    request_id=request_id,
                ),
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        await self.app(scope, receive, send)


class CacheControlMiddleware:
    """Echo X-Request-Id and set Cache-Control: no-store on HTTP responses."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _ensure_request_id(scope)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers = _apply_response_headers(headers, request_id)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _apply_response_headers(
    headers: list[tuple[bytes, bytes]], request_id: str
) -> list[tuple[bytes, bytes]]:
    out: list[tuple[bytes, bytes]] = []
    saw_cache = False
    rid = request_id.encode("utf-8", errors="replace")
    for key, value in headers:
        lowered = key.lower()
        if lowered == b"cache-control":
            text = value.decode("latin-1")
            if "no-store" not in text.lower():
                text = f"{text}, no-store" if text.strip() else "no-store"
            out.append((b"cache-control", text.encode("latin-1")))
            saw_cache = True
        elif lowered == b"x-request-id":
            continue
        else:
            out.append((key, value))
    if not saw_cache:
        out.append((b"cache-control", b"no-store"))
    out.append((b"x-request-id", rid))
    return out
