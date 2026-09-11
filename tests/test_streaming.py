"""Shipped --stream path: NDJSON, identity, redaction, cancel, partials, SSE."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import CancellationRequested
from readyagents.llm.base import CompletionResult, Message
from readyagents.policy import Redactor
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.workflow.cancellation import CancellationToken
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.stream import (
    IncrementalRedactor,
    StreamHub,
    StreamSession,
    VoiceReadySession,
    format_sse,
    snapshot_events,
    wants_event_stream,
)

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]

_AGENT = {
    "name": "stream_agent",
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


def _ndjson(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        raw = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if not raw:
            continue
        if raw.startswith("{"):
            rows.append(json.loads(raw))
    return rows


def test_cli_without_stream_twice_same_keys() -> None:
    first = runner.invoke(
        app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    second = runner.invoke(
        app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--json", "--no-persist"]
    )
    assert first.exit_code == 0
    assert second.exit_code == 0
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    for key in ("ok", "command", "run_id", "status", "node_results"):
        assert key in a and key in b
    assert a["command"] == "run"


def test_cli_stream_json_ndjson_no_rich() -> None:
    first = runner.invoke(
        app,
        [
            "run",
            str(ROOT / "examples" / "calc_pipeline.yaml"),
            "--stream",
            "--json",
            "--no-persist",
        ],
    )
    second = runner.invoke(
        app,
        [
            "run",
            str(ROOT / "examples" / "calc_pipeline.yaml"),
            "--stream",
            "--json",
            "--no-persist",
        ],
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    assert "\x1b[" not in first.stdout
    events = _ndjson(first.stdout)
    kinds = [row["event"] for row in events]
    assert "run.started" in kinds
    assert "node.started" in kinds
    assert "node.finished" in kinds
    assert "run.finished" in kinds
    again = [row["event"] for row in _ndjson(second.stdout)]
    assert set(kinds) == set(again)


def test_streamed_and_nonstreamed_outputs_match() -> None:
    llm_a = ScriptedLLM().enqueue("hello-stream")
    llm_b = ScriptedLLM().enqueue("hello-stream")
    plain = run_workflow_spec(_AGENT, llm=llm_a)
    session = StreamSession()
    streamed = run_workflow_spec(_AGENT, llm=llm_b, stream=session)
    assert plain.status == streamed.status == "succeeded"
    assert plain.output_keys == streamed.output_keys == {"text": "hello-stream"}
    assert [r.output for r in plain.results] == [r.output for r in streamed.results]
    token_events = [e for e in session.events if e.event == "token"]
    assert token_events
    assert "".join(e.text or "" for e in token_events) == "hello-stream"
    finished = [e for e in session.events if e.event == "node.finished"]
    assert finished
    assert streamed.results[0].ttft_ms is not None or finished[0].ttft_ms is not None


def test_incremental_redaction_split_secret() -> None:
    redactor = Redactor(literals=["SUPERSECRET"])
    inc = IncrementalRedactor(redactor)
    leaked = inc.push("SUPER") + inc.push("SECRET and more") + inc.flush()
    assert "SUPERSECRET" not in leaked
    assert "[redacted]" in leaked


def test_stream_session_never_emits_split_secret() -> None:
    redactor = Redactor(literals=["SUPERSECRET"])
    session = StreamSession(redactor=redactor)
    session.on_token("a", "SUPER")
    session.on_token("a", "SECRET-tail")
    session.on_event(
        type(
            "E",
            (),
            {
                "name": "node.finished",
                "run_id": "r",
                "node_id": "a",
                "status": "ok",
                "duration_ms": 1,
            },
        )()
    )
    blob = "".join(e.text or "" for e in session.events if e.event == "token")
    assert "SUPERSECRET" not in blob
    assert "[redacted]" in blob


def test_output_schema_buffers_tokens() -> None:
    llm = ScriptedLLM().enqueue('{"ok": true}')
    spec = {
        "name": "contracted",
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "hi",
                "model": "scripted:t",
                "output_key": "text",
                "output_schema": {"type": "object"},
            }
        ],
    }
    session = StreamSession()
    state = run_workflow_spec(spec, llm=llm, stream=session)
    assert state.status == "succeeded"
    assert not [e for e in session.events if e.event == "token"]


def test_mid_stream_cancel_no_phantom_result() -> None:
    token = CancellationToken()

    class BoomLLM:
        name = "scripted"

        def complete(self, messages, *, model, tools=None, **kwargs):
            return CompletionResult(text="secret-complete", model=model)

        def stream(self, messages, *, model, tools=None, on_token=None, **kwargs):
            if on_token:
                on_token("hel")
            token.request(reason="test")
            if on_token:
                on_token("lo")
            return CompletionResult(text="secret-complete", model=model)

    workflow = WorkflowSpec.model_validate(_AGENT)
    session = StreamSession()
    from readyagents.tools import ToolRegistry

    ctx = ExecutionContext(
        workflow,
        ToolRegistry(),
        llm=BoomLLM(),
        default_model="scripted:t",
        cancellation=token,
        stream=session,
    )
    try:
        run_workflow(workflow, {}, ctx)
        state = ctx.usage_state
    except CancellationRequested as exc:
        state = getattr(exc, "state", None) or ctx.usage_state
    assert state is not None
    assert state.status == "cancelled"
    assert not any(r.status == "ok" and r.output == "secret-complete" for r in state.results)


def test_partial_not_complete(tmp_path: Path, tmp_settings) -> None:
    persisted: list[dict] = []

    def save(state):
        persisted.append(dict(state.pending or {}))

    session = StreamSession(on_persist=save, partial_bytes=4)
    session.on_token(
        "a",
        "abcdefgh",
        state=type("S", (), {"run_id": "r", "pending_node": None, "pending": None})(),
    )
    assert persisted
    assert persisted[-1].get("complete") is False
    assert persisted[-1].get("partial") is True


def test_voice_ready_barge_in() -> None:
    token = CancellationToken()
    voice = VoiceReadySession(token)
    voice.push_input("hello")
    seen: list[str] = []
    voice.on_partial(seen.append)
    voice.emit_partial("hel")
    voice.barge_in()
    assert voice.input_chunks == ["hello"]
    assert seen == ["hel"]
    assert token.is_requested()


def test_sse_snapshot_and_cap(tmp_settings, examples_dir: Path) -> None:
    state = run_workflow_file(
        examples_dir / "calc_pipeline.yaml",
        settings=tmp_settings,
        persist=True,
    )
    events = snapshot_events(state)
    kinds = [e["event"] for e in events]
    assert "run.started" in kinds
    assert "run.finished" in kinds
    hub = StreamHub(max_streams=1)
    q = hub.try_subscribe("r1")
    assert q is not None
    assert hub.try_subscribe("r2") is None
    hub.unsubscribe("r1", q)
    assert hub.try_subscribe("r3") is not None


def test_mcp_sse_http_emits_durable_events(tmp_settings, examples_dir: Path) -> None:
    state = run_workflow_file(
        examples_dir / "calc_pipeline.yaml",
        settings=tmp_settings,
        persist=True,
    )
    other = run_workflow_file(
        examples_dir / "calc_pipeline.yaml",
        settings=tmp_settings,
        persist=True,
    )
    events = snapshot_events(state)
    kinds = [row["event"] for row in events]
    assert "run.finished" in kinds
    blob = json.dumps(events)
    assert state.run_id in blob
    assert other.run_id not in blob
    assert not wants_event_stream(None)
    assert not wants_event_stream("*/*")
    assert not wants_event_stream("application/json")
    assert wants_event_stream("text/event-stream")
    assert wants_event_stream("application/json, text/event-stream")
    frame = format_sse({"event": "run.finished", "run_id": state.run_id})
    assert frame.startswith(b"data: ")
    assert b"run.finished" in frame
    assert state.run_id.encode() in frame


def test_scripted_stream_matches_complete() -> None:
    llm = ScriptedLLM().enqueue("abcdef", model="m")
    a = llm.complete([Message(role="user", content="x")], model="m")
    llm2 = ScriptedLLM().enqueue("abcdef", model="m")
    chunks: list[str] = []
    b = llm2.stream(
        [Message(role="user", content="x")],
        model="m",
        on_token=chunks.append,
    )
    assert a.text == b.text == "abcdef"
    assert "".join(chunks) == "abcdef"
