"""Adversarial suite for V2-13 long-horizon waits. Fail closed; no theater."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from readyagents.errors import (
    ApprovalRequired,
    WaitCapExceeded,
    WaitError,
    WaitEventRefused,
    WaitingRequired,
    WaitPathDenied,
)
from readyagents.tools import ToolRegistry
from readyagents.wait.evaluate import WaitWorld
from readyagents.wait.events import accept_event, list_events, sign_event
from readyagents.wait.wake import wake_one
from readyagents.wait.world import inspect_file
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import persist_run

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FAKE_SECRET = "brokered-token-SHOULD-NOT-LEAK"


def _spec(**extra):
    node = {
        "id": "await",
        "type": "wait",
        "until": "1h",
        "on_deadline": "continue",
        "output_key": "signal",
    }
    node.update(extra)
    return {"name": "adv-wait", "nodes": [node]}


def _run(spec, *, clock, wait_world=None, persist=None, pin_home=None, metadata=None, **kwargs):
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=clock,
        wait_world=wait_world,
        pin_home=pin_home,
        on_persist=persist,
        default_model="mock:test",
        **kwargs,
    )
    return run_workflow(wf, wf.input_defaults(), ctx, metadata=metadata)


def test_unsigned_or_wrong_hmac_does_not_wake_parked_run(tmp_settings) -> None:
    """Unsigned / wrong-secret events must not land or wake a waiting run."""
    secret = "correct-event-secret"
    home = tmp_settings.home_path()
    spec = _spec(for_event={"name": "go"}, on_deadline="fail")

    def persist(state):
        persist_run(state, tmp_settings.runs_dir())

    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=home, persist=persist)
    run_id = parked.value.state.run_id
    assert parked.value.state.status == "waiting"

    with pytest.raises(WaitEventRefused):
        accept_event(
            home,
            name="go",
            payload={"id": "x"},
            secret=secret,
            signature=None,
        )
    forged = sign_event("wrong-secret", "go", {"id": "x"})
    with pytest.raises(WaitEventRefused):
        accept_event(
            home,
            name="go",
            payload={"id": "x"},
            secret=secret,
            signature=forged,
        )
    assert list_events(home) == []

    report = wake_one(
        run_id,
        settings=tmp_settings,
        ctx_extras={
            "clock": lambda: T0,
            "pin_home": home,
            "wait_world": WaitWorld(),
        },
    )
    assert report["woke"] is False
    assert report["status"] == "waiting"

    # Separate assertion: a later correctly signed matching event may be accepted.
    good_payload = {"id": "ok"}
    accepted = accept_event(
        home,
        name="go",
        payload=good_payload,
        secret=secret,
        signature=sign_event(secret, "go", good_payload),
        actor="op",
    )
    assert accepted["ok"] is True
    assert accepted["idempotent"] is False
    assert any(row.get("name") == "go" for row in list_events(home))


def test_for_file_symlink_and_parent_escape_denied(tmp_path: Path) -> None:
    """Symlink at watched path and ../ escape must raise WaitPathDenied."""
    with pytest.raises(WaitPathDenied):
        inspect_file("../outside.pdf", tmp_path)
    with pytest.raises(WaitPathDenied):
        inspect_file("../../etc/passwd", tmp_path)

    target = tmp_path.parent / "escape-adversarial.pdf"
    target.write_bytes(b"escape")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    link = inbox / "watched.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        # Still covered ../ above; skip only the symlink half when OS refuses.
        return
    with pytest.raises(WaitPathDenied):
        inspect_file("inbox/watched.pdf", tmp_path)


def test_stalling_world_does_not_extend_deadline_into_approval(tmp_path: Path) -> None:
    """A WaitWorld that never satisfies extras must fire deadline, not ApprovalRequired."""
    stall = WaitWorld(events=[], files={}, runs={})

    fail_spec = _spec(
        for_event={"name": "never.arrives"},
        whichever="first",
        on_deadline="fail",
    )
    with pytest.raises(WaitingRequired) as parked_fail:
        _run(
            fail_spec,
            clock=lambda: T0,
            pin_home=tmp_path,
            wait_world=stall,
        )
    assert parked_fail.value.state.status == "waiting"
    wf_fail = WorkflowSpec.model_validate(fail_spec)
    ctx_fail = ExecutionContext(
        wf_fail,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        wait_world=stall,
        default_model="mock:test",
    )
    with pytest.raises(WaitError, match="deadline") as dead:
        run_workflow(wf_fail, {}, ctx_fail, state=parked_fail.value.state)
    failed_state = getattr(dead.value, "state", None) or parked_fail.value.state
    assert failed_state.status == "failed"
    assert failed_state.status != "paused"
    assert failed_state.status != "waiting"

    cont_spec = _spec(
        for_event={"name": "never.arrives"},
        for_file={"path": "missing/file.pdf", "on": "created"},
        whichever="all",
        on_deadline="continue",
        **{"default": {"stalled": True}},
    )
    with pytest.raises(WaitingRequired) as parked_cont:
        _run(
            cont_spec,
            clock=lambda: T0,
            pin_home=tmp_path,
            wait_world=stall,
        )
    wf_cont = WorkflowSpec.model_validate(cont_spec)
    ctx_cont = ExecutionContext(
        wf_cont,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=3),
        pin_home=tmp_path,
        wait_world=stall,
        default_model="mock:test",
    )
    done = run_workflow(wf_cont, {}, ctx_cont, state=parked_cont.value.state)
    assert done.status == "succeeded"
    assert done.status != "paused"
    assert done.output_keys["signal"]["reason"] == "deadline"
    assert done.output_keys["signal"]["action"] == "continue"


def test_stale_brokered_secret_not_visible_after_wake(tmp_path: Path) -> None:
    """Parked waits must drop brokered secrets; resume rebrokers and epochs."""
    spec = _spec(on_deadline="continue", **{"default": {"ok": True}})
    leak_meta = {
        "granted_secrets": {"API_TOKEN": FAKE_SECRET},
        "credential_env": {"API_TOKEN": FAKE_SECRET},
    }
    pre_wait_env = {"API_TOKEN": FAKE_SECRET}

    with pytest.raises(WaitingRequired) as parked:
        _run(
            spec,
            clock=lambda: T0,
            pin_home=tmp_path,
            metadata=leak_meta,
            credential_env=pre_wait_env,
        )
    state = parked.value.state
    assert state.status == "waiting"
    assert "granted_secrets" not in state.metadata
    assert "credential_env" not in state.metadata
    blob = json.dumps(state.to_record())
    assert FAKE_SECRET not in blob
    assert state.metadata.get("_wait_rebroker") is True

    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
        credential_env=dict(pre_wait_env),
    )
    assert ctx.credential_env == pre_wait_env
    done = run_workflow(wf, {}, ctx, state=state)
    assert done.status == "succeeded"
    assert done.output_keys["signal"]["action"] == "continue"
    # run_wait_node rebrokers on deadline continue
    assert ctx.credential_env is not pre_wait_env
    assert ctx.credential_env != pre_wait_env
    assert ctx.credential_env is None
    meta = done.metadata
    assert meta.get("credentials_epoch") or meta.get("_wait_rebroker")
    assert FAKE_SECRET not in json.dumps(meta)
    assert "granted_secrets" not in meta
    assert meta.get("credential_env") != pre_wait_env


def test_on_deadline_continue_does_not_satisfy_approval_gate(tmp_path: Path) -> None:
    """Wait→approval: deadline continue must pause at the gate, not succeed."""
    spec = {
        "name": "wait-then-gate",
        "nodes": [
            {
                "id": "await",
                "type": "wait",
                "until": "1h",
                "on_deadline": "continue",
                "output_key": "signal",
                "next": "gate",
            },
            {
                "id": "gate",
                "type": "approval",
                "prompt": "release?",
                "next": "done",
            },
            {
                "id": "done",
                "type": "transform",
                "template": "APPROVAL_SUCCESS_OUTPUT",
                "output_key": "out",
            },
        ],
    }
    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_path)
    assert parked.value.state.status == "waiting"

    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
    )
    with pytest.raises(ApprovalRequired) as gate:
        run_workflow(wf, {}, ctx, state=parked.value.state)
    assert gate.value.state.status == "paused"
    assert gate.value.state.status != "succeeded"
    assert gate.value.state.status != "waiting"
    assert gate.value.node_id == "gate"
    outputs = gate.value.state.output_keys
    assert "APPROVAL_SUCCESS_OUTPUT" not in str(outputs)
    assert outputs.get("out") != "APPROVAL_SUCCESS_OUTPUT"
    assert "APPROVAL_SUCCESS_OUTPUT" not in json.dumps(gate.value.state.to_record())


def test_max_waiting_zero_is_not_unset(tmp_path: Path) -> None:
    """max_waiting=0 must fire WaitCapExceeded; zero is not 'use default'."""
    spec = _spec(on_deadline="fail")
    with pytest.raises(WaitCapExceeded) as cap:
        _run(
            spec,
            clock=lambda: T0,
            pin_home=tmp_path,
            max_waiting=0,
            waiting_count=0,
        )
    assert cap.value.limit == 0
    assert cap.value.used == 0
