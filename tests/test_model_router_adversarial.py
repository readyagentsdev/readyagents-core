"""Hostile cases for V2-08 model routing. Drive shipped APIs; fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readyagents.config import Settings
from readyagents.errors import ConfigError, LLMError, NodeError, RoutingError
from readyagents.firewall.taint import untrusted
from readyagents.llm.base import CompletionResult
from readyagents.llm.bedrock_provider import BedrockProvider
from readyagents.llm.capabilities import clear_capability_cache, load_capability_matrix
from readyagents.llm.gemini_provider import GeminiProvider
from readyagents.llm.openai_provider import OpenAIProvider
from readyagents.llm.registry import get_provider
from readyagents.llm.resilience import CircuitBreaker
from readyagents.llm.vertex_provider import VertexProvider
from readyagents.routing.select import select_route
from readyagents.routing.taint import resolve_routing_taint
from readyagents.sovereign.egress import install_guard
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState

_HOSTED_MODEL_IDS = frozenset(
    {
        "gpt-4o",
        "gpt-4o-mini",
        "claude-3-5-haiku",
        "claude-sonnet-4-5",
        "claude-opus-4",
        "gemini-2.0-flash",
        "amazon.nova-lite-v1:0",
    }
)
_HOSTED_PREFIXES = ("gpt-", "claude-", "gemini-", "amazon.")


class TrapLLM:
    name = "trap"

    def complete(self, messages, *, model, tools=None, **kwargs):
        raise AssertionError(f"complete() must not run (model={model})")


class HostedTrapLLM:
    """Accepts only local model ids; any hosted id fails the test."""

    name = "hosted-trap"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def complete(self, messages, *, model, tools=None, **kwargs):
        blob = "\n".join(getattr(m, "content", "") or "" for m in messages)
        self.calls.append(model)
        self.prompts.append(blob)
        if model in _HOSTED_MODEL_IDS or any(model.startswith(p) for p in _HOSTED_PREFIXES):
            raise AssertionError(f"hosted complete() forbidden (model={model})")
        return CompletionResult(text="local-ok", model=model, usage={})


def _leak_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            name="leak",
            description="emit untrusted text",
            handler=lambda: "tainted-blob",
            schema={"type": "object", "properties": {}},
        )
    )
    return registry


def _tainted_agent_spec(*, rules: list[dict], **agent_extra: object) -> dict:
    agent = {
        "id": "draft",
        "type": "agent",
        "prompt": "classify {{page}}",
        "output_key": "out",
        **agent_extra,
    }
    return {
        "name": "adv-residency",
        "default_model": "openai:gpt-4o-mini",
        "fallback_models": [
            "openai:gpt-4o",
            "anthropic:claude-3-5-haiku",
            "gemini:gemini-2.0-flash",
            "bedrock:amazon.nova-lite-v1:0",
            "vertex:gemini-2.0-flash",
            "groq:llama-3.1-8b-instant",
        ],
        "routing": {"version": 1, "rules": rules},
        "nodes": [
            {
                "id": "seed",
                "type": "tool",
                "tool": "leak",
                "arguments": {},
                "output_key": "page",
                "next": "draft",
            },
            agent,
        ],
    }


def _matrix_caps() -> dict:
    return {
        "context_window": 128000,
        "tool_calling": True,
        "structured_output": True,
        "media": False,
        "streaming": True,
        "local": False,
        "latency_class": "fast",
        "quality_class": "standard",
    }


def _config_failure(exc: BaseException) -> ConfigError:
    if isinstance(exc, ConfigError):
        return exc
    if isinstance(exc, NodeError) and isinstance(exc.cause, ConfigError):
        return exc.cause
    raise AssertionError(f"expected ConfigError (got {type(exc).__name__}: {exc})") from exc


# --- 1. Residency bypass via fallback_models ---


def test_local_only_refuses_hosted_fallback_models_with_tainted_prompt() -> None:
    tools = _leak_tools()
    llm = HostedTrapLLM()
    spec = _tainted_agent_spec(
        rules=[
            {
                "match": {"node": "draft"},
                "strategy": "local_only",
                "pool": ["openai:gpt-4o-mini", "ollama:llama3"],
            }
        ],
        fallback_models=["openai:gpt-4o", "anthropic:claude-3-5-haiku", "gemini:gemini-2.0-flash"],
    )
    state = run_workflow_spec(spec, llm=llm, tools=tools)
    assert state.status == "succeeded"
    route = state.metadata["routes"][0]
    assert route["model"] == "ollama:llama3"
    assert route["local_only"] is True
    assert route["taint"] == "untrusted"
    assert llm.calls == ["llama3"]
    assert "openai:gpt-4o-mini" in route["skipped"]


def test_local_only_local_failure_does_not_fall_through_to_hosted() -> None:
    tools = _leak_tools()
    llm = ScriptedLLM()
    llm.enqueue(error=LLMError("ollama down"), model="llama3")
    spec = _tainted_agent_spec(
        rules=[
            {
                "match": {"node": "draft"},
                "strategy": "local_only",
                "pool": ["ollama:llama3", "openai:gpt-4o"],
            }
        ],
        fallback_models=["openai:gpt-4o-mini", "bedrock:amazon.nova-lite-v1:0"],
    )
    with pytest.raises((LLMError, NodeError)):
        run_workflow_spec(spec, llm=llm, tools=tools)
    assert [c["model"] for c in llm.calls] == ["llama3"]


def test_taint_untrusted_pin_to_hosted_raises_routing_error() -> None:
    tools = _leak_tools()
    llm = TrapLLM()
    spec = _tainted_agent_spec(
        rules=[{"match": {"taint": "untrusted"}, "pin": "openai:gpt-4o-mini"}],
        fallback_models=["openai:gpt-4o", "anthropic:claude-3-5-haiku"],
    )
    with pytest.raises(RoutingError) as caught:
        run_workflow_spec(spec, llm=llm, tools=tools)
    assert caught.value.taint == "untrusted"


def test_local_only_open_circuit_does_not_select_hosted() -> None:
    tools = _leak_tools()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure("ollama:llama3")
    spec = _tainted_agent_spec(
        rules=[
            {
                "match": {"node": "draft"},
                "strategy": "local_only",
                "pool": ["ollama:llama3", "openai:gpt-4o-mini"],
            }
        ],
        fallback_models=["openai:gpt-4o"],
    )
    with pytest.raises(RoutingError) as caught:
        run_workflow_spec(spec, llm=TrapLLM(), tools=tools, circuit_breaker=breaker)
    assert "ollama:llama3" in str(caught.value)
    assert caught.value.taint == "untrusted"


def test_memory_provenance_blocks_hosted_under_taint_rule() -> None:
    llm = HostedTrapLLM()
    workflow = WorkflowSpec.model_validate(
        {
            "name": "mem-taint",
            "default_model": "openai:gpt-4o-mini",
            "fallback_models": ["openai:gpt-4o", "anthropic:claude-3-5-haiku"],
            "routing": {
                "version": 1,
                "rules": [
                    {
                        "match": {"taint": "untrusted"},
                        "strategy": "local_only",
                        "pool": ["openai:gpt-4o-mini", "ollama:llama3"],
                    }
                ],
            },
            "nodes": [
                {
                    "id": "draft",
                    "type": "agent",
                    "prompt": "use {{memory_hit}}",
                    "output_key": "out",
                }
            ],
        }
    )
    state = RunState.start(workflow.name, {})
    state.node_outputs["memory_hit"] = "from-store"
    state.provenance["memory_hit"] = untrusted(source="memory", node_id="recall").as_dict()
    ctx = ExecutionContext(
        workflow,
        ToolRegistry(),
        llm=llm,
        default_model="openai:gpt-4o-mini",
        fallback_models=["openai:gpt-4o"],
    )
    # Fresh start with pre-seeded untrusted memory provenance (not a seeded input).
    finished = run_workflow(workflow, {}, ctx, state=state)
    assert finished.status == "succeeded"
    assert finished.metadata["routes"][0]["model"] == "ollama:llama3"
    assert finished.metadata["routes"][0]["taint"] == "untrusted"
    assert llm.calls == ["llama3"]


def test_resolve_routing_taint_outputs_seed_is_untrusted() -> None:
    state = RunState.start("taint-outputs", {})
    state.node_outputs["seed"] = "tainted-blob"
    state.provenance["seed"] = untrusted(source="tool:leak", node_id="seed").as_dict()
    node = WorkflowSpec.model_validate(
        {
            "name": "n",
            "nodes": [
                {"id": "draft", "type": "agent", "prompt": "classify {{outputs.seed}}"},
            ],
        }
    ).nodes[0]
    assert resolve_routing_taint(state, node) == "untrusted"


def test_outputs_namespace_interpolation_never_calls_hosted() -> None:
    """{{outputs.seed}} must not skip taint; hosted pin must not fire."""
    tools = _leak_tools()
    llm = HostedTrapLLM()
    spec = {
        "name": "outputs-ns",
        "default_model": "openai:gpt-4o-mini",
        "routing": {
            "version": 1,
            "rules": [
                {
                    "match": {"taint": "untrusted"},
                    "strategy": "local_only",
                    "pool": ["ollama:llama3"],
                },
                {"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"},
            ],
        },
        "nodes": [
            {
                "id": "seed",
                "type": "tool",
                "tool": "leak",
                "arguments": {},
                "next": "draft",
            },
            {
                "id": "draft",
                "type": "agent",
                "prompt": "classify {{outputs.seed}}",
                "output_key": "out",
            },
        ],
    }
    state = run_workflow_spec(spec, llm=llm, tools=tools)
    assert state.status == "succeeded"
    assert llm.calls == ["llama3"]
    assert "gpt-4o-mini" not in llm.calls
    assert any("tainted-blob" in prompt for prompt in llm.prompts)
    route = state.metadata["routes"][0]
    assert route["taint"] == "untrusted"
    assert route["local_only"] is True
    assert route["model"] == "ollama:llama3"


# --- 2. Indeterminate taint fails closed ---


def test_indeterminate_taint_refuses_hosted_under_local_only() -> None:
    llm = HostedTrapLLM()
    workflow = WorkflowSpec.model_validate(
        {
            "name": "indeterminate",
            "default_model": "openai:gpt-4o-mini",
            "fallback_models": ["openai:gpt-4o", "anthropic:claude-3-5-haiku"],
            "routing": {
                "version": 1,
                "rules": [
                    {
                        "match": {"taint": "untrusted"},
                        "strategy": "local_only",
                        "pool": ["openai:gpt-4o-mini", "ollama:llama3"],
                    }
                ],
            },
            "nodes": [
                {
                    "id": "draft",
                    "type": "agent",
                    "prompt": "see {{shadow}}",
                    "output_key": "out",
                    "fallback_models": ["openai:gpt-4o"],
                }
            ],
        }
    )
    ctx = ExecutionContext(
        workflow,
        ToolRegistry(),
        llm=llm,
        default_model="openai:gpt-4o-mini",
        fallback_models=["openai:gpt-4o"],
    )
    # metadata key is in the template namespace but has no provenance (not an input).
    state = run_workflow(workflow, {}, ctx, metadata={"shadow": "no-provenance"})
    assert state.status == "succeeded"
    route = state.metadata["routes"][0]
    assert route["taint"] == "indeterminate"
    assert route["local_only"] is True
    assert route["model"] == "ollama:llama3"
    assert llm.calls == ["llama3"]


def test_indeterminate_taint_hosted_only_pool_raises_before_complete() -> None:
    workflow = WorkflowSpec.model_validate(
        {
            "name": "indeterminate-hosted",
            "fallback_models": ["openai:gpt-4o"],
            "routing": {
                "version": 1,
                "rules": [
                    {
                        "match": {"taint": "untrusted"},
                        "strategy": "cheapest_capable",
                        "pool": ["openai:gpt-4o-mini", "anthropic:claude-3-5-haiku"],
                    }
                ],
            },
            "nodes": [
                {"id": "draft", "type": "agent", "prompt": "see {{shadow}}", "output_key": "out"}
            ],
        }
    )
    decision_state = RunState.start(workflow.name, {})
    decision_state.node_outputs["shadow"] = "unknown"
    # no provenance on shadow → indeterminate → local_only forced → unsatisfiable
    with pytest.raises(RoutingError) as caught:
        select_route(workflow, workflow.nodes[0], state=decision_state)
    assert caught.value.taint == "indeterminate"

    ctx = ExecutionContext(
        workflow, ToolRegistry(), llm=TrapLLM(), default_model="openai:gpt-4o-mini"
    )
    with pytest.raises(RoutingError):
        run_workflow(workflow, {}, ctx, metadata={"shadow": "unknown"})


# --- 3. Credential over-grant ---


def test_get_provider_credentials_stay_with_selected_provider() -> None:
    settings = Settings(  # type: ignore[call-arg]
        openai_api_key="sk-openai-dummy",
        anthropic_api_key="sk-anth-dummy",
        gemini_api_key="gem-dummy",
        aws_access_key_id="AKIADUMMY",
        aws_secret_access_key="awssecret-dummy",
        aws_region="us-east-1",
        vertex_project="proj-dummy",
        google_application_credentials="/tmp/adc-dummy.json",
        _env_file=(),
    )
    openai, _ = get_provider("openai:gpt-4o-mini", settings=settings)
    assert isinstance(openai, OpenAIProvider)
    assert openai._api_key == "sk-openai-dummy"
    blob = str(vars(openai))
    assert "AKIADUMMY" not in blob
    assert "awssecret-dummy" not in blob
    assert "gem-dummy" not in blob
    assert "proj-dummy" not in blob
    assert not hasattr(openai, "_access_key")
    assert not hasattr(openai, "_project")

    bedrock, _ = get_provider("bedrock:amazon.nova-lite-v1:0", settings=settings)
    assert isinstance(bedrock, BedrockProvider)
    assert bedrock._access_key == "AKIADUMMY"
    assert bedrock._secret_key == "awssecret-dummy"
    bblob = str(vars(bedrock))
    assert "sk-openai-dummy" not in bblob
    assert "sk-anth-dummy" not in bblob
    assert "gem-dummy" not in bblob
    assert not hasattr(bedrock, "_api_key")

    gemini, _ = get_provider("gemini:gemini-2.0-flash", settings=settings)
    assert isinstance(gemini, GeminiProvider)
    assert gemini._api_key == "gem-dummy"
    gblob = str(vars(gemini))
    assert "sk-openai-dummy" not in gblob
    assert "AKIADUMMY" not in gblob

    vertex, _ = get_provider("vertex:gemini-2.0-flash", settings=settings)
    assert isinstance(vertex, VertexProvider)
    assert vertex._project == "proj-dummy"
    vblob = str(vars(vertex))
    assert "sk-openai-dummy" not in vblob
    assert "AKIADUMMY" not in vblob
    assert "gem-dummy" not in vblob


# --- 4. Stale / malformed capability override refused before complete ---


def test_malformed_capability_matrix_refused_before_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "caps.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_CAPABILITY_MATRIX", str(bad))
    clear_capability_cache()
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_capability_matrix()
    monkeypatch.delenv("READYAGENTS_CAPABILITY_MATRIX")
    clear_capability_cache()

    llm = TrapLLM()
    spec = {
        "name": "bad-matrix",
        "routing": {
            "version": 1,
            "capability_matrix": str(bad),
            "rules": [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
        },
        "nodes": [{"id": "draft", "type": "agent", "prompt": "hi", "output_key": "out"}],
    }
    with pytest.raises((ConfigError, NodeError)) as caught:
        run_workflow_spec(spec, llm=llm)
    cfg = _config_failure(caught.value)
    assert "not valid JSON" in str(cfg)


def test_missing_version_capability_matrix_refused_before_complete(tmp_path: Path) -> None:
    path = tmp_path / "nover.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": "2026-01-01T00:00:00Z",
                "warn_after_days": 90,
                "models": {"openai:gpt-4o-mini": _matrix_caps()},
                "prefixes": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unsupported version"):
        load_capability_matrix(path)

    llm = TrapLLM()
    spec = {
        "name": "nover",
        "routing": {
            "version": 1,
            "capability_matrix": str(path),
            "rules": [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
        },
        "nodes": [{"id": "draft", "type": "agent", "prompt": "hi", "output_key": "out"}],
    }
    with pytest.raises((ConfigError, NodeError)) as caught:
        run_workflow_spec(spec, llm=llm)
    assert "unsupported version" in str(_config_failure(caught.value))


def test_stale_capability_matrix_refused_before_complete(tmp_path: Path) -> None:
    path = tmp_path / "stale.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2020-01-01T00:00:00Z",
                "warn_after_days": 90,
                "models": {"openai:gpt-4o-mini": _matrix_caps()},
                "prefixes": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="stale"):
        load_capability_matrix(path)

    workflow = WorkflowSpec.model_validate(
        {
            "name": "stale",
            "routing": {
                "version": 1,
                "capability_matrix": str(path),
                "rules": [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
            },
            "nodes": [{"id": "draft", "type": "agent", "prompt": "hi", "output_key": "out"}],
        }
    )
    with pytest.raises(ConfigError, match="stale"):
        select_route(workflow, workflow.nodes[0])

    with pytest.raises((ConfigError, NodeError)) as caught:
        run_workflow_spec(
            {
                "name": "stale-run",
                "routing": {
                    "version": 1,
                    "capability_matrix": str(path),
                    "rules": [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
                },
                "nodes": [{"id": "draft", "type": "agent", "prompt": "hi", "output_key": "out"}],
            },
            llm=TrapLLM(),
        )
    assert "stale" in str(_config_failure(caught.value))


# --- 5. Routing cannot widen egress / sovereign ---


def test_hosted_get_provider_does_not_widen_sovereign_allowlist() -> None:
    guard = install_guard()
    before = (set(guard.allow_specs), set(guard.allow_hosts), set(guard.allow_ips))
    try:
        settings = Settings(  # type: ignore[call-arg]
            openai_api_key="sk-openai-dummy",
            anthropic_api_key="sk-anth-dummy",
            gemini_api_key="gem-dummy",
            aws_access_key_id="AKIADUMMY",
            aws_secret_access_key="awssecret-dummy",
            aws_region="us-east-1",
            vertex_project="proj-dummy",
            _env_file=(),
        )
        for ref in (
            "openai:gpt-4o-mini",
            "anthropic:claude-3-5-haiku",
            "gemini:gemini-2.0-flash",
            "bedrock:amazon.nova-lite-v1:0",
            "vertex:gemini-2.0-flash",
        ):
            get_provider(ref, settings=settings)
        after = (set(guard.allow_specs), set(guard.allow_hosts), set(guard.allow_ips))
        assert after == before
    finally:
        guard.close()


def test_routing_pin_to_hosted_does_not_add_allowlist_entries() -> None:
    guard = install_guard(["10.0.0.8"])
    before = (set(guard.allow_specs), set(guard.allow_hosts), set(guard.allow_ips))
    try:
        llm = ScriptedLLM()
        llm.enqueue("pinned")
        spec = {
            "name": "sovereign-pin",
            "routing": {
                "version": 1,
                "rules": [{"match": {"node": "draft"}, "pin": "openai:gpt-4o-mini"}],
            },
            "nodes": [{"id": "draft", "type": "agent", "prompt": "hi", "output_key": "out"}],
        }
        state = run_workflow_spec(spec, llm=llm)
        assert state.metadata["routes"][0]["model"] == "openai:gpt-4o-mini"
        # Injected ScriptedLLM avoids live egress; allowlist must stay unchanged.
        after = (set(guard.allow_specs), set(guard.allow_hosts), set(guard.allow_ips))
        assert after == before
        settings = Settings(openai_api_key="sk-openai-dummy", _env_file=())  # type: ignore[call-arg]
        get_provider("openai:gpt-4o-mini", settings=settings)
        after_provider = (set(guard.allow_specs), set(guard.allow_hosts), set(guard.allow_ips))
        assert after_provider == before
    finally:
        guard.close()
