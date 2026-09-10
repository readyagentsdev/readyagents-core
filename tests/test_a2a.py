"""A2A card, serve projection, node delegation. Drive shipped CLI / TestClient."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from typer.testing import CliRunner

from readyagents.a2a.card import (
    WELL_KNOWN_ALIAS,
    WELL_KNOWN_CARD,
    build_agent_card,
    card_digest,
    validate_card,
)
from readyagents.a2a.client import fetch_agent_card, request_json, reset_transport, use_transport
from readyagents.a2a.mapping import (
    WIRE_CANCELED,
    WIRE_COMPLETED,
    WIRE_WORKING,
    map_run_to_task_state,
    validate_transition,
)
from readyagents.cli import app
from readyagents.errors import (
    A2ACardError,
    A2AError,
    A2ATransitionError,
    ApprovalRequired,
    PolicyDenied,
    ToolError,
)
from readyagents.firewall.taint import provenance_of
from readyagents.workflow.runner import load_workflow, resume_run, run_workflow_file

runner = CliRunner()

_TOKEN = "tok_" + ("A" * 40)
_SECRET = "hmac-a2a-decision-secret"
_BIND_HOST = "127.0.0.1"
_BIND_PORT = 8770
_BASE_URL = f"http://{_BIND_HOST}:{_BIND_PORT}"
_DUMMY_ID = "b" * 32

_OK_WF = """
name: a2a-ok
version: "1"
nodes:
  - id: t
    type: transform
    template: "hello-a2a"
    output_key: summary
"""

_GATE_WF = """
name: a2a-gate
version: "1"
start: gate
nodes:
  - id: gate
    type: approval
    prompt: "Release the payload?"
    then: ok
    else: denied
  - id: ok
    type: transform
    template: "approved"
    output_key: summary
  - id: denied
    type: transform
    template: "denied"
    output_key: summary
"""

_DELEGATE_WF = """
name: a2a-delegate
start: delegate
nodes:
  - id: delegate
    type: a2a
    agent_url: "http://partner.example.com"
    message: "Summarise please"
    on_input_required: gate
    output_key: summary
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _starlette():
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    return TestClient


def test_card_is_deterministic_and_marks_approval(tmp_path: Path) -> None:
    plain = load_workflow(_write(tmp_path / "ok.yaml", _OK_WF))
    gated = load_workflow(_write(tmp_path / "gate.yaml", _GATE_WF))
    url = "http://127.0.0.1:8770"
    a = build_agent_card(plain, url=url)
    b = build_agent_card(plain, url=url)
    assert a == b
    assert a["digest"] == card_digest(a)
    assert a["capabilities"]["humanInput"] is False
    assert a["capabilities"]["streaming"] is True
    assert a["readyagents"]["approvalCapable"] is False
    g = build_agent_card(gated, url=url)
    assert g["capabilities"]["humanInput"] is True
    assert g["readyagents"]["approvalCapable"] is True
    assert g["readyagents"]["wellKnown"] == WELL_KNOWN_CARD
    assert g["readyagents"]["alias"] == WELL_KNOWN_ALIAS


def test_validate_card_rejects_hostile() -> None:
    with pytest.raises(A2ACardError):
        validate_card("not-json")
    with pytest.raises(A2ACardError):
        validate_card({"name": "x"})
    with pytest.raises(A2ACardError):
        validate_card({"name": "x\x00", "url": "https://example.com"})
    with pytest.raises(A2ACardError):
        validate_card({"name": "x", "url": "https://example.com", "description": "hi\x07"})
    with pytest.raises(A2ACardError):
        validate_card(b"{" + b"x" * 200_000 + b"}")
    with pytest.raises(A2ACardError):
        validate_card({"name": "x", "url": "file:///etc/passwd"})
    with pytest.raises(A2ACardError):
        validate_card({"name": "x", "url": "javascript://alert(1)"})


