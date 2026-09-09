from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.cost.estimate import estimate_workflow_file
from readyagents.cost.ledger import parse_labels, query_spend, read_spend_entries
from readyagents.cost.prices import load_price_table, quote_model
from readyagents.errors import (
    BudgetExceeded,
    CircuitOpen,
    ConfigError,
    LLMError,
    NodeError,
    RunawayGuard,
)
from readyagents.llm.resilience import CircuitBreaker
from readyagents.testing import ScriptedLLM
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]


def _agent_pair(model: str = "openai:gpt-4o-mini") -> dict:
    return {
        "name": "tokenops-pair",
        "start": "a",
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "first prompt for pricing",
                "model": model,
                "next": "b",
            },
            {
                "id": "b",
                "type": "agent",
                "prompt": "second prompt for pricing",
                "model": model,
            },
        ],
    }


def _write_workflow(tmp_path: Path, spec: dict, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    import yaml

    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def test_price_exact_and_prefix_and_unpriced() -> None:
    exact = quote_model("openai:gpt-4o-mini")
    assert exact.priced is True
    assert exact.match == "exact"
    assert exact.cost_micros(1_000_000, 0) == 150_000
    prefixed = quote_model("openai:not-in-the-table-zzzz")
    assert prefixed.priced is True
    assert prefixed.match.startswith("prefix:")
    unknown = quote_model("mystery-model-does-not-exist")
    assert unknown.priced is False
    assert unknown.cost_micros(10_000, 10_000) is None
    assert unknown.unpriced_reason


def test_price_override_and_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = tmp_path / "prices.json"
    good.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-09-01T00:00:00Z",
                "models": {"custom:foo": {"input": 1.0, "output": 2.0}},
                "prefixes": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("READYAGENTS_PRICES", str(good))
    table = load_price_table()
    quote = table.quote("custom:foo")
    assert quote.priced is True
    assert quote.cost_micros(1_000_000, 0) == 1_000_000
    assert table.quote("mystery-model-does-not-exist").priced is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_price_table(bad)
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        load_price_table(empty)
    neg = tmp_path / "neg.json"
    neg.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-09-01T00:00:00Z",
                "models": {"x": {"input": -1, "output": 1}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=">= 0"):
        load_price_table(neg)


def test_estimate_no_model_workflow_is_zero() -> None:
    result = estimate_workflow_file(ROOT / "examples" / "calc_pipeline.yaml")
    assert result.floor_tokens == 0
    assert result.ceiling_tokens == 0
    assert result.unpriced is False
    assert result.floor_micros == 0
    assert result.ceiling_micros == 0


def test_estimate_every_construct_and_determinism(tmp_path: Path) -> None:
    child = {
        "name": "child-agent",
        "nodes": [
            {
                "id": "inner",
                "type": "agent",
                "prompt": "included {{n}}",
                "model": "openai:gpt-4o-mini",
            }
        ],
    }
    _write_workflow(tmp_path, child, "child.yaml")
    spec = {
        "name": "all-constructs",
        "inputs": {"n": 3, "items": ["a", "b"]},
        "start": "fan",
        "nodes": [
            {
                "id": "fan",
                "type": "parallel",
                "branches": [
                    {
                        "id": "left",
                        "type": "agent",
                        "prompt": "left",
                        "model": "openai:gpt-4o-mini",
                    },
                    {
                        "id": "right",
                        "type": "transform",
                        "template": "ok",
                    },
                ],
                "next": "loop",
            },
            {
                "id": "loop",
                "type": "foreach",
                "items": "items",
                "max_items": 8,
                "body": {
                    "id": "each",
                    "type": "agent",
                    "prompt": "item {{item}}",
                    "model": "openai:gpt-4o-mini",
                },
                "next": "retry",
            },
            {
                "id": "retry",
                "type": "agent",
                "prompt": "retry me",
                "model": "openai:gpt-4o-mini",
                "fallback_models": ["openai:gpt-4o"],
                "retry": {"max_attempts": 3},
                "tools": ["calc"],
                "max_tool_rounds": 4,
                "next": "child",
            },
            {
                "id": "child",
                "type": "include",
                "path": "child.yaml",
                "inputs": {"n": "{{n}}"},
            },
        ],
    }
    path = _write_workflow(tmp_path, spec)
    first = estimate_workflow_file(path, inputs={"n": 3, "items": ["a", "b"]})
    second = estimate_workflow_file(path, inputs={"n": 3, "items": ["a", "b"]})
    assert first.as_dict() == second.as_dict()
    assert first.ceiling_tokens > first.floor_tokens
    assert first.floor_tokens > 0
    ids = {row["node_id"] for row in first.nodes}
    assert {"left", "each", "retry", "inner"} <= ids
    retry = next(row for row in first.nodes if row["node_id"] == "retry")
    assert retry["floor_calls"] == 1
    assert retry["ceiling_calls"] >= 3 * 2 * (1 + 4)
    each = next(row for row in first.nodes if row["node_id"] == "each")
    assert each["floor_calls"] == 2
    assert each["ceiling_calls"] == 2
    assert any("invoice is authoritative" in item for item in first.assumptions)


def test_estimate_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    result = estimate_workflow_file(ROOT / "examples" / "research_brief.yaml")
    assert result.floor_tokens >= 1
    assert result.ceiling_tokens >= result.floor_tokens


def test_cap_consulted_before_complete(tmp_path: Path, tmp_settings) -> None:
    path = _write_workflow(tmp_path, _agent_pair())
    llm = ScriptedLLM()
    llm.enqueue("should-not-run", model="gpt-4o-mini")
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=False,
            max_tokens_cap=1,
            override_budget=True,
        )
    assert exc.value.reason == "before_call"
    assert llm.calls == []
    assert exc.value.state is not None
    assert exc.value.state.status == "failed"


