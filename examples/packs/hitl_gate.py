"""ReadyAgents Gate (example pack): signed inbound HTTP → decide / resume.

Core has no always-on listener. This module is opt-in: verify + apply are
pure functions; ``serve_once`` starts a one-request stdlib server for checks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.decisions.signing import (
    parse_signature_header,
    sign_body,
)
from readyagents.decisions.signing import (
    verify_signed_body as _verify_hmac,
)
from readyagents.errors import ReadyAgentsError
from readyagents.packs import BasePack
from readyagents.workflow.runner import resume_run
from readyagents.workflow.state import RunState, parse_decision_payload

SIGNATURE_HEADER = "X-ReadyAgents-Signature"

__all__ = [
    "SIGNATURE_HEADER",
    "SignatureError",
    "sign_body",
    "parse_signature_header",
    "verify_signed_body",
    "apply_signed_decision",
    "handle_http_post",
    "serve_once",
    "HitlGatePack",
    "get_pack",
]


class SignatureError(ValueError):
    """Missing, empty, or forged HMAC on a Gate decision payload."""


def verify_signed_body(secret: str, body: bytes, signature: str | None) -> dict[str, Any]:
    """Return the JSON object if HMAC-SHA256 matches. Never resumes a run."""
    try:
        _verify_hmac(secret, body, signature)
    except ValueError as exc:
        raise SignatureError(str(exc)) from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignatureError("decision payload is not JSON") from exc
    if not isinstance(data, dict):
        raise SignatureError("decision payload must be a JSON object")
    return data


def apply_signed_decision(
    body: bytes,
    signature: str | None,
    *,
    secret: str,
    settings: Settings | None = None,
    persist: bool = True,
) -> RunState:
    """Verify HMAC, then resume via the shipped ``resume_run`` / decide path."""
    data = verify_signed_body(secret, body, signature)
    run_id = str(data.get("run_id") or "").strip()
    if not run_id:
        raise SignatureError("decision payload requires run_id")
    decisions = parse_decision_payload(data)
    decisions.pop("run_id", None)
    if not decisions:
        raise SignatureError("decision payload has no node decisions")
    return resume_run(
        run_id,
        settings=settings or get_settings(),
        persist=persist,
        decisions=decisions,
    )


def handle_http_post(
    headers: Mapping[str, str],
    body: bytes,
    *,
    secret: str,
    settings: Settings | None = None,
    persist: bool = True,
) -> tuple[int, dict[str, Any]]:
    """HTTP POST /decide handler used by the one-shot server and tests."""
    sig = None
    for key, value in headers.items():
        if key.lower() == SIGNATURE_HEADER.lower():
            sig = value
            break
    try:
        state = apply_signed_decision(body, sig, secret=secret, settings=settings, persist=persist)
    except SignatureError as exc:
        return 401, {"ok": False, "error": "SignatureError", "message": str(exc)}
    except ReadyAgentsError as exc:
        return 400, {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    return 200, {
        "ok": True,
        "run_id": state.run_id,
        "status": state.status,
        "outputs": state.output_keys,
    }


def serve_once(
    host: str,
    port: int,
    *,
    secret: str,
    settings: Settings | None = None,
    timeout: float = 15.0,
) -> None:
    """Accept exactly one POST /decide, then exit. Not used by core."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            code, payload = handle_http_post(
                dict(self.headers),
                raw,
                secret=secret,
                settings=settings,
            )
            blob = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    httpd = HTTPServer((host, port), _Handler)
    httpd.timeout = timeout
    try:
        httpd.handle_request()
    finally:
        httpd.server_close()


class HitlGatePack(BasePack):
    name = "hitl-gate"
    version = "0.3.0"


def get_pack() -> HitlGatePack:
    return HitlGatePack()
