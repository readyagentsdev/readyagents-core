from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from readyagents.approvals.app import SESSION_COOKIE, compose_approval_app
from readyagents.errors import ApprovalRequired
from readyagents.mcp.run_api import RunCoordinator
from readyagents.run_store import JsonRunStore
from readyagents.workflow.runner import run_workflow_file

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _headers(app, *, cookie: str | None = None, origin: bool = False, host: str | None = None):
    headers = {"Host": host or f"{app.bind_host}:{app.bind_port}"}
    if origin:
        headers["Origin"] = f"http://{app.bind_host}:{app.bind_port}"
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return headers


def _app(tmp_settings):
    store = JsonRunStore(tmp_settings.runs_dir())
    coordinator = RunCoordinator(settings=tmp_settings, workspace=tmp_settings.workspace_path())
    coordinator.attach_store(store)
    application = compose_approval_app(
        store=store,
        coordinator=coordinator,
        bind_host="127.0.0.1",
        bind_port=8766,
        settings=tmp_settings,
        actor="reviewer",
    )
    return application, store, coordinator


def _session(app) -> str:
    token = app.tokens.issue_bootstrap()
    response = app.handle("GET", "/approvals", query={"token": token}, headers=_headers(app))
    assert response.status == 303
    cookie = dict(response.headers)["Set-Cookie"]
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _paused(tmp_settings, store) -> str:
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(
            EXAMPLES / "approval_gate.yaml",
            settings=tmp_settings,
            persist=True,
            store=store,
            actor="reviewer",
        )
    return exc.value.run_id


def _listed_row(app, session: str) -> dict:
    response = app.handle("GET", "/approvals/api/runs", headers=_headers(app, cookie=session))
    payload = json.loads(response.body.decode())
    assert payload["runs"]
    return payload["runs"][0]


