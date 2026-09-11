"""Hostile cases against the shipped studio handler. Drive handle(); fail closed."""

from __future__ import annotations

import json
from pathlib import Path

from readyagents.approvals.tokens import TokenService
from readyagents.mcp.run_api import RunCoordinator
from readyagents.run_store import JsonRunStore
from readyagents.studio.app import SESSION_COOKIE, compose_studio_app
from readyagents.workflow.runner import run_workflow_file

_XSS = "<script>alert(1)</script>"
_ENV_CANARY = "STUDIO_ADV_ENV_SECRET_9f3a"
_POLICY_CANARY = "STUDIO_ADV_POLICY_SECRET_7c1d"
_DUMMY_RUN = "a" * 32


def _host_headers(application, *, origin: bool = False) -> dict[str, str]:
    headers = {"Host": f"{application.bind_host}:{application.bind_port}"}
    if origin:
        headers["Origin"] = f"http://{application.bind_host}:{application.bind_port}"
    return headers


def _make_studio(tmp_settings, store, *, read_only: bool = False, tokens=None):
    coordinator = RunCoordinator(settings=tmp_settings, workspace=tmp_settings.workspace_path())
    coordinator.attach_store(store)
    tokens = tokens or TokenService()
    application = compose_studio_app(
        store=store,
        tokens=tokens,
        coordinator=coordinator,
        bind_host="127.0.0.1",
        bind_port=8790,
        settings=tmp_settings,
        actor="reviewer",
        read_only=read_only,
    )
    return application, coordinator


def _unlock(application, token: str):
    headers = _host_headers(application, origin=True)
    headers["Content-Type"] = "application/json"
    return application.handle(
        "POST",
        "/studio/session",
        headers=headers,
        body=json.dumps({"token": token}).encode(),
    )


