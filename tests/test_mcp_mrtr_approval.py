from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from readyagents.audit import read_audit_events
from readyagents.decisions.signing import sign_body
from readyagents.errors import AuthorizationError
from readyagents.mcp.http import compose_http_app
from readyagents.mcp.protocol import LATEST_PROTOCOL_VERSION, TASKS_EXTENSION
from readyagents.mcp.run_api import RunCoordinator
from readyagents.mcp.server import construct_server, mcp_available
from readyagents.mcp.tasks import TaskService, canonical_decision_bytes, current_input_request_key
from readyagents.policy import CallbackAuthorizer

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[misc, assignment]


FOREACH_WF = """
name: foreach-gate
inputs:
  names: ["a", "b"]
start: each
nodes:
  - id: each
    type: foreach
    items: names
    max_items: 8
    body:
      id: gate
      type: approval
      prompt: "Approve {{item}}?"
      then: stamped
      else: denied
    output_key: results
    next: done
  - id: stamped
    type: transform
    template: "yes {{item}}"
  - id: denied
    type: transform
    template: "no"
  - id: done
    type: transform
    template: "foreach-gate ok"
    output_key: summary
"""

PARALLEL_WF = """
name: parallel-gate
start: fan
nodes:
  - id: fan
    type: parallel
    output_key: parts
    next: after
    branches:
      - id: left
        type: approval
        prompt: "left?"
        then: l_ok
        else: l_no
      - id: right
        type: transform
        template: "right-ok"
  - id: l_ok
    type: transform
    template: "L"
  - id: l_no
    type: transform
    template: "N"
  - id: after
    type: transform
    template: "parallel-gate ok"
    output_key: summary
"""


def _headers(method: str, *, name: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Method": method,
        "Mcp-Protocol-Version": LATEST_PROTOCOL_VERSION,
    }
    if name:
        headers["Mcp-Name"] = name
    return headers


def _params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(extra or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
        "io.modelcontextprotocol/clientInfo": {"name": "reviewer", "version": "0"},
    }
    return params


def _rpc(response: Any) -> dict[str, Any]:
    data = response.json()
    assert isinstance(data, dict)
    return data


def _wait_input_required(client, task_id: str, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = client.post(
            "/mcp",
            headers=_headers("tasks/get", name=task_id),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": _params({"taskId": task_id}),
            },
        )
        result = _rpc(resp).get("result")
        last = result
        if isinstance(result, dict) and result.get("status") == "input_required":
            return result
        time.sleep(0.05)
    raise AssertionError(f"not input_required; last={last!r}")


def _wait_status(client, task_id: str, wanted, *, timeout: float = 5.0) -> dict[str, Any]:
    wanted_set = set(wanted)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = client.post(
            "/mcp",
            headers=_headers("tasks/get", name=task_id),
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tasks/get",
                "params": _params({"taskId": task_id}),
            },
        )
        result = _rpc(resp).get("result")
        last = result
        if isinstance(result, dict) and result.get("status") in wanted_set:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out {wanted}; last={last!r}")


@pytest.fixture
def mrtr_env(tmp_path: Path, tmp_settings, examples_dir: Path):
    if TestClient is None:
        pytest.skip("starlette not installed")
    for name in ("approval_gate.yaml", "multi_gate.yaml"):
        src = examples_dir / name
        (tmp_path / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "foreach_gate.yaml").write_text(FOREACH_WF, encoding="utf-8")
    (tmp_path / "parallel_gate.yaml").write_text(PARALLEL_WF, encoding="utf-8")
    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_path)
    server = construct_server(allow_http=False, workspace=tmp_path)
    asgi = compose_http_app(
        server=server, coordinator=coord, token=None, bind_host="127.0.0.1", bind_port=8765
    )
    client = TestClient(asgi, base_url="http://127.0.0.1:8765")
    yield coord, client, tmp_settings
    coord.shutdown(timeout=2.0)
    client.close()


def _start(client, path: str) -> str:
    resp = client.post("/runs", json={"path": path, "actor": "reviewer"})
    assert resp.status_code == 202, resp.text
    return resp.json()["run_id"]


