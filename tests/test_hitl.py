"""Enterprise HITL: quorum, SoD, lazy expiry, delegation. Drive shipped runner/CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.approvals.gate import evaluate_gate, pause_from_pending
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ApprovalRequired
from readyagents.workflow.runner import resume_run, run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState

runner = CliRunner()


def _gate_wf(
    tmp: Path,
    *,
    extra: str = "",
    name: str = "q",
) -> Path:
    text = f"""
name: {name}
start: g
nodes:
  - id: g
    type: approval
    prompt: "Go?"
    then: ok
    else: denied
{extra}
  - id: ok
    type: transform
    template: "ok"
    output_key: summary
  - id: denied
    type: transform
    template: "denied"
    output_key: summary
"""
    path = tmp / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_on_expire_approve_fails_schema_validation() -> None:
    with pytest.raises(ValidationError, match="on_expire: approve is refused"):
        WorkflowSpec.model_validate(
            {
                "name": "t",
                "start": "g",
                "nodes": [
                    {
                        "id": "g",
                        "type": "approval",
                        "prompt": "x",
                        "then": "ok",
                        "else": "no",
                        "expires_in": "1h",
                        "on_expire": "approve",
                    },
                    {"id": "ok", "type": "transform", "template": "ok"},
                    {"id": "no", "type": "transform", "template": "no"},
                ],
            }
        )


def test_quorum_2_of_2_and_crash_reload(tmp_settings) -> None:
    path = _gate_wf(tmp_settings.workspace_path(), extra="    approvals_required: 2\n")
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as first:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    paused = first.value.state
    assert paused.status == "paused"
    assert len(paused.pending["approvals_received"]) == 1
    record = paused.to_record()
    reloaded = RunState.from_record(record)
    assert len(reloaded.pending["approvals_received"]) == 1
    state = resume_run(
        paused.run_id, settings=tmp_settings, decisions={"g": "approve"}, actor="bob"
    )
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "ok"
    assert len(state.node_outputs["g"]["approvals_received"]) == 2


def test_quorum_2_of_3_reject_short_circuit(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 3\n",
        name="r",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = resume_run(
        started.value.run_id,
        settings=tmp_settings,
        decisions={"g": "reject"},
        actor="alice",
    )
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "denied"


def test_distinct_actors_casing(tmp_settings) -> None:
    path = _gate_wf(tmp_settings.workspace_path(), extra="    approvals_required: 2\n", name="d")
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as first:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="Alice",
        )
    assert len(first.value.state.pending["approvals_received"]) == 1
    with pytest.raises(ApprovalRequired) as second:
        resume_run(
            first.value.state.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    assert len(second.value.state.pending["approvals_received"]) == 1


def test_legacy_pause_without_new_fields_resumes(tmp_settings) -> None:
    path = _gate_wf(tmp_settings.workspace_path(), name="legacy")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    pending = paused.value.state.pending
    assert "approvals_required" not in pending
    state = resume_run(paused.value.state.run_id, settings=tmp_settings, decisions={"g": "approve"})
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "ok"


def test_require_reason_stays_paused(tmp_settings) -> None:
    path = _gate_wf(tmp_settings.workspace_path(), extra="    require_reason: true\n", name="rr")
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    assert refused.value.state.status == "paused"
    assert refused.value.state.pending.get("approvals_received") in ([], None) or (
        len(refused.value.state.pending.get("approvals_received") or []) == 0
    )
    state = resume_run(
        refused.value.state.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="alice",
        vote_reasons={"g": "checked invoice"},
    )
    assert state.status == "succeeded"


def test_lazy_expiry_reject_on_resume(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    expires_in: 1h\n    on_expire: reject\n",
        name="ex",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    future = datetime.now(UTC) + timedelta(hours=2)

    def _now() -> datetime:
        return future

    monkeypatch.setattr("readyagents.approvals.gate.clock_now", _now)
    state = resume_run(paused.value.state.run_id, settings=tmp_settings)
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "denied"


def test_evaluate_gate_table() -> None:
    pause = pause_from_pending(
        {
            "approvals_required": 2,
            "approvals_received": [
                {"actor": "a", "decision": "approve", "at": "t"},
                {"actor": "b", "decision": "approve", "at": "t"},
            ],
            "distinct_actors": True,
        }
    )
    assert evaluate_gate(pause).status == "approved"
    pause = pause_from_pending(
        {
            "approvals_required": 2,
            "approvals_received": [{"actor": "a", "decision": "reject", "at": "t"}],
        }
    )
    assert evaluate_gate(pause).status == "rejected"


def test_cli_quorum_and_approvals_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    path = _gate_wf(tmp_path, extra="    approvals_required: 2\n", name="cliq")
    paused = runner.invoke(app, ["run", str(path)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    listed = runner.invoke(app, ["approvals", "list", "--json"])
    assert listed.exit_code == 0, listed.stdout
    payload = json.loads(listed.stdout)
    assert payload["ok"] is True
    assert "approvals" in payload
    listed2 = runner.invoke(app, ["approvals", "list", "--json"])
    payload2 = json.loads(listed2.stdout)
    assert payload2["ok"] is True
    assert list(payload2["approvals"][0]) == list(payload["approvals"][0])
    assert payload2["approvals"][0]["run_id"] == payload["approvals"][0]["run_id"]
    run_id = payload["approvals"][0]["run_id"]
    first = runner.invoke(
        app, ["decide", run_id, "--node", "g", "--decision", "approve", "--actor", "alice"]
    )
    assert first.exit_code == 2, first.stdout + first.stderr
    second = runner.invoke(
        app, ["decide", run_id, "--node", "g", "--decision", "approve", "--actor", "bob"]
    )
    assert second.exit_code == 0, second.stdout + second.stderr
    assert "ok" in second.stdout
    clear_settings_cache()


def test_cli_delegate_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = runner.invoke(
        app, ["delegate", "--from", "alice", "--to", "bob", "--until", until, "--json"]
    )
    assert added.exit_code == 0, added.stdout + added.stderr
    listed = runner.invoke(app, ["delegations", "list", "--json"])
    assert listed.exit_code == 0
    body = json.loads(listed.stdout)
    assert body["ok"] is True
    assert body["delegations"]
    listed2 = runner.invoke(app, ["delegations", "list", "--json"])
    assert json.loads(listed2.stdout)["delegations"] == body["delegations"]
    selfed = runner.invoke(app, ["delegate", "--from", "alice", "--to", "Alice", "--until", until])
    assert selfed.exit_code == 1
    clear_settings_cache()


def test_channel_failure_does_not_change_run(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra=("    notify:\n      - kind: webhook\n        url: http://127.0.0.1:1/nope\n"),
        name="ch",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    assert paused.value.state.status == "paused"


def test_file_channel_writes_redacted_payload(tmp_settings) -> None:
    root = tmp_settings.workspace_path()
    dest = root / "pending.jsonl"
    path = _gate_wf(
        root,
        extra=f"    notify:\n      - kind: file\n        path: {dest.name}\n",
        name="filech",
    )
    with pytest.raises(ApprovalRequired):
        run_workflow_file(path, settings=tmp_settings, persist=False)
    assert dest.is_file()
    row = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    assert row["event"] == "approval_required"
    assert "question" in row
    assert "prompt" not in row or row.get("prompt") is None
    assert "node_outputs" not in row
    assert len(json.dumps(row)) <= 4096


class _RoleAuth:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping

    def check(self, actor: str | None, action: str, resource: str) -> None:
        return None

    def roles_for(self, actor: str | None) -> list[str]:
        return list(self.mapping.get(str(actor or ""), []))


class _DenyAuth:
    def check(self, actor: str | None, action: str, resource: str) -> None:
        from readyagents.errors import AuthorizationError

        if action in {"approve", "reject"}:
            raise AuthorizationError(actor, action, resource)


def test_hitl_fields_refused_on_non_approval() -> None:
    with pytest.raises(ValidationError, match="only valid on approval"):
        WorkflowSpec.model_validate(
            {
                "name": "t",
                "start": "x",
                "nodes": [
                    {
                        "id": "x",
                        "type": "transform",
                        "template": "x",
                        "approvals_required": 2,
                    }
                ],
            }
        )


def test_roles_any_and_all(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra=(
            "    approvals_required: 1\n    approver_roles: [security, finance]\n    require: any\n"
        ),
        name="anyr",
    )
    auth = _RoleAuth({"alice": ["security"], "bob": ["ops"]})
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True, actor="runner")
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="bob",
            authorizer=auth,
        )
    assert refused.value.state.status == "paused"
    state = resume_run(
        refused.value.state.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="alice",
        authorizer=auth,
    )
    assert state.status == "succeeded"
    pending_pause = None
    path2 = _gate_wf(
        tmp_settings.workspace_path(),
        extra=(
            "    approvals_required: 2\n    approver_roles: [security, finance]\n    require: all\n"
        ),
        name="allr",
    )
    auth2 = _RoleAuth({"alice": ["security"], "bob": ["finance"]})
    with pytest.raises(ApprovalRequired) as started2:
        run_workflow_file(path2, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as first:
        resume_run(
            started2.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
            authorizer=auth2,
        )
    pending_pause = first.value.state.pending
    assert "role:security" in pending_pause.get("eligible_actors", [])
    state = resume_run(
        first.value.state.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="bob",
        authorizer=auth2,
    )
    assert state.status == "succeeded"


def test_deny_actor_initiator(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra='    deny_actor: ["$initiator"]\n',
        name="sod",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True, actor="mallory")
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="mallory",
        )
    assert refused.value.state.status == "paused"
    state = resume_run(
        refused.value.state.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="alice",
    )
    assert state.status == "succeeded"


def test_lazy_expiry_fail_and_escalate(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.approvals.gate import GateExpired

    fail_path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    expires_in: 1h\n    on_expire: fail\n",
        name="exfail",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(fail_path, settings=tmp_settings, persist=True)
    future = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    with pytest.raises(GateExpired) as failed:
        resume_run(paused.value.state.run_id, settings=tmp_settings)
    assert failed.value.state is not None
    assert failed.value.state.status == "failed"

    esc_path = _gate_wf(
        tmp_settings.workspace_path(),
        extra=(
            "    expires_in: 1h\n"
            "    on_expire: escalate\n"
            "    escalate_to: [director]\n"
            "    approver_roles: [ops]\n"
        ),
        name="exesc",
    )
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: datetime.now(UTC))
    with pytest.raises(ApprovalRequired) as paused2:
        run_workflow_file(esc_path, settings=tmp_settings, persist=True)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    with pytest.raises(ApprovalRequired) as escalated:
        resume_run(paused2.value.state.run_id, settings=tmp_settings)
    assert escalated.value.state.status == "paused"
    assert "role:director" in (escalated.value.state.pending or {}).get("eligible_actors", [])
    with pytest.raises(GateExpired):
        resume_run(escalated.value.state.run_id, settings=tmp_settings)


def test_lazy_expiry_on_status_query(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.approvals.queue import fire_lazy_expiry

    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    expires_in: 1h\n    on_expire: reject\n",
        name="exstat",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    future = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    state = fire_lazy_expiry(paused.value.state, settings=tmp_settings)
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "denied"


def test_delegation_at_decide_time(tmp_settings) -> None:
    from readyagents.approvals.delegate import add_delegation, revoke_delegation
    from readyagents.errors import ConfigError

    home = tmp_settings.home_path()
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    grant = add_delegation(
        from_actor="alice", to_actor="bob", until=until, scope="finance", home=home
    )
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approver_roles: [finance]\n",
        name="delg",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = resume_run(
        started.value.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="bob",
    )
    assert state.status == "succeeded"
    votes = state.node_outputs["g"]["approvals_received"]
    assert votes[0]["delegated_from"] == "alice"

    with pytest.raises(ConfigError, match="self-delegation"):
        add_delegation(from_actor="alice", to_actor="Alice", until=until, home=home)
    with pytest.raises(ConfigError, match="chains"):
        add_delegation(from_actor="bob", to_actor="carol", until=until, home=home)
    with pytest.raises(ConfigError, match="widen"):
        add_delegation(from_actor="alice", to_actor="bob", until=until, home=home)

    past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ConfigError, match="future"):
        add_delegation(from_actor="erin", to_actor="frank", until=past, home=home)

    until2 = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    other = add_delegation(from_actor="gina", to_actor="hank", until=until2, scope="ops", home=home)
    revoke_delegation(other.id, home=home)
    path2 = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approver_roles: [ops]\n",
        name="delrev",
    )
    with pytest.raises(ApprovalRequired) as started2:
        run_workflow_file(path2, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started2.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="hank",
        )
    assert refused.value.state.status == "paused"
    assert grant.id


def test_scoped_delegation_succeeds_when_actor_holds_other_roles(tmp_settings) -> None:
    """A finance grant still matches a finance gate when bob's RBAC role is ops."""
    from readyagents.approvals.delegate import add_delegation

    home = tmp_settings.home_path()
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    add_delegation(from_actor="alice", to_actor="bob", until=until, scope="finance", home=home)
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approver_roles: [finance]\n",
        name="delops",
    )
    auth = _RoleAuth({"bob": ["ops"]})
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = resume_run(
        started.value.run_id,
        settings=tmp_settings,
        decisions={"g": "approve"},
        actor="bob",
        authorizer=auth,
    )
    assert state.status == "succeeded"
    vote = state.node_outputs["g"]["approvals_received"][0]
    assert vote["delegated_from"] == "alice"
    assert vote["actor"] == "bob"