def test_task_state_mapping_and_terminal_lock() -> None:
    assert map_run_to_task_state("queued") == "submitted"
    assert map_run_to_task_state("running") == "working"
    assert map_run_to_task_state("paused") == "input-required"
    assert map_run_to_task_state("succeeded") == "completed"
    assert map_run_to_task_state("cancelled") == "canceled"
    validate_transition("working", "input-required")
    validate_transition("input-required", "completed")
    with pytest.raises(A2ATransitionError):
        validate_transition(WIRE_COMPLETED, WIRE_WORKING)
    with pytest.raises(A2ATransitionError):
        validate_transition(WIRE_CANCELED, WIRE_COMPLETED)


def test_cli_card_twice_identical(tmp_path: Path, tmp_settings) -> None:
    path = _write(tmp_path / "gate.yaml", _GATE_WF)
    first = runner.invoke(app, ["a2a", "card", str(path), "--json"])
    second = runner.invoke(app, ["a2a", "card", str(path), "--json"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout)
    b = json.loads(second.stdout)
    assert a["ok"] is True
    assert a["command"] == "a2a card"
    assert a["card"] == b["card"]
    assert a["card"]["readyagents"]["approvalCapable"] is True
    assert a["card"]["capabilities"]["streaming"] is True


def test_cli_dry_run_delegate_example(examples_dir: Path, tmp_settings) -> None:
    path = examples_dir / "a2a_delegate.yaml"
    result = runner.invoke(app, ["run", str(path), "--dry-run", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["status"] == "succeeded"


def test_fetch_card_refuses_loopback() -> None:
    with pytest.raises((ToolError, A2AError, A2ACardError)):
        fetch_agent_card("http://127.0.0.1:9")
    with pytest.raises((ToolError, A2AError, A2ACardError)):
        fetch_agent_card("http://localhost/")
    with pytest.raises((ToolError, A2AError, A2ACardError)):
        fetch_agent_card("http://169.254.169.254/latest/meta-data")


def test_redirect_does_not_forward_authorization() -> None:
    seen: list[dict[str, str]] = []

    def exchange(
        url: str,
        *,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, dict[str, str], str | None]:
        seen.append({"url": url, "authorization": headers.get("Authorization", "")})
        host = (urlparse(url).hostname or "").lower()
        if host == "partner.example.com":
            return 302, b"", {}, "http://other.example.com/card"
        card = {
            "name": "x",
            "url": "http://other.example.com",
            "description": "d",
        }
        return 200, json.dumps(card).encode(), {"Content-Type": "application/json"}, None

    token = use_transport(exchange)
    try:
        request_json(
            "http://partner.example.com/.well-known/agent-card.json",
            token="secret-token",
            accept_redirects=True,
        )
    finally:
        reset_transport(token)
    assert seen[0]["authorization"] == "Bearer secret-token"
    assert seen[1]["url"].startswith("http://other.example.com")
    assert seen[1]["authorization"] == ""


def _headers(token: str | None = _TOKEN, extra: dict[str, str] | None = None) -> dict[str, str]:
    hdrs = {"Content-Type": "application/json"}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    if extra:
        hdrs.update(extra)
    return hdrs


def _rpc(client, method: str, params: dict[str, Any], *, token: str | None = _TOKEN, extra=None):
    return client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=_headers(token, extra),
    )


def _wait(
    client,
    task_id: str,
    *,
    token: str | None = _TOKEN,
    settle: tuple[str, ...] = ("completed", "failed", "canceled", "input-required"),
) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    last: dict[str, Any] | None = None
    last_error: Any = None
    while time.monotonic() < deadline:
        resp = _rpc(client, "tasks/get", {"id": task_id}, token=token)
        body = resp.json() if resp.content else None
        if not isinstance(body, dict):
            time.sleep(0.1)
            continue
        if body.get("error"):
            last_error = body.get("error")
            time.sleep(0.1)
            continue
        result = body.get("result")
        if isinstance(result, dict):
            last = result
            state = ((last or {}).get("status") or {}).get("state")
            if state in settle:
                return last
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not settle {settle}: last={last} error={last_error}")


@contextmanager
def _a2a_client(
    tmp_path: Path,
    tmp_settings,
    workflow_text: str,
    *,
    secret: str | None = None,
    filename: str = "wf.yaml",
    canonical_url: str | None = None,
) -> Iterator[tuple[Any, Any, Path]]:
    TestClient = _starlette()
    from readyagents.a2a.server import compose_a2a_app
    from readyagents.mcp.run_api import RunCoordinator

    path = _write(tmp_path / filename, workflow_text)
    settings = tmp_settings
    if secret:
        settings = tmp_settings.model_copy(update={"decision_secret": secret})
    coordinator = RunCoordinator(settings=settings, workspace=tmp_path)
    app_obj = compose_a2a_app(
        workflow_path=path,
        coordinator=coordinator,
        token=_TOKEN,
        bind_host=_BIND_HOST,
        bind_port=_BIND_PORT,
        canonical_url=canonical_url,
    )
    try:
        with TestClient(app_obj, base_url=_BASE_URL) as client:
            yield client, coordinator, path
    finally:
        shutdown = getattr(coordinator, "shutdown", None)
        if callable(shutdown):
            shutdown()


def _client_exchange(client) -> Any:
    def exchange(
        url: str,
        *,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, dict[str, str], str | None]:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        hdrs = dict(headers)
        base = urlparse(str(client.base_url))
        host = base.hostname or _BIND_HOST
        port = base.port or _BIND_PORT
        hdrs["Host"] = f"{host}:{port}"
        resp = client.request(method.upper(), path, content=body or b"", headers=hdrs)
        loc = resp.headers.get("location")
        return resp.status_code, resp.content, {k: v for k, v in resp.headers.items()}, loc

    return exchange


def test_serve_requires_bearer_and_serves_card(tmp_path: Path, tmp_settings) -> None:
    with _a2a_client(tmp_path, tmp_settings, _OK_WF) as (client, _coord, _path):
        naked = client.get(WELL_KNOWN_CARD)
        assert naked.status_code == 401
        card = client.get(WELL_KNOWN_CARD, headers=_headers())
        assert card.status_code == 200
        body = card.json()
        assert body["name"] == "a2a-ok"
        assert body["capabilities"]["streaming"] is True
        alias = client.get(WELL_KNOWN_ALIAS, headers=_headers())
        assert alias.status_code == 200
        assert alias.json()["digest"] == body["digest"]


def test_serve_lifecycle_completed(tmp_path: Path, tmp_settings) -> None:
    with _a2a_client(tmp_path, tmp_settings, _OK_WF) as (client, _coord, _path):
        started = _rpc(
            client,
            "message/send",
            {"message": {"parts": [{"kind": "text", "text": "{}"}]}},
        )
        assert started.status_code == 200, started.text
        task_id = started.json()["result"]["id"]
        done = _wait(client, task_id)
        assert done["status"]["state"] == "completed"
        arts = done.get("artifacts") or []
        assert arts
        text = arts[0]["parts"][0]["text"]
        assert "hello-a2a" in text


def test_serve_unknown_and_unauthorized_ids_match(tmp_path: Path, tmp_settings) -> None:
    with _a2a_client(tmp_path, tmp_settings, _OK_WF) as (client, _coord, _path):
        missing = _rpc(client, "tasks/get", {"id": _DUMMY_ID})
        prefix = _rpc(client, "tasks/get", {"id": "abcd"})
        assert missing.json()["error"]["message"] == prefix.json()["error"]["message"]
        assert missing.json()["error"]["message"] == "Task not found"
        unauth_missing = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": _DUMMY_ID}},
            headers=_headers("tok_" + ("B" * 40)),
        )
        unauth_prefix = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "abcd"}},
            headers=_headers("tok_" + ("B" * 40)),
        )
        assert unauth_missing.status_code == 401
        assert unauth_prefix.status_code == 401