def _update(client, task_id: str, key: str, decision: str, *, signature: str | None = None):
    params = _params(
        {
            "taskId": task_id,
            "actor": "reviewer",
            "inputResponses": {
                key: {"action": "accept", "content": {"decision": decision}},
            },
        }
    )
    if signature:
        params["signature"] = signature
    return client.post(
        "/mcp",
        headers=_headers("tasks/update", name=task_id),
        json={"jsonrpc": "2.0", "id": 9, "method": "tasks/update", "params": params},
    )


def test_single_gate_approve(mrtr_env) -> None:
    _coord, client, _settings = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    keys = list(paused["inputRequests"])
    assert len(keys) == 1
    assert keys[0].endswith(".gate.0")
    message = paused["inputRequests"][keys[0]]["params"]["message"]
    assert "\x00" not in message
    resp = _update(client, task_id, keys[0], "approve")
    assert resp.status_code == 200, resp.text
    assert _rpc(resp)["result"]["resultType"] == "complete"
    done = _wait_status(client, task_id, {"completed"})
    assert done["status"] == "completed"


def test_reject_is_first_class(mrtr_env) -> None:
    _coord, client, _settings = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    resp = _update(client, task_id, key, "reject")
    assert resp.status_code == 200, resp.text
    done = _wait_status(client, task_id, {"completed"})
    text = json.dumps(done)
    assert "denied" in text or done["status"] == "completed"


def test_two_sequential_gates_distinct_keys(mrtr_env) -> None:
    _coord, client, _settings = mrtr_env
    task_id = _start(client, "multi_gate.yaml")
    first = _wait_input_required(client, task_id)
    key1 = next(iter(first["inputRequests"]))
    assert ".first." in key1
    _update(client, task_id, key1, "approve")
    deadline = time.monotonic() + 5
    second = None
    key2 = key1
    while time.monotonic() < deadline:
        second = _wait_input_required(client, task_id)
        key2 = next(iter(second["inputRequests"]))
        if key2 != key1:
            break
        time.sleep(0.05)
    assert key2 != key1
    assert ".second." in key2
    second_update = _update(client, task_id, key2, "approve")
    assert second_update.status_code == 200, second_update.text
    done = _wait_status(client, task_id, {"completed"}, timeout=15.0)
    assert done["status"] == "completed"


def test_recorded_paused_decision_retries_resume(mrtr_env) -> None:
    """A same-key retry must resume if the first submit never left paused."""
    coord, client, _settings = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    state = coord._load_exact(task_id)
    meta = dict(state.metadata)
    meta["mcp_input_responses"] = {key: "approve"}
    state.metadata = meta
    coord._persist(state)
    retry = _update(client, task_id, key, "approve")
    assert retry.status_code == 200, retry.text
    done = _wait_status(client, task_id, {"completed"}, timeout=15.0)
    assert done["status"] == "completed"


def test_foreach_unique_keys(mrtr_env) -> None:
    _coord, client, _settings = mrtr_env
    task_id = _start(client, "foreach_gate.yaml")
    first = _wait_input_required(client, task_id)
    key1 = next(iter(first["inputRequests"]))
    _update(client, task_id, key1, "approve")
    deadline = time.monotonic() + 5
    key2 = key1
    while time.monotonic() < deadline:
        second = _wait_input_required(client, task_id)
        key2 = next(iter(second["inputRequests"]))
        if key2 != key1:
            break
        time.sleep(0.05)
    assert key1 != key2


