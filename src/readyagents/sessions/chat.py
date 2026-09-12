"""Loopback-by-default, token-bound chat HTTP surface. Foreground only."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from readyagents.approvals.bind import assert_loopback_host, assert_port
from readyagents.config import Settings, get_settings
from readyagents.errors import ConfigError, ConverseRequired, SessionRefused
from readyagents.sessions.redact import redact_chunks
from readyagents.sessions.service import (
    barge_in,
    close_session,
    reply_session,
    start_session,
)
from readyagents.sessions.store import SessionStore

DEFAULT_CHAT_HOST = "127.0.0.1"
DEFAULT_CHAT_PORT = 8795
MAX_BODY = 65_536
MAX_TURNS_PER_WINDOW = 30
WINDOW_SECONDS = 60.0
NOT_FOUND = b'{"ok":false,"error":"not found"}'
PUBLIC_WARN = (
    "WARNING: binding the chat surface off loopback exposes a stranger-typing "
    "endpoint. You own the exposure. Default is 127.0.0.1."
)

_ASSETS = Path(__file__).resolve().parent / "assets"


class ChatRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if now - t < WINDOW_SECONDS]
            if len(bucket) >= MAX_TURNS_PER_WINDOW:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True


def make_chat_server(
    workflow: Path | str,
    *,
    host: str = DEFAULT_CHAT_HOST,
    port: int = DEFAULT_CHAT_PORT,
    token: str | None = None,
    allow_public_bind: bool = False,
    settings: Settings | None = None,
    widget: bool = False,
) -> ThreadingHTTPServer:
    settings = settings or get_settings()
    host = _bind_host(host, allow_public=allow_public_bind)
    port = int(port)
    if port != 0:
        port = assert_port(port)
    limiter = ChatRateLimiter()
    store = SessionStore(settings=settings)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _token(self) -> str | None:
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return token

        def _auth(self) -> bool:
            expected = token
            if not expected:
                return True
            got = self._token()
            return bool(got) and got == expected

        def _reject(self) -> None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(NOT_FOUND)))
            self.end_headers()
            self.wfile.write(NOT_FOUND)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise SessionRefused("request too large", reason="size")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise SessionRefused("body must be an object", reason="body")
            return data

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if widget and path in {"/", "/widget", "/widget/"}:
                self._asset("index.html", "text/html; charset=utf-8")
                return
            if widget and path.endswith(".js"):
                self._asset("app.js", "text/javascript; charset=utf-8")
                return
            if widget and path.endswith(".css"):
                self._asset("style.css", "text/css; charset=utf-8")
                return
            if not self._auth():
                self._reject()
                return
            if path.startswith("/v1/sessions/") and path.endswith("/stream"):
                sid = path.split("/")[3]
                self._stream(sid)
                return
            if path.startswith("/v1/sessions/") and path.count("/") == 3:
                sid = path.rsplit("/", 1)[-1]
                try:
                    session = store.get(sid)
                except SessionRefused:
                    self._reject()
                    return
                if session.token_hash and (not token or True):
                    from readyagents.sessions.model import hash_token

                    if token and session.token_hash != hash_token(token, sid):
                        self._reject()
                        return
                self._json(200, {"ok": True, "session": session.as_dict()})
                return
            self._reject()

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth():
                self._reject()
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            key = self._token() or "anon"
            try:
                body = self._body()
            except SessionRefused as extra:
                if extra.reason == "size":
                    self._json(413, {"ok": False, "error": "too large"})
                    return
                self._reject()
                return
            except json.JSONDecodeError:
                self._reject()
                return
            try:
                if path == "/v1/sessions":
                    session = start_session(
                        workflow, inputs=body.get("inputs") or {}, settings=settings, token=token
                    )
                    self._json(
                        200,
                        {
                            "ok": True,
                            "session_id": session.session_id,
                            "status": session.status,
                            "say": _last_say(session),
                        },
                    )
                    return
                parts = path.split("/")
                if len(parts) >= 4 and parts[1] == "v1" and parts[2] == "sessions":
                    sid = parts[3]
                    action = parts[4] if len(parts) > 4 else ""
                    if action == "turns":
                        if not limiter.allow(key):
                            self._json(429, {"ok": False, "error": "rate"})
                            return
                        session = reply_session(
                            sid,
                            str(body.get("text") or ""),
                            token=token,
                            settings=settings,
                        )
                        self._json(
                            200,
                            {
                                "ok": True,
                                "session_id": session.session_id,
                                "status": session.status,
                                "say": _last_say(session),
                            },
                        )
                        return
                    if action == "close":
                        session = close_session(sid, token=token, settings=settings)
                        self._json(200, {"ok": True, "status": session.status})
                        return
                    if action == "barge-in":
                        session = barge_in(
                            sid, str(body.get("text") or ""), token=token, settings=settings
                        )
                        self._json(
                            200,
                            {
                                "ok": True,
                                "status": session.status,
                                "say": _last_say(session),
                            },
                        )
                        return
            except SessionRefused:
                self._reject()
                return
            except ConverseRequired as extra:
                self._json(
                    200,
                    {
                        "ok": True,
                        "status": "awaiting_user",
                        "say": extra.say,
                        "run_id": extra.run_id,
                    },
                )
                return
            self._reject()

        def _asset(self, name: str, content_type: str) -> None:
            path = _ASSETS / name
            if not path.is_file():
                self._reject()
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, session_id: str) -> None:
            try:
                session = store.get(session_id)
            except SessionRefused:
                self._reject()
                return
            say = _last_say(session) or ""
            redactor = None
            try:
                from readyagents.policy import redactor_from_settings

                redactor = redactor_from_settings(settings)
            except Exception:  # noqa: BLE001
                redactor = None
            pieces = list(say) if say else []
            chunks = redact_chunks(pieces, redactor) if pieces else []
            body = "".join(chunks).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def serve_chat(
    workflow: Path | str,
    *,
    host: str = DEFAULT_CHAT_HOST,
    port: int = DEFAULT_CHAT_PORT,
    token_env: str = "READYAGENTS_CHAT_TOKEN",
    allow_public_bind: bool = False,
    widget: bool = False,
    settings: Settings | None = None,
) -> None:
    token = (os.environ.get(token_env) or "").strip() or None
    if allow_public_bind:
        print(PUBLIC_WARN, file=sys.stderr)
    server = make_chat_server(
        workflow,
        host=host,
        port=port,
        token=token,
        allow_public_bind=allow_public_bind,
        settings=settings,
        widget=widget,
    )
    bound = server.server_address
    print(
        f"ReadyAgents chat (foreground; not a hosted product) http://{bound[0]}:{bound[1]}/",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def _bind_host(host: str, *, allow_public: bool) -> str:
    if allow_public:
        raw = str(host or "").strip()
        if not raw:
            raise ConfigError("chat bind host must not be empty")
        return raw
    return assert_loopback_host(host)


def _last_say(session: Any) -> str:
    for turn in reversed(list(getattr(session, "turns", None) or [])):
        if getattr(turn, "role", None) == "assistant" and not getattr(turn, "superseded", False):
            return str(turn.text or "")
    return ""
