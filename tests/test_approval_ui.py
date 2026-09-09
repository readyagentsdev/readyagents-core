from __future__ import annotations

import json
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.approvals.app import SESSION_COOKIE, ApprovalApplication, compose_approval_app
from readyagents.approvals.bind import assert_loopback_host
from readyagents.approvals.server import _make_handler
from readyagents.approvals.tokens import TokenService
from readyagents.audit import read_audit_events
from readyagents.cli import app
from readyagents.errors import ApprovalRequired, ConfigError
from readyagents.mcp.run_api import RunCoordinator
from readyagents.packs.protocol import BasePack
from readyagents.policy import CallbackAuthorizer
from readyagents.run_store import JsonRunStore, SQLiteRunStore
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class _DenyPack(BasePack):
    name = "deny-approve"
    version = "0"

    def register_authorizers(self):
        def allowed(actor, action, resource) -> bool:
            if action in {"approve", "reject"}:
                return False
            return True

        return [CallbackAuthorizer(allowed, name="deny-approve")]


def _host_headers(application: ApprovalApplication, *, origin: bool = False) -> dict[str, str]:
    headers = {"Host": f"{application.bind_host}:{application.bind_port}"}
    if origin:
        headers["Origin"] = f"http://{application.bind_host}:{application.bind_port}"
    return headers


def _make_ui(tmp_settings, store, tokens=None):
    coordinator = RunCoordinator(settings=tmp_settings, workspace=tmp_settings.workspace_path())
    coordinator.attach_store(store)
    tokens = tokens or TokenService()
    application = compose_approval_app(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host="127.0.0.1",
        bind_port=8766,
        settings=tmp_settings,
        actor="reviewer",
    )
    return application, coordinator


def _pause(tmp_settings, store) -> str:
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(
            EXAMPLES / "approval_gate.yaml",
            settings=tmp_settings,
            persist=True,
            store=store,
            actor="reviewer",
        )
    return exc.value.run_id


def _session(application: ApprovalApplication) -> str:
    token = application.tokens.issue_bootstrap()
    response = application.handle(
        "GET",
        "/approvals",
        query={"token": token},
        headers=_host_headers(application),
        body=b"",
    )
    assert response.status == 303, response.body
    cookie = dict(response.headers).get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/approvals" in cookie
    assert SESSION_COOKIE in cookie
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _cookie_headers(
    application: ApprovalApplication, session: str, *, origin: bool = False
) -> dict[str, str]:
    headers = _host_headers(application, origin=origin)
    headers["Cookie"] = f"{SESSION_COOKIE}={session}"
    return headers


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_list_only_approval_pauses_redacted(tmp_settings, tmp_path, backend: str) -> None:
    store = (
        JsonRunStore(tmp_settings.runs_dir())
        if backend == "json"
        else SQLiteRunStore(tmp_path / "ui.sqlite3")
    )
    run_id = _pause(tmp_settings, store)
    run_workflow_file(
        EXAMPLES / "calc_pipeline.yaml", settings=tmp_settings, persist=True, store=store
    )
    application, _coord = _make_ui(tmp_settings, store)
    session = _session(application)
    response = application.handle(
        "GET",
        "/approvals/api/runs",
        headers=_cookie_headers(application, session),
        body=b"",
    )
    assert response.status == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["ok"] is True
    assert [row["run_id"] for row in payload["runs"]] == [run_id]
    row = payload["runs"][0]
    assert row["node_id"] == "gate"
    assert "inputs" not in row
    assert "outputs" not in row
    assert "node_results" not in row
    assert "approve_token" in row["actions"]
    assert "reject_token" in row["actions"]
    csp = dict(response.headers).get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    store.close()


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_approve_and_reject_follow_resume(tmp_settings, tmp_path, backend: str) -> None:
    store = (
        JsonRunStore(tmp_settings.runs_dir())
        if backend == "json"
        else SQLiteRunStore(tmp_path / "ui2.sqlite3")
    )
    run_id = _pause(tmp_settings, store)
    application, _coord = _make_ui(tmp_settings, store)
    session = _session(application)
    listed = json.loads(
        application.handle(
            "GET",
            "/approvals/api/runs",
            headers=_cookie_headers(application, session),
            body=b"",
        ).body
    )
    row = listed["runs"][0]
    body = json.dumps(
        {
            "node_id": row["node_id"],
            "decision": "approve",
            "revision": row["revision"],
            "action_token": row["actions"]["approve_token"],
        }
    ).encode("utf-8")
    headers = _cookie_headers(application, session, origin=True)
    headers["Content-Type"] = "application/json"
    decided = application.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=body,
    )
    assert decided.status in {200, 202}, decided.body
    deadline = 50
    for _ in range(deadline):
        stored = store.get(run_id, allow_prefix=False)
        if stored.state.status in {"succeeded", "failed", "cancelled", "paused"}:
            if stored.state.status == "succeeded":
                break
        import time

        time.sleep(0.05)
    stored = store.get(run_id, allow_prefix=False)
    assert stored.state.status == "succeeded"
    assert "approval_gate ok" in str(stored.state.output_keys.get("summary"))
    events = read_audit_events(tmp_settings.audit_dir(), run_id)
    decisions = [e for e in events if e.get("event") == "decision"]
    assert len(decisions) == 1
    assert decisions[0].get("decision") == "approve"
    assert decisions[0].get("actor") == "reviewer"
    store.close()


