"""Promotion gates, canary, shadow, rollback, env status/history/diff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.env.promote import evaluate_gates, promote, rollback_env
from readyagents.env.release import deploy, diff_releases
from readyagents.env.run import _canary_hit, run_in_environment
from readyagents.env.schema import (
    EnvCanarySpec,
    EnvGatesSpec,
    EnvironmentSpec,
    EnvRollbackSpec,
    EnvShadowSpec,
    load_env_file,
)
from readyagents.env.store import EnvStore
from readyagents.errors import (
    ApprovalRequired,
    EnvGateBenchmark,
    EnvGateEval,
    EnvGateFixtures,
    EnvGateHealth,
)
from readyagents.run_store import JsonRunStore
from readyagents.workflow.schema import BudgetSpec
from readyagents.workflow.state import RunState

runner = CliRunner()


def _flow(tmp: Path, token: str, name: str = "echo.yaml") -> Path:
    path = tmp / name
    path.write_text(
        "name: echo\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        f"    template: '{token}'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    return path


def _env_yaml(tmp: Path, extra_prod: str = "") -> Path:
    path = tmp / "readyagents.env.yaml"
    path.write_text(
        f"version: 1\nenvironments:\n  staging: {{}}\n  prod:\n    secrets: prod\n{extra_prod}",
        encoding="utf-8",
    )
    return path


def test_each_gate_has_distinct_typed_reason(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_path, "g")
    candidate = {"digest": "sha256:a", "pins": {"workflow": "sha256:a"}}
    current = {"digest": "sha256:b", "pins": {"workflow": "sha256:b"}}
    spec = EnvironmentSpec(
        gates=EnvGatesSpec(
            eval={"must_pass": True},
            fixtures={"no_regression": True},
            benchmark={"tolerance": "10%"},
            health={"min_score": 0.95},
            approval={"roles": ["release_manager"]},
        )
    )
    with pytest.raises(EnvGateEval) as ev:
        evaluate_gates(
            spec, workflow=flow, candidate=candidate, current=current, hooks={"eval": lambda: False}
        )
    assert ev.value.reason == "eval"
    with pytest.raises(EnvGateFixtures) as fx:
        evaluate_gates(
            spec,
            workflow=flow,
            candidate=candidate,
            current=current,
            hooks={"eval": lambda: True, "fixtures": lambda: False},
        )
    assert fx.value.reason == "fixtures"
    with pytest.raises(EnvGateBenchmark) as bn:
        evaluate_gates(
            spec,
            workflow=flow,
            candidate=candidate,
            current=current,
            hooks={"eval": lambda: True, "fixtures": lambda: True, "benchmark": lambda: False},
        )
    assert bn.value.reason == "benchmark"
    with pytest.raises(EnvGateHealth) as hl:
        evaluate_gates(
            spec,
            workflow=flow,
            candidate=candidate,
            current=current,
            hooks={
                "eval": lambda: True,
                "fixtures": lambda: True,
                "benchmark": lambda: True,
                "health": lambda: 0.1,
            },
        )
    assert hl.value.reason == "health"
    with pytest.raises(ApprovalRequired) as ap:
        evaluate_gates(
            spec,
            workflow=flow,
            candidate=candidate,
            current=current,
            hooks={
                "eval": lambda: True,
                "fixtures": lambda: True,
                "benchmark": lambda: True,
                "health": lambda: 1.0,
            },
        )
    assert "UNTRUSTED RELEASE DIFF" in ap.value.prompt
    assert ap.value.pause and ap.value.pause.get("diff", {}).get("complete") is True
    reasons = {ev.value.reason, fx.value.reason, bn.value.reason, hl.value.reason, "approval"}
    assert len(reasons) == 5


def test_promote_copies_source_pointer_not_working_copy(tmp_path: Path, tmp_settings) -> None:
    _env_yaml(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    staging = _flow(tmp_path, "STAGING", "s.yaml")
    working = _flow(tmp_path, "WORKING", "w.yaml")
    deploy(staging, "staging", spec=loaded.environments["staging"], settings=tmp_settings)
    prod_spec = EnvironmentSpec(
        gates=EnvGatesSpec(
            eval={"must_pass": True},
            approval={"roles": ["release_manager"]},
        )
    )
    pointer = promote(
        working,
        source="staging",
        target="prod",
        spec=prod_spec,
        settings=tmp_settings,
        actor="ops",
        decisions={"promote": "approve"},
        hooks={"eval": lambda: True},
    )
    store = EnvStore(tmp_settings)
    current = store.current("prod")
    assert current is not None
    assert current["digest"] == pointer["digest"]
    state = run_in_environment(working, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "STAGING"


def test_canary_is_deterministic_per_run_id(tmp_path: Path, tmp_settings) -> None:
    _env_yaml(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    v1 = _flow(tmp_path, "V1", "v1.yaml")
    v2 = _flow(tmp_path, "V2", "v2.yaml")
    deploy(v1, "prod", spec=loaded.environments["prod"], settings=tmp_settings)
    deploy(v2, "prod", spec=loaded.environments["prod"], settings=tmp_settings, as_candidate=True)
    # Patch the loaded spec with canary by rewriting env file.
    path = tmp_path / "readyagents.env.yaml"
    path.write_text(
        "version: 1\nenvironments:\n  prod:\n    canary: {percent: 50}\n",
        encoding="utf-8",
    )
    hit = miss = None
    for i in range(4000):
        rid = f"r{i}"
        if _canary_hit(rid, 50):
            hit = rid
        else:
            miss = rid
        if hit and miss:
            break
    assert hit and miss
    assert _canary_hit(hit, 50) is True
    assert _canary_hit(hit, 50) is True
    a = run_in_environment(v1, "prod", settings=tmp_settings, persist=True, run_id=hit)
    b = run_in_environment(v1, "prod", settings=tmp_settings, persist=True, run_id=hit)
    c = run_in_environment(v1, "prod", settings=tmp_settings, persist=True, run_id=miss)
    assert a.metadata["release_channel"] == "canary"
    assert b.metadata["release_channel"] == "canary"
    assert a.metadata["release"] == b.metadata["release"]
    assert c.metadata["release_channel"] == "current"
    assert a.output_keys["out"] == "V2"
    assert c.output_keys["out"] == "V1"


def test_shadow_output_unused_and_cost_distinct(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "readyagents.env.yaml"
    path.write_text(
        "version: 1\nenvironments:\n  prod:\n    shadow:\n      budget: {max_cost_usd: 0.01}\n",
        encoding="utf-8",
    )
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    v1 = _flow(tmp_path, "PRIMARY", "p.yaml")
    v2 = _flow(tmp_path, "SHADOW", "s.yaml")
    deploy(v1, "prod", spec=loaded.environments["prod"], settings=tmp_settings)
    deploy(v2, "prod", spec=loaded.environments["prod"], settings=tmp_settings, as_candidate=True)
    state = run_in_environment(v1, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "PRIMARY"
    assert state.metadata.get("shadow_unused") is True
    assert "shadow_cost_micros" in state.metadata
    store = EnvStore(tmp_settings)
    shadow_dir = store.env_dir("prod") / "shadow-runs"
    shadows = list(shadow_dir.glob("*.json"))
    assert shadows
    blob = json.loads(shadows[0].read_text(encoding="utf-8"))
    assert blob.get("metadata", {}).get("unused") is True
    assert blob.get("outputs", {}).get("out") == "SHADOW"
    events = store.history("prod")
    assert any(row.get("event") == "shadow" and row.get("unused") is True for row in events)


def test_rollback_guard_never_auto_forwards(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  prod:\n"
        "    rollback:\n"
        "      on: {error_rate_above: 0.05, window: 20}\n",
        encoding="utf-8",
    )
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    spec = loaded.environments["prod"]
    v1 = _flow(tmp_path, "SAFE", "safe.yaml")
    v2 = _flow(tmp_path, "BAD", "bad.yaml")
    safe = deploy(v1, "prod", spec=spec, settings=tmp_settings)
    bad = deploy(v2, "prod", spec=spec, settings=tmp_settings)
    store = EnvStore(tmp_settings)
    before = store.current("prod")
    assert before is not None
    assert before["digest"] == bad["digest"]
    env_store = JsonRunStore(store.runs_dir("prod"))
    for i in range(8):
        failed = RunState.start("echo", {}, run_id=f"fail{i}")
        failed.status = "failed"
        env_store.save(failed)
    state = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "BAD"
    assert state.metadata.get("rollback")
    after = store.current("prod")
    assert after is not None
    assert after["digest"] == safe["digest"]
    assert after["digest"] != bad["digest"]
    assert store.candidate("prod") is None
    prev = store.previous("prod")
    assert prev is None or prev.get("digest") != bad["digest"]
    nxt = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert nxt.output_keys["out"] == "SAFE"
    assert nxt.metadata["release"] == safe["digest"]
    held = store.current("prod")
    assert held is not None
    assert held["digest"] == safe["digest"]
    third = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert third.output_keys["out"] == "SAFE"
    assert third.metadata["release"] == safe["digest"]
    still = store.current("prod")
    assert still is not None
    assert still["digest"] == safe["digest"]
    assert still["digest"] != bad["digest"]
    assert nxt.metadata["release"] == after["digest"]


def test_manual_rollback_requires_approval_when_declared(tmp_path: Path, tmp_settings) -> None:
    _env_yaml(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    v1 = _flow(tmp_path, "OLD", "old.yaml")
    v2 = _flow(tmp_path, "NEW", "new.yaml")
    deploy(v1, "prod", spec=loaded.environments["prod"], settings=tmp_settings)
    deploy(v2, "prod", spec=loaded.environments["prod"], settings=tmp_settings)
    gated = EnvironmentSpec(gates=EnvGatesSpec(approval={"roles": ["release_manager"]}))
    with pytest.raises(ApprovalRequired):
        rollback_env("prod", spec=gated, settings=tmp_settings, actor="ops")
    pointer = rollback_env(
        "prod",
        spec=gated,
        settings=tmp_settings,
        actor="ops",
        decisions={"rollback": "approve"},
        reason="manual",
    )
    assert pointer.get("rollback_reason") == "manual"
    state = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "OLD"


def test_env_status_history_diff_cli(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _env_yaml(tmp_path)
    flow = _flow(tmp_path, "A")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    first = runner.invoke(app, ["env", "deploy", str(flow), "--env", "staging", "--json"])
    assert first.exit_code == 0, first.stdout
    second_flow = _flow(tmp_path, "B", "b.yaml")
    second = runner.invoke(app, ["env", "deploy", str(second_flow), "--env", "staging", "--json"])
    assert second.exit_code == 0, second.stdout
    status = runner.invoke(app, ["env", "status", "--env", "staging", "--json"])
    assert status.exit_code == 0, status.stdout
    hist = runner.invoke(app, ["env", "history", "--env", "staging", "--json"])
    assert hist.exit_code == 0, hist.stdout
    diff = runner.invoke(
        app, ["env", "diff", "--env", "staging", "--from", "previous", "--to", "current", "--json"]
    )
    assert diff.exit_code == 0, diff.stdout
    payload = json.loads(diff.stdout[diff.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "env diff"
    assert payload["complete"] is True
    assert payload["from"] != payload["to"]


def test_cli_help_env_promote_rollback() -> None:
    for args in (["env", "--help"], ["promote", "--help"], ["rollback", "--help"]):
        first = runner.invoke(app, args)
        second = runner.invoke(app, args)
        assert first.exit_code == 0
        assert second.exit_code == 0
        assert first.exit_code == second.exit_code


def test_diff_releases_complete(tmp_path: Path, tmp_settings) -> None:
    a = deploy(_flow(tmp_path, "A", "a.yaml"), "staging", settings=tmp_settings)
    b = deploy(_flow(tmp_path, "B", "b.yaml"), "prod", settings=tmp_settings)
    payload = diff_releases(a, b, settings=tmp_settings)
    assert payload["complete"] is True
    assert "workflow" in payload["pins"]


def test_shadow_spec_has_separate_budget() -> None:
    spec = EnvironmentSpec(
        budget=BudgetSpec(max_cost_usd=50),
        canary=EnvCanarySpec(percent=10),
        shadow=EnvShadowSpec(budget=BudgetSpec(max_cost_usd=0.01)),
        rollback=EnvRollbackSpec(on={"error_rate_above": 0.05, "window": 50}),
    )
    assert spec.budget is not None and spec.shadow is not None
    assert spec.shadow.budget is not None
    assert spec.budget.max_cost_usd != spec.shadow.budget.max_cost_usd