def test_unauthorized_stays_paused(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(), extra="    require_reason: true\n", name="unauth"
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
            authorizer=_DenyAuth(),
            vote_reasons={"g": "ok"},
        )
    assert refused.value.state.status == "paused"


def test_legacy_09_pause_payload_resumes(tmp_settings) -> None:
    path = _gate_wf(tmp_settings.workspace_path(), name="era09")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = paused.value.state
    state.pending = {
        "node_id": "g",
        "type": "approval",
        "prompt": "Go?",
        "then": "ok",
        "else": "no",
        "resume": "readyagents resume x --approve g",
        "decide": "readyagents decide x --node g --decision approve",
    }
    result = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        resume_state=state,
        decisions={"g": "approve"},
    )
    assert result.status == "succeeded"


def test_evaluate_gate_expiry_and_quorum_table() -> None:
    from readyagents.approvals.gate import PauseState, Vote, evaluate_gate, format_clock

    now = datetime.now(UTC)
    past = format_clock(now - timedelta(hours=1))
    pause = PauseState(
        approvals_required=1,
        expires_at=past,
        on_expire="reject",
        paused_at=format_clock(now - timedelta(hours=2)),
    )
    out = evaluate_gate(pause, now)
    assert out.status == "rejected"
    assert out.reason == "expired"
    assert out.elapsed_seconds is not None
    pause.on_expire = "fail"
    assert evaluate_gate(pause, now).status == "expired"
    pause.on_expire = "escalate"
    assert evaluate_gate(pause, now).status == "escalated"
    pause.escalated_at = past
    assert evaluate_gate(pause, now).status == "expired"
    pause.expires_at = None
    pause.approvals_received = [
        Vote(actor="a", decision="approve", at="t"),
        Vote(actor="A", decision="approve", at="t"),
    ]
    pause.approvals_required = 2
    pause.distinct_actors = True
    assert evaluate_gate(pause, now).status == "pending"