def test_unpriced_plus_max_spend_fails_closed(tmp_path: Path, tmp_settings) -> None:
    path = _write_workflow(tmp_path, _agent_pair("mock:unpriced"))
    llm = ScriptedLLM()
    llm.enqueue("nope", model="unpriced")
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=False,
            max_spend=1.0,
            override_budget=True,
        )
    assert exc.value.reason in {"unpriced", "before_call"}
    assert llm.calls == []


def test_parallel_branches_share_meter(tmp_path: Path, tmp_settings) -> None:
    spec = {
        "name": "fan-cap",
        "nodes": [
            {
                "id": "fan",
                "type": "parallel",
                "branches": [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "branch-a",
                        "model": "openai:gpt-4o-mini",
                    },
                    {
                        "id": "b",
                        "type": "agent",
                        "prompt": "branch-b",
                        "model": "openai:gpt-4o-mini",
                    },
                ],
            }
        ],
    }
    path = _write_workflow(tmp_path, spec)
    llm = ScriptedLLM()
    llm.enqueue(
        "one",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 90, "completion_tokens": 0, "total_tokens": 90},
    )
    llm.enqueue(
        "two",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 90, "completion_tokens": 0, "total_tokens": 90},
    )
    with pytest.raises(BudgetExceeded):
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=False,
            max_tokens_cap=90,
            override_budget=True,
        )
    assert len(llm.calls) == 1


def test_resume_continues_the_same_budget(tmp_path: Path, tmp_settings) -> None:
    path = _write_workflow(tmp_path, _agent_pair())
    llm = ScriptedLLM()
    llm.enqueue(
        "first",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50},
    )
    llm.enqueue(
        "second",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 40, "completion_tokens": 0, "total_tokens": 40},
    )
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=True,
            max_tokens_cap=50,
            override_budget=True,
        )
    state = exc.value.state
    assert state is not None
    assert len(llm.calls) == 1
    llm2 = ScriptedLLM()
    llm2.enqueue("resume-should-not", model="gpt-4o-mini")
    with pytest.raises(BudgetExceeded):
        run_workflow_file(
            path,
            llm=llm2,
            settings=tmp_settings,
            persist=True,
            resume_state=state,
        )
    assert llm2.calls == []