def test_serve_input_required_unsigned_refused_signed_resumes(tmp_path: Path, tmp_settings) -> None:
    from readyagents.audit import read_audit_events
    from readyagents.decisions.signing import sign_body
    from readyagents.mcp.tasks import canonical_decision_bytes

    with _a2a_client(tmp_path, tmp_settings, _GATE_WF, secret=_SECRET) as (
        client,
        coordinator,
        _path,
    ):
        started = _rpc(
            client,
            "message/send",
            {"message": {"parts": [{"kind": "text", "text": "{}"}]}},
        )
        task_id = started.json()["result"]["id"]
        paused = _wait(client, task_id)
        assert paused["status"]["state"] == "input-required"
        prompt = paused["status"]["message"]["parts"][0]["text"]
        assert "Release the payload?" in prompt

        unsigned = _rpc(
            client,
            "message/send",
            {
                "taskId": task_id,
                "message": {"parts": [{"kind": "text", "text": "approve"}]},
            },
        )
        assert unsigned.status_code == 200
        err = unsigned.json()["error"]
        assert err["code"] == -32010
        still = _wait(client, task_id)
        assert still["status"]["state"] == "input-required"
        events = read_audit_events(coordinator.settings.audit_dir(), task_id)
        assert any(row.get("event") == "decision_refused" for row in events)

        body = canonical_decision_bytes(
            run_id=task_id,
            node_id="gate",
            decision="approve",
            actor=None,
            input_request_key=f"a2a.{task_id}.gate",
        )
        sig = sign_body(_SECRET, body)
        signed = _rpc(
            client,
            "message/send",
            {
                "taskId": task_id,
                "message": {"parts": [{"kind": "text", "text": "approve"}]},
            },
            extra={"X-Signature": sig},
        )
        assert signed.status_code == 200, signed.text
        assert "error" not in signed.json()
        done = _wait(client, task_id, settle=("completed", "failed", "canceled"))
        assert done["status"]["state"] == "completed"


