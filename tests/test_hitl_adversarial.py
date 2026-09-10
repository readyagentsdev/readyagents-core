"""Adversarial suite for TASK-08 enterprise HITL. Drive shipped code only; fail closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.approvals.channels import notify_channels
from readyagents.approvals.delegate import add_delegation, revoke_delegation
from readyagents.approvals.gate import (
    PauseState,
    Vote,
    evaluate_gate,
    format_clock,
    pause_from_pending,
)
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ApprovalRequired, AuthorizationError, ConfigError
from readyagents.workflow.runner import resume_run, run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import load_run, persist_run

runner = CliRunner()


def _gate_wf(
    tmp: Path,
    *,
    extra: str = "",
    name: str = "adv",
    prompt: str = "Go?",
) -> Path:
    text = f"""
name: {name}
start: g
nodes:
  - id: g
    type: approval
    prompt: {json.dumps(prompt)}
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


class _DenyAuth:
    def check(self, actor: str | None, action: str, resource: str) -> None:
        if action in {"approve", "reject"}:
            raise AuthorizationError(actor, action, resource)


# --- 1. Quorum bypass ---


def test_quorum_casing_variant_cannot_satisfy_2_of_2(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 2\n",
        name="qcase",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as first:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="Alice",
        )
    assert first.value.state.status == "paused"
    assert len(first.value.state.pending["approvals_received"]) == 1
    with pytest.raises(ApprovalRequired) as second:
        resume_run(
            first.value.state.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    assert second.value.state.status == "paused"
    assert len(second.value.state.pending["approvals_received"]) == 1
    loaded = load_run(tmp_settings.runs_dir(), second.value.state.run_id)
    assert loaded.status == "paused"
    assert len(loaded.pending["approvals_received"]) == 1


def test_quorum_replayed_second_vote_stays_paused(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 2\n",
        name="qreplay",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as first:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    with pytest.raises(ApprovalRequired) as replayed:
        resume_run(
            first.value.state.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="alice",
        )
    assert replayed.value.state.status == "paused"
    assert len(replayed.value.state.pending["approvals_received"]) == 1


def test_quorum_decision_file_cannot_count_twice(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 2\n",
        name="qfile",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    decision = tmp_settings.workspace_path() / "double.json"
    # List form that tries to inject two approvals for the same node.
    decision.write_text(
        json.dumps(
            [
                {"node": "g", "decision": "approve"},
                {"node_id": "g", "decision": "approve"},
                {"g": "approve"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as paused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decision_file=decision,
            actor="alice",
        )
    assert paused.value.state.status == "paused"
    assert len(paused.value.state.pending["approvals_received"]) == 1


def test_quorum_tampered_casing_dupes_still_pending(tmp_settings) -> None:
    """Even if the store is stuffed with Alice/alice rows, evaluate_gate dedupes."""
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 2\n",
        name="qtamp",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    state = started.value.state
    pending = dict(state.pending or {})
    stamp = format_clock(datetime.now(UTC))
    pending["approvals_received"] = [
        {"actor": "Alice", "decision": "approve", "at": stamp},
        {"actor": "alice", "decision": "approve", "at": stamp},
        {"actor": "ALICE", "decision": "approve", "at": stamp},
    ]
    state.pending = pending
    persist_run(state, tmp_settings.runs_dir())
    pause = pause_from_pending(pending)
    assert evaluate_gate(pause).status == "pending"
    with pytest.raises(ApprovalRequired) as still:
        resume_run(state.run_id, settings=tmp_settings)
    assert still.value.state.status == "paused"
    # One distinct actor still recorded after reload/eval path.
    received = still.value.state.pending.get("approvals_received") or []
    norms = {(v.get("actor") or "").strip().casefold() for v in received}
    assert "alice" in norms
    assert evaluate_gate(pause_from_pending(still.value.state.pending)).status == "pending"


def test_cli_quorum_casing_stays_paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    path = _gate_wf(tmp_path, extra="    approvals_required: 2\n", name="qcli")
    paused = runner.invoke(app, ["run", str(path)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    listed = runner.invoke(app, ["approvals", "list", "--json"])
    run_id = json.loads(listed.stdout)["approvals"][0]["run_id"]
    first = runner.invoke(
        app, ["decide", run_id, "--node", "g", "--decision", "approve", "--actor", "Alice"]
    )
    assert first.exit_code == 2, first.stdout + first.stderr
    second = runner.invoke(
        app, ["decide", run_id, "--node", "g", "--decision", "approve", "--actor", "alice"]
    )
    assert second.exit_code == 2, second.stdout + second.stderr
    clear_settings_cache()


# --- 2. Expiry-as-approve ---


def test_on_expire_approve_refused_at_schema() -> None:
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
                        "else": "denied",
                        "expires_in": "1h",
                        "on_expire": "approve",
                    },
                    {"id": "ok", "type": "transform", "template": "ok"},
                    {"id": "denied", "type": "transform", "template": "no"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="on_expire: approve is refused"):
        WorkflowSpec.model_validate(
            {
                "name": "t2",
                "start": "g",
                "nodes": [
                    {
                        "id": "g",
                        "type": "approval",
                        "prompt": "x",
                        "then": "ok",
                        "else": "denied",
                        "expires_in": "30m",
                        "on_expire": "APPROVE",
                    },
                    {"id": "ok", "type": "transform", "template": "ok"},
                    {"id": "denied", "type": "transform", "template": "no"},
                ],
            }
        )


def test_evaluate_gate_never_auto_approves_on_expire() -> None:
    now = datetime.now(UTC)
    past = format_clock(now - timedelta(hours=1))
    paused = format_clock(now - timedelta(hours=2))
    for poison in ("approve", "APPROVE", "yes", "true", "ok", ""):
        pause = PauseState(
            approvals_required=1,
            expires_at=past,
            on_expire=poison or None,
            paused_at=paused,
        )
        out = evaluate_gate(pause, now)
        assert out.status != "approved", poison
        assert out.status in {"rejected", "expired", "escalated"}
        if poison.strip().lower() == "approve" or poison in {"yes", "true", "ok", ""}:
            # Unknown / approve collapses to reject (never approve).
            assert out.status == "rejected"
            assert out.reason == "expired"


def test_lazy_expiry_paths_are_reject_fail_escalate_only(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from readyagents.approvals.gate import GateExpired

    future = datetime.now(UTC) + timedelta(hours=2)

    reject_path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    expires_in: 1h\n    on_expire: reject\n",
        name="exrej",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(reject_path, settings=tmp_settings, persist=True)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    state = resume_run(paused.value.state.run_id, settings=tmp_settings)
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "denied"

    fail_path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    expires_in: 1h\n    on_expire: fail\n",
        name="exfail",
    )
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: datetime.now(UTC))
    with pytest.raises(ApprovalRequired) as paused2:
        run_workflow_file(fail_path, settings=tmp_settings, persist=True)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    with pytest.raises(GateExpired):
        resume_run(paused2.value.state.run_id, settings=tmp_settings)

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
    with pytest.raises(ApprovalRequired) as paused3:
        run_workflow_file(esc_path, settings=tmp_settings, persist=True)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    with pytest.raises(ApprovalRequired) as escalated:
        resume_run(paused3.value.state.run_id, settings=tmp_settings)
    assert escalated.value.state.status == "paused"
    assert "role:director" in (escalated.value.state.pending or {}).get("eligible_actors", [])


# --- 3. Delegation widening ---


def test_delegation_self_chain_and_widen_refused(tmp_settings) -> None:
    home = tmp_settings.home_path()
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    add_delegation(from_actor="alice", to_actor="bob", until=until, scope="finance", home=home)
    with pytest.raises(ConfigError, match="self-delegation"):
        add_delegation(from_actor="carol", to_actor="Carol", until=until, home=home)
    with pytest.raises(ConfigError, match="self-delegation"):
        add_delegation(from_actor="dave", to_actor="dave", until=until, home=home)
    # bob is already a delegatee — bob -> carol is a chain.
    with pytest.raises(ConfigError, match="chains"):
        add_delegation(from_actor="bob", to_actor="carol", until=until, home=home)
    # Later unscoped grant would widen the existing scoped grant.
    with pytest.raises(ConfigError, match="widen"):
        add_delegation(from_actor="alice", to_actor="bob", until=until, home=home)


def test_expired_and_revoked_delegation_cannot_approve(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_settings.home_path()
    until = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    grant = add_delegation(
        from_actor="alice", to_actor="bob", until=until, scope="finance", home=home
    )
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approver_roles: [finance]\n",
        name="delexp",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    # Expire the grant before decide time.
    future = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: future)
    with pytest.raises(ApprovalRequired) as refused:
        resume_run(
            started.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="bob",
        )
    assert refused.value.state.status == "paused"

    monkeypatch.setattr("readyagents.approvals.gate.clock_now", lambda: datetime.now(UTC))
    until2 = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    other = add_delegation(
        from_actor="erin", to_actor="frank", until=until2, scope="ops", home=home
    )
    revoke_delegation(other.id, home=home)
    path2 = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approver_roles: [ops]\n",
        name="delrev",
    )
    with pytest.raises(ApprovalRequired) as started2:
        run_workflow_file(path2, settings=tmp_settings, persist=True)
    with pytest.raises(ApprovalRequired) as refused2:
        resume_run(
            started2.value.run_id,
            settings=tmp_settings,
            decisions={"g": "approve"},
            actor="frank",
        )
    assert refused2.value.state.status == "paused"
    assert grant.id


def test_cli_delegation_self_and_chain_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    until = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok = runner.invoke(app, ["delegate", "--from", "alice", "--to", "bob", "--until", until])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    selfed = runner.invoke(app, ["delegate", "--from", "alice", "--to", "Alice", "--until", until])
    assert selfed.exit_code == 1
    chained = runner.invoke(app, ["delegate", "--from", "bob", "--to", "carol", "--until", until])
    assert chained.exit_code == 1
    clear_settings_cache()


# --- 4. Signature / authorization bypass ---


def test_unauthorized_decide_stays_paused_not_failed(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 1\n    require_reason: true\n",
        name="unauth",
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
            vote_reasons={"g": "looks fine"},
        )
    assert refused.value.state.status == "paused"
    assert refused.value.state.status != "failed"
    loaded = load_run(tmp_settings.runs_dir(), refused.value.state.run_id)
    assert loaded.status == "paused"
    votes = loaded.pending.get("approvals_received") or []
    assert votes == [] or len(votes) == 0


def test_unsigned_hmac_decide_does_not_resume(tmp_settings) -> None:
    from readyagents.decisions.signing import verify_signed_body

    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra="    approvals_required: 1\n",
        name="unsign",
    )
    with pytest.raises(ApprovalRequired) as started:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    run_id = started.value.run_id
    body = json.dumps(
        {"run_id": run_id, "node_id": "g", "decision": "approve"},
        separators=(",", ":"),
    ).encode("utf-8")
    with pytest.raises(ValueError, match="unsigned"):
        verify_signed_body("enterprise-hitl-secret", body, None)
    with pytest.raises(ValueError, match="unsigned"):
        verify_signed_body("enterprise-hitl-secret", body, "")
    with pytest.raises(ValueError, match="forged"):
        verify_signed_body("enterprise-hitl-secret", body, "00" * 32)
    # Run must remain paused — signing failure never reaches resume.
    loaded = load_run(tmp_settings.runs_dir(), run_id)
    assert loaded.status == "paused"


def test_cli_unauthorized_pack_authorizer_stays_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorizer that denies approve/reject must leave the enterprise gate paused."""
    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    pack = tmp_path / "deny_pack.py"
    pack.write_text(
        """
from readyagents.packs import BasePack
from readyagents.errors import AuthorizationError

class Deny:
    def check(self, actor, action, resource):
        if action in {"approve", "reject"}:
            raise AuthorizationError(actor, action, resource)

class P(BasePack):
    name = "deny_auth"
    version = "0.0.1"
    def register_authorizers(self):
        return [Deny()]
    def register_tools(self):
        return []
    def register_nodes(self):
        return {}
    def register_workflows(self):
        return []

def get_pack():
    return P()
""",
        encoding="utf-8",
    )
    path = _gate_wf(
        tmp_path,
        extra="    approvals_required: 1\n    require_reason: true\n",
        name="cliunauth",
    )
    paused = runner.invoke(app, ["run", str(path), "--pack", str(pack)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    listed = runner.invoke(app, ["approvals", "list", "--json"])
    run_id = json.loads(listed.stdout)["approvals"][0]["run_id"]
    decide = runner.invoke(
        app,
        [
            "decide",
            run_id,
            "--node",
            "g",
            "--decision",
            "approve",
            "--actor",
            "alice",
            "--reason",
            "nope",
            "--pack",
            str(pack),
        ],
    )
    # Still paused (exit 2) — not failed.
    assert decide.exit_code == 2, decide.stdout + decide.stderr
    clear_settings_cache()


# --- 5. Notification leakage ---


def test_notify_payload_strips_secrets_outputs_and_bounds(tmp_settings) -> None:
    dest = tmp_settings.workspace_path() / "pending.jsonl"
    secret_prompt = "Approve wire with api_key=sk-live-LEAKME999 password=hunter2 " + ("X" * 500)
    # Precede the gate with a node output that must never appear in notify payload.
    fat = tmp_settings.workspace_path() / "leakfile.yaml"
    fat.write_text(
        f"""
name: leakfile
start: prep
nodes:
  - id: prep
    type: transform
    template: "SECRET_NODE_OUTPUT_should_never_leak api_key=sk-live-NODE99"
    output_key: secret_blob
    next: g
  - id: g
    type: approval
    prompt: {json.dumps(secret_prompt)}
    then: ok
    else: denied
    notify:
      - kind: file
        path: {dest.name}
  - id: ok
    type: transform
    template: "ok"
    output_key: summary
  - id: denied
    type: transform
    template: "denied"
    output_key: summary
""",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(fat, settings=tmp_settings, persist=True)
    assert paused.value.state.status == "paused"
    assert dest.is_file()
    row = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    blob = json.dumps(row)
    assert row["event"] == "approval_required"
    assert "run_id" in row and "node_id" in row
    assert "question" in row
    assert "node_outputs" not in row
    assert "secret_blob" not in blob
    assert "SECRET_NODE_OUTPUT" not in blob
    assert "sk-live-LEAKME999" not in blob
    assert "sk-live-NODE99" not in blob
    assert "hunter2" not in blob or "[REDACTED]" in row["question"]
    assert len(row["question"]) <= 200
    assert len(blob) <= 4096


def test_notify_channels_direct_redaction_and_bounds(tmp_settings) -> None:
    dest = tmp_settings.workspace_path() / "direct.jsonl"
    huge = "token=ghp_LEAKTOKEN12345 " + ("P" * 1000)
    notify_channels(
        [{"kind": "file", "path": str(dest)}],
        run_id="r1",
        node_id="g",
        prompt=huge,
        eligible=["role:finance"],
        expires_at=None,
        workspace=tmp_settings.workspace_path(),
    )
    row = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    assert "ghp_LEAKTOKEN12345" not in json.dumps(row)
    assert len(row["question"]) <= 200
    assert set(row) <= {
        "event",
        "run_id",
        "node_id",
        "question",
        "eligible_roles",
        "deadline",
    }


def test_channel_failure_leaves_run_paused(tmp_settings) -> None:
    path = _gate_wf(
        tmp_settings.workspace_path(),
        extra=(
            "    notify:\n"
            "      - kind: webhook\n"
            "        url: http://127.0.0.1:1/nope\n"
            "      - kind: command\n"
            "        command: [/no/such/readyagents-notify-bin]\n"
        ),
        name="chfail",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    assert paused.value.state.status == "paused"
    loaded = load_run(tmp_settings.runs_dir(), paused.value.state.run_id)
    assert loaded.status == "paused"


def test_evaluate_gate_quorum_table_rejects_single_actor_dupes() -> None:
    pause = PauseState(
        approvals_required=2,
        distinct_actors=True,
        approvals_received=[
            Vote(actor="Alice", decision="approve", at="t"),
            Vote(actor="alice", decision="approve", at="t"),
        ],
    )
    assert evaluate_gate(pause).status == "pending"
    pause.approvals_received.append(Vote(actor="bob", decision="approve", at="t"))
    assert evaluate_gate(pause).status == "approved"
