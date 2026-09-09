from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.errors import CassetteMiss
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.cache import LLMCache, completion_key, tool_call_key
from readyagents.replay.cassette import Cassette
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
