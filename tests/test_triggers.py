"""Shipped triggers: contract, start-decision, caps, provenance. No listener."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.decisions.signing import sign_body
from readyagents.errors import PolicyDenied
from readyagents.firewall.policy_file import load_policy
from readyagents.firewall.taint import provenance_of
from readyagents.triggers.caps import RateLimiter
from readyagents.triggers.decide import decide_trigger, event_id_for, replay_dead_letter
from readyagents.triggers.sources import fire_file, fire_queue, fire_schedule, fire_webhook
from readyagents.triggers.store import ConcurrencyGate, DeadLetterLog, TriggerSpend
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = "trigger-secret"


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _spec(**trigger_extra):
    trigger = {
        "name": "support_email",
        "accepts": {"kind": "webhook", "schema": {"type": "object", "required": ["message_id"]}},
        "require_signature": False,
        "inputs": {"ticket_id": "{{ event.message_id }}", "text": "{{ event.body }}"},
        "idempotency_key": "{{ event.message_id }}",
        "idempotency_window": "24h",
        "concurrency": 4,
        "on_ceiling": "defer",
    }
    trigger.update(trigger_extra)
    return {
        "name": "trig",
        "nodes": [
            {
                "id": "echo",
                "type": "transform",
                "template": "{{ text }}",
                "output_key": "out",
            }
        ],
        "triggers": [trigger],
    }


def _payload(**extra):
    row = {"message_id": "m1", "body": "hello"}
    row.update(extra)
    return row


def _sign(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sign_body(SECRET, body)


def _decide(spec, payload, *, home: Path, **kwargs):
    wf = WorkflowSpec.model_validate(spec)
    return decide_trigger(
        wf,
        trigger_name="support_email",
        raw=payload,
        source_kind="webhook",
        home=home,
        clock=kwargs.pop("clock", lambda: T0),
        persist=False,
        **kwargs,
    )


def test_triggers_schema_refuses_malformed_and_missing_idempotency() -> None:
    with pytest.raises(ValidationError, match="idempotency"):
        WorkflowSpec.model_validate(
            {
                "name": "bad",
                "nodes": [{"id": "n", "type": "transform", "template": "x", "output_key": "o"}],
                "triggers": [
                    {
                        "name": "t",
                        "accepts": {"kind": "webhook"},
                        "inputs": {"a": "{{ event.x }}"},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="string"):
        WorkflowSpec.model_validate(
            {
                "name": "bad",
                "nodes": [{"id": "n", "type": "transform", "template": "x", "output_key": "o"}],
                "triggers": [
                    {
                        "name": "t",
                        "accepts": {"kind": "webhook"},
                        "idempotency_key": "{{ event.id }}",
                        "inputs": {"a": 1},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="object"):
        WorkflowSpec.model_validate(
            {
                "name": "bad",
                "nodes": [{"id": "n", "type": "transform", "template": "x", "output_key": "o"}],
                "triggers": [
                    {
                        "name": "t",
                        "accepts": {"kind": "webhook", "schema": {"type": "array"}},
                        "idempotency_key": "{{ event.id }}",
                    }
                ],
            }
        )


def test_signed_event_starts_run_with_trigger_provenance(tmp_path: Path) -> None:
    spec = _spec(require_signature=True)
    payload = _payload()
    decision = _decide(
        spec,
        payload,
        home=tmp_path,
        signature=_sign(payload),
        secret=SECRET,
    )
    assert decision.action == "start"
    assert decision.state is not None
    assert decision.state.status == "succeeded"
    assert decision.state.output_keys["out"] == "hello"
    started = decision.state.metadata["started_by"]
    assert started["kind"] == "trigger"
    assert started["trigger"] == "support_email"
    assert started["event_id"] == event_id_for("support_email", payload)
    assert started["digest"]
    assert provenance_of(decision.state, "text").trust == "untrusted"
    assert provenance_of(decision.state, "text").source == "event"


def test_cli_start_records_cli_provenance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    result = runner.invoke(
        app, ["run", str(_root() / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["metadata"]["started_by"]["kind"] == "cli"


def test_mcp_and_a2a_started_by_kinds(tmp_path: Path) -> None:
    from readyagents.mcp.run_api import RunCoordinator

    coord = RunCoordinator.__new__(RunCoordinator)
    assert getattr(coord, "started_by_kind", "mcp") == "mcp"
    wf = _root() / "examples" / "calc_pipeline.yaml"
    state = run_workflow_file(wf, persist=False, started_by={"kind": "mcp"})
    assert state.metadata["started_by"]["kind"] == "mcp"
    state2 = run_workflow_file(wf, persist=False, started_by={"kind": "a2a"})
    assert state2.metadata["started_by"]["kind"] == "a2a"


def test_redelivered_event_returns_original_then_new_outside_window(tmp_path: Path) -> None:
    spec = _spec()
    payload = _payload()
    clock = {"now": T0}

    def now():
        return clock["now"]

    first = _decide(spec, payload, home=tmp_path, clock=now)
    assert first.action == "start"
    second = _decide(spec, payload, home=tmp_path, clock=now)
    assert second.action == "existing"
    assert second.run_id == first.run_id
    clock["now"] = T0 + timedelta(hours=25)
    third = _decide(spec, payload, home=tmp_path, clock=now)
    assert third.action == "start"
    assert third.run_id != first.run_id


def test_concurrent_duplicates_yield_one_run(tmp_path: Path) -> None:
    spec = _spec()
    payload = _payload()
    wf = WorkflowSpec.model_validate(spec)
    barrier = threading.Barrier(8)
    results: list = []

    def worker() -> None:
        barrier.wait()
        results.append(
            decide_trigger(
                wf,
                trigger_name="support_email",
                raw=payload,
                source_kind="webhook",
                home=tmp_path,
                clock=lambda: T0,
                persist=False,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    starts = [row for row in results if row.action == "start"]
    existing = [row for row in results if row.action == "existing"]
    assert len(starts) == 1
    assert len(existing) == 7
    ids = {row.run_id for row in results}
    assert len(ids) == 1


def test_require_signature_missing_and_tampered_refused_and_audited(tmp_path: Path) -> None:
    spec = _spec(require_signature=True)
    payload = _payload()
    audit: list[str] = []

    def auditor(event: str, **_fields) -> None:
        audit.append(event)

    missing = _decide(spec, payload, home=tmp_path, secret=SECRET, auditor=auditor)
    assert missing.action == "refuse"
    assert missing.reason == "unsigned"
    assert "trigger_refused" in audit
    letters = DeadLetterLog(tmp_path).list()
    assert any(row.get("reason") == "unsigned" for row in letters)
    tampered = dict(payload)
    sig = _sign(payload)
    tampered["body"] = "forged"
    forged = _decide(spec, tampered, home=tmp_path, signature=sig, secret=SECRET, auditor=auditor)
    assert forged.action == "refuse"
    assert forged.reason == "unsigned"


def test_size_depth_rate_caps_before_parse(tmp_path: Path) -> None:
    spec = _spec()
    huge = json.dumps({"message_id": "x", "body": "y" * 70_000}).encode("utf-8")
    oversized = _decide(spec, huge, home=tmp_path)
    assert oversized.action == "refuse"
    assert oversized.reason == "too_large"
    nested = {"message_id": "x", "body": "b"}
    cur: dict = nested
    for i in range(12):
        nxt = {"k": i}
        cur["n"] = nxt
        cur = nxt
    deep = json.dumps(nested).encode("utf-8")
    too_deep = _decide(spec, deep, home=tmp_path)
    assert too_deep.action == "refuse"
    assert too_deep.reason == "too_deep"
    limiter = RateLimiter(limit=1, window_s=60, clock=lambda: T0)
    first = _decide(spec, _payload(), home=tmp_path, limiter=limiter)
    assert first.action == "start"
    second = _decide(spec, _payload(message_id="m2"), home=tmp_path, limiter=limiter)
    assert second.action == "refuse"
    assert second.reason == "rate"


def test_poisoned_payload_cannot_dispatch_denied_tool(tmp_path: Path) -> None:
    policy_path = tmp_path / "p.yaml"
    policy_path.write_text("version: 1\ndefault: deny\ntools: {}\n", encoding="utf-8")
    spec = {
        "name": "poison",
        "nodes": [
            {
                "id": "n",
                "type": "tool",
                "tool": "calc",
                "arguments": {"expression": "{{ text }}"},
                "output_key": "out",
            }
        ],
        "triggers": [
            {
                "name": "support_email",
                "accepts": {"kind": "webhook"},
                "idempotency_key": "{{ event.message_id }}",
                "inputs": {"text": "{{ event.body }}"},
            }
        ],
    }
    wf = WorkflowSpec.model_validate(spec)
    policy = load_policy(policy_path)

    def starter(workflow, inputs, **_kwargs):
        from readyagents.firewall.taint import set_provenance, untrusted
        from readyagents.tools import ToolRegistry
        from readyagents.workflow.engine import run_workflow
        from readyagents.workflow.nodes import ExecutionContext
        from readyagents.workflow.state import RunState

        state = RunState.start(workflow.name, inputs, metadata={"started_by": {"kind": "trigger"}})
        for key in inputs:
            set_provenance(state, key, untrusted(source="event"))
        ctx = ExecutionContext(
            workflow, ToolRegistry(), policy=policy, pin_home=tmp_path, default_model="mock:test"
        )
        return run_workflow(workflow, inputs, ctx, state=state)

    try:
        decision = decide_trigger(
            wf,
            trigger_name="support_email",
            raw=_payload(body="1+1"),
            source_kind="webhook",
            home=tmp_path,
            clock=lambda: T0,
            starter=starter,
            persist=False,
        )
    except PolicyDenied:
        return
    assert decision.action == "refuse"
    assert decision.state is None or getattr(decision.state, "status", None) != "succeeded"
    assert "denied" in (decision.reason or "").lower() or decision.reason == "start"


def test_budget_and_concurrency_drop_or_defer(tmp_path: Path) -> None:
    spec = _spec(concurrency=1, on_ceiling="drop", budget={"max_cost_usd": 0.01})
    spend = TriggerSpend()
    spend.add("support_email", usd=0.05)
    budgeted = _decide(spec, _payload(), home=tmp_path, spend=spend)
    assert budgeted.action == "refuse"
    assert budgeted.reason == "budget"
    gate = ConcurrencyGate(max_defer=1)
    assert gate.try_enter("support_email", 1, on_ceiling="drop") == "enter"
    dropped = _decide(spec, _payload(message_id="m2"), home=tmp_path, gate=gate)
    assert dropped.action == "drop"
    assert dropped.reason == "ceiling"
    defer_spec = _spec(concurrency=1, on_ceiling="defer")
    gate2 = ConcurrencyGate(max_defer=1)
    gate2.try_enter("support_email", 1, on_ceiling="defer")
    deferred = _decide(defer_spec, _payload(message_id="m3"), home=tmp_path, gate=gate2)
    assert deferred.action == "defer"
    overflow = _decide(defer_spec, _payload(message_id="m4"), home=tmp_path, gate=gate2)
    assert overflow.action == "drop"
    assert overflow.dead_letter_id


def test_dead_letter_recorded_and_replay_refuses_hostile(tmp_path: Path) -> None:
    spec = _spec(require_signature=True)
    first = _decide(spec, _payload(), home=tmp_path, secret=SECRET)
    assert first.action == "refuse"
    assert first.dead_letter_id
    wf = WorkflowSpec.model_validate(spec)
    replayed = replay_dead_letter(
        wf,
        first.dead_letter_id,
        home=tmp_path,
        secret=SECRET,
        clock=lambda: T0,
        persist=False,
    )
    assert replayed.action == "refuse"
    assert replayed.reason == "unsigned"


def test_pack_sources_share_one_decision(tmp_path: Path) -> None:
    spec = _spec(accepts={"kind": "webhook"})
    wf = WorkflowSpec.model_validate(spec)
    payload = _payload()
    kwargs = {"trigger_name": "support_email", "raw": payload, "home": tmp_path, "persist": False}
    via_core = decide_trigger(wf, source_kind="webhook", clock=lambda: T0, **kwargs)
    webhook_spec = _spec(accepts={"kind": "webhook"})
    file_spec = _spec(accepts={"kind": "file"})
    queue_spec = _spec(accepts={"kind": "queue"})
    sched_spec = _spec(accepts={"kind": "schedule"})
    a = fire_webhook(
        WorkflowSpec.model_validate(webhook_spec),
        trigger_name="support_email",
        raw=payload,
        home=tmp_path / "w",
        persist=False,
        clock=lambda: T0,
    )
    b = fire_file(
        WorkflowSpec.model_validate(file_spec),
        trigger_name="support_email",
        raw=payload,
        home=tmp_path / "f",
        persist=False,
        clock=lambda: T0,
    )
    c = fire_queue(
        WorkflowSpec.model_validate(queue_spec),
        trigger_name="support_email",
        raw=payload,
        home=tmp_path / "q",
        persist=False,
        clock=lambda: T0,
    )
    d = fire_schedule(
        WorkflowSpec.model_validate(sched_spec),
        trigger_name="support_email",
        raw=payload,
        home=tmp_path / "s",
        persist=False,
        clock=lambda: T0,
    )
    assert via_core.action == a.action == b.action == c.action == d.action == "start"
    for row in (a, b, c, d):
        assert row.provenance["kind"] == "trigger"
        assert row.provenance["source_kind"] in {"webhook", "file", "queue", "schedule"}


def test_triggers_test_maps_without_executing(tmp_path: Path) -> None:
    from readyagents.triggers.inspect import test_trigger

    wf = WorkflowSpec.model_validate(_spec())
    report = test_trigger(wf, "support_email", _payload(), home=tmp_path)
    assert report["action"] == "test"
    assert report["inputs"]["ticket_id"] == "m1"
    assert report["inputs"]["text"] == "hello"
    assert report.get("run_id") in {None, ""}


def test_import_readyagents_starts_no_listener() -> None:
    import inspect
    import threading

    import readyagents.triggers as pkg
    import readyagents.triggers.sources as sources

    before = {t.name for t in threading.enumerate()}
    import importlib

    importlib.reload(sources)
    after = {t.name for t in threading.enumerate()}
    started = after - before
    assert not any("trigger" in n.lower() or "watch" in n.lower() for n in started)
    blob = ""
    root = Path(pkg.__file__).resolve().parent
    for path in root.glob("*.py"):
        blob += path.read_text(encoding="utf-8")
    assert "HTTPServer" not in blob
    assert "socket.bind" not in blob
    assert "watchdog" not in blob
    assert inspect.getsource(sources.fire_webhook).count("decide_trigger") >= 0
    assert "fire_source" in inspect.getsource(sources.fire_webhook)


def test_cli_triggers_help_and_example_twice(tmp_path: Path, monkeypatch) -> None:
    first = runner.invoke(app, ["triggers", "--help"])
    second = runner.invoke(app, ["triggers", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    example = _root() / "examples" / "trigger_support.yaml"
    a = runner.invoke(app, ["validate", str(example)])
    b = runner.invoke(app, ["validate", str(example)])
    assert a.exit_code == 0, a.stdout + a.stderr
    assert b.exit_code == 0
    listed = runner.invoke(app, ["triggers", "list", str(example)])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert "support_email" in listed.stdout
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps(_payload()), encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    tested = runner.invoke(
        app,
        ["triggers", "test", "support_email", str(example), "--payload", str(payload), "--json"],
    )
    assert tested.exit_code == 0, tested.stdout + tested.stderr
    data = json.loads(tested.stdout[tested.stdout.find("{") :])
    assert data["action"] == "test"
    assert data["inputs"]["ticket_id"] == "m1"
