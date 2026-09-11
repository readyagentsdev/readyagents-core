"""Adversarial suite for V2-18 event triggers. Fail closed; no theater."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from readyagents.decisions.signing import sign_body
from readyagents.errors import PolicyDenied
from readyagents.firewall.policy_file import load_policy
from readyagents.triggers.decide import decide_trigger, replay_dead_letter
from readyagents.triggers.store import ConcurrencyGate, DeadLetterLog
from readyagents.workflow.schema import WorkflowSpec

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = "trigger-adversarial-secret"
FLOOD_N = 20


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
        "name": "trig-adv",
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


def test_replay_flood_inside_window_does_not_double_start(tmp_path: Path) -> None:
    """Same event fired FLOOD_N times must yield exactly one start and one run_id."""
    spec = _spec()
    payload = _payload()
    wf = WorkflowSpec.model_validate(spec)
    barrier = threading.Barrier(FLOOD_N)
    results: list = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        decision = decide_trigger(
            wf,
            trigger_name="support_email",
            raw=payload,
            source_kind="webhook",
            home=tmp_path,
            clock=lambda: T0,
            persist=False,
        )
        with lock:
            results.append(decision)

    threads = [threading.Thread(target=worker) for _ in range(FLOOD_N)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == FLOOD_N
    starts = [row for row in results if row.action == "start"]
    existing = [row for row in results if row.action == "existing"]
    assert len(starts) == 1, (
        f"expected one start, got {len(starts)} actions={[r.action for r in results]}"
    )
    assert len(existing) == FLOOD_N - 1
    assert {row.run_id for row in results} == {starts[0].run_id}
    assert starts[0].run_id


def test_forged_hmac_signature_does_not_start(tmp_path: Path) -> None:
    """Tampering the payload after signing must refuse; never start a run."""
    spec = _spec(require_signature=True)
    payload = _payload()
    signature = _sign(payload)
    forged = dict(payload)
    forged["body"] = "pwned-after-sign"
    decision = _decide(spec, forged, home=tmp_path, signature=signature, secret=SECRET)
    assert decision.action == "refuse"
    assert decision.action != "start"
    assert decision.state is None
    assert decision.run_id in {None, ""}
    assert decision.reason in {"unsigned", "forged", "signature"}
    letters = DeadLetterLog(tmp_path).list()
    assert letters, "forged signature must be dead-lettered"


def test_unsigned_required_refused_audited_and_dead_lettered(tmp_path: Path) -> None:
    """Unsigned event with require_signature must refuse, audit, and dead-letter."""
    spec = _spec(require_signature=True)
    audit: list[tuple[str, dict]] = []

    def auditor(event: str, **fields) -> None:
        audit.append((event, dict(fields)))

    decision = _decide(spec, _payload(), home=tmp_path, secret=SECRET, auditor=auditor)
    assert decision.action == "refuse"
    assert decision.reason == "unsigned"
    assert decision.state is None
    assert any(name == "trigger_refused" for name, _ in audit), audit
    assert decision.dead_letter_id
    letters = DeadLetterLog(tmp_path).list()
    assert any(
        row.get("reason") == "unsigned" and row.get("id") == decision.dead_letter_id
        for row in letters
    ), letters


def test_payload_injection_cannot_run_denied_tool(tmp_path: Path) -> None:
    """Event body interpolated into calc args under default-deny must not succeed."""
    policy_path = tmp_path / "deny.yaml"
    policy_path.write_text("version: 1\ndefault: deny\ntools: {}\n", encoding="utf-8")
    policy = load_policy(policy_path)
    spec = {
        "name": "poison-adv",
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

    decision = None
    denied = False
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
        denied = True

    if denied:
        return

    assert decision is not None
    assert decision.action != "start" or getattr(decision.state, "status", None) != "succeeded"
    assert decision.state is None or getattr(decision.state, "status", None) != "succeeded"
    out = None
    if decision.state is not None:
        out = getattr(decision.state, "output_keys", None) or {}
        if isinstance(out, dict):
            assert out.get("out") not in {2, "2"}
    assert decision.action == "refuse" or "denied" in (decision.reason or "").lower()


def test_over_ceiling_firehose_defers_then_drops_bounded(tmp_path: Path) -> None:
    """concurrency=1 + defer + max_defer=2: first extras defer, overflow drops, queue bounded."""
    max_defer = 2
    defer_spec = _spec(concurrency=1, on_ceiling="defer")
    gate = ConcurrencyGate(max_defer=max_defer)
    assert gate.try_enter("support_email", 1, on_ceiling="defer") == "enter"

    decisions = []
    for i in range(max_defer + 5):
        row = _decide(
            defer_spec,
            _payload(message_id=f"firehose-{i}"),
            home=tmp_path,
            gate=gate,
        )
        decisions.append(row)
        assert gate.deferred_count("support_email") <= max_defer

    assert decisions[0].action == "defer", decisions[0]
    assert decisions[0].reason == "ceiling"
    deferred = [row for row in decisions if row.action == "defer"]
    dropped = [row for row in decisions if row.action == "drop"]
    actions = [row.action for row in decisions]
    assert len(deferred) == max_defer, actions
    assert len(dropped) >= 1
    assert all(row.reason == "ceiling" for row in dropped)
    assert all(row.dead_letter_id for row in dropped)
    assert gate.deferred_count("support_email") == max_defer
    assert gate.deferred_count("support_email") <= max_defer
    assert not any(row.action == "start" for row in decisions)


def test_dead_letter_replay_of_hostile_unsigned_still_refuses(tmp_path: Path) -> None:
    """Replaying a dead-lettered unsigned hostile event must still refuse."""
    spec = _spec(require_signature=True)
    first = _decide(spec, _payload(body="hostile"), home=tmp_path, secret=SECRET)
    assert first.action == "refuse"
    assert first.reason == "unsigned"
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
    assert replayed.state is None
    assert replayed.run_id in {None, ""}
