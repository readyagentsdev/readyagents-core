"""V2-08 model router: extras, matrix, strategies, explain, record, budgets."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import (
    CapabilityError,
    ConfigError,
    LLMError,
    RouteBudgetExceeded,
    RoutingError,
    missing_extra_message,
)
from readyagents.llm.base import Message
from readyagents.llm.bedrock_provider import BedrockProvider
from readyagents.llm.capabilities import assert_capable, load_capability_matrix, lookup_model
from readyagents.llm.gemini_provider import GeminiProvider
from readyagents.llm.resilience import CircuitBreaker
from readyagents.llm.vertex_provider import VertexProvider
from readyagents.replay.cassette import Cassette
from readyagents.routing.select import explain_route, select_route
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def _plain(text: str) -> str:
    import re

    return re.sub(r"\s+", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _spec_with_routing(rules: list[dict], *, node_id: str = "draft", **node_extra: object) -> dict:
    node = {
        "id": node_id,
        "type": "agent",
        "prompt": "classify this",
        "output_key": "out",
        **node_extra,
    }
    return {
        "name": "routed",
        "default_model": "openai:gpt-4o",
        "routing": {"version": 1, "rules": rules},
        "nodes": [node],
    }


class TrapLLM:
    name = "trap"

    def complete(self, messages, *, model, tools=None, **kwargs):
        raise AssertionError(f"complete() must not run (model={model})")


def test_missing_extra_message_gemini_bedrock_vertex() -> None:
    assert "readyagentsdev[gemini]" in missing_extra_message("Gemini", "gemini")
    assert "readyagentsdev[bedrock]" in missing_extra_message("Bedrock", "bedrock")
    assert "readyagentsdev[vertex]" in missing_extra_message("Vertex", "vertex")


def test_gemini_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.genai", None)
    with pytest.raises(LLMError, match=r"readyagentsdev\[gemini\]"):
        GeminiProvider("k").complete([Message(role="user", content="hi")], model="gemini-2.0-flash")


def test_bedrock_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(LLMError, match=r"readyagentsdev\[bedrock\]"):
        BedrockProvider(access_key="a", secret_key="s", region="us-east-1").complete(
            [Message(role="user", content="hi")], model="amazon.nova-lite-v1:0"
        )


def test_vertex_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "vertexai", None)
    with pytest.raises(LLMError, match=r"readyagentsdev\[vertex\]"):
        VertexProvider(project="p").complete(
            [Message(role="user", content="hi")], model="gemini-2.0-flash"
        )


def test_gemini_complete_tools_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")

    class _Models:
        def generate_content(self, model, contents=None, config=None):
            self.seen = {"model": model, "contents": contents, "config": config}

            class _Usage:
                prompt_token_count = 3
                candidates_token_count = 2

            class _Resp:
                text = '{"ok": true}'
                usage_metadata = _Usage()
                candidates = []

            return _Resp()

    class _Client:
        def __init__(self, api_key=None):
            self.models = _Models()

    fake_genai.Client = _Client
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    result = GeminiProvider("k").complete(
        [Message(role="user", content="hi")],
        model="gemini-2.0-flash",
        tools=[{"name": "calc", "description": "c", "schema": {"type": "object"}}],
        structured=True,
    )
    assert result.text.startswith("{")
    assert result.usage.get("prompt_tokens") == 3


def test_bedrock_complete_and_error_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("boto3")

    class _Client:
        def converse(self, **payload):
            self.payload = payload
            return {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "usage": {"inputTokens": 4, "outputTokens": 1, "totalTokens": 5},
            }

    fake.client = lambda **kwargs: _Client()
    monkeypatch.setitem(sys.modules, "boto3", fake)
    result = BedrockProvider(access_key="a", secret_key="s", region="us-east-1").complete(
        [Message(role="system", content="s"), Message(role="user", content="hi")],
        model="amazon.nova-lite-v1:0",
        tools=[{"name": "calc", "schema": {"type": "object"}}],
    )
    assert result.text == "ok"
    assert result.usage.get("prompt_tokens") == 4

    class _Boom:
        def converse(self, **payload):
            raise RuntimeError("throttled")

    fake.client = lambda **kwargs: _Boom()
    with pytest.raises(LLMError, match="Bedrock request failed"):
        BedrockProvider(access_key="a", secret_key="s", region="us-east-1").complete(
            [Message(role="user", content="hi")], model="amazon.nova-lite-v1:0"
        )


def test_vertex_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_vertex = types.ModuleType("vertexai")
    fake_models = types.ModuleType("vertexai.generative_models")

    class _Model:
        def __init__(self, name):
            self.name = name

        def generate_content(self, contents, **kwargs):
            class _Resp:
                text = "vertex-ok"
                usage_metadata = None
                candidates = []

            return _Resp()

    fake_vertex.init = lambda **kwargs: None
    fake_models.GenerativeModel = _Model
    fake_vertex.generative_models = fake_models
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertex)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", fake_models)
    result = VertexProvider(project="p").complete(
        [Message(role="user", content="hi")], model="gemini-2.0-flash"
    )
    assert result.text == "vertex-ok"


def test_capability_matrix_loads_and_lookup() -> None:
    matrix = load_capability_matrix()
    assert matrix.version == 1
    caps = lookup_model("openai:gpt-4o-mini", matrix=matrix)
    assert caps is not None
    assert caps.tool_calling is True
    ollama = lookup_model("ollama:llama3", matrix=matrix)
    assert ollama is not None and ollama.local is True


def test_capability_unsupported_before_spend() -> None:
    with pytest.raises(CapabilityError):
        assert_capable("ollama:llama3", {"tool_calling": True})
    llm = TrapLLM()
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "require": {"tool_calling": True},
                "pool": ["ollama:llama3"],
            }
        ]
    )
    with pytest.raises(RoutingError):
        run_workflow_spec(spec, llm=llm)


def test_malformed_capability_override_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "caps.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_CAPABILITY_MATRIX", str(bad))
    from readyagents.llm.capabilities import clear_capability_cache

    clear_capability_cache()
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_capability_matrix()
    monkeypatch.delenv("READYAGENTS_CAPABILITY_MATRIX")
    clear_capability_cache()


def test_stale_capability_override_refused(tmp_path: Path) -> None:
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2020-01-01T00:00:00Z",
                "warn_after_days": 90,
                "models": {
                    "openai:gpt-4o-mini": {
                        "context_window": 128000,
                        "tool_calling": True,
                        "structured_output": True,
                        "media": False,
                        "streaming": True,
                        "local": False,
                        "latency_class": "fast",
                        "quality_class": "standard",
                    }
                },
                "prefixes": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="stale"):
        load_capability_matrix(stale)


def test_first_matching_rule_wins() -> None:
    llm = ScriptedLLM()
    llm.enqueue("pinned")
    spec = _spec_with_routing(
        [
            {"match": {"node": "draft"}, "pin": "anthropic:claude-3-5-haiku"},
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "pool": ["openai:gpt-4o-mini"],
            },
        ]
    )
    state = run_workflow_spec(spec, llm=llm)
    assert llm.calls[0]["model"] == "claude-3-5-haiku"
    assert state.metadata["routes"][0]["strategy"] == "pin"


def test_cheapest_capable_uses_price_table() -> None:
    llm = ScriptedLLM()
    llm.enqueue("cheap")
    spec = _spec_with_routing(
        [
            {
                "match": {"node_tag": "classify"},
                "strategy": "cheapest_capable",
                "require": {"tool_calling": True},
                "pool": ["openai:gpt-4o", "openai:gpt-4o-mini"],
            }
        ],
        tags=["classify"],
    )
    state = run_workflow_spec(spec, llm=llm)
    assert llm.calls[0]["model"] == "gpt-4o-mini"
    assert state.metadata["routes"][0]["strategy"] == "cheapest_capable"


def test_fastest_and_highest_quality() -> None:
    llm = ScriptedLLM()
    llm.enqueue("fast")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "fastest",
                "pool": ["openai:gpt-4o", "openai:gpt-4o-mini"],
            }
        ]
    )
    run_workflow_spec(spec, llm=llm)
    assert llm.calls[0]["model"] == "gpt-4o-mini"
    llm2 = ScriptedLLM()
    llm2.enqueue("best")
    spec2 = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "highest-quality",
                "pool": ["anthropic:claude-3-5-haiku", "anthropic:claude-opus-4"],
            }
        ]
    )
    run_workflow_spec(spec2, llm=llm2)
    assert llm2.calls[0]["model"] == "claude-opus-4"


def test_pin_honoured() -> None:
    llm = ScriptedLLM()
    llm.enqueue("sonnet")
    spec = _spec_with_routing([{"match": {"node": "draft"}, "pin": "anthropic:claude-sonnet-4-5"}])
    state = run_workflow_spec(spec, llm=llm)
    assert llm.calls[0]["model"] == "claude-sonnet-4-5"
    assert state.metadata["routes"][0]["pin"] == "anthropic:claude-sonnet-4-5"


def test_explain_matches_run() -> None:
    spec_raw = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "pool": ["openai:gpt-4o", "openai:gpt-4o-mini"],
            }
        ]
    )
    workflow = WorkflowSpec.model_validate(spec_raw)
    node = workflow.nodes[0]
    explained = explain_route(workflow, node, legacy_candidates=["openai:gpt-4o"])
    llm = ScriptedLLM()
    llm.enqueue("x")
    state = run_workflow_spec(spec_raw, llm=llm)
    assert explained.model == "openai:gpt-4o-mini"
    assert state.metadata["routes"][0]["model"] == explained.model
    assert llm.calls[0]["model"] == "gpt-4o-mini"


def test_models_cli_list_show_route_twice_no_provider() -> None:
    first = runner.invoke(app, ["models", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    help_text = _plain(first.stdout)
    assert "list" in help_text
    assert "show" in help_text
    assert "route" in help_text
    listed = [runner.invoke(app, ["models", "list"]) for _ in range(2)]
    for result in listed:
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "openai:gpt-4o-mini" in result.stdout
    shown = [runner.invoke(app, ["models", "show", "openai:gpt-4o-mini"]) for _ in range(2)]
    for result in shown:
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "tool_calling" in _plain(result.stdout) or "tool_calling=True" in result.stdout
    wf = ROOT / "examples" / "calc_pipeline.yaml"
    routed = [
        runner.invoke(app, ["models", "route", str(wf), "--node", "add", "--explain"])
        for _ in range(2)
    ]
    for result in routed:
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "add ->" in result.stdout or "node add" in result.stdout


def _override_matrix(*, tool_calling: bool = True) -> dict:
    return {
        "version": 1,
        "updated_at": "2026-09-11T00:00:00Z",
        "warn_after_days": 90,
        "models": {
            "ollama:llama3": {
                "context_window": 8192,
                "tool_calling": tool_calling,
                "structured_output": False,
                "media": False,
                "streaming": True,
                "local": True,
                "latency_class": "standard",
                "quality_class": "compact",
            }
        },
        "prefixes": {},
    }


def test_capability_override_honoured_before_spend(tmp_path: Path) -> None:
    path = tmp_path / "caps.json"
    path.write_text(json.dumps(_override_matrix(tool_calling=True)), encoding="utf-8")
    llm = ScriptedLLM()
    llm.enqueue("from-override")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "require": {"tool_calling": True},
                "pool": ["ollama:llama3"],
            }
        ]
    )
    spec["routing"]["capability_matrix"] = str(path)
    state = run_workflow_spec(spec, llm=llm)
    assert state.status == "succeeded"
    assert llm.calls[0]["model"] == "llama3"
    assert state.output_keys["out"] == "from-override"


def test_fallback_route_stamped_and_offline_replay_uses_recorded_model() -> None:
    tape = Cassette.new(run_id="r-fb", workflow="routed")
    llm = ScriptedLLM()
    llm.enqueue(error=LLMError("primary-down"), model="gpt-4o-mini")
    llm.enqueue("from-fallback", model="gpt-4o")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "pool": ["openai:gpt-4o-mini", "openai:gpt-4o"],
            }
        ]
    )
    first = run_workflow_spec(spec, llm=llm, cassette=tape, recording=True)
    assert first.output_keys["out"] == "from-fallback"
    assert first.metadata["routes"][0]["model"] == "openai:gpt-4o"
    assert first.metadata["routes"][0]["fallback"] is True
    llm_entries = [e for e in tape.entries.values() if e.get("kind") == "llm"]
    assert llm_entries
    assert llm_entries[0].get("route", {}).get("model") == "openai:gpt-4o"
    assert llm_entries[0].get("route", {}).get("fallback") is True

    class Boom:
        name = "boom"

        def complete(self, *a, **k):
            raise AssertionError("offline replay must not call complete")

    second = run_workflow_spec(spec, llm=Boom(), cassette=tape, offline=True)
    assert second.output_keys["out"] == "from-fallback"
    assert second.metadata["routes"][0]["model"] == "openai:gpt-4o"
    assert second.metadata["routes"][0]["fallback"] is True


def test_offline_replay_uses_recorded_route_when_circuit_now_closed() -> None:
    tape = Cassette.new(run_id="r-circ", workflow="routed")
    llm = ScriptedLLM()
    llm.enqueue("from-gpt4o", model="gpt-4o")
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure("openai:gpt-4o-mini")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "pool": ["openai:gpt-4o-mini", "openai:gpt-4o"],
            }
        ]
    )
    first = run_workflow_spec(spec, llm=llm, cassette=tape, recording=True, circuit_breaker=breaker)
    assert first.metadata["routes"][0]["model"] == "openai:gpt-4o"
    llm_entries = [e for e in tape.entries.values() if e.get("kind") == "llm"]
    assert llm_entries[0].get("route", {}).get("model") == "openai:gpt-4o"

    class Boom:
        name = "boom"

        def complete(self, *a, **k):
            raise AssertionError("offline replay must not call complete")

    second = run_workflow_spec(spec, llm=Boom(), cassette=tape, offline=True)
    assert second.output_keys["out"] == "from-gpt4o"
    assert second.metadata["routes"][0]["model"] == "openai:gpt-4o"


def test_route_record_and_offline_replay_traps_complete() -> None:
    tape = Cassette.new(run_id="r1", workflow="routed")
    llm = ScriptedLLM()
    llm.enqueue("recorded")
    spec = _spec_with_routing(
        [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
    )
    first = run_workflow_spec(spec, llm=llm, cassette=tape, recording=True)
    assert first.metadata["routes"][0]["model"] == "openai:gpt-4o-mini"
    llm_entries = [e for e in tape.entries.values() if e.get("kind") == "llm"]
    assert llm_entries
    assert llm_entries[0].get("route", {}).get("model") == "openai:gpt-4o-mini"

    class Boom:
        name = "boom"

        def complete(self, *a, **k):
            raise AssertionError("offline replay must not call complete")

    second = run_workflow_spec(spec, llm=Boom(), cassette=tape, offline=True)
    assert second.output_keys["out"] == "recorded"
    assert second.metadata["routes"][0]["model"] == "openai:gpt-4o-mini"


def test_route_budget_distinct_from_run_cap() -> None:
    llm = ScriptedLLM()
    llm.enqueue("too-dear")
    spec = _spec_with_routing(
        [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini", "id": "classify"}],
    )
    spec["routing"]["budgets"] = {"classify": {"max_tokens": 1}}
    with pytest.raises(RouteBudgetExceeded) as caught:
        run_workflow_spec(spec, llm=llm)
    assert caught.value.kind == "route_tokens"
    assert caught.value.route == "classify"
    assert llm.calls == []


def test_open_circuit_skipped_in_routing() -> None:
    llm = ScriptedLLM()
    llm.enqueue("fallback")
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure("openai:gpt-4o-mini")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "cheapest_capable",
                "pool": ["openai:gpt-4o-mini", "openai:gpt-4o"],
            }
        ]
    )
    state = run_workflow_spec(spec, llm=llm, circuit_breaker=breaker)
    assert llm.calls[0]["model"] == "gpt-4o"
    assert "openai:gpt-4o-mini" in state.metadata["routes"][0]["skipped"]


def test_unknown_routing_strategy_refused() -> None:
    with pytest.raises(Exception, match="strategy"):
        WorkflowSpec.model_validate(
            _spec_with_routing([{"match": {"node": "draft"}, "strategy": "magic_quality"}])
        )


def test_local_only_never_selects_hosted() -> None:
    llm = ScriptedLLM()
    llm.enqueue("local")
    spec = _spec_with_routing(
        [
            {
                "match": {"node": "draft"},
                "strategy": "local_only",
                "pool": ["openai:gpt-4o-mini", "ollama:llama3"],
            }
        ]
    )
    state = run_workflow_spec(spec, llm=llm)
    assert llm.calls[0]["model"] == "llama3"
    assert state.metadata["routes"][0]["local_only"] is True
    assert state.metadata["routes"][0]["model"] == "ollama:llama3"


def test_select_route_no_policy_is_legacy() -> None:
    workflow = WorkflowSpec.model_validate(
        {
            "name": "plain",
            "default_model": "openai:gpt-4o-mini",
            "nodes": [{"id": "a", "type": "agent", "prompt": "hi"}],
        }
    )
    decision = select_route(workflow, workflow.nodes[0], legacy_candidates=["openai:gpt-4o-mini"])
    assert decision.policy is False
    assert decision.model == "openai:gpt-4o-mini"
    assert decision.reason == "no_routing_policy"
