"""Localhost approval UI application: list redacted pauses and decide.

No listener starts on import. Bind and serve live in ``server.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from readyagents.approvals.bind import assert_loopback_host, assert_port
from readyagents.approvals.tokens import TokenError, TokenService
from readyagents.approvals.view import approval_view, is_approval_pause
from readyagents.config import LOOPBACK_HOSTS, MAX_HTTP_BODY_BYTES, Settings, get_settings
from readyagents.errors import (
    AuthorizationError,
    ConfigError,
    ReadyAgentsError,
)
from readyagents.logging import get_logger
from readyagents.policy import Redactor, redactor_from_settings
from readyagents.run_store import RunQuery, RunStore, RunStoreConflict, open_run_store

log = get_logger("approvals")

SESSION_COOKIE = "ra_approval_session"
COOKIE_PATH = "/approvals"
PAGE_LIMIT = 100
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
_DECIDE_FIELDS = frozenset({"node_id", "decision", "revision", "action_token"})
_ASSET_TYPES = {
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_GENERIC_401 = "Authentication required."
_GENERIC_TOKEN = "invalid token"


@dataclass
class HttpResponse:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


def _security_headers() -> list[tuple[str, str]]:
    return [
        ("Content-Security-Policy", _CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
        ("X-Frame-Options", "DENY"),
    ]


def _header_map(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(headers, dict):
        items = headers.items()
    else:
        items = headers or ()
    for key, value in items:
        if isinstance(key, bytes):
            name = key.decode("latin-1")
        else:
            name = str(key)
        if isinstance(value, bytes):
            text = value.decode("latin-1")
        else:
            text = str(value)
        result[name.lower()] = text
    return result


def _cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    parsed = SimpleCookie()
    try:
        parsed.load(header)
    except Exception:  # noqa: BLE001
        return {}
    return {key: morsel.value for key, morsel in parsed.items()}


def _canonical_host(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]") and len(value) > 2:
        value = value[1:-1]
    if value == "0:0:0:0:0:0:0:1":
        return "::1"
    try:
        import ipaddress

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


def _asset_bytes(name: str) -> bytes:
    if name not in _ASSET_TYPES:
        raise FileNotFoundError(name)
    root = resources.files("readyagents.approvals")
    return (root / "assets" / name).read_bytes()


def _index_html() -> bytes:
    root = resources.files("readyagents.approvals")
    return (root / "assets" / "index.html").read_bytes()


def _json_body(payload: dict[str, Any], status: int) -> HttpResponse:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _security_headers()
    headers.append(("Content-Type", "application/json; charset=utf-8"))
    headers.append(("Content-Length", str(len(blob))))
    return HttpResponse(status, headers, blob)


def _html_body(
    blob: bytes, status: int, extra: list[tuple[str, str]] | None = None
) -> HttpResponse:
    headers = _security_headers()
    headers.append(("Content-Type", "text/html; charset=utf-8"))
    headers.append(("Content-Length", str(len(blob))))
    if extra:
        headers.extend(extra)
    return HttpResponse(status, headers, blob)


def _static_body(blob: bytes, content_type: str) -> HttpResponse:
    headers = _security_headers()
    headers.append(("Content-Type", content_type))
    headers.append(("Content-Length", str(len(blob))))
    return HttpResponse(200, headers, blob)


class ApprovalApplication:
    """Request handler for ``/approvals``. No sockets; no threads."""

    def __init__(
        self,
        *,
        store: RunStore,
        tokens: TokenService,
        coordinator: Any,
        bind_host: str,
        bind_port: int,
        actor: str | None = None,
        settings: Settings | None = None,
        redactor: Any | None = None,
        max_body_bytes: int = MAX_HTTP_BODY_BYTES,
        session_ttl: float = 1800,
    ) -> None:
        self.store = store
        self.tokens = tokens
        self.coordinator = coordinator
        self.bind_host = assert_loopback_host(bind_host)
        self.bind_port = assert_port(bind_port)
        self.actor = actor
        self.settings = settings or get_settings()
        if redactor is not None:
            self.redactor = redactor
        else:
            self.redactor = Redactor(
                patterns=self.settings.redact_pattern_list(),
                literals=self.settings.redact_literal_list(),
            )
        self.max_body_bytes = int(max_body_bytes)
        self.session_ttl = int(max(1, session_ttl))

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        headers: Any = None,
        body: bytes = b"",
    ) -> HttpResponse:
        method = (method or "GET").upper()
        raw_path = path or "/"
        if "?" in raw_path and query is None:
            raw_path, qs = raw_path.split("?", 1)
            query = {k: v[-1] for k, v in parse_qs(qs, keep_blank_values=True).items()}
        path_only = raw_path.split("?", 1)[0]
        hdrs = _header_map(headers)
        query = query or {}
        try:
            host_err = self._check_host(hdrs)
            if host_err is not None:
                return host_err
            origin_err = self._check_origin(method, hdrs)
            if origin_err is not None:
                return origin_err
            if len(body) > self.max_body_bytes:
                return _json_body(
                    {"ok": False, "error": "PayloadTooLarge", "message": "Request body too large."},
                    413,
                )
            return self._route(method, path_only, query=query, headers=hdrs, body=body)
        except Exception:  # noqa: BLE001
            log.exception("approval UI handler error")
            return _json_body(
                {"ok": False, "error": "InternalError", "message": "internal error"},
                500,
            )

    def _route(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> HttpResponse:
        path = path.rstrip("/") or "/"
        if path == "/approvals":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._page(query=query, headers=headers)
        if path.startswith("/approvals/assets/"):
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._asset(path)
        if path == "/approvals/api/runs":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._list_runs(headers=headers, query=query)
        decide = _match_decide(path)
        if decide is not None:
            if method != "POST":
                return self._method_not_allowed(("POST",))
            return self._decide(decide, headers=headers, body=body)
        return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)

    def _page(self, *, query: dict[str, str], headers: dict[str, str]) -> HttpResponse:
        token = (query.get("token") or "").strip()
        cookies = _cookies(headers.get("cookie"))
        session = cookies.get(SESSION_COOKIE, "")
        if token:
            try:
                session_token = self.tokens.consume_bootstrap(token)
            except TokenError:
                return _html_body(_unauthorized_html(), 401)
            cookie = (
                f"{SESSION_COOKIE}={session_token}; HttpOnly; SameSite=Strict; "
                f"Path={COOKIE_PATH}; Max-Age={self.session_ttl}"
            )
            location = "/approvals"
            headers_extra = [
                ("Set-Cookie", cookie),
                ("Location", location),
            ]
            return _html_body(b"", 303, extra=headers_extra)
        if not session or not self.tokens.verify_session(session):
            return _html_body(_unauthorized_html(), 401)
        return _html_body(_index_html(), 200)

    def _asset(self, path: str) -> HttpResponse:
        if ".." in path or "\\" in path or "%" in path:
            return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)
        prefix = "/approvals/assets/"
        if not path.startswith(prefix):
            return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)
        name = path[len(prefix) :]
        if name not in _ASSET_TYPES or "/" in name:
            return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)
        try:
            blob = _asset_bytes(name)
        except (FileNotFoundError, OSError):
            return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)
        return _static_body(blob, _ASSET_TYPES[name])

    def _require_session(self, headers: dict[str, str]) -> str | None:
        cookies = _cookies(headers.get("cookie"))
        session = cookies.get(SESSION_COOKIE, "")
        if not session or not self.tokens.verify_session(session):
            return None
        return session

    def _list_runs(self, *, headers: dict[str, str], query: dict[str, str]) -> HttpResponse:
        if self._require_session(headers) is None:
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_401},
                401,
            )
        cursor = (query.get("cursor") or "").strip() or None
        stored = self.store.list(RunQuery(status="paused", limit=PAGE_LIMIT, cursor=cursor))
        runs: list[dict[str, Any]] = []
        next_cursor = None
        for item in stored:
            if not is_approval_pause(item.state):
                continue
            view = approval_view(
                item.state,
                revision=item.revision,
                redactor=self.redactor,
                actor=self.actor,
            )
            approve = self.tokens.issue_action(
                run_id=view["run_id"],
                node_id=str(view["node_id"]),
                revision=int(view["revision"]),
                decision="approve",
            )
            reject = self.tokens.issue_action(
                run_id=view["run_id"],
                node_id=str(view["node_id"]),
                revision=int(view["revision"]),
                decision="reject",
            )
            view["actions"] = {"approve_token": approve, "reject_token": reject}
            runs.append(view)
            next_cursor = item.cursor
        payload: dict[str, Any] = {"ok": True, "runs": runs}
        if len(stored) >= PAGE_LIMIT and next_cursor:
            payload["cursor"] = next_cursor
        return _json_body(payload, 200)

    def _decide(self, run_id: str, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        if self._require_session(headers) is None:
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_401},
                401,
            )
        ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return _json_body(
                {
                    "ok": False,
                    "error": "UnsupportedMediaType",
                    "message": "Content-Type must be application/json",
                },
                415,
            )
        if not _RUN_ID_RE.fullmatch(run_id):
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": f"Invalid run id: {run_id}"},
                400,
            )
        try:
            parsed = json.loads(body.decode("utf-8") if body else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "Malformed JSON."},
                400,
            )
        if not isinstance(parsed, dict):
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "JSON object required."},
                400,
            )
        unknown = set(parsed) - _DECIDE_FIELDS
        if unknown:
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "Unknown fields: " + ", ".join(sorted(unknown)),
                },
                400,
            )
        node_id = parsed.get("node_id")
        decision = parsed.get("decision")
        revision = parsed.get("revision")
        action_token = parsed.get("action_token")
        if not isinstance(node_id, str) or not node_id.strip():
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "node_id is required and must be a string",
                },
                400,
            )
        if decision not in {"approve", "reject"}:
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": 'decision must be exactly "approve" or "reject"',
                },
                400,
            )
        if not isinstance(revision, int) or isinstance(revision, bool):
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "revision must be an integer",
                },
                400,
            )
        if not isinstance(action_token, str) or not action_token.strip():
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_TOKEN},
                401,
            )
        node_id = node_id.strip()
        try:
            self.tokens.consume_action(
                action_token,
                run_id=run_id,
                node_id=node_id,
                revision=int(revision),
                decision=str(decision),
            )
        except TokenError:
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_TOKEN},
                401,
            )
        try:
            stored = self.store.get(run_id, allow_prefix=False)
        except ConfigError:
            return _json_body(
                {"ok": False, "error": "NotFound", "message": f"Run not found: {run_id}"},
                404,
            )
        except RunStoreConflict:
            return _json_body(
                {"ok": False, "error": "RunConflict", "message": "stale approval state"},
                409,
            )
        if stored.revision != int(revision):
            return _json_body(
                {"ok": False, "error": "RunConflict", "message": "stale approval state"},
                409,
            )
        if (
            stored.state.status != "paused"
            or stored.state.pending_node != node_id
            or not is_approval_pause(stored.state)
        ):
            return _json_body(
                {"ok": False, "error": "RunConflict", "message": "stale approval state"},
                409,
            )
        actor = self.actor if self.actor is not None else self.settings.actor
        try:
            result = self.coordinator.decide(
                run_id,
                {"node_id": node_id, "decision": decision, "actor": actor},
            )
        except AuthorizationError as exc:
            return _json_body(
                {
                    "ok": False,
                    "error": "AuthorizationError",
                    "message": str(exc),
                    "run_id": run_id,
                },
                403,
            )
        except ReadyAgentsError as exc:
            status = _status_for_decide(exc)
            return _json_body(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "run_id": run_id,
                },
                status,
            )
        status_name = str(result.get("status") or "")
        http_status = 200 if status_name in {"succeeded", "failed", "cancelled"} else 202
        body_out = dict(result)
        body_out.setdefault("ok", True)
        body_out.setdefault("run_id", run_id)
        return _json_body(body_out, http_status)

    def _check_host(self, headers: dict[str, str]) -> HttpResponse | None:
        host = headers.get("host")
        if not host:
            return _json_body(
                {"ok": False, "error": "InvalidHost", "message": "Missing Host header."},
                400,
            )
        try:
            hostname, port = _split_host_port(host)
        except ValueError:
            return _json_body(
                {"ok": False, "error": "InvalidHost", "message": "Invalid Host header."},
                421,
            )
        if not hostname or not _hostname_allowed(hostname, self.bind_host):
            return _json_body(
                {"ok": False, "error": "InvalidHost", "message": "Invalid Host header."},
                421,
            )
        if port is not None and port != self.bind_port:
            return _json_body(
                {"ok": False, "error": "InvalidHost", "message": "Invalid Host header."},
                421,
            )
        return None

    def _check_origin(self, method: str, headers: dict[str, str]) -> HttpResponse | None:
        origin = headers.get("origin")
        if method in {"GET", "HEAD", "OPTIONS"} and not origin:
            return None
        if method in {"POST", "PUT", "PATCH", "DELETE"} and not origin:
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        if not origin:
            return None
        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        if parsed.username or parsed.password:
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        hostname = parsed.hostname
        if not hostname or not _hostname_allowed(hostname, self.bind_host):
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        port = parsed.port if parsed.port is not None else 80
        if port != self.bind_port:
            return _json_body(
                {"ok": False, "error": "Forbidden", "message": "Invalid Origin header."},
                403,
            )
        return None

    def _method_not_allowed(self, allowed: tuple[str, ...]) -> HttpResponse:
        resp = _json_body(
            {"ok": False, "error": "MethodNotAllowed", "message": "method not allowed"},
            405,
        )
        resp.headers.append(("Allow", ", ".join(allowed)))
        return resp


def _match_decide(path: str) -> str | None:
    prefix = "/approvals/api/runs/"
    suffix = "/decide"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    mid = path[len(prefix) : -len(suffix)]
    if not mid or "/" in mid:
        return None
    return mid


def _unauthorized_html() -> bytes:
    return (
        b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        b"<title>ReadyAgents approvals</title></head><body>"
        b"<p>Authentication required.</p></body></html>"
    )


def _status_for_decide(exc: BaseException) -> int:
    from readyagents.errors import HttpRequestError, RunConflict

    if isinstance(exc, AuthorizationError):
        return 403
    if isinstance(exc, RunConflict):
        return 409
    if isinstance(exc, ConfigError) and "not found" in str(exc).lower():
        return 404
    if isinstance(exc, HttpRequestError):
        return getattr(exc, "status_code", 400) or 400
    name = type(exc).__name__
    if name in {"QueueOverflow"}:
        return 429
    return 400


def compose_approval_app(
    *,
    store: RunStore | None = None,
    tokens: TokenService | None = None,
    coordinator: Any = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8766,
    actor: str | None = None,
    settings: Settings | None = None,
    secret: bytes | None = None,
    bootstrap_ttl: float = 300,
    session_ttl: float = 1800,
    action_ttl: float = 300,
) -> ApprovalApplication:
    """Build the approval UI application. Does not bind a socket."""
    bind_host = assert_loopback_host(bind_host)
    bind_port = assert_port(bind_port)
    settings = settings or get_settings()
    store = store if store is not None else open_run_store(settings)
    tokens = tokens or TokenService(
        secret=secret,
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        action_ttl=action_ttl,
    )
    if coordinator is None:
        from readyagents.mcp.run_api import RunCoordinator

        coordinator = RunCoordinator(settings=settings, workspace=settings.workspace_path())
        if hasattr(coordinator, "attach_store"):
            coordinator.attach_store(store)
        elif hasattr(coordinator, "_store"):
            coordinator._store = store
    redactor = Redactor(
        patterns=settings.redact_pattern_list(),
        literals=settings.redact_literal_list(),
    )
    display = redactor_from_settings(
        enabled=True,
        patterns=settings.redact_pattern_list(),
        literals=settings.redact_literal_list(),
    )
    return ApprovalApplication(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host=bind_host,
        bind_port=bind_port,
        actor=actor,
        settings=settings,
        redactor=display or redactor,
        session_ttl=session_ttl,
    )


def asgi_app(application: ApprovalApplication) -> Any:
    """Wrap ``ApprovalApplication.handle`` as ASGI for TASK-01 mount."""

    async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        method = str(scope.get("method") or "GET")
        path = str(scope.get("path") or "/")
        raw_qs = scope.get("query_string") or b""
        if isinstance(raw_qs, bytes):
            qs = raw_qs.decode("latin-1")
        else:
            qs = str(raw_qs)
        query = {k: v[-1] for k, v in parse_qs(qs, keep_blank_values=True).items()}
        header_list = list(scope.get("headers") or [])
        chunks = bytearray()
        more = True
        while more:
            event = await receive()
            if event.get("type") != "http.request":
                break
            chunks.extend(event.get("body") or b"")
            more = bool(event.get("more_body"))
        response = application.handle(
            method,
            path,
            query=query,
            headers=header_list,
            body=bytes(chunks),
        )
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [
                    (k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers
                ],
            }
        )
        await send({"type": "http.response.body", "body": response.body})

    return _app