def test_approvals_list_unauthorized_looks_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    path = _gate_wf(
        tmp_path,
        extra="    approver_roles: [finance]\n",
        name="qvis",
    )
    paused = runner.invoke(app, ["run", str(path)])
    assert paused.exit_code == 2, paused.stdout
    hidden = runner.invoke(app, ["approvals", "list", "--actor", "eve", "--json"])
    assert hidden.exit_code == 0
    body = json.loads(hidden.stdout)
    assert body["approvals"] == []
    missing = runner.invoke(app, ["approvals", "list", "--actor", "nobody-here", "--json"])
    assert json.loads(missing.stdout)["approvals"] == []
    visible = runner.invoke(app, ["approvals", "list", "--role", "finance", "--json"])
    assert json.loads(visible.stdout)["approvals"]
    clear_settings_cache()


def test_recommendation_override_recorded(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    recommendation: approve\n",
        name="ov",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = resume_run(
        started.value.run_id,
        settings=tmp_settings,
        decisions={"g": "reject"},
        actor="alice",
    )
    assert state.status == "succeeded"
    assert state.node_outputs["g"].get("override") is True
    assert state.node_outputs["g"]["approvals_received"][0]["override"] is True


def test_command_channel_failure_isolated(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra=(
            "    notify:\n      - kind: command\n        command: [/no/such/readyagents-notify]\n"
        ),
        name="cmdch",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    assert paused.value.state.status == "paused"