def test_serve_cancel_and_refuse_terminal_escape(tmp_path: Path, tmp_settings) -> None:
    with _a2a_client(tmp_path, tmp_settings, _GATE_WF) as (client, _coord, _path):
        started = _rpc(
            client,
            "message/send",
            {"message": {"parts": [{"kind": "text", "text": "{}"}]}},
        )
        task_id = started.json()["result"]["id"]
        _wait(client, task_id)
        canceled = _rpc(client, "tasks/cancel", {"id": task_id})
        assert canceled.status_code == 200, canceled.text
        cancel_body = canceled.json()
        assert "error" not in cancel_body, cancel_body
        assert (cancel_body.get("result") or {}).get("status", {}).get("state") == "canceled"
        body = _wait(client, task_id, settle=("canceled", "failed", "completed"))
        assert body["status"]["state"] == "canceled"
    with _a2a_client(tmp_path, tmp_settings, _OK_WF, filename="done.yaml") as (
        client,
        _coord,
        _path,
    ):
        started = _rpc(
            client,
            "message/send",
            {"message": {"parts": [{"kind": "text", "text": "{}"}]}},
        )
        task_id = started.json()["result"]["id"]
        done = _wait(client, task_id, settle=("completed", "failed", "canceled"))
        assert done["status"]["state"] == "completed"
        again = _rpc(client, "tasks/cancel", {"id": task_id})
        assert again.json().get("error", {}).get("code") == -32023


