"""Adversarial streaming tests. Drive shipped APIs only; fail closed."""

from __future__ import annotations

import json

import pytest

from readyagents.errors import CancellationRequested
from readyagents.llm.base import CompletionResult
from readyagents.observability import RunEvent
from readyagents.policy import Redactor
from readyagents.tools import ToolRegistry
from readyagents.workflow.cancellation import CancellationToken
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState, load_run, persist_run, utc_now
from readyagents.workflow.stream import (
    StreamHub,
    StreamSession,
    snapshot_events,
    wants_event_stream,
)

_SECRET = "LEAKMESECRET"
_FULL_COMPLETION = "FULL-ASSEMBLED-COMPLETION-TEXT"
_SSE_TOKEN = "sse-adv-bearer-token-not-for-logs"
_AGENT = {
    "name": "stream-adv-cancel",
    "default_model": "scripted:t",
    "nodes": [
        {
            "id": "a",
            "type": "agent",
            "prompt": "hi",
            "model": "scripted:t",
            "output_key": "text",
        }
    ],
}


def _token_text(events) -> str:
    return "".join(e.text or "" for e in events if e.event == "token")


def _node_finished(node_id: str, run_id: str = "r") -> RunEvent:
    return RunEvent(
        name="node.finished",
        timestamp=utc_now(),
        run_id=run_id,
        workflow="adv",
        node_id=node_id,
        status="ok",
        duration_ms=1,
    )


def test_boundary_split_secret_not_in_joined_token_text(tmp_settings) -> None:
    redactor = Redactor(literals=[_SECRET])
    hub = StreamHub()
    queue = hub.try_subscribe("r")
    assert queue is not None
    session = StreamSession(redactor=redactor, hub=hub, run_id="r")
    session.on_token("n", "LEAKME")
    session.on_token("n", "SECRET")
    session.on_token("n", "z" * 80)
    session.on_event(_node_finished("n"))
    joined = _token_text(session.events)
    assert joined
    assert _SECRET not in joined
    published = "".join(
        str(item.get("text") or "") for item in queue if item.get("event") == "token"
    )
    assert _SECRET not in published
    blob = "".join(str(e.as_dict()) for e in session.events)
    assert _SECRET not in blob
    hub.unsubscribe("r", queue)


def test_boundary_split_secret_straddles_lookback_window(tmp_settings) -> None:
    redactor = Redactor(literals=[_SECRET])
    session = StreamSession(redactor=redactor)
    session.on_token("n", "LEAKME")
    session.on_token("n", "SECRET" + "z" * 54)
    session.on_event(_node_finished("n"))
    joined = _token_text(session.events)
    assert "[redacted]" in joined
    assert _SECRET not in joined
    blob = "".join(str(e.as_dict()) for e in session.events)
    assert _SECRET not in blob


def test_unauthorised_sse_does_not_stream_run_events(tmp_settings) -> None:
    from readyagents.mcp.http import bearer_authorized

    workspace = tmp_settings.workspace_path()
    path = workspace / "sse_adv.yaml"
    path.write_text(
        "name: sse-adv\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: 'ok-visible'\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(path, settings=tmp_settings, persist=True)
    other = run_workflow_file(path, settings=tmp_settings, persist=True)
    own = json.dumps(snapshot_events(state))
    assert state.run_id in own
    assert other.run_id not in own
    assert not wants_event_stream(None)
    assert wants_event_stream("text/event-stream")
    assert not wants_event_stream("application/json, text/event-stream")
    assert not bearer_authorized(authorization=None, token=_SSE_TOKEN)
    assert not bearer_authorized(
        authorization="Bearer definitely-not-the-token",
        token=_SSE_TOKEN,
    )
    assert bearer_authorized(
        authorization=f"Bearer {_SSE_TOKEN}",
        token=_SSE_TOKEN,
    )
    assert _SSE_TOKEN not in own
    assert other.run_id not in own


def test_mid_stream_cancel_no_phantom_ok_completion(tmp_settings) -> None:
    token = CancellationToken()

    class BoomLLM:
        name = "scripted"

        def complete(self, messages, *, model, tools=None, **kwargs):
            return CompletionResult(text=_FULL_COMPLETION, model=model)

        def stream(self, messages, *, model, tools=None, on_token=None, **kwargs):
            if on_token:
                on_token("FULL-")
            token.request(reason="test")
            if on_token:
                on_token("ASSEMBLED-COMPLETION-TEXT")
            return CompletionResult(text=_FULL_COMPLETION, model=model)

    spec = WorkflowSpec.model_validate(_AGENT)
    session = StreamSession()

    def save(state: RunState) -> None:
        persist_run(state, tmp_settings.runs_dir())

    ctx = ExecutionContext(
        spec,
        ToolRegistry(),
        llm=BoomLLM(),
        default_model="scripted:t",
        cancellation=token,
        stream=session,
        on_persist=save,
    )
    with pytest.raises(CancellationRequested) as exc:
        run_workflow(spec, {}, ctx)
    state = getattr(exc.value, "state", None) or ctx.usage_state
    assert state is not None
    assert state.status == "cancelled"
    assert not any(r.status == "ok" and r.output == _FULL_COMPLETION for r in state.results)
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.status == "cancelled"
    assert not any(r.status == "ok" and r.output == _FULL_COMPLETION for r in loaded.results)
    assert _FULL_COMPLETION not in _token_text(session.events)
    assert not any(e.event == "node.finished" and e.status == "ok" for e in session.events)


def test_stream_hub_cap_unsubscribe_frees_slot(tmp_settings) -> None:
    hub = StreamHub(max_streams=1)
    first = hub.try_subscribe("r1")
    assert first is not None
    assert hub.try_subscribe("r2") is None
    hub.unsubscribe("r1", first)
    freed = hub.try_subscribe("r3")
    assert freed is not None
    hub.unsubscribe("r3", freed)


def test_partial_pending_complete_is_false(tmp_settings) -> None:
    state = RunState.start("partial-adv", {})
    session = StreamSession(
        on_persist=lambda s: persist_run(s, tmp_settings.runs_dir()),
        partial_bytes=4,
    )
    session.on_token("draft", "abcdefgh", state=state)
    assert state.pending is not None
    assert state.pending.get("complete") is False
    assert state.pending.get("partial") is True
    assert not any(r.status == "ok" for r in state.results)
    loaded = load_run(tmp_settings.runs_dir(), state.run_id)
    assert loaded.pending is not None
    assert loaded.pending.get("complete") is False
    events = snapshot_events(loaded)
    assert not any(row.get("event") == "node.finished" for row in events)
    assert not any(row.get("event") == "run.finished" for row in events)
    partials = [row for row in events if row.get("event") == "node.partial"]
    assert partials
    assert all(row.get("complete") is False for row in partials)
    streamed = [e for e in session.events if e.event == "node.partial"]
    assert streamed
    assert all(e.complete is False for e in streamed)