def test_stop_reasons_are_distinct(tmp_path: Path, tmp_settings) -> None:
    assert BudgetExceeded is not RunawayGuard
    assert RunawayGuard is not CircuitOpen
    path = _write_workflow(tmp_path, _agent_pair())
    llm = ScriptedLLM()
    llm.enqueue(
        "ok",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    llm.enqueue(
        "ok2",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(RunawayGuard) as runaway:
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=False,
            max_model_calls=1,
            override_budget=True,
        )
    assert runaway.value.kind == "model_calls"
    assert runaway.value.state is not None
    assert runaway.value.state.status == "failed"
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    spec = WorkflowSpec.model_validate(
        {
            "name": "cb",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "x",
                    "model": "mock:primary",
                }
            ],
        }
    )
    llm_cb = ScriptedLLM()
    llm_cb.enqueue(error=LLMError("fail"), model="primary")
    with pytest.raises(NodeError):
        run_workflow(
            spec,
            {},
            ExecutionContext(spec, ToolRegistry(), llm=llm_cb, circuit_breaker=breaker),
        )
    with pytest.raises(CircuitOpen):
        run_workflow(
            spec,
            {},
            ExecutionContext(spec, ToolRegistry(), llm=llm_cb, circuit_breaker=breaker),
        )


def test_refuse_to_start_and_audited_override(tmp_path: Path, tmp_settings) -> None:
    path = _write_workflow(tmp_path, _agent_pair())
    llm = ScriptedLLM()
    llm.enqueue("nope", model="gpt-4o-mini")
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow_file(
            path,
            llm=llm,
            settings=tmp_settings,
            persist=True,
            max_tokens_cap=1,
        )
    assert exc.value.reason == "refuse_to_start"
    assert llm.calls == []
    llm2 = ScriptedLLM()
    llm2.enqueue(
        "ok",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
    )
    llm2.enqueue(
        "ok2",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
    )
    with pytest.raises(BudgetExceeded) as second:
        run_workflow_file(
            path,
            llm=llm2,
            settings=tmp_settings,
            persist=True,
            max_tokens_cap=1,
            override_budget=True,
        )
    assert second.value.reason == "before_call"
    from readyagents.audit import read_audit_events

    events = []
    audit_dir = tmp_settings.audit_dir()
    if audit_dir.is_dir():
        for item in audit_dir.glob("*.jsonl"):
            events.extend(read_audit_events(audit_dir, item.stem.split(".")[0]))
    kinds = {row.get("event") for row in events}
    assert "budget_override" in kinds
    assert "budget_refuse" in kinds


def test_ledger_labels_aggregation_and_corrupt(tmp_path: Path, tmp_settings) -> None:
    path = _write_workflow(
        tmp_path,
        {
            "name": "ledger-wf",
            "nodes": [{"id": "t", "type": "transform", "template": "ok", "output_key": "out"}],
        },
    )
    state = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        labels={"team": "platform", "purpose": "triage"},
        actor="operator@example.com",
    )
    assert state.status == "succeeded"
    assert state.metadata.get("labels") == {"team": "platform", "purpose": "triage"}
    state2 = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        labels={"team": "platform"},
        actor="operator@example.com",
    )
    assert state2.status == "succeeded"
    entries = read_spend_entries(tmp_settings.ledger_dir())
    assert len(entries) >= 2
    assert entries[0]["prev_hash"]
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
    by_label = query_spend(tmp_settings.ledger_dir(), by="label")
    keys = {row.key for row in by_label.rows}
    assert "team=platform" in keys
    by_day = query_spend(tmp_settings.ledger_dir(), by="day")
    assert by_day.as_dict()["total"]["runs"] >= 2
    by_wf = query_spend(tmp_settings.ledger_dir(), by="workflow")
    assert any(row.key == "ledger-wf" for row in by_wf.rows)
    empty = query_spend(tmp_path / "missing-ledger", by="day")
    assert empty.as_dict()["total"]["runs"] == 0
    ledger = tmp_settings.ledger_dir() / "spend.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b"{not json\n")
    corrupt = query_spend(tmp_settings.ledger_dir(), by="day")
    assert corrupt.skipped_corrupt >= 1


def test_parse_labels_rejects_bad_pairs() -> None:
    assert parse_labels(["team=platform"]) == {"team": "platform"}
    with pytest.raises(Exception, match="KEY=VALUE"):
        parse_labels(["nocolon"])


def test_cache_savings_on_record(tmp_path: Path, tmp_settings) -> None:
    tmp_settings.llm_cache = True
    spec = {
        "name": "cache-wf",
        "cache_llm": True,
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "identical cache prompt",
                "model": "openai:gpt-4o-mini",
            }
        ],
    }
    path = _write_workflow(tmp_path, spec)
    llm = ScriptedLLM()
    llm.enqueue(
        "cached-text",
        model="gpt-4o-mini",
        usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    )
    first = run_workflow_file(path, llm=llm, settings=tmp_settings, persist=True)
    assert first.usage.get("cache_misses", 0) >= 1
    llm2 = ScriptedLLM()
    llm2.enqueue("should-not-run", model="gpt-4o-mini")
    second = run_workflow_file(path, llm=llm2, settings=tmp_settings, persist=True)
    assert second.usage.get("cache_hits", 0) >= 1
    assert second.usage.get("cache_savings_micros", 0) > 0
    assert llm2.calls == []
    from readyagents.report import render_html

    html = render_html(second)
    assert "cache" in html.lower()
    assert "cost_micros" in html


