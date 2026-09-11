"""Shipped type: wait path: lazy wake, signed events, waiting status."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import (
    ApprovalRequired,
    WaitCapExceeded,
    WaitError,
    WaitEventRefused,
    WaitingRequired,
    WaitPathDenied,
)
from readyagents.firewall.taint import provenance_of
from readyagents.tools import ToolRegistry
from readyagents.wait.evaluate import WaitWorld, evaluate_wait
from readyagents.wait.events import accept_event, sign_event
from readyagents.wait.record import WaitRecord, resolve_deadline
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import gc_runs, persist_run

runner = CliRunner()
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _spec(**extra):
    node = {
        "id": "await",
        "type": "wait",
        "until": "1h",
        "on_deadline": "continue",
        "output_key": "signal",
    }
    node.update(extra)
    return {"name": "long", "nodes": [node]}


def _run(spec, *, clock, wait_world=None, persist=None, pin_home=None, **kwargs):
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
    return run_workflow(wf, wf.input_defaults(), ctx)


def test_for_file_yaml_on_boolean_key() -> None:
    spec = yaml.safe_load(
        "name: y\n"
        "nodes:\n"
        "  - id: await\n"
        "    type: wait\n"
        "    until: 1h\n"
        "    for_file: {path: inbox/a.pdf, on: created}\n"
    )
    wf = WorkflowSpec.model_validate(spec)
    assert wf.nodes[0].for_file is not None
    assert wf.nodes[0].for_file["on"] == "created"


def test_wait_without_deadline_is_schema_error() -> None:
    with pytest.raises(ValidationError, match="until"):
        WorkflowSpec.model_validate(
            {"name": "bad", "nodes": [{"id": "w", "type": "wait", "for_event": {"name": "x"}}]}
        )


def test_until_deadline_continue(tmp_path: Path) -> None:
    spec = _spec(on_deadline="continue", **{"default": {"ok": True}})
    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_path)
    assert parked.value.state.status == "waiting"
    assert parked.value.state.status != "paused"
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx, state=parked.value.state)
    assert done.status == "succeeded"
    assert done.output_keys["signal"]["reason"] == "deadline"
    assert done.output_keys["signal"]["action"] == "continue"


def test_whichever_first_event_before_deadline(tmp_path: Path) -> None:
    spec = _spec(
        for_event={"name": "contract.signed", "match": {"id": "c1"}},
        whichever="first",
        on_deadline="fail",
    )
    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_path, wait_world=WaitWorld())
    world = WaitWorld(events=[{"name": "contract.signed", "payload": {"id": "c1"}}])
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(minutes=5),
        wait_world=world,
        pin_home=tmp_path,
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx, state=parked.value.state)
    assert done.status == "succeeded"
    assert done.output_keys["signal"]["reason"] == "event"
    assert provenance_of(done, "signal").trust == "untrusted"


def test_whichever_all_needs_every_condition() -> None:
    world = WaitWorld(events=[{"name": "go", "payload": {}}])
    rec = WaitRecord(
        node_id="await",
        until="1h",
        deadline_at=(T0 + timedelta(hours=1)).isoformat(),
        whichever="all",
        for_event={"name": "go"},
        for_file={"path": "inbox/a.pdf", "on": "created"},
        created_at=T0.isoformat(),
    )
    mid = evaluate_wait(rec, now=T0 + timedelta(minutes=1), world=world)
    assert mid.satisfied is False
    world.files["inbox/a.pdf"] = {"exists": True, "mtime": "9", "symlink": False}
    ready = evaluate_wait(rec, now=T0 + timedelta(minutes=1), world=world)
    assert ready.satisfied is True
    assert ready.reason == "all"


def test_for_file_and_for_run_kinds() -> None:
    rec = WaitRecord(
        node_id="w",
        until="1h",
        deadline_at=(T0 + timedelta(hours=1)).isoformat(),
        for_file={"path": "inbox/a.pdf", "on": "created"},
        created_at=T0.isoformat(),
    )
    miss = evaluate_wait(rec, now=T0, world=WaitWorld())
    assert miss.satisfied is False
    hit = evaluate_wait(
        rec,
        now=T0,
        world=WaitWorld(files={"inbox/a.pdf": {"exists": True, "mtime": "2", "symlink": False}}),
    )
    assert hit.satisfied is True
    assert hit.reason == "file"
    rec2 = WaitRecord(
        node_id="w",
        until="1h",
        deadline_at=(T0 + timedelta(hours=1)).isoformat(),
        for_run={"run_id": "abc", "status": "succeeded"},
        created_at=T0.isoformat(),
    )
    assert evaluate_wait(rec2, now=T0, world=WaitWorld(runs={"abc": "running"})).satisfied is False
    assert evaluate_wait(rec2, now=T0, world=WaitWorld(runs={"abc": "succeeded"})).reason == "run"


def test_changed_compares_unix_mtime_to_iso_created() -> None:
    rec = WaitRecord(
        node_id="w",
        until="1h",
        deadline_at=(T0 + timedelta(hours=1)).isoformat(),
        for_file={"path": "inbox/a.pdf", "on": "changed"},
        created_at=T0.isoformat(),
    )
    before = str((T0 - timedelta(minutes=1)).timestamp())
    stale = WaitWorld(files={"inbox/a.pdf": {"exists": True, "mtime": before, "symlink": False}})
    assert evaluate_wait(rec, now=T0 + timedelta(minutes=2), world=stale).satisfied is False
    after = str((T0 + timedelta(minutes=1)).timestamp())
    ready = WaitWorld(files={"inbox/a.pdf": {"exists": True, "mtime": after, "symlink": False}})
    hit = evaluate_wait(rec, now=T0 + timedelta(minutes=2), world=ready)
    assert hit.satisfied is True
    assert hit.reason == "file"


def test_for_file_changed_after_wait_via_inspect_file(tmp_path: Path) -> None:
    from readyagents.wait.world import inspect_file

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    target = inbox / "doc.pdf"
    target.write_bytes(b"before")
    past = datetime.now(UTC).timestamp() - 120
    os.utime(target, (past, past))
    spec = _spec(for_file={"path": "inbox/doc.pdf", "on": "changed"}, on_deadline="fail")
    with pytest.raises(WaitingRequired) as parked:
        _run(
            spec,
            clock=lambda: datetime.now(UTC),
            pin_home=tmp_path,
            workflow_dir=tmp_path,
        )
    assert parked.value.state.status == "waiting"
    before = inspect_file("inbox/doc.pdf", tmp_path)
    assert before["exists"] is True
    assert before["symlink"] is False
    assert "T" in str(before["mtime"])
    wf = WorkflowSpec.model_validate(spec)
    ctx_same = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: datetime.now(UTC),
        pin_home=tmp_path,
        workflow_dir=tmp_path,
        default_model="mock:test",
    )
    with pytest.raises(WaitingRequired) as still:
        run_workflow(wf, {}, ctx_same, state=parked.value.state)
    assert still.value.state.status == "waiting"
    later = datetime.now(UTC).timestamp() + 5
    target.write_bytes(b"after")
    os.utime(target, (later, later))
    changed = inspect_file("inbox/doc.pdf", tmp_path)
    created = still.value.state.pending["wait"]["created_at"]
    assert changed["mtime"] != before["mtime"]
    rec = WaitRecord.from_dict(still.value.state.pending["wait"])
    world = WaitWorld(files={"inbox/doc.pdf": changed})
    assert evaluate_wait(rec, now=datetime.now(UTC), world=world).satisfied is True
    ctx_ready = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: datetime.now(UTC),
        pin_home=tmp_path,
        workflow_dir=tmp_path,
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx_ready, state=still.value.state)
    assert done.status == "succeeded"
    assert done.output_keys["signal"]["reason"] == "file"
    assert created  # wait record carried created_at from the park


def test_on_deadline_fail_escalate_branch(tmp_path: Path) -> None:
    fail_spec = _spec(on_deadline="fail")
    with pytest.raises(WaitingRequired) as parked:
        _run(fail_spec, clock=lambda: T0, pin_home=tmp_path)
    wf = WorkflowSpec.model_validate(fail_spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
    )
    with pytest.raises(WaitError, match="deadline"):
        run_workflow(wf, {}, ctx, state=parked.value.state)
    esc = _spec(on_deadline="escalate", escalate_to=["account_manager"])
    with pytest.raises(WaitingRequired) as parked2:
        _run(esc, clock=lambda: T0, pin_home=tmp_path)
    wf2 = WorkflowSpec.model_validate(esc)
    ctx2 = ExecutionContext(
        wf2,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
    )
    with pytest.raises(ApprovalRequired) as gate:
        run_workflow(wf2, {}, ctx2, state=parked2.value.state)
    assert gate.value.state.status == "paused"
    assert gate.value.state.status != "waiting"
    branch = {
        "name": "br",
        "nodes": [
            {
                "id": "await",
                "type": "wait",
                "until": "1h",
                "on_deadline": "branch",
                "else": "alt",
                "output_key": "signal",
                "next": "alt",
            },
            {"id": "alt", "type": "transform", "template": "branched", "output_key": "out"},
        ],
    }
    with pytest.raises(WaitingRequired) as parked3:
        _run(branch, clock=lambda: T0, pin_home=tmp_path)
    wf3 = WorkflowSpec.model_validate(branch)
    ctx3 = ExecutionContext(
        wf3,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_path,
        default_model="mock:test",
    )
    done = run_workflow(wf3, {}, ctx3, state=parked3.value.state)
    assert done.status == "succeeded"
    assert done.output_keys["out"] == "branched"


def test_waiting_survives_restart_and_record_version_bump(tmp_settings, tmp_path: Path) -> None:
    spec = _spec()
    saved = []

    def persist(state):
        persist_run(state, tmp_settings.runs_dir())
        saved.append(state.status)

    with pytest.raises(WaitingRequired):
        _run(spec, clock=lambda: T0, pin_home=tmp_settings.home_path(), persist=persist)
    from readyagents.workflow.state import list_runs, load_run

    found = [s for s in list_runs(tmp_settings.runs_dir()) if s.status == "waiting"]
    assert found
    path = tmp_settings.runs_dir() / f"{found[0].run_id}.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["record_version"] = 2
    rec["pending"]["wait"]["wait_record_version"] = 99
    rec["pending"]["wait"]["future_field"] = "ok"
    path.write_text(json.dumps(rec), encoding="utf-8")
    loaded = load_run(tmp_settings.runs_dir(), found[0].run_id)
    assert loaded.status == "waiting"
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        clock=lambda: T0 + timedelta(hours=2),
        pin_home=tmp_settings.home_path(),
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx, state=loaded)
    assert done.status == "succeeded"


def test_signed_event_unsigned_refused_idempotent_taint(
    tmp_settings, tmp_path: Path, monkeypatch
) -> None:
    secret = "event-secret"
    monkeypatch.setenv("READYAGENTS_EVENT_SECRET", secret)
    payload = {"id": "c1"}
    sig = sign_event(secret, "contract.signed", payload)
    first = accept_event(
        tmp_settings.home_path(),
        name="contract.signed",
        payload=payload,
        secret=secret,
        signature=sig,
        actor="op",
    )
    assert first["idempotent"] is False
    second = accept_event(
        tmp_settings.home_path(),
        name="contract.signed",
        payload=payload,
        secret=secret,
        signature=sig,
        actor="op",
    )
    assert second["idempotent"] is True
    with pytest.raises(WaitEventRefused):
        accept_event(
            tmp_settings.home_path(),
            name="contract.signed",
            payload={"id": "other"},
            secret=secret,
            signature=None,
        )
    huge = {"blob": "x" * 70_000}
    with pytest.raises(WaitEventRefused, match="bound"):
        accept_event(
            tmp_settings.home_path(),
            name="big",
            payload=huge,
            secret=secret,
            signature=sign_event(secret, "big", huge),
        )


def test_unsigned_event_does_not_wake(tmp_settings) -> None:
    spec = _spec(for_event={"name": "go"}, on_deadline="fail")
    audit: list[str] = []

    def persist(state):
        persist_run(state, tmp_settings.runs_dir())

    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_settings.home_path(), persist=persist)
    with pytest.raises(WaitEventRefused):
        accept_event(
            tmp_settings.home_path(),
            name="go",
            payload={"id": "x"},
            secret="secret",
            signature=None,
            auditor=lambda event, **_fields: audit.append(event),
        )
    assert "event_refused" in audit
    from readyagents.wait.events import list_events
    from readyagents.wait.wake import wake_one

    assert list_events(tmp_settings.home_path()) == []
    report = wake_one(
        parked.value.state.run_id,
        settings=tmp_settings,
        ctx_extras={
            "clock": lambda: T0,
            "pin_home": tmp_settings.home_path(),
            "wait_world": WaitWorld(),
        },
    )
    assert report["woke"] is False
    assert report["status"] == "waiting"


def test_file_wait_symlink_refused(tmp_path: Path) -> None:
    from readyagents.wait.world import inspect_file

    target = tmp_path.parent / "escape.pdf"
    target.write_bytes(b"x")
    link = tmp_path / "inbox"
    link.mkdir()
    dest = link / "a.pdf"
    try:
        dest.symlink_to(target)
        linked = True
    except OSError:
        linked = False
    if linked:
        with pytest.raises(WaitPathDenied):
            inspect_file("inbox/a.pdf", tmp_path)
    with pytest.raises(WaitPathDenied):
        inspect_file("../escape.pdf", tmp_path)
    inside = link / "real.pdf"
    inside.write_bytes(b"y")
    local = link / "link.pdf"
    try:
        local.symlink_to(inside)
        local_linked = True
    except OSError:
        local_linked = False
    if local_linked:
        with pytest.raises(WaitPathDenied):
            inspect_file("inbox/link.pdf", tmp_path)
        spec = _spec(for_file={"path": "inbox/link.pdf", "on": "created"}, on_deadline="fail")
        with pytest.raises(WaitPathDenied):
            _run(
                spec,
                clock=lambda: T0,
                pin_home=tmp_path,
                workflow_dir=tmp_path,
            )


def test_continue_does_not_grant_approval(tmp_path: Path) -> None:
    spec = {
        "name": "no-backdoor",
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
                "prompt": "ok?",
                "next": "done",
            },
            {"id": "done", "type": "transform", "template": "granted", "output_key": "out"},
        ],
    }
    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_path)
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
    assert "granted" not in str(gate.value.state.output_keys)


def test_gc_never_collects_waiting(tmp_settings, tmp_path: Path) -> None:
    spec = _spec()

    def persist(state):
        persist_run(state, tmp_settings.runs_dir())

    with pytest.raises(WaitingRequired):
        _run(spec, clock=lambda: T0, pin_home=tmp_settings.home_path(), persist=persist)
    deleted = gc_runs(tmp_settings.runs_dir(), include_paused=True, override_retention=True)
    from readyagents.workflow.state import list_runs

    still = [s for s in list_runs(tmp_settings.runs_dir()) if s.status == "waiting"]
    assert still
    assert still[0].run_id not in deleted


def test_dormant_cap_typed(tmp_path: Path) -> None:
    spec = _spec()
    with pytest.raises(WaitCapExceeded):
        _run(spec, clock=lambda: T0, pin_home=tmp_path, max_waiting=0, waiting_count=1)


def test_rebroker_flag_on_wait(tmp_path: Path) -> None:
    spec = _spec()
    with pytest.raises(WaitingRequired) as parked:
        _run(spec, clock=lambda: T0, pin_home=tmp_path)
    assert parked.value.state.metadata.get("_wait_rebroker") is True
    assert "granted_secrets" not in parked.value.state.metadata


def test_cli_wake_help_twice() -> None:
    first = runner.invoke(app, ["wake", "--help"])
    second = runner.invoke(app, ["wake", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    assert first.exit_code == second.exit_code


def test_validate_wait_example_twice() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "wait_inbox.yaml"
    first = runner.invoke(app, ["validate", str(example)])
    second = runner.invoke(app, ["validate", str(example)])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr


def test_runs_list_waiting_and_timeline(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    spec = _spec()

    def persist(state):
        persist_run(state, tmp_settings.runs_dir())

    with pytest.raises(WaitingRequired):
        _run(spec, clock=lambda: T0, pin_home=tmp_settings.home_path(), persist=persist)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    listed = runner.invoke(app, ["runs", "list", "--waiting", "--json"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    payload = json.loads(listed.stdout[listed.stdout.find("[") :])
    assert payload and payload[0]["status"] == "waiting"
    assert "expires_at" in payload[0]
    rid = payload[0]["run_id"]
    tl = runner.invoke(app, ["runs", "timeline", rid, "--json"])
    assert tl.exit_code == 0, tl.stdout + tl.stderr
    body = json.loads(tl.stdout[tl.stdout.find("{") :])
    assert body["status"] == "waiting"
    assert "spend" in body


def test_compact_keeps_replayable_markers() -> None:
    from readyagents.wait.compact import compact_results
    from readyagents.workflow.state import NodeResult, RunState

    state = RunState.start("long", {})
    for i in range(20):
        state.results.append(
            NodeResult(
                node_id=f"n{i}",
                type="transform",
                status="ok",
                output="x" * 5000,
            )
        )
    compact_results(state)
    assert any(
        isinstance(r.output, dict) and r.output.get("_compacted") for r in state.results[:-16]
    )
    blob = json.dumps(state.to_record())
    assert "_compacted" in blob


def test_resolve_deadline_duration_and_timestamp() -> None:
    at = resolve_deadline("2h", now=T0)
    assert at == T0 + timedelta(hours=2)
    iso = resolve_deadline("2026-01-02T00:00:00+00:00", now=T0)
    assert iso.day == 2
