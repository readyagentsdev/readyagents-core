"""Foreground stdlib HTTP server for the localhost studio.

Nothing here runs on import. ``serve_studio`` binds only after explicit call
and only after the loopback host check succeeds.
"""

from __future__ import annotations

import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from readyagents.approvals.bind import assert_loopback_host, assert_port
from readyagents.approvals.tokens import TokenService
from readyagents.config import Settings, get_settings
from readyagents.studio.app import DEFAULT_STUDIO_PORT, StudioApplication, compose_studio_app


def serve_studio(
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_STUDIO_PORT,
    read_only: bool = False,
    open_browser: bool = False,
    actor: str | None = None,
    settings: Settings | None = None,
    store: Any = None,
    coordinator: Any = None,
    bootstrap_ttl: float = 300,
    session_ttl: float = 1800,
    action_ttl: float = 300,
) -> None:
    """Blocking loopback server. Prints the bootstrap token once to stderr."""
    host = assert_loopback_host(host)
    port = assert_port(port)
    settings = settings or get_settings()
    tokens = TokenService(
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        action_ttl=action_ttl,
    )
    application = compose_studio_app(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host=host,
        bind_port=port,
        actor=actor,
        settings=settings,
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        action_ttl=action_ttl,
        read_only=read_only,
    )
    bootstrap = application.tokens.issue_bootstrap()
    url = f"http://{host}:{port}/studio"
    print(
        "ReadyAgents studio (foreground localhost; not a hosted product).",
        file=sys.stderr,
    )
    if read_only:
        print("Read-only: write paths are disabled server-side.", file=sys.stderr)
    print(f"Open {url}", file=sys.stderr)
    print("Bootstrap token (single-use, paste once; not in the URL):", file=sys.stderr)
    print(bootstrap, file=sys.stderr)
    httpd = ThreadingHTTPServer((host, port), _make_handler(application))
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
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


def _make_handler(application: StudioApplication) -> type[BaseHTTPRequestHandler]:
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
            path = urlparse(self.path).path
            sys.stderr.write(f"{self.address_string()} - {path}\n")

    return Handler