def test_cli_estimate_json_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    first = runner.invoke(
        app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--estimate", "--json"]
    )
    second = runner.invoke(
        app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--estimate", "--json"]
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert a["ok"] is True
    assert a["command"] == "run"
    assert a["estimate"] is True
    assert a["floor_tokens"] == b["floor_tokens"]
    assert a["ceiling_tokens"] == b["ceiling_tokens"]
    assert a["assumptions"] == b["assumptions"]


def test_cli_spend_json_after_labelled_runs(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    path = _write_workflow(
        tmp_path,
        {
            "name": "cli-spend",
            "nodes": [{"id": "t", "type": "transform", "template": "ok"}],
        },
    )
    one = runner.invoke(
        app,
        ["run", str(path), "--label", "team=platform", "--json"],
    )
    two = runner.invoke(
        app,
        ["run", str(path), "--label", "team=platform", "--json"],
    )
    assert one.exit_code == 0, one.stdout + one.stderr
    assert two.exit_code == 0, two.stdout + two.stderr
    spent = runner.invoke(app, ["spend", "--json", "--by", "label"])
    assert spent.exit_code == 0, spent.stdout + spent.stderr
    payload = json.loads(spent.stdout[spent.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "spend"
    assert payload["total"]["runs"] >= 2
    assert any(row["key"] == "team=platform" for row in payload["rows"])


def test_no_flags_identity_examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    calc = runner.invoke(app, ["run", str(ROOT / "examples" / "calc_pipeline.yaml"), "--json"])
    assert calc.exit_code == 0, calc.stdout + calc.stderr
    calc_payload = json.loads(calc.stdout[calc.stdout.find("{") :])
    assert calc_payload["status"] == "succeeded"
    assert calc_payload["ok"] is True
    gate = runner.invoke(app, ["run", str(ROOT / "examples" / "approval_gate.yaml"), "--json"])
    assert gate.exit_code == 2, gate.stdout + gate.stderr
    gate_payload = json.loads(gate.stdout[gate.stdout.find("{") :])
    assert gate_payload["status"] == "paused"


def test_existing_budget_spec_still_allows_first_call() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        "alpha",
        model="a",
        usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    )
    llm.enqueue("should-not-run", model="b")
    spec = WorkflowSpec.model_validate(_agent_pair("mock:a"))
    ctx = ExecutionContext(
        spec,
        ToolRegistry(),
        llm=llm,
        default_model="mock:a",
        budget_tokens=10,
    )
    with pytest.raises(BudgetExceeded) as exc:
        run_workflow(spec, {}, ctx)
    assert exc.value.kind == "tokens"
    assert len(llm.calls) == 1