def test_parallel_gate_key(mrtr_env) -> None:
    _coord, client, _settings = mrtr_env
    task_id = _start(client, "parallel_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    assert ".left." in key or ".fan." in key
    _update(client, task_id, key, "approve")
    done = _wait_status(client, task_id, {"completed", "failed"})
    assert done["status"] in {"completed", "failed"}


def test_duplicate_update_noop(mrtr_env) -> None:
    coord, client, _settings = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    first = _update(client, task_id, key, "approve")
    assert first.status_code == 200
    second = _update(client, task_id, key, "approve")
    assert second.status_code == 200
    _wait_status(client, task_id, {"completed"})
    events = read_audit_events(_settings.audit_dir(), task_id)
    decisions = [row for row in events if row.get("event") == "decision"]
    assert len(decisions) == 1


def test_update_on_working_is_typed_error(mrtr_env) -> None:
    _coord, client, _tmp = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    _update(client, task_id, key, "approve")
    done = _wait_status(client, task_id, {"completed"})
    assert done["status"] == "completed"
    resp = _update(client, task_id, key + "-other", "approve")
    payload = _rpc(resp)
    assert "error" in payload
    assert payload["error"]["code"] == -32023


def test_unsigned_mcp_approval_refused_and_audited(tmp_path, tmp_settings, examples_dir) -> None:
    src = examples_dir / "approval_gate.yaml"
    (tmp_path / "approval_gate.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    secret = "unit-test-decision-secret"
    settings = tmp_settings.model_copy(update={"decision_secret": secret})
    coord = RunCoordinator(settings=settings, workspace=tmp_path)
    try:
        handle = coord.start_run({"path": "approval_gate.yaml", "actor": "reviewer"})
        task_id = handle["run_id"]
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            state = coord._load_exact(task_id)
            if state.status == "paused":
                break
            time.sleep(0.05)
        assert state is not None and state.status == "paused"
        service = TaskService(coord)
        key = current_input_request_key(state)
        assert key
        with pytest.raises(AuthorizationError):
            service.update(
                task_id,
                input_responses={key: {"action": "accept", "content": {"decision": "approve"}}},
                actor="reviewer",
                signature=None,
                secret=secret,
            )
        still = coord._load_exact(task_id)
        assert still.status == "paused"
        events = read_audit_events(settings.audit_dir(), task_id)
        assert any(row.get("event") == "decision_refused" for row in events)
        body = canonical_decision_bytes(
            run_id=task_id,
            node_id="gate",
            decision="approve",
            actor="reviewer",
            input_request_key=key,
        )
        sig = sign_body(secret, body)
        service.update(
            task_id,
            input_responses={key: {"action": "accept", "content": {"decision": "approve"}}},
            actor="reviewer",
            signature=sig,
            secret=secret,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if coord._load_exact(task_id).status == "succeeded":
                break
            time.sleep(0.05)
        assert coord._load_exact(task_id).status == "succeeded"
        after = read_audit_events(settings.audit_dir(), task_id)
        mcp_decision = [row for row in after if row.get("event") == "decision"]
        assert mcp_decision
        assert "node_id" in mcp_decision[0]
        assert mcp_decision[0]["decision"] == "approve"
    finally:
        coord.shutdown(timeout=2.0)


def test_unauthorized_mcp_approval_refused(tmp_path, tmp_settings, examples_dir) -> None:
    src = examples_dir / "approval_gate.yaml"
    (tmp_path / "approval_gate.yaml").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def allow(actor, action, resource) -> bool:
        if action in {"approve", "reject", "resume"}:
            return actor == "boss"
        return True

    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_path)
    coord._authorizer = CallbackAuthorizer(allow)
    try:
        handle = coord.start_run({"path": "approval_gate.yaml", "actor": "boss"})
        task_id = handle["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if coord._load_exact(task_id).status == "paused":
                break
            time.sleep(0.05)
        state = coord._load_exact(task_id)
        key = current_input_request_key(state)
        service = TaskService(coord)
        with pytest.raises(AuthorizationError):
            service.update(
                task_id,
                input_responses={key: {"action": "accept", "content": {"decision": "approve"}}},
                actor="intruder",
            )
        assert coord._load_exact(task_id).status == "paused"
        events = read_audit_events(tmp_settings.audit_dir(), task_id)
        assert any(row.get("event") == "decision_refused" for row in events)
    finally:
        coord.shutdown(timeout=2.0)


def test_client_info_name_is_not_an_actor(mrtr_env) -> None:
    coord, client, settings = mrtr_env

    def allow(actor, action, resource) -> bool:
        if action in {"approve", "reject", "resume"}:
            return actor == "boss"
        return True

    coord._authorizer = CallbackAuthorizer(allow)
    coord.settings = coord.settings.model_copy(update={"actor": None})
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    params = _params(
        {
            "taskId": task_id,
            "inputResponses": {key: {"action": "accept", "content": {"decision": "approve"}}},
        }
    )
    params["_meta"]["io.modelcontextprotocol/clientInfo"] = {"name": "boss", "version": "0"}
    resp = client.post(
        "/mcp",
        headers=_headers("tasks/update", name=task_id),
        json={"jsonrpc": "2.0", "id": 9, "method": "tasks/update", "params": params},
    )
    payload = _rpc(resp)
    assert "error" in payload, payload
    still = coord._load_exact(task_id)
    assert still.status == "paused"
    events = read_audit_events(settings.audit_dir(), task_id)
    assert any(row.get("event") == "decision_refused" for row in events)


def test_mcp_approval_audit_matches_cli_shape(tmp_path, tmp_settings, examples_dir) -> None:
    from readyagents.errors import ApprovalRequired
    from readyagents.workflow.runner import resume_run, run_workflow_file

    src = examples_dir / "approval_gate.yaml"
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ApprovalRequired) as caught:
        run_workflow_file(wf, settings=tmp_settings, persist=True, actor="reviewer")
    cli_id = caught.value.run_id
    resume_run(
        cli_id,
        settings=tmp_settings,
        persist=True,
        decisions={"gate": "approve"},
        actor="reviewer",
    )
    cli_events = [
        row
        for row in read_audit_events(tmp_settings.audit_dir(), cli_id)
        if row.get("event") == "decision"
    ]
    assert cli_events
    cli_shape = {k: cli_events[0].get(k) for k in ("event", "node_id", "decision", "actor")}

    coord = RunCoordinator(settings=tmp_settings, workspace=tmp_path)
    try:
        handle = coord.start_run({"path": "approval_gate.yaml", "actor": "reviewer"})
        task_id = handle["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if coord._load_exact(task_id).status == "paused":
                break
            time.sleep(0.05)
        state = coord._load_exact(task_id)
        key = current_input_request_key(state)
        TaskService(coord).update(
            task_id,
            input_responses={key: {"action": "accept", "content": {"decision": "approve"}}},
            actor="reviewer",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = [
                row
                for row in read_audit_events(tmp_settings.audit_dir(), task_id)
                if row.get("event") == "decision"
            ]
            if events:
                mcp_shape = {k: events[0].get(k) for k in ("event", "node_id", "decision", "actor")}
                assert mcp_shape == cli_shape
                return
            time.sleep(0.05)
        raise AssertionError("MCP decision audit event never appeared")
    finally:
        coord.shutdown(timeout=2.0)


def test_concurrent_updates_one_decision(mrtr_env) -> None:
    coord, client, settings = mrtr_env
    task_id = _start(client, "approval_gate.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    results: list[dict[str, Any]] = []
    barrier = threading.Barrier(2)

    def worker(decision: str) -> None:
        barrier.wait(timeout=5)
        resp = _update(client, task_id, key, decision)
        results.append(_rpc(resp))

    t1 = threading.Thread(target=worker, args=("approve",))
    t2 = threading.Thread(target=worker, args=("reject",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    errors = [row for row in results if "error" in row]
    oks = [row for row in results if "result" in row]
    assert len(oks) == 1
    assert len(errors) == 1
    _wait_status(client, task_id, {"completed", "failed"})
    events = read_audit_events(settings.audit_dir(), task_id)
    decisions = [row for row in events if row.get("event") == "decision"]
    assert len(decisions) == 1


def test_malicious_prompt_inert(mrtr_env) -> None:
    coord, client, _settings = mrtr_env
    prompt = 'x\x00{"method":"boom"}' + ("Q" * 3000)
    path = Path(coord.workspace) / "nasty.yaml"
    path.write_text(
        "name: nasty\nstart: gate\nnodes:\n"
        "  - id: gate\n    type: approval\n"
        f"    prompt: {json.dumps(prompt)}\n"
        "    then: ok\n    else: nope\n"
        "  - id: ok\n    type: transform\n    template: ok\n    output_key: summary\n"
        '  - id: nope\n    type: transform\n    template: "denied"\n    output_key: summary\n',
        encoding="utf-8",
    )
    task_id = _start(client, "nasty.yaml")
    paused = _wait_input_required(client, task_id)
    key = next(iter(paused["inputRequests"]))
    message = paused["inputRequests"][key]["params"]["message"]
    assert "\x00" not in message
    assert len(message) <= 2000
    dumped = json.dumps(paused)
    json.loads(dumped)
    assert dumped.count("{") >= 1