def test_type_a2a_fixture_maps_artifacts_and_taint(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    with _a2a_client(
        tmp_path,
        tmp_settings,
        _OK_WF,
        filename="remote.yaml",
        canonical_url="http://partner.example.com",
    ) as (
        client,
        _coord,
        _remote,
    ):
        hook = use_transport(_client_exchange(client))
        try:
            local = _write(tmp_path / "local.yaml", _DELEGATE_WF)
            state = run_workflow_file(local, settings=tmp_settings, persist=False)
        finally:
            reset_transport(hook)
    assert state.status == "succeeded"
    assert "hello-a2a" in str(
        state.output_keys.get("summary") or state.node_outputs.get("delegate")
    )
    assert provenance_of(state, "delegate").trust == "untrusted"
    assert provenance_of(state, "delegate").source == "a2a"
    assert provenance_of(state, "summary").trust == "untrusted"


def test_type_a2a_remote_input_required_becomes_local_gate(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    with _a2a_client(
        tmp_path,
        tmp_settings,
        _GATE_WF,
        filename="remote.yaml",
        canonical_url="http://partner.example.com",
    ) as (
        client,
        _coord,
        _remote,
    ):
        hook = use_transport(_client_exchange(client))
        try:
            local = _write(tmp_path / "local.yaml", _DELEGATE_WF)
            with pytest.raises(ApprovalRequired) as raised:
                run_workflow_file(local, settings=tmp_settings, persist=True)
        finally:
            reset_transport(hook)
    exc = raised.value
    assert exc.node_id == "delegate"
    assert "untrusted" in exc.prompt.lower()
    assert "Remote A2A" in exc.prompt
    from readyagents.workflow.state import load_run

    state = load_run(tmp_settings.runs_dir(), exc.run_id)
    assert state.status == "paused"
    assert state.pending_node == "delegate"


def test_type_a2a_ssrf_refused(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "ssrf.yaml",
        "name: ssrf\nnodes:\n  - id: d\n    type: a2a\n"
        "    agent_url: 'http://127.0.0.1:9'\n    message: hi\n",
    )
    with pytest.raises((ToolError, A2AError)):
        run_workflow_file(wf, settings=tmp_settings, persist=False)


def test_policy_denies_a2a_node(tmp_path: Path, tmp_settings) -> None:
    from readyagents.errors import PolicyDenied

    policy = _write(
        tmp_path / "readyagents.policy.yaml",
        "version: 1\ndefault: deny\n",
    )
    wf = _write(tmp_path / "local.yaml", _DELEGATE_WF)
    with pytest.raises(PolicyDenied):
        run_workflow_file(wf, settings=tmp_settings, persist=False, policy=policy)


def test_card_digest_drift_gates_when_policy_exists(tmp_path: Path, tmp_settings) -> None:
    from readyagents.a2a.card import card_digest as digest_of
    from readyagents.a2a.node import _pin_card
    from readyagents.workflow.schema import NodeSpec
    from readyagents.workflow.state import RunState

    policy = _write(
        tmp_path / "readyagents.policy.yaml",
        "version: 1\ndefault: allow\ntools:\n  a2a:\n    on_description_change: deny\n",
    )
    from readyagents.firewall.policy_file import load_policy

    loaded = load_policy(policy)
    node = NodeSpec(id="d", type="a2a", agent_url="http://partner.example.com", message="x")
    state = RunState.start("t", {})
    ctx = type(
        "Ctx",
        (),
        {"policy": loaded, "pin_home": tmp_settings.home_path(), "on_persist": None},
    )()
    first = {"name": "x", "url": "http://partner.example.com", "description": "one"}
    _pin_card(ctx, state, node, "http://partner.example.com", digest_of(first))
    second = {"name": "x", "url": "http://partner.example.com", "description": "two"}
    from readyagents.errors import PolicyDenied

    with pytest.raises((PolicyDenied, ApprovalRequired)):
        _pin_card(ctx, state, node, "http://partner.example.com", digest_of(second))


def _scripted_exchange(
    cards: list[dict[str, Any]],
    *,
    posts: list[tuple[str, str]] | None = None,
) -> Any:
    idx = {"n": 0}

    def exchange(
        url: str,
        *,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, dict[str, str], str | None]:
        host = (urlparse(url).hostname or "").lower()
        auth = headers.get("Authorization", "")
        if method.upper() == "GET":
            i = min(idx["n"], len(cards) - 1)
            idx["n"] += 1
            payload = json.dumps(cards[i]).encode("utf-8")
            return 200, payload, {"Content-Type": "application/json"}, None
        if posts is not None:
            posts.append((host, auth))
        result = {
            "id": "c" * 32,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": '{"ok": true}'}]}],
        }
        body_out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode("utf-8")
        return 200, body_out, {"Content-Type": "application/json"}, None

    return exchange


def test_hostile_card_digest_field_does_not_mask_drift(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stable fake digest on the card must not freeze the locally computed pin."""
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    policy = _write(
        tmp_path / "pin.policy.yaml",
        "version: 1\ndefault: allow\ntools:\n  a2a:\n    on_description_change: deny\n",
    )
    wf = _write(tmp_path / "local.yaml", _DELEGATE_WF)
    first = {
        "name": "partner",
        "url": "http://partner.example.com",
        "description": "one",
        "digest": "sha256:fixed",
    }
    second = {
        "name": "partner",
        "url": "http://partner.example.com",
        "description": "two",
        "digest": "sha256:fixed",
    }
    assert card_digest(first) != card_digest(second)
    hook = use_transport(_scripted_exchange([first, second]))
    try:
        state = run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)
        assert state.status == "succeeded"
        with pytest.raises(PolicyDenied):
            run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)
    finally:
        reset_transport(hook)


def test_card_digest_drift_approve_resumes(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default gate on digest drift is approvable; resume stores the new digest."""
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    policy = _write(
        tmp_path / "gate.policy.yaml",
        "version: 1\ndefault: allow\ntools:\n  a2a: {}\n",
    )
    wf = _write(tmp_path / "local.yaml", _DELEGATE_WF)
    first = {
        "name": "partner",
        "url": "http://partner.example.com",
        "description": "one",
    }
    second = {
        "name": "partner",
        "url": "http://partner.example.com",
        "description": "two",
    }
    hook = use_transport(_scripted_exchange([first, second, second]))
    try:
        ok = run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)
        assert ok.status == "succeeded"
        with pytest.raises(ApprovalRequired) as raised:
            run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)
        resumed = resume_run(
            raised.value.run_id,
            decisions={"delegate": "approve"},
            settings=tmp_settings,
            policy=policy,
        )
        assert resumed.status == "succeeded"
        pins = dict(resumed.metadata.get("a2a_pins") or {})
        assert pins.get("http://partner.example.com") == card_digest(second)
    finally:
        reset_transport(hook)


def test_card_url_other_host_does_not_receive_bearer(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    posts: list[tuple[str, str]] = []
    card = {
        "name": "partner",
        "url": "http://evil.example.com",
        "description": "steal",
    }
    wf = _write(tmp_path / "local.yaml", _DELEGATE_WF)
    hook = use_transport(_scripted_exchange([card], posts=posts))
    try:
        state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    finally:
        reset_transport(hook)
    assert state.status == "succeeded"
    assert posts, "expected an RPC POST to the card url"
    host, auth = posts[0]
    assert host == "evil.example.com"
    assert auth == ""


def test_cli_a2a_probe_json_signature_status(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    card = {"name": "x", "url": "http://partner.example.com", "description": "d"}
    hook = use_transport(_scripted_exchange([card]))
    monkeypatch.setenv("READYAGENTS_A2A_TOKEN", _TOKEN)
    try:
        result = runner.invoke(app, ["a2a", "probe", "http://partner.example.com", "--json"])
    finally:
        reset_transport(hook)
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["command"] == "a2a probe"
    assert data["signatureStatus"] == "unsigned"
    assert data["digest"] == card_digest(card)
    blob = result.stdout + result.stderr
    assert _TOKEN not in blob
    assert "READYAGENTS_A2A_TOKEN" not in blob or data["signatureStatus"] == "unsigned"


def test_cli_a2a_probe_json_signature_verified_and_invalid(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from readyagents.a2a.card import card_signing_body
    from readyagents.decisions.signing import sign_body

    secret = "card-hmac-secret"
    unsigned = {"name": "x", "url": "http://partner.example.com", "description": "d"}
    verified = dict(unsigned)
    verified["signature"] = sign_body(secret, card_signing_body(verified))
    forged = dict(unsigned)
    forged["signature"] = "00" * 32
    monkeypatch.setenv("READYAGENTS_A2A_CARD_SECRET", secret)
    for card, expected in ((verified, "verified"), (forged, "invalid")):
        hook = use_transport(_scripted_exchange([card]))
        try:
            result = runner.invoke(app, ["a2a", "probe", "http://partner.example.com", "--json"])
        finally:
            reset_transport(hook)
        assert result.exit_code == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data["signatureStatus"] == expected
        assert secret not in result.stdout
        assert data["digest"] == card_digest(card)
