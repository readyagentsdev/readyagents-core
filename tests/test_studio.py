"""Shipped readyagents studio: loopback, graph, edit, inspector, reuse."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.approvals.bind import assert_loopback_host
from readyagents.approvals.tokens import TokenService
from readyagents.cli import app
from readyagents.errors import ConfigError
from readyagents.mcp.run_api import RunCoordinator
from readyagents.run_store import JsonRunStore
from readyagents.studio.app import SESSION_COOKIE, compose_studio_app
from readyagents.studio.graph import workflow_graph
from readyagents.workflow.runner import load_workflow, run_workflow_file

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


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


def _session(application) -> str:
    token = application.tokens.issue_bootstrap()
    response = application.handle(
        "GET",
        "/studio",
        query={"token": token},
        headers=_host_headers(application),
        body=b"",
    )
    assert response.status == 303, response.body
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


def test_studio_help_lists_flags() -> None:
    result = runner.invoke(app, ["studio", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = result.stdout + result.stderr
    assert "--port" in text
    assert "--open" in text
    assert "--read-only" in text


def test_non_loopback_bind_refused() -> None:
    with pytest.raises(ConfigError, match="loopback"):
        assert_loopback_host("0.0.0.0")
    with pytest.raises(ConfigError, match="loopback"):
        compose_studio_app(bind_host="8.8.8.8", bind_port=8790)


def test_origin_and_host_mismatch_refused(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    headers = _cookie_headers(application, session, origin=True)
    headers["Origin"] = "http://evil.test"
    response = application.handle(
        "POST",
        "/studio/api/workflow/validate",
        headers=headers,
        body=b"{}",
    )
    assert response.status == 403
    assert _json(response)["error"] == "Forbidden"
    bad_host = _cookie_headers(application, session)
    bad_host["Host"] = "evil.example:8790"
    response = application.handle("GET", "/studio/api/me", headers=bad_host, body=b"")
    assert response.status == 421
    assert _json(response)["error"] == "InvalidHost"


def test_bootstrap_token_single_use_and_expiry(tmp_settings, monkeypatch) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    tokens = TokenService(bootstrap_ttl=30)
    application, _c = _make_studio(tmp_settings, store, tokens=tokens)
    token = application.tokens.issue_bootstrap()
    first = application.handle(
        "GET", "/studio", query={"token": token}, headers=_host_headers(application)
    )
    assert first.status == 303
    second = application.handle(
        "GET", "/studio", query={"token": token}, headers=_host_headers(application)
    )
    assert second.status == 401
    expired = TokenService(bootstrap_ttl=0.01)
    application2, _c2 = _make_studio(tmp_settings, store, tokens=expired)
    tok = application2.tokens.issue_bootstrap()
    real = time.time
    monkeypatch.setattr("readyagents.approvals.tokens.time.time", lambda: real() + 10)
    late = application2.handle(
        "GET", "/studio", query={"token": tok}, headers=_host_headers(application2)
    )
    assert late.status == 401


def test_read_only_refuses_every_write(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store, read_only=True)
    session = _session(application)
    headers = _cookie_headers(application, session, origin=True)
    dest = tmp_path / "wf.yaml"
    dest.write_text("name: x\nnodes:\n  - id: a\n    type: transform\n    template: hi\n")
    writes = [
        (
            "/studio/api/workflow/save",
            {"path": str(dest), "node_id": "a", "field": "template", "value": "no"},
        ),
        ("/studio/api/runs/" + "a" * 32 + "/fork", {"from_node": "a"}),
        ("/studio/api/runs/" + "a" * 32 + "/freeze", {"out": "frozen"}),
        (
            "/studio/api/runs/" + "a" * 32 + "/decide",
            {"node_id": "gate", "decision": "approve", "revision": 1, "action_token": "x"},
        ),
    ]
    for path, payload in writes:
        response = application.handle(
            "POST", path, headers=headers, body=json.dumps(payload).encode()
        )
        assert response.status == 403, (path, response.body)
        assert _json(response)["error"] == "ReadOnly"


def test_graph_payload_has_nested_constructs() -> None:
    spec = load_workflow(EXAMPLES / "composed_gate.yaml")
    graph = workflow_graph(spec, source_path=str(EXAMPLES / "composed_gate.yaml"))
    types = set(graph["constructs"])
    assert "include" in types
    assert "parallel" in types
    assert "approval" in types
    ids = {n["id"] for n in graph["nodes"]}
    assert {"nested", "fan", "extra", "stamp", "gate"} <= ids
    kinds = {e["kind"] for e in graph["edges"]}
    assert "branch" in kinds
    foreach = load_workflow(EXAMPLES / "foreach_calc.yaml")
    g2 = workflow_graph(foreach)
    assert "foreach" in g2["constructs"]
    assert any(n["id"] == "math" and n["relation"] == "body" for n in g2["nodes"])


def test_save_preserves_comments_and_refuses_external_change(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "commented.yaml"
    dest.write_text(
        "# keep this header\n"
        "name: demo\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    # keep me\n"
        "    template: hello\n"
        "    next: b\n"
        "  - id: b\n"
        "    type: transform\n"
        "    template: world\n",
        encoding="utf-8",
    )
    headers = _cookie_headers(application, session)
    opened = application.handle(
        "GET",
        "/studio/api/workflow",
        query={"path": str(dest)},
        headers=headers,
        body=b"",
    )
    assert opened.status == 200, opened.body
    payload = _json(opened)
    fingerprint = payload["fingerprint"]
    write_headers = _cookie_headers(application, session, origin=True)
    saved = application.handle(
        "POST",
        "/studio/api/workflow/save",
        headers=write_headers,
        body=json.dumps(
            {
                "path": str(dest),
                "node_id": "a",
                "field": "template",
                "value": "hello-edited",
                "fingerprint": fingerprint,
            }
        ).encode(),
    )
    assert saved.status == 200, saved.body
    text = dest.read_text(encoding="utf-8")
    assert "# keep this header" in text
    assert "# keep me" in text
    assert "template: hello-edited" in text
    assert text.index("template:") < text.index("next:")
    dest.write_text(text + "# external\n", encoding="utf-8")
    refused = application.handle(
        "POST",
        "/studio/api/workflow/save",
        headers=write_headers,
        body=json.dumps(
            {
                "path": str(dest),
                "node_id": "a",
                "field": "template",
                "value": "again",
                "fingerprint": fingerprint,
            }
        ).encode(),
    )
    assert refused.status == 409
    assert _json(refused)["error"] == "ExternalChange"
    assert "hello-edited" in dest.read_text(encoding="utf-8")
    assert "again" not in dest.read_text(encoding="utf-8")


def test_live_validation_matches_cli(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "bad.yaml"
    dest.write_text(
        "name: bad\nnodes:\n  - id: a\n    type: transform\n    template: ok\n",
        encoding="utf-8",
    )
    headers = _cookie_headers(application, session, origin=True)
    studio = application.handle(
        "POST",
        "/studio/api/workflow/validate",
        headers=headers,
        body=json.dumps(
            {"path": str(dest), "node_id": "a", "field": "next", "value": "ghost"}
        ).encode(),
    )
    cli = runner.invoke(app, ["validate", str(dest), "--json"])
    # CLI still sees the file on disk (valid). Studio validates the patched source.
    assert studio.status == 400, studio.body
    body = _json(studio)
    assert body["error"] == "WorkflowError"
    assert body["problems"]
    assert cli.exit_code == 0


def test_invalid_edit_cannot_save(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "ok.yaml"
    dest.write_text(
        "name: ok\nnodes:\n  - id: a\n    type: transform\n    template: hi\n",
        encoding="utf-8",
    )
    opened = _json(
        application.handle(
            "GET",
            "/studio/api/workflow",
            query={"path": str(dest)},
            headers=_cookie_headers(application, session),
        )
    )
    response = application.handle(
        "POST",
        "/studio/api/workflow/save",
        headers=_cookie_headers(application, session, origin=True),
        body=json.dumps(
            {
                "path": str(dest),
                "node_id": "a",
                "field": "next",
                "value": "ghost",
                "fingerprint": opened["fingerprint"],
            }
        ).encode(),
    )
    assert response.status == 400
    assert dest.read_text(encoding="utf-8").count("transform") == 1


def test_xss_corpus_escaped_in_graph_and_json(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    dest = tmp_path / "xss.yaml"
    dest.write_text(
        "name: xss\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    description: '<script>alert(1)</script>'\n"
        "    template: ok\n",
        encoding="utf-8",
    )
    opened = application.handle(
        "GET",
        "/studio/api/workflow",
        query={"path": str(dest)},
        headers=_cookie_headers(application, session),
    )
    raw = opened.body.decode("utf-8")
    assert "<script>" not in raw
    payload = _json(opened)
    labels = " ".join(
        n.get("label", "") + n.get("description", "") for n in payload["graph"]["nodes"]
    )
    assert "<script>" not in labels
    assert "alert" in labels
    index = application.handle("GET", "/studio", headers=_cookie_headers(application, session))
    html = index.body.decode("utf-8")
    csp = dict(index.headers).get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert '<script src="/studio/assets/app.js"' in html
    assert "alert(1)" not in html


def test_fork_freeze_diff_and_unsigned_decide(tmp_settings, tmp_path: Path) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    recorded = run_workflow_file(
        EXAMPLES / "calc_pipeline.yaml",
        settings=tmp_settings,
        persist=True,
        record=True,
        store=store,
    )
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    headers = _cookie_headers(application, session, origin=True)
    timeline = application.handle(
        "GET",
        f"/studio/api/runs/{recorded.run_id}",
        headers=_cookie_headers(application, session),
    )
    body = _json(timeline)
    assert body["ok"] is True
    assert body["nodes"]
    assert "usage" in body
    node = application.handle(
        "GET",
        f"/studio/api/runs/{recorded.run_id}/nodes/{body['nodes'][0]['node_id']}",
        headers=_cookie_headers(application, session),
    )
    assert _json(node)["ok"] is True
    unsigned = application.handle(
        "POST",
        f"/studio/api/runs/{recorded.run_id}/decide",
        headers=headers,
        body=json.dumps({"node_id": "gate", "decision": "approve", "revision": 1}).encode(),
    )
    assert unsigned.status == 401
    fork = application.handle(
        "POST",
        f"/studio/api/runs/{recorded.run_id}/fork",
        headers=headers,
        body=json.dumps({"from_node": body["nodes"][0]["node_id"]}).encode(),
    )
    assert fork.status == 200, fork.body
    child_id = _json(fork)["run_id"]
    diff = application.handle(
        "GET",
        "/studio/api/runs/diff",
        query={"a": recorded.run_id, "b": child_id},
        headers=_cookie_headers(application, session),
    )
    assert _json(diff)["ok"] is True
    assert "first_divergence" in _json(diff)
    freeze = application.handle(
        "POST",
        f"/studio/api/runs/{recorded.run_id}/freeze",
        headers=headers,
        body=json.dumps({"out": str(tmp_path / "frozen-case"), "allow_unsealed": True}).encode(),
    )
    assert freeze.status == 200, freeze.body
    assert Path(_json(freeze)["path"]).is_dir()


def test_index_and_asset_twice_have_csp(tmp_settings) -> None:
    store = JsonRunStore(tmp_settings.runs_dir())
    application, _c = _make_studio(tmp_settings, store)
    session = _session(application)
    headers = _cookie_headers(application, session)
    for _ in range(2):
        page = application.handle("GET", "/studio", headers=headers, body=b"")
        csp = dict(page.headers).get("Content-Security-Policy", "")
        assert page.status == 200
        assert "script-src 'self'" in csp
        assert b"ReadyAgents studio" in page.body
        assert b'<script src="/studio/assets/app.js"' in page.body
        asset = application.handle("GET", "/studio/assets/app.js", headers=headers, body=b"")
        assert asset.status == 200
        assert b"fetch" in asset.body
        assert "script-src 'self'" in dict(asset.headers).get("Content-Security-Policy", "")
