"""Shipped health: fingerprints, clustering, recovery, flaky vs broken, explain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ApprovalRequired, ConfigError, LLMError, NodeError
from readyagents.health.fingerprint import classify_failure, fingerprint
from readyagents.health.flaky import classify_stability
from readyagents.health.layout import HARD_MAX_RUNS
from readyagents.health.query import query_health
from readyagents.health.score import score_node
from readyagents.run_store import open_run_store
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import NodeResult, RunState

runner = CliRunner()

_SECRET = "sk-abcdefghijksecret"
_PATH = "/etc/shadow/outside"


def _save_run(
    settings,
    *,
    workflow: str,
    node: str,
    error: str | None,
    cost: int,
    status: str = "failed",
    ok_first: bool = False,
    run_id: str | None = None,
    attempts: int = 1,
    started: str = "2026-01-01T00:00:00+00:00",
) -> RunState:
    state = RunState.start(workflow, {"draft": "x"}, run_id=run_id)
    state.started_at = started
    if ok_first:
        state.record("prep", "ok", node_type="transform", attempts=1)
    if error:
        state.results.append(
            NodeResult(
                node_id=node,
                type="agent",
                status="error",
                error=error,
                attempts=attempts,
                usage={"cost_micros": cost},
            )
        )
        state.errors.append(error)
    else:
        state.record(node, "ok", node_type="agent", attempts=attempts, usage={"cost_micros": cost})
    state.usage["cost_micros"] = cost
    state.finish(status)
    state.finished_at = started
    store = open_run_store(settings)
    try:
        store.save(state)
    finally:
        store.close()
    return state


def test_fingerprint_same_bug_same_id_different_class_differs() -> None:
    a = fingerprint(
        LLMError("run_id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa truncated at 2026-01-01T00:00:00Z"),
        node_id="draft",
        provider="openai",
    )
    b = fingerprint(
        LLMError("run_id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb truncated at 2026-02-02T11:22:33Z"),
        node_id="draft",
        provider="openai",
    )
    assert a.id == b.id
    assert a.klass == "truncation"
    other = fingerprint(LLMError("429 rate limit"), node_id="draft", provider="openai")
    assert other.id != a.id
    assert other.klass == "rate_limit"
    assert classify_failure("StructuredOutputError", "schema") == "schema_violation"
    assert classify_failure("LLMError", "provider 503") == "provider_error"


def test_fingerprint_redacts_secrets_and_paths() -> None:
    fp = fingerprint(
        LLMError(f"failed {_SECRET} wrote {_PATH} run_id=cccccccccccccccccccccccccccccccc"),
        node_id="draft",
        secrets=[_SECRET],
    )
    blob = json.dumps(fp.as_dict())
    assert _SECRET not in blob
    assert _PATH not in blob
    assert "/etc/shadow" not in blob
    assert "sk-" not in blob


def test_health_clusters_rank_by_cost(tmp_settings) -> None:
    _save_run(
        tmp_settings,
        workflow="w",
        node="draft",
        error="truncated max_tokens",
        cost=100,
        started="2026-01-01T00:00:00+00:00",
        run_id="a" * 32,
    )
    _save_run(
        tmp_settings,
        workflow="w",
        node="draft",
        error="truncated max_tokens",
        cost=100,
        started="2026-01-02T00:00:00+00:00",
        run_id="b" * 32,
    )
    _save_run(
        tmp_settings,
        workflow="w",
        node="other",
        error="429 rate limit",
        cost=5,
        started="2026-01-03T00:00:00+00:00",
        run_id="c" * 32,
    )
    _save_run(
        tmp_settings,
        workflow="w",
        node="okn",
        error=None,
        cost=1,
        status="succeeded",
        started="2026-01-04T00:00:00+00:00",
        run_id="d" * 32,
    )
    store = open_run_store(tmp_settings)
    try:
        report = query_health(store, workflow="w", window=50, limit=64)
    finally:
        store.close()
    assert report.clusters
    assert report.clusters[0].cost_micros >= report.clusters[-1].cost_micros
    assert report.clusters[0].count == 2
    assert len(report.clusters[0].run_ids) == 2
    rates = {row.name: row.success_rate for row in report.workflows}
    assert rates["w"] == 0.25


def test_health_query_is_bounded(tmp_settings) -> None:
    for i in range(12):
        _save_run(
            tmp_settings,
            workflow="bound",
            node="n",
            error="429 rate limit",
            cost=1,
            run_id=f"{i:032x}",
            started=f"2026-01-01T00:00:{i:02d}+00:00",
        )
    store = open_run_store(tmp_settings)
    try:
        report = query_health(store, workflow="bound", limit=5)
    finally:
        store.close()
    assert report.scanned <= 5
    assert report.truncated is True
    assert report.limit <= HARD_MAX_RUNS


def test_declared_recovery_recorded_undeclared_falls_through() -> None:
    llm = ScriptedLLM()
    llm.enqueue(error=LLMError("429 rate limit"))
    llm.enqueue(text="recovered")
    spec = {
        "name": "rec",
        "default_model": "mock:x",
        "nodes": [
            {
                "id": "a",
                "type": "agent",
                "prompt": "hi",
                "output_key": "t",
                "retry": {"max_attempts": 1},
                "recovery": {"on": [{"class": "rate_limit", "action": "backoff", "seconds": 0}]},
            }
        ],
    }
    state = run_workflow_spec(spec, llm=llm)
    assert state.status == "succeeded"
    notes = state.metadata.get("recovery") or []
    assert notes
    assert notes[0]["action"] == "backoff"
    assert notes[0]["class"] == "rate_limit"

    llm2 = ScriptedLLM()
    llm2.enqueue(error=LLMError("weird boom"))
    with pytest.raises(NodeError, match="weird boom"):
        run_workflow_spec(
            {
                "name": "plain",
                "default_model": "mock:x",
                "nodes": [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "hi",
                        "retry": {"max_attempts": 1},
                    }
                ],
            },
            llm=llm2,
        )


def test_recovery_retry_with_and_fallback() -> None:
    llm = ScriptedLLM()
    llm.enqueue(error=LLMError("truncated max_tokens"))
    llm.enqueue(text="ok-after-budget")
    state = run_workflow_spec(
        {
            "name": "tok",
            "default_model": "mock:x",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "hi",
                    "output_key": "t",
                    "retry": {"max_attempts": 1},
                    "recovery": {
                        "on": [{"class": "truncation", "action": "retry_with", "max_tokens": 4000}]
                    },
                }
            ],
        },
        llm=llm,
    )
    assert state.status == "succeeded"
    assert any(n.get("action") == "retry_with" for n in (state.metadata.get("recovery") or []))

    llm_fb = ScriptedLLM()
    llm_fb.enqueue(error=LLMError("provider 503"))
    llm_fb.enqueue(text="from-fallback")
    fb = run_workflow_spec(
        {
            "name": "fb",
            "default_model": "mock:primary",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "hi",
                    "output_key": "t",
                    "fallback_models": ["mock:backup"],
                    "retry": {"max_attempts": 1},
                    "recovery": {"on": [{"class": "provider_error", "action": "fallback"}]},
                }
            ],
        },
        llm=llm_fb,
    )
    assert fb.status == "succeeded"
    assert fb.output_keys["t"] == "from-fallback"


def test_schema_repair_action() -> None:
    llm = ScriptedLLM()
    llm.enqueue(text="not-json")
    llm.enqueue(text='{"n": 1}')
    state = run_workflow_spec(
        {
            "name": "sch",
            "default_model": "mock:x",
            "nodes": [
                {
                    "id": "a",
                    "type": "agent",
                    "prompt": "hi",
                    "output_key": "t",
                    "output_schema": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                    },
                    "retry": {"max_attempts": 1},
                    "recovery": {
                        "on": [{"class": "schema_violation", "action": "repair", "max_repairs": 2}]
                    },
                }
            ],
        },
        llm=llm,
    )
    assert state.status == "succeeded"
    assert any(n.get("action") == "repair" for n in (state.metadata.get("recovery") or []))


def test_flaky_vs_broken() -> None:
    assert classify_stability([("abc", True), ("abc", False)]) == "flaky"
    assert classify_stability([("abc", False), ("def", False)]) == "broken"
    assert classify_stability([("abc", True), ("def", True)]) == "healthy"


def test_health_score_moves_with_failures() -> None:
    good = [{"ok": True, "attempts": 1, "latency_ms": 10, "cost_micros": 1}] * 10
    mixed = good[:5] + [{"ok": False, "attempts": 3, "latency_ms": 80, "cost_micros": 9}] * 5
    a = score_node(good, node_id="n", window=10)
    b = score_node(mixed, node_id="n", window=10)
    assert a.success_rate == 1.0
    assert b.success_rate == 0.5
    assert b.score < a.score
    assert b.retry_rate > 0


def test_quarantine_gates_not_skips(tmp_settings) -> None:
    for i in range(5):
        _save_run(
            tmp_settings,
            workflow="q",
            node="draft",
            error="429 rate limit",
            cost=1,
            run_id=f"{i:032x}",
        )
    flow = tmp_settings.workspace_path() / "q.yaml"
    flow.write_text(
        "name: q\n"
        "default_model: mock:x\n"
        "start: draft\n"
        "nodes:\n"
        "  - id: draft\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    recovery:\n"
        "      health: {min_success_rate: 0.9, window: 5, below: gate}\n"
        "  - id: done\n"
        "    type: transform\n"
        "    template: ok\n"
        "    output_key: summary\n",
        encoding="utf-8",
        newline="\n",
    )
    llm = ScriptedLLM()
    llm.enqueue(text="should-not-run")
    with pytest.raises(ApprovalRequired) as caught:
        run_workflow_file(flow, llm=llm, settings=tmp_settings, persist=True)
    assert caught.value.state is not None
    assert "success_rate" in str(caught.value).lower() or "health" in str(caught.value).lower()


def test_quarantine_fallback_path(tmp_settings) -> None:
    for i in range(5):
        _save_run(
            tmp_settings,
            workflow="qf",
            node="draft",
            error="429 rate limit",
            cost=1,
            run_id=f"{i + 20:032x}",
        )
    flow = tmp_settings.workspace_path() / "qf.yaml"
    flow.write_text(
        "name: qf\n"
        "default_model: mock:x\n"
        "start: draft\n"
        "nodes:\n"
        "  - id: draft\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    recovery:\n"
        "      health:\n"
        "        min_success_rate: 0.9\n"
        "        window: 5\n"
        "        below: gate\n"
        "        fallback: safe\n"
        "  - id: safe\n"
        "    type: transform\n"
        "    template: gated-ok\n"
        "    output_key: summary\n",
        encoding="utf-8",
        newline="\n",
    )
    llm = ScriptedLLM()
    llm.enqueue(text="should-not-run")
    state = run_workflow_file(flow, llm=llm, settings=tmp_settings, persist=True)
    assert state.status == "succeeded"
    assert state.output_keys.get("summary") == "gated-ok"
    assert "draft" in (state.metadata.get("quarantine") or {})
    assert not llm.calls


def test_untrusted_below_skip_refused() -> None:
    with pytest.raises(ValidationError, match="gate"):
        WorkflowSpec.model_validate(
            {
                "name": "bad",
                "nodes": [
                    {
                        "id": "a",
                        "type": "transform",
                        "template": "x",
                        "output_key": "s",
                        "recovery": {
                            "health": {"min_success_rate": 0.1, "window": 2, "below": "skip"}
                        },
                    }
                ],
            }
        )


def test_health_explain_bundle(tmp_settings, tmp_path: Path) -> None:
    _save_run(
        tmp_settings,
        workflow="ex",
        node="draft",
        error="truncated max_tokens",
        cost=9,
        run_id="e" * 32,
        ok_first=True,
    )
    _save_run(
        tmp_settings,
        workflow="ex",
        node="draft",
        error=None,
        cost=1,
        status="succeeded",
        run_id="f" * 32,
        started="2026-01-01T00:00:00+00:00",
    )
    store = open_run_store(tmp_settings)
    try:
        report = query_health(store, workflow="ex")
        assert report.clusters
        fp_id = report.clusters[0].fingerprint.id
        from readyagents.health.explain import write_explain_bundle

        dest = write_explain_bundle(
            fp_id,
            store,
            out_dir=tmp_settings.workspace_path() / "explain-out",
            workspace=tmp_settings.workspace_path(),
            settings=tmp_settings,
            confirm=True,
        )
    finally:
        store.close()
    assert (dest / "fingerprint.json").is_file()
    assert (dest / "runs.json").is_file()
    assert (dest / "README.md").is_file()
    blob = ""
    for path in dest.iterdir():
        blob += path.read_text(encoding="utf-8", errors="replace")
    assert _SECRET not in blob
    with pytest.raises(ConfigError, match="--yes"):
        from readyagents.health.explain import write_explain_bundle
        from readyagents.run_store import open_run_store as open_store

        store2 = open_store(tmp_settings)
        try:
            write_explain_bundle(
                fp_id,
                store2,
                out_dir=tmp_settings.workspace_path() / "nope",
                workspace=tmp_settings.workspace_path(),
                settings=tmp_settings,
                confirm=False,
            )
        finally:
            store2.close()


def test_cli_health_help_twice_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    first = runner.invoke(app, ["health", "--help"])
    second = runner.invoke(app, ["health", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    ran = runner.invoke(app, ["health", "--json", "--limit", "8"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    payload = json.loads(ran.stdout[ran.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "health"
    assert "clusters" in payload
    clear_settings_cache()
