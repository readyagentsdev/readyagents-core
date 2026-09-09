"""Foreground stdlib HTTP server for the localhost approval UI.

Nothing here runs on import. ``serve_approvals`` binds only after explicit call
and only after the loopback host check succeeds.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from readyagents.approvals.app import ApprovalApplication, compose_approval_app
from readyagents.approvals.bind import (
    DEFAULT_APPROVAL_HOST,
    DEFAULT_APPROVAL_PORT,
    assert_loopback_host,
    assert_port,
)
from readyagents.approvals.tokens import TokenService
from readyagents.config import Settings, get_settings
from readyagents.errors import ConfigError


def resolve_ui_secret(*, token_env: str) -> bytes | None:
    """Read the named env var. Never take the secret from argv."""
    name = (token_env or "").strip()
    if not name:
        return None
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).encode("utf-8")


def serve_approvals(
    *,
    host: str = DEFAULT_APPROVAL_HOST,
    port: int = DEFAULT_APPROVAL_PORT,
    token_env: str = "READYAGENTS_APPROVAL_UI_SECRET",
    session_ttl: float = 1800,
    action_ttl: float = 300,
    bootstrap_ttl: float = 300,
    actor: str | None = None,
    settings: Settings | None = None,
    store: Any = None,
    coordinator: Any = None,
    no_open: bool = True,
) -> None:
    """Blocking loopback server. Prints the bootstrap URL once to stderr."""
    host = assert_loopback_host(host)
    port = assert_port(port)
    settings = settings or get_settings()
    secret = resolve_ui_secret(token_env=token_env)
    tokens = TokenService(
        secret=secret,
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        action_ttl=action_ttl,
    )
    application = compose_approval_app(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host=host,
        bind_port=port,
        actor=actor,
        settings=settings,
        secret=secret,
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        action_ttl=action_ttl,
    )
    bootstrap = application.tokens.issue_bootstrap()
    url = f"http://{host}:{port}/approvals?token={bootstrap}"
    print(
        "ReadyAgents approval UI (foreground localhost; not a hosted dashboard).",
        file=sys.stderr,
    )
    print("Open this loopback URL once (token is single-use and not logged):", file=sys.stderr)
    print(url, file=sys.stderr)
    httpd = ThreadingHTTPServer((host, port), _make_handler(application))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        close = getattr(application.store, "close", None)
        if callable(close):
            close()
        shutdown = getattr(application.coordinator, "shutdown", None)
        if callable(shutdown):
            shutdown()
    if not no_open:
        # Browser launch is opt-in and must never keep the server from starting.
        return


def _make_handler(application: ApprovalApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            parsed = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                length = 0
            body = self.rfile.read(length) if length else b""
            header_items = [(k, v) for k, v in self.headers.items()]
            response = application.handle(
                self.command,
                parsed.path,
                query=query,
                headers=header_items,
                body=body,
            )
            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            # Never log query strings (bootstrap token) or cookie/token material.
            path = urlparse(self.path).path
            sys.stderr.write(f"{self.address_string()} - {path}\n")

    return Handler


def bind_rejected_before_listen(host: str) -> None:
    """Used by tests: raise without opening a socket."""
    assert_loopback_host(host)
    raise ConfigError("bind check unexpectedly passed")