def _session(application) -> str:
    token = application.tokens.issue_bootstrap()
    response = _unlock(application, token)
    assert response.status == 303, response.body
    location = dict(response.headers).get("Location", "")
    assert location == "/studio"
    assert token not in location
    cookie = dict(response.headers).get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert SESSION_COOKIE in cookie
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _cookie_headers(application, session: str, *, origin: bool = False) -> dict[str, str]:
    headers = _host_headers(application, origin=origin)
    headers["Cookie"] = f"{SESSION_COOKIE}={session}"
    return headers


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_xss_payload_not_raw_in_graph_inspector_or_html(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "xss.yaml"
    dest.write_text(
        f'name: "{_XSS}"\n'
        "nodes:\n"
        "  - id: xss\n"
        "    type: transform\n"
        f'    description: "{_XSS}"\n'
        f'    template: "{_XSS}"\n'
        "    output_key: summary\n",
        encoding="utf-8",
    )
    recorded = run_workflow_file(dest, settings=tmp_settings, persist=True, store=store)
    assert recorded.status == "succeeded", recorded.errors

    graph = application.handle(
        "GET",
        "/studio/api/workflow",
        query={"path": str(dest)},
        headers=_cookie_headers(application, session),
        body=b"",
    )
    assert graph.status == 200, graph.body
    assert b"<script>" not in graph.body
    assert b"\\u003c" in graph.body
    graph_payload = _json(graph)
    labels = " ".join(
        str(n.get("label", "")) + str(n.get("description", ""))
        for n in graph_payload["graph"]["nodes"]
    )
    assert "<script>" not in labels
    assert "alert" in labels or "alert" in str(graph_payload["graph"].get("name", ""))

    inspector = application.handle(
        "GET",
        f"/studio/api/runs/{recorded.run_id}/nodes/xss",
        headers=_cookie_headers(application, session),
        body=b"",
    )
    assert inspector.status == 200, inspector.body
    assert b"<script>" not in inspector.body
    assert b"\\u003c" in inspector.body
    inspected = _json(inspector)
    assert "alert(1)" in str(inspected.get("output") or "")

    index = application.handle(
        "GET",
        "/studio",
        headers=_cookie_headers(application, session),
        body=b"",
    )
    assert index.status == 200, index.body
    html = index.body.decode("utf-8")
    assert _XSS not in html
    assert "alert(1)" not in html


def test_csrf_post_without_origin_is_forbidden(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "wf.yaml"
    dest.write_text(
        "name: csrf\nnodes:\n  - id: a\n    type: transform\n    template: hi\n",
        encoding="utf-8",
    )
    headers = _cookie_headers(application, session, origin=False)
    assert "Origin" not in headers
    response = application.handle(
        "POST",
        "/studio/api/workflow/save",
        headers=headers,
        body=json.dumps(
            {"path": str(dest), "node_id": "a", "field": "template", "value": "no"}
        ).encode(),
    )
    assert response.status == 403, response.body
    assert _json(response)["error"] == "Forbidden"


def test_dns_rebinding_host_rejected(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    cookie = f"{SESSION_COOKIE}={session}"
    for host in ("evil.example:8790", "127.0.0.1.nip.io:8790"):
        response = application.handle(
            "GET",
            "/studio/api/me",
            headers={"Host": host, "Cookie": cookie},
            body=b"",
        )
        assert response.status == 421, (host, response.body)
        assert _json(response)["error"] == "InvalidHost"


def test_path_traversal_refuses_secrets_and_policy(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    headers = _cookie_headers(application, session)
    env_path = tmp_path / ".env"
    env_path.write_text(f"SECRET={_ENV_CANARY}\n", encoding="utf-8")
    policy_path = tmp_path / "readyagents.policy.yaml"
    policy_path.write_text(f"rules:\n  - {_POLICY_CANARY}\n", encoding="utf-8")
    parent_env = tmp_path.parent / ".env"
    wrote_parent = not parent_env.exists()
    if wrote_parent:
        parent_env.write_text(f"SECRET={_ENV_CANARY}\n", encoding="utf-8")
    passwd = Path("/etc/passwd")
    passwd_marker = "root:*:0:0"
    if passwd.is_file():
        for line in passwd.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped) > 10:
                passwd_marker = stripped
                break
    try:
        probes = ("../.env", "/etc/passwd", "readyagents.policy.yaml")
        for raw in probes:
            response = application.handle(
                "GET",
                "/studio/api/workflow",
                query={"path": raw},
                headers=headers,
                body=b"",
            )
            assert response.status == 400, (raw, response.status, response.body)
            error = _json(response)["error"]
            assert error in {"PathError", "ConfigError"}, (raw, error, response.body)
            leaked = response.body.decode("utf-8", errors="replace")
            assert _ENV_CANARY not in leaked, raw
            assert _POLICY_CANARY not in leaked, raw
            assert passwd_marker not in leaked, raw
    finally:
        if wrote_parent:
            parent_env.unlink(missing_ok=True)


def test_read_only_refuses_save_fork_freeze_decide(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store, read_only=True)
    session = _session(application)
    headers = _cookie_headers(application, session, origin=True)
    dest = tmp_path / "wf.yaml"
    dest.write_text(
        "name: x\nnodes:\n  - id: a\n    type: transform\n    template: hi\n",
        encoding="utf-8",
    )
    writes = [
        (
            "/studio/api/workflow/save",
            {"path": str(dest), "node_id": "a", "field": "template", "value": "no"},
        ),
        (f"/studio/api/runs/{_DUMMY_RUN}/fork", {"from_node": "a"}),
        (f"/studio/api/runs/{_DUMMY_RUN}/freeze", {"out": "frozen"}),
        (
            f"/studio/api/runs/{_DUMMY_RUN}/decide",
            {"node_id": "gate", "decision": "approve", "revision": 1, "action_token": "x"},
        ),
    ]
    for path, payload in writes:
        response = application.handle(
            "POST", path, headers=headers, body=json.dumps(payload).encode()
        )
        assert response.status == 403, (path, response.body)
        assert _json(response)["error"] == "ReadOnly"


def test_unsigned_approval_decision_rejected(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    response = application.handle(
        "POST",
        f"/studio/api/runs/{_DUMMY_RUN}/decide",
        headers=_cookie_headers(application, session, origin=True),
        body=json.dumps({"node_id": "gate", "decision": "approve", "revision": 1}).encode(),
    )
    assert response.status == 401, response.body
    assert _json(response)["error"] == "Unauthorized"


def test_bootstrap_and_action_token_replay_rejected(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    token = application.tokens.issue_bootstrap()
    first = _unlock(application, token)
    assert first.status == 303, first.body
    assert dict(first.headers).get("Location") == "/studio"
    assert token not in dict(first.headers).get("Location", "")
    second = _unlock(application, token)
    assert second.status == 401, second.body

    session = _session(application)
    action = application.tokens.issue_action(
        run_id=_DUMMY_RUN,
        node_id="gate",
        revision=1,
        decision="approve",
    )
    payload = {
        "node_id": "gate",
        "decision": "approve",
        "revision": 1,
        "action_token": action,
    }
    headers = _cookie_headers(application, session, origin=True)
    consumed = application.handle(
        "POST",
        f"/studio/api/runs/{_DUMMY_RUN}/decide",
        headers=headers,
        body=json.dumps(payload).encode(),
    )
    assert consumed.status != 401, consumed.body
    replay = application.handle(
        "POST",
        f"/studio/api/runs/{_DUMMY_RUN}/decide",
        headers=headers,
        body=json.dumps(payload).encode(),
    )
    assert replay.status == 401, replay.body
    assert _json(replay)["error"] == "Unauthorized"