def test_reject_branch(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    run_id = _pause(tmp_settings, store)
    application, _coord = _make_ui(tmp_settings, store)
    session = _session(application)
    listed = json.loads(
        application.handle(
            "GET",
            "/approvals/api/runs",
            headers=_cookie_headers(application, session),
            body=b"",
        ).body
    )
    row = listed["runs"][0]
    headers = _cookie_headers(application, session, origin=True)
    headers["Content-Type"] = "application/json"
    body = json.dumps(
        {
            "node_id": row["node_id"],
            "decision": "reject",
            "revision": row["revision"],
            "action_token": row["actions"]["reject_token"],
        }
    ).encode()
    decided = application.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=body,
    )
    assert decided.status in {200, 202}, decided.body
    import time

    for _ in range(50):
        stored = store.get(run_id, allow_prefix=False)
        if stored.state.status == "succeeded":
            break
        time.sleep(0.05)
    stored = store.get(run_id, allow_prefix=False)
    assert "denied" in str(stored.state.output_keys.get("summary"))
    store.close()


def test_authorizer_denial_leaves_paused(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    run_id = _pause(tmp_settings, store)
    coordinator = RunCoordinator(
        settings=tmp_settings,
        workspace=tmp_settings.workspace_path(),
        extra_packs=[_DenyPack()],
    )
    coordinator.attach_store(store)
    application = compose_approval_app(
        store=store,
        coordinator=coordinator,
        bind_host="127.0.0.1",
        bind_port=8766,
        settings=tmp_settings,
        actor="reviewer",
    )
    session = _session(application)
    listed = json.loads(
        application.handle(
            "GET",
            "/approvals/api/runs",
            headers=_cookie_headers(application, session),
            body=b"",
        ).body
    )
    row = listed["runs"][0]
    headers = _cookie_headers(application, session, origin=True)
    headers["Content-Type"] = "application/json"
    body = json.dumps(
        {
            "node_id": row["node_id"],
            "decision": "approve",
            "revision": row["revision"],
            "action_token": row["actions"]["approve_token"],
        }
    ).encode()
    decided = application.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=body,
    )
    assert decided.status == 403, decided.body
    stored = store.get(run_id, allow_prefix=False)
    assert stored.state.status == "paused"
    store.close()


def test_non_loopback_rejected_before_socket() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        assert_loopback_host("0.0.0.0")
    with pytest.raises(ConfigError, match="loopback"):
        assert_loopback_host("1.2.3.4")


def test_cli_rejects_non_loopback_host() -> None:
    result = runner.invoke(app, ["approvals", "serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "loopback" in text.lower()


def test_cli_help_lists_approvals() -> None:
    result = runner.invoke(app, ["approvals", "serve", "--help"])
    assert result.exit_code == 0
    text = result.stdout + result.stderr
    assert "127.0.0.1" in text
    assert "8766" in text


def test_import_does_not_bind(monkeypatch) -> None:
    opened: list[object] = []
    real = socket.socket

    class _Sock(socket.socket):
        def bind(self, address):  # type: ignore[override]
            opened.append(address)
            return super().bind(address)

    monkeypatch.setattr(socket, "socket", _Sock)
    import importlib

    import readyagents.approvals as approvals

    importlib.reload(approvals)
    from readyagents.cli import app as cli_app

    help_result = runner.invoke(cli_app, ["--help"])
    assert help_result.exit_code == 0
    assert opened == []
    _ = real


def test_bootstrap_redirect_and_list_requires_session(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _coord = _make_ui(tmp_settings, store)
    missing = application.handle("GET", "/approvals/api/runs", headers=_host_headers(application))
    assert missing.status == 401
    token = application.tokens.issue_bootstrap()
    first = application.handle(
        "GET",
        "/approvals",
        query={"token": token},
        headers=_host_headers(application),
    )
    assert first.status == 303
    assert dict(first.headers).get("Location") == "/approvals"
    second = application.handle(
        "GET",
        "/approvals",
        query={"token": token},
        headers=_host_headers(application),
    )
    assert second.status == 401
    store.close()


def test_real_bind_and_bootstrap_http(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, coordinator = _make_ui(tmp_settings, store)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    application.bind_port = port
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(application))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        token = application.tokens.issue_bootstrap()
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", f"/approvals?token={token}", headers={"Host": f"127.0.0.1:{port}"})
        response = conn.getresponse()
        assert response.status == 303
        location = response.getheader("Location")
        assert location == "/approvals"
        cookie = response.getheader("Set-Cookie") or ""
        assert SESSION_COOKIE in cookie
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        coordinator.shutdown(timeout=1)
        store.close()
