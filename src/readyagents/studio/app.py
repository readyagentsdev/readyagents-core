"""Localhost studio application. No listener starts on import."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from readyagents.approvals.bind import assert_loopback_host, assert_port
from readyagents.approvals.tokens import TokenError, TokenService
from readyagents.config import LOOPBACK_HOSTS, MAX_HTTP_BODY_BYTES, Settings, get_settings
from readyagents.errors import (
    AuthorizationError,
    ConfigError,
    ReadyAgentsError,
)
from readyagents.logging import get_logger
from readyagents.policy import Redactor, redactor_from_settings
from readyagents.run_store import RunStore, open_run_store
from readyagents.studio.edit import (
    ExternalChange,
    ReadOnlyError,
    apply_field_edit,
    assert_workflow_path,
    confined_out_dir,
    file_fingerprint,
    save_field_edit,
    validate_source,
)
from readyagents.studio.graph import node_form_fields, workflow_graph
from readyagents.studio.inspect import (
    compare_runs,
    list_runs,
    node_inspector,
    run_timeline,
)
from readyagents.workflow.runner import load_workflow

log = get_logger("studio")

SESSION_COOKIE = "ra_studio_session"
COOKIE_PATH = "/studio"
DEFAULT_STUDIO_PORT = 8790
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
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
_RATE_WINDOW = 10.0
_RATE_MAX = 60


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
        name = key.decode("latin-1") if isinstance(key, bytes) else str(key)
        text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
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
    root = resources.files("readyagents.studio")
    return (root / "assets" / name).read_bytes()


def _index_html() -> bytes:
    root = resources.files("readyagents.studio")
    return (root / "assets" / "index.html").read_bytes()


def _dumps(payload: dict[str, Any]) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False, default=str)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return blob.encode("utf-8")


def _json_body(payload: dict[str, Any], status: int) -> HttpResponse:
    blob = _dumps(payload)
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


class StudioApplication:
    """Request handler for ``/studio``. No sockets; no threads."""

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
        read_only: bool = False,
    ) -> None:
        self.store = store
        self.tokens = tokens
        self.coordinator = coordinator
        self.bind_host = assert_loopback_host(bind_host)
        self.bind_port = assert_port(bind_port)
        self.actor = actor
        self.settings = settings or get_settings()
        self.redactor = redactor or redactor_from_settings(
            enabled=True,
            patterns=self.settings.redact_pattern_list(),
            literals=self.settings.redact_literal_list(),
        )
        self.max_body_bytes = int(max_body_bytes)
        self.session_ttl = int(max(1, session_ttl))
        self.read_only = bool(read_only)
        self._hits: list[float] = []

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
            if method in {"POST", "PUT", "PATCH", "DELETE"} and not self._rate_ok():
                return _json_body(
                    {"ok": False, "error": "RateLimited", "message": "Too many writes."},
                    429,
                )
            return self._route(method, path_only, query=query, headers=hdrs, body=body)
        except ReadOnlyError as exc:
            return _json_body(
                {"ok": False, "error": "ReadOnly", "message": str(exc)},
                403,
            )
        except ExternalChange as exc:
            return _json_body(
                {"ok": False, "error": "ExternalChange", "message": str(exc)},
                409,
            )
        except Exception:  # noqa: BLE001
            log.exception("studio handler error")
            return _json_body(
                {"ok": False, "error": "InternalError", "message": "internal error"},
                500,
            )

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        self._hits = [t for t in self._hits if now - t < _RATE_WINDOW]
        if len(self._hits) >= _RATE_MAX:
            return False
        self._hits.append(now)
        return True

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
        if path == "/studio":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._page(query=query, headers=headers)
        if path.startswith("/studio/assets/"):
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._asset(path)
        if path == "/studio/api/me":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._me(headers=headers)
        if path == "/studio/api/schema":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._schema(headers=headers)
        if path == "/studio/api/workflow":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._workflow(headers=headers, query=query)
        if path == "/studio/api/workflow/validate":
            if method != "POST":
                return self._method_not_allowed(("POST",))
            return self._validate(headers=headers, body=body)
        if path == "/studio/api/workflow/save":
            if method != "POST":
                return self._method_not_allowed(("POST",))
            self._refuse_write()
            return self._save(headers=headers, body=body)
        if path == "/studio/api/runs":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._runs(headers=headers, query=query)
        if path == "/studio/api/runs/diff":
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._diff(headers=headers, query=query)
        fork = _match_suffix(path, "/studio/api/runs/", "/fork")
        if fork is not None:
            if method != "POST":
                return self._method_not_allowed(("POST",))
            self._refuse_write()
            return self._fork(fork, headers=headers, body=body)
        freeze = _match_suffix(path, "/studio/api/runs/", "/freeze")
        if freeze is not None:
            if method != "POST":
                return self._method_not_allowed(("POST",))
            self._refuse_write()
            return self._freeze(freeze, headers=headers, body=body)
        decide = _match_suffix(path, "/studio/api/runs/", "/decide")
        if decide is not None:
            if method != "POST":
                return self._method_not_allowed(("POST",))
            self._refuse_write()
            return self._decide(decide, headers=headers, body=body)
        node = _match_node(path)
        if node is not None:
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._node(node[0], node[1], headers=headers)
        run = _match_run(path)
        if run is not None:
            if method != "GET":
                return self._method_not_allowed(("GET",))
            return self._run(run, headers=headers)
        return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)

    def _refuse_write(self) -> None:
        if self.read_only:
            raise ReadOnlyError("Studio is --read-only; write paths are disabled.")

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
            return _html_body(
                b"",
                303,
                extra=[("Set-Cookie", cookie), ("Location", "/studio")],
            )
        if not session or not self.tokens.verify_session(session):
            return _html_body(_index_html(), 200)
        return _html_body(_index_html(), 200)

    def _asset(self, path: str) -> HttpResponse:
        if ".." in path or "\\" in path or "%" in path:
            return _json_body({"ok": False, "error": "NotFound", "message": "not found"}, 404)
        prefix = "/studio/assets/"
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

    def _auth(self, headers: dict[str, str]) -> HttpResponse | None:
        if self._require_session(headers) is None:
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_401},
                401,
            )
        return None

    def _me(self, *, headers: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        return _json_body(
            {
                "ok": True,
                "read_only": self.read_only,
                "actor": self.actor,
                "workspace": str(self.settings.workspace_path()),
            },
            200,
        )

    def _schema(self, *, headers: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        from readyagents.workflow.jsonschema import workflow_json_schema

        return _json_body({"ok": True, "schema": workflow_json_schema()}, 200)

    def _workflow(self, *, headers: dict[str, str], query: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        raw = (query.get("path") or "").strip()
        if not raw:
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "path is required"},
                400,
            )
        try:
            path = assert_workflow_path(Path(raw), self.settings.workspace_path())
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        if not path.is_file():
            return _json_body(
                {"ok": False, "error": "NotFound", "message": f"Workflow file not found: {raw}"},
                404,
            )
        text = path.read_text(encoding="utf-8")
        try:
            spec = load_workflow(path, source=text, display_path=str(path))
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        graph = workflow_graph(spec, source_path=str(path), source=text)
        forms = [node_form_fields(node) for node in spec.nodes]
        return _json_body(
            {
                "ok": True,
                "path": str(path),
                "text": text,
                "graph": graph,
                "forms": forms,
                "fingerprint": file_fingerprint(path),
            },
            200,
        )

    def _validate(self, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        parsed = _read_json(body)
        if isinstance(parsed, HttpResponse):
            return parsed
        raw_path = str(parsed.get("path") or "").strip()
        if not raw_path:
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "path is required"},
                400,
            )
        try:
            path = assert_workflow_path(Path(raw_path), self.settings.workspace_path())
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        source = parsed.get("source")
        if not isinstance(source, str) or not source:
            if path.is_file():
                source = path.read_text(encoding="utf-8")
            else:
                return _json_body(
                    {"ok": False, "error": "NotFound", "message": "workflow not found"},
                    404,
                )
        node_id = parsed.get("node_id")
        field = parsed.get("field")
        if isinstance(node_id, str) and isinstance(field, str) and "value" in parsed:
            try:
                source = apply_field_edit(source, node_id, field, parsed.get("value"))
            except ReadyAgentsError as exc:
                return _json_body(
                    {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                    400,
                )
        result = validate_source(path, source)
        status = 200 if result["ok"] else 400
        result["ok"] = bool(result["ok"])
        return _json_body(result, status)

    def _save(self, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        parsed = _read_json(body)
        if isinstance(parsed, HttpResponse):
            return parsed
        raw_path = str(parsed.get("path") or "").strip()
        node_id = parsed.get("node_id")
        field = parsed.get("field")
        if not raw_path or not isinstance(node_id, str) or not isinstance(field, str):
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "path, node_id, and field are required",
                },
                400,
            )
        expected = parsed.get("fingerprint")
        if expected is not None and not isinstance(expected, dict):
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "fingerprint must be an object",
                },
                400,
            )
        try:
            saved = save_field_edit(
                Path(raw_path),
                node_id=node_id,
                field=field,
                value=parsed.get("value"),
                expected=expected,
                workspace=self.settings.workspace_path(),
            )
        except ReadyAgentsError as exc:
            status = 409 if isinstance(exc, ExternalChange) else 400
            if isinstance(exc, ReadOnlyError):
                status = 403
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                status,
            )
        return _json_body({"ok": True, **saved}, 200)

    def _runs(self, *, headers: dict[str, str], query: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        status = (query.get("status") or "").strip() or None
        rows = list_runs(self.store, status=status)
        return _json_body({"ok": True, "runs": rows}, 200)

    def _run(self, run_id: str, *, headers: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        loaded = self._load_stored(run_id)
        if isinstance(loaded, HttpResponse):
            return loaded
        stored, state = loaded
        payload = run_timeline(state, redactor=self.redactor)
        if state.status == "paused" and state.pending_node:
            payload["revision"] = stored.revision
            payload["actions"] = {
                "approve_token": self.tokens.issue_action(
                    run_id=state.run_id,
                    node_id=str(state.pending_node),
                    revision=int(stored.revision),
                    decision="approve",
                ),
                "reject_token": self.tokens.issue_action(
                    run_id=state.run_id,
                    node_id=str(state.pending_node),
                    revision=int(stored.revision),
                    decision="reject",
                ),
            }
        return _json_body({"ok": True, **payload}, 200)

    def _node(self, run_id: str, node_id: str, *, headers: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        state = self._load_run(run_id)
        if isinstance(state, HttpResponse):
            return state
        payload = node_inspector(state, node_id, redactor=self.redactor)
        return _json_body({"ok": True, **payload}, 200)

    def _diff(self, *, headers: dict[str, str], query: dict[str, str]) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        left_id = (query.get("a") or "").strip()
        right_id = (query.get("b") or "").strip()
        if not left_id or not right_id:
            return _json_body(
                {
                    "ok": False,
                    "error": "HttpRequestError",
                    "message": "a and b run ids are required",
                },
                400,
            )
        left = self._load_run(left_id)
        if isinstance(left, HttpResponse):
            return left
        right = self._load_run(right_id)
        if isinstance(right, HttpResponse):
            return right
        payload = compare_runs(left, right, redactor=self.redactor)
        return _json_body({"ok": True, **payload}, 200)

    def _fork(self, run_id: str, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        parsed = _read_json(body)
        if isinstance(parsed, HttpResponse):
            return parsed
        from_node = parsed.get("from_node") or parsed.get("from")
        if not isinstance(from_node, str) or not from_node.strip():
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "from_node is required"},
                400,
            )
        from readyagents.replay.fork import fork_run

        try:
            child = fork_run(
                run_id,
                from_node.strip(),
                settings=self.settings,
                persist=True,
                actor=self.actor,
            )
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        return _json_body(
            {"ok": True, "run_id": child.run_id, "status": child.status, "forked_from": run_id},
            200,
        )

    def _freeze(self, run_id: str, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        parsed = _read_json(body)
        if isinstance(parsed, HttpResponse):
            return parsed
        out = parsed.get("out") or parsed.get("out_dir")
        if not isinstance(out, str) or not out.strip():
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "out is required"},
                400,
            )
        state = self._load_run(run_id)
        if isinstance(state, HttpResponse):
            return state
        cassette_path = (state.metadata or {}).get("cassette")
        if not cassette_path:
            return _json_body(
                {"ok": False, "error": "CassetteError", "message": "run has no cassette"},
                400,
            )
        from readyagents.replay.cassette import Cassette
        from readyagents.replay.freeze import freeze_run

        try:
            dest = confined_out_dir(out.strip(), self.settings.workspace_path())
            tape = Cassette.load(Path(str(cassette_path)))
            frozen = freeze_run(
                state,
                tape,
                out_dir=dest,
                workspace=self.settings.workspace_path(),
                redactor=self.redactor,
                allow_unsealed=bool(parsed.get("allow_unsealed")),
            )
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        return _json_body({"ok": True, "path": str(frozen)}, 200)

    def _decide(self, run_id: str, *, headers: dict[str, str], body: bytes) -> HttpResponse:
        denied = self._auth(headers)
        if denied is not None:
            return denied
        parsed = _read_json(body)
        if isinstance(parsed, HttpResponse):
            return parsed
        node_id = parsed.get("node_id")
        decision = parsed.get("decision")
        revision = parsed.get("revision")
        action_token = parsed.get("action_token")
        if not isinstance(node_id, str) or not node_id.strip():
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": "node_id is required"},
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
        if not isinstance(action_token, str) or not action_token.strip():
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_TOKEN},
                401,
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
        try:
            self.tokens.consume_action(
                action_token,
                run_id=run_id,
                node_id=node_id.strip(),
                revision=int(revision),
                decision=str(decision),
            )
        except TokenError:
            return _json_body(
                {"ok": False, "error": "Unauthorized", "message": _GENERIC_TOKEN},
                401,
            )
        actor = self.actor if self.actor is not None else self.settings.actor
        try:
            result = self.coordinator.decide(
                run_id,
                {"node_id": node_id.strip(), "decision": decision, "actor": actor},
            )
        except AuthorizationError as exc:
            return _json_body(
                {"ok": False, "error": "AuthorizationError", "message": str(exc)},
                403,
            )
        except ReadyAgentsError as exc:
            return _json_body(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                400,
            )
        body_out = dict(result)
        body_out.setdefault("ok", True)
        body_out.setdefault("run_id", run_id)
        return _json_body(body_out, 200)

    def _load_stored(self, run_id: str) -> Any:
        if not _RUN_ID_RE.fullmatch(run_id) and len(run_id) < 8:
            return _json_body(
                {"ok": False, "error": "HttpRequestError", "message": f"Invalid run id: {run_id}"},
                400,
            )
        try:
            stored = self.store.get(run_id, allow_prefix=True)
        except ConfigError:
            return _json_body(
                {"ok": False, "error": "NotFound", "message": f"Run not found: {run_id}"},
                404,
            )
        return stored, stored.state

    def _load_run(self, run_id: str) -> Any:
        loaded = self._load_stored(run_id)
        if isinstance(loaded, HttpResponse):
            return loaded
        return loaded[1]

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


def _read_json(body: bytes) -> dict[str, Any] | HttpResponse:
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
    return parsed


def _match_suffix(path: str, prefix: str, suffix: str) -> str | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    mid = path[len(prefix) : -len(suffix)]
    if not mid or "/" in mid:
        return None
    return mid


def _match_run(path: str) -> str | None:
    prefix = "/studio/api/runs/"
    if not path.startswith(prefix):
        return None
    mid = path[len(prefix) :]
    if not mid or "/" in mid:
        return None
    return mid


def _match_node(path: str) -> tuple[str, str] | None:
    prefix = "/studio/api/runs/"
    marker = "/nodes/"
    if not path.startswith(prefix) or marker not in path:
        return None
    rest = path[len(prefix) :]
    run_id, _, node_id = rest.partition(marker)
    if not run_id or not node_id or "/" in node_id:
        return None
    return run_id, node_id


def _unauthorized_html() -> bytes:
    return (
        b'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        b"<title>ReadyAgents studio</title></head><body>"
        b"<p>Authentication required.</p></body></html>"
    )


def compose_studio_app(
    *,
    store: RunStore | None = None,
    tokens: TokenService | None = None,
    coordinator: Any = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = DEFAULT_STUDIO_PORT,
    actor: str | None = None,
    settings: Settings | None = None,
    secret: bytes | None = None,
    bootstrap_ttl: float = 300,
    session_ttl: float = 1800,
    action_ttl: float = 300,
    read_only: bool = False,
) -> StudioApplication:
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
    redactor = Redactor(
        patterns=settings.redact_pattern_list(),
        literals=settings.redact_literal_list(),
    )
    display = redactor_from_settings(
        enabled=True,
        patterns=settings.redact_pattern_list(),
        literals=settings.redact_literal_list(),
    )
    return StudioApplication(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host=bind_host,
        bind_port=bind_port,
        actor=actor,
        settings=settings,
        redactor=display or redactor,
        session_ttl=session_ttl,
        read_only=read_only,
    )
