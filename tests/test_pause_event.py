from __future__ import annotations

from types import SimpleNamespace

import pytest

from readyagents.decisions.signing import sign_body, verify_signed_body
from readyagents.workflow.pause import build_pause_event
from readyagents.workflow.state import RunState

_LEAK_KEYS = ("inputs", "outputs", "tokens", "token", "cookies", "secrets", "node_results")


def test_build_pause_event_fields_omit_optional_and_secrets() -> None:
    exc = SimpleNamespace(node_id="gate", prompt="ship it?")
    state = RunState.start("wf", {"user_token": "tok-secret", "password": "hidden"})
    state.pending_node = "gate"
    state.node_outputs["gate"] = {"cookie": "session"}
    state.output_keys["summary"] = "do-not-leak"
    event = build_pause_event(exc, state)
    assert event["event"] == "approval_required"
    assert event["run_id"] == state.run_id
    assert event["node_id"] == "gate"
    assert event["prompt"] == "ship it?"
    assert event["resume"] == f"readyagents resume {state.run_id} --approve gate"
    assert "approval_url" not in event
    assert set(event) == {"event", "run_id", "node_id", "prompt", "resume"}
    for key in _LEAK_KEYS:
        assert key not in event
    blob = str(event)
    assert "tok-secret" not in blob
    assert "hidden" not in blob
    assert "session" not in blob
    assert "do-not-leak" not in blob
    assert "user_token" not in blob


def test_build_pause_event_includes_approval_url_when_set() -> None:
    exc = SimpleNamespace(node_id="gate", prompt="")
    state = RunState.start("wf", {})
    url = "https://hooks.example.invalid/approve"
    event = build_pause_event(exc, state, approval_url=url)
    assert event["approval_url"] == url
    event_omitted = build_pause_event(exc, state, approval_url=None)
    assert "approval_url" not in event_omitted


def test_build_pause_event_falls_back_to_pending_node() -> None:
    exc = SimpleNamespace()
    state = RunState.start("wf", {})
    state.pending_node = "wait"
    event = build_pause_event(exc, state)
    assert event["node_id"] == "wait"
    assert event["prompt"] == ""
    assert event["resume"] == f"readyagents resume {state.run_id} --approve wait"


def test_sign_body_verify_roundtrip_and_forged() -> None:
    secret = "gate-test-secret"
    body = b'{"run_id":"abc","decision":"approve"}'
    sig = sign_body(secret, body)
    verify_signed_body(secret, body, sig)
    verify_signed_body(secret, body, f"sha256={sig}")
    with pytest.raises(ValueError, match="forged decision: signature mismatch"):
        verify_signed_body(secret, body, "deadbeef" * 8)
    with pytest.raises(ValueError, match="unsigned decision: missing signature"):
        verify_signed_body(secret, body, None)
    with pytest.raises(ValueError, match="unsigned decision: missing signature"):
        verify_signed_body(secret, body, "")