def test_xss_prompt_is_text_not_html(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    # Pause a normal run then mutate prompt via store save of the same state fields
    run_id = _paused(tmp_settings, store)
    stored = store.get(run_id)
    stored.state.pending = dict(stored.state.pending or {})
    stored.state.pending["prompt"] = "<script>alert(1)</script>"
    store.save(stored.state)
    app, store, _c = _app(tmp_settings)
    session = _session(app)
    row = _listed_row(app, session)
    assert "<script>" in row["prompt"] or "script" in row["prompt"].lower()
    page = app.handle("GET", "/approvals", headers=_headers(app, cookie=session))
    html = page.body.decode("utf-8")
    assert "<script>alert(1)</script>" not in html
    js = Path(__file__).resolve().parents[1] / "src/readyagents/approvals/assets/app.js"
    source = js.read_text(encoding="utf-8")
    assert "textContent" in source
    assert "innerHTML" not in source
    store.close()


def test_host_origin_csrf(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    session = _session(app)
    bad_host = app.handle(
        "GET",
        "/approvals/api/runs",
        headers={"Host": "evil.example:8766", "Cookie": f"{SESSION_COOKIE}={session}"},
    )
    assert bad_host.status in {400, 421}
    headers = _headers(app, cookie=session, origin=False)
    headers["Content-Type"] = "application/json"
    csrf = app.handle(
        "POST",
        f"/approvals/api/runs/{'a' * 32}/decide",
        headers=headers,
        body=b'{"node_id":"gate","decision":"approve","revision":1,"action_token":"x"}',
    )
    assert csrf.status == 403
    headers["Origin"] = "http://evil.example:8766"
    csrf2 = app.handle(
        "POST",
        f"/approvals/api/runs/{'a' * 32}/decide",
        headers=headers,
        body=b'{"node_id":"gate","decision":"approve","revision":1,"action_token":"x"}',
    )
    assert csrf2.status == 403
    store.close()


def test_get_does_not_mutate(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    run_id = _paused(tmp_settings, store)
    session = _session(app)
    row = _listed_row(app, session)
    response = app.handle(
        "GET",
        f"/approvals/api/runs/{run_id}/decide",
        headers=_headers(app, cookie=session, origin=True),
        query={
            "node_id": row["node_id"],
            "decision": "approve",
            "revision": str(row["revision"]),
            "action_token": row["actions"]["approve_token"],
        },
    )
    assert response.status in {404, 405}
    assert store.get(run_id).state.status == "paused"
    store.close()


def test_traversal_unknown_asset(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    session = _session(app)
    for path in (
        "/approvals/assets/../../errors.py",
        "/approvals/assets/%2e%2e/app.py",
        "/approvals/assets/secret.txt",
        "/approvals/assets/app.js/../app.js",
    ):
        response = app.handle("GET", path, headers=_headers(app, cookie=session))
        assert response.status in {404, 400}
        assert b"src/readyagents" not in response.body
    ok = app.handle("GET", "/approvals/assets/app.js", headers=_headers(app, cookie=session))
    assert ok.status == 200
    store.close()


def test_oversized_and_invalid_body(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    session = _session(app)
    headers = _headers(app, cookie=session, origin=True)
    headers["Content-Type"] = "application/json"
    huge = app.handle(
        "POST",
        f"/approvals/api/runs/{'a' * 32}/decide",
        headers=headers,
        body=b"{" + b"x" * (app.max_body_bytes + 10),
    )
    assert huge.status == 413
    bad_ct = dict(headers)
    bad_ct["Content-Type"] = "text/plain"
    ct = app.handle(
        "POST",
        f"/approvals/api/runs/{'a' * 32}/decide",
        headers=bad_ct,
        body=b"{}",
    )
    assert ct.status == 415
    store.close()


def test_action_replay_and_stale_revision(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    run_id = _paused(tmp_settings, store)
    session = _session(app)
    row = _listed_row(app, session)
    headers = _headers(app, cookie=session, origin=True)
    headers["Content-Type"] = "application/json"
    payload = {
        "node_id": row["node_id"],
        "decision": "approve",
        "revision": row["revision"],
        "action_token": row["actions"]["approve_token"],
    }
    first = app.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=json.dumps(payload).encode(),
    )
    assert first.status in {200, 202, 409}
    second = app.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=json.dumps(payload).encode(),
    )
    assert second.status == 401
    row2 = _listed_row(app, session) if store.get(run_id).state.status == "paused" else None
    if row2:
        stale = {
            "node_id": row2["node_id"],
            "decision": "approve",
            "revision": 1,
            "action_token": row2["actions"]["approve_token"],
        }
        # consume token with wrong revision already failed above; issue new
        listed = json.loads(
            app.handle("GET", "/approvals/api/runs", headers=_headers(app, cookie=session)).body
        )
        if listed["runs"]:
            fresh = listed["runs"][0]
            stale["action_token"] = fresh["actions"]["approve_token"]
            stale["revision"] = fresh["revision"] + 5
            conflict = app.handle(
                "POST",
                f"/approvals/api/runs/{run_id}/decide",
                headers=headers,
                body=json.dumps(stale).encode(),
            )
            assert conflict.status in {401, 409}
    store.close()


def test_wrong_node_conflict(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    run_id = _paused(tmp_settings, store)
    session = _session(app)
    row = _listed_row(app, session)
    headers = _headers(app, cookie=session, origin=True)
    headers["Content-Type"] = "application/json"
    other = app.tokens.issue_action(
        run_id=run_id, node_id="not-gate", revision=row["revision"], decision="approve"
    )
    body = {
        "node_id": "not-gate",
        "decision": "approve",
        "revision": row["revision"],
        "action_token": other,
    }
    response = app.handle(
        "POST",
        f"/approvals/api/runs/{run_id}/decide",
        headers=headers,
        body=json.dumps(body).encode(),
    )
    assert response.status == 409
    assert store.get(run_id).state.status == "paused"
    store.close()


def test_concurrent_clicks_one_resume(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    run_id = _paused(tmp_settings, store)
    session = _session(app)
    row = _listed_row(app, session)
    approve = row["actions"]["approve_token"]
    reject = row["actions"]["reject_token"]
    headers = _headers(app, cookie=session, origin=True)
    headers["Content-Type"] = "application/json"

    def _post(token: str, decision: str) -> int:
        payload = {
            "node_id": row["node_id"],
            "decision": decision,
            "revision": row["revision"],
            "action_token": token,
        }
        resp = app.handle(
            "POST",
            f"/approvals/api/runs/{run_id}/decide",
            headers=headers,
            body=json.dumps(payload).encode(),
        )
        return resp.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(lambda item: _post(*item), [(approve, "approve"), (reject, "reject")])
        )
    assert 409 in statuses or statuses.count(202) + statuses.count(200) == 1
    import time

    for _ in range(40):
        if store.get(run_id).state.status != "paused":
            break
        time.sleep(0.05)
    # Exactly one resume path executed; status is terminal succeeded.
    assert store.get(run_id).state.status == "succeeded"
    store.close()


def test_logs_do_not_contain_tokens(tmp_settings, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    app, store, _c = _app(tmp_settings)
    token = app.tokens.issue_bootstrap()
    app.handle("GET", "/approvals", query={"token": token}, headers=_headers(app))
    text = caplog.text
    assert token not in text
    store.close()


def test_prefix_id_rejected(tmp_settings) -> None:
    app, store, _c = _app(tmp_settings)
    run_id = _paused(tmp_settings, store)
    session = _session(app)
    row = _listed_row(app, session)
    headers = _headers(app, cookie=session, origin=True)
    headers["Content-Type"] = "application/json"
    body = json.dumps(
        {
            "node_id": row["node_id"],
            "decision": "approve",
            "revision": row["revision"],
            "action_token": row["actions"]["approve_token"],
        }
    ).encode()
    response = app.handle(
        "POST",
        f"/approvals/api/runs/{run_id[:8]}/decide",
        headers=headers,
        body=body,
    )
    assert response.status in {400, 404}
    assert store.get(run_id).state.status == "paused"
    store.close()
