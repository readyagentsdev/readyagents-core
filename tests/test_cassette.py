from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.decide.types import Answer, Decision, Question
from readyagents.errors import CassetteMiss
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.cache import LLMCache, completion_key, tool_call_key
from readyagents.replay.cassette import Cassette, classify_node_type
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.testing.recorded import RecordedLLM


def test_completion_key_matches_llm_cache() -> None:
    messages = [Message(role="user", content="hello")]
    tools = [{"name": "calc", "description": "x"}]
    cache = LLMCache(Path("unused-cache"))
    assert cache.key("mock:m", messages, tools) == completion_key("mock:m", messages, tools)
    assert completion_key("mock:m", messages, tools) != completion_key(
        "mock:other", messages, tools
    )
    assert completion_key("mock:m", messages, tools) != completion_key(
        "mock:m", [Message(role="user", content="other")], tools
    )
    assert completion_key("mock:m", messages, tools) != completion_key("mock:m", messages, [])


def test_occurrence_orders_identical_calls(tmp_path: Path) -> None:
    tape = Cassette.new(run_id="r1", workflow="w")
    messages = [Message(role="user", content="same")]
    first = CompletionResult(text="one", model="m")
    second = CompletionResult(text="two", model="m")
    tape.record_llm(node_id="n", model="m", messages=messages, tools=None, result=first)
    tape.record_llm(node_id="n", model="m", messages=messages, tools=None, result=second)
    path = tmp_path / "c.json"
    tape.save(path, root=tmp_path)
    loaded = Cassette.load(path)
    assert loaded.replay_llm(node_id="n", model="m", messages=messages, tools=None).text == "one"
    assert loaded.replay_llm(node_id="n", model="m", messages=messages, tools=None).text == "two"


def test_legacy_list_tape_still_loads(tmp_path: Path) -> None:
    cassette = tmp_path / "legacy.json"
    inner = ScriptedLLM()
    inner.enqueue("hello-offline", model="m")
    recorder = RecordedLLM(cassette, inner=inner)
    first = run_workflow_spec(
        {
            "name": "rec",
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "hi", "model": "mock:m", "output_key": "t"}
            ],
        },
        llm=recorder,
    )
    assert first.output_keys["t"] == "hello-offline"
    loaded = Cassette.load(cassette)
    assert loaded.positional_fallback is True
    replay = RecordedLLM(cassette)
    second = run_workflow_spec(
        {
            "name": "rec",
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "hi", "model": "mock:m", "output_key": "t"}
            ],
        },
        llm=replay,
    )
    assert second.output_keys["t"] == "hello-offline"


def test_edited_workflow_is_a_miss_not_a_wrong_answer(tmp_path: Path) -> None:
    tape = Cassette.new(run_id="r1", workflow="w")
    messages = [Message(role="user", content="original")]
    tape.record_llm(
        node_id="draft",
        model="m",
        messages=messages,
        tools=None,
        result=CompletionResult(text="secret-answer", model="m"),
    )
    tape.save(tmp_path / "c.json", root=tmp_path)
    loaded = Cassette.load(tmp_path / "c.json")
    with pytest.raises(CassetteMiss) as info:
        loaded.replay_llm(
            node_id="draft",
            model="m",
            messages=[Message(role="user", content="edited prompt")],
            tools=None,
        )
    assert info.value.node_id == "draft"
    assert info.value.nearest_key
    assert "miss" in str(info.value).lower() or "missing" in info.value.reason


def test_tool_call_key_stable() -> None:
    assert tool_call_key("now", {}) == tool_call_key("now", {})
    assert tool_call_key("now", {}) != tool_call_key("http_get", {"url": "x"})


def test_decide_record_replay_and_key_absent(tmp_path: Path) -> None:
    assert classify_node_type("decide") == "sealed"
    questions = {
        "department": Question(
            type="choice",
            instructions="team",
            criteria={"billing": "b", "technical": "t", "sales": "s"},
        )
    }
    decision = Decision(
        answers={"department": Answer(type="choice", choice="billing", confidence=0.9)},
        model="jev-1.13.0",
        decider="jev",
        usage={"prompt_tokens": 3, "completion_tokens": 0},
    )
    tape = Cassette.new(run_id="r1", workflow="w")
    tape.record_decide(
        node_id="triage",
        model="jev-1.13.0",
        state="checkout is down",
        questions=questions,
        decision=decision,
    )
    path = tmp_path / "c.json"
    tape.save(path, root=tmp_path)
    blob = path.read_text(encoding="utf-8")
    assert "sk-test-typesafe-not-a-real-key" not in blob
    loaded = Cassette.load(path)
    replayed = loaded.replay_decide(
        node_id="triage",
        model="jev-1.13.0",
        state="checkout is down",
        questions=questions,
    )
    assert replayed.answers["department"].choice == "billing"
    assert replayed.model == "jev-1.13.0"
    assert replayed.decider == "jev"

    def boom(*_a, **_k):
        raise AssertionError("replay must not open a transport")

    from readyagents.decide.jev import reset_transport, use_transport

    token = use_transport(boom)
    try:
        loaded2 = Cassette.load(path)
        again = loaded2.replay_decide(
            node_id="triage",
            model="jev-1.13.0",
            state="checkout is down",
            questions=questions,
        )
        assert again.answers["department"].choice == "billing"
    finally:
        reset_transport(token)

    missing = Cassette.load(path)
    with pytest.raises(CassetteMiss, match="triage"):
        missing.replay_decide(
            node_id="triage",
            model="jev-1.13.0",
            state="DIFFERENT STATE",
            questions=questions,
        )


def test_decide_occurrence_order(tmp_path: Path) -> None:
    questions = {"q": Question(type="noul", instructions="t")}
    tape = Cassette.new(run_id="r1", workflow="w")
    tape.record_decide(
        node_id="n",
        model="m",
        state="s",
        questions=questions,
        decision=Decision(
            answers={"q": Answer(type="noul", noul=0.1, confidence=0.8)},
            model="m",
            decider="fake",
        ),
    )
    tape.record_decide(
        node_id="n",
        model="m",
        state="s",
        questions=questions,
        decision=Decision(
            answers={"q": Answer(type="noul", noul=0.9, confidence=0.8)},
            model="m",
            decider="fake",
        ),
    )
    path = tmp_path / "c.json"
    tape.save(path, root=tmp_path)
    loaded = Cassette.load(path)
    first = loaded.replay_decide(node_id="n", model="m", state="s", questions=questions)
    second = loaded.replay_decide(node_id="n", model="m", state="s", questions=questions)
    assert first.answers["q"].noul == 0.1
    assert second.answers["q"].noul == 0.9


def test_decide_blocked_is_unsealable() -> None:
    questions = {"q": Question(type="noul", instructions="t")}
    tape = Cassette.new(run_id="r1", workflow="w")
    tape.record_decide(
        node_id="n",
        model="m",
        state="secret",
        questions=questions,
        decision=Decision(
            answers={"q": Answer(type="noul", noul=0.5)},
            model="m",
            decider="fake",
        ),
        blocked=True,
    )
    assert "n" in tape.blocked_nodes
    assert "n" in tape.report.unsealable
    with pytest.raises(CassetteMiss, match="redacted_blocked"):
        tape.replay_decide(node_id="n", model="m", state="secret", questions=questions)
