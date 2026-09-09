"""Firewall allow/gate/deny at dispatch_tool. Drive shipped runner + CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import PolicyDenied, PolicyError
from readyagents.firewall.enforce import ToolRequest, evaluate
from readyagents.firewall.policy_file import Policy, ToolRule
from readyagents.packs.protocol import BasePack
from readyagents.tools import FunctionTool
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState

_runner = CliRunner()


def _cli_env(monkeypatch, tmp_path: Path, tmp_settings) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("READYAGENTS_POLICY", raising=False)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()


def test_no_policy_evaluate_allows() -> None:
    state = RunState.start("t", {})
    decision = evaluate(
        ToolRequest(name="calc", arguments={"expression": "1"}, node_id="n"), state, None
    )
    assert decision.action == "allow"
    assert decision.rule == "none"


def test_default_deny_without_rule() -> None:
    policy = Policy(default="deny")
    state = RunState.start("t", {})
    decision = evaluate(ToolRequest(name="calc", arguments={}, node_id="n"), state, policy)
    assert decision.action == "deny"


def test_deny_is_audited_with_rule(tmp_path: Path, tmp_settings) -> None:
    from readyagents.audit import read_audit_events

    policy = tmp_path / "readyagents.policy.yaml"
    policy.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: n\n    type: tool\n    tool: calc\n"
        "    arguments: {expression: '1+1'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyDenied):
        run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)
    events = []
    audit_dir = tmp_settings.home_path() / "audit"
    if audit_dir.is_dir():
        for path in audit_dir.glob("*.jsonl"):
            events.extend(read_audit_events(audit_dir, path.stem))
    kinds = {e.get("event") for e in events}
    assert "policy_deny" in kinds
    assert any(e.get("rule") == "default" for e in events)


def test_run_default_deny_raises(tmp_path: Path, tmp_settings) -> None:
    policy = tmp_path / "readyagents.policy.yaml"
    policy.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: n\n    type: tool\n    tool: calc\n"
        "    arguments: {expression: '1+1'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyDenied):
        run_workflow_file(wf, settings=tmp_settings, persist=False, policy=policy)


def test_on_tainted_deny(tmp_path: Path, tmp_settings) -> None:
    policy = tmp_path / "p.yaml"
    policy.write_text(
        "version: 1\ndefault: allow\ntools:\n  write_file:\n    on_tainted: deny\n",
        encoding="utf-8",
    )
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n"
        "  - id: stamp\n    type: tool\n    tool: now\n    output_key: ts\n    next: out\n"
        "  - id: out\n    type: tool\n    tool: write_file\n"
        "    arguments: {path: 'out.txt', content: '{{ts}}'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyDenied, match="tainted"):
        run_workflow_file(wf, settings=tmp_settings, persist=True, policy=policy)


def test_on_tainted_gate_then_resume(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _cli_env(monkeypatch, tmp_path, tmp_settings)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "p.yaml").write_text(
        "version: 1\ndefault: allow\ntools:\n  write_file:\n    on_tainted: gate\n",
        encoding="utf-8",
    )
    (tmp_path / "w.yaml").write_text(
        "name: w\nnodes:\n"
        "  - id: stamp\n    type: tool\n    tool: now\n    output_key: ts\n    next: out\n"
        "  - id: out\n    type: tool\n    tool: write_file\n"
        "    arguments: {path: 'out.txt', content: '{{ts}}'}\n    output_key: wrote\n",
        encoding="utf-8",
    )
    paused = _runner.invoke(app, ["run", "w.yaml", "--policy", "p.yaml", "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    assert "Policy gate" in paused.stdout
    run_id = None
    import json

    payload = json.loads(paused.stdout[paused.stdout.find("{") :])
    run_id = payload["run_id"]
    resumed = _runner.invoke(
        app, ["resume", run_id, "--approve", "out", "--policy", "p.yaml", "--json"]
    )
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    done = json.loads(resumed.stdout[resumed.stdout.find("{") :])
    assert done["status"] == "succeeded"


def test_pack_tool_cannot_opt_out(tmp_path: Path, tmp_settings) -> None:
    class PingPack(BasePack):
        name = "ping"
        version = "0"

        def register_tools(self):
            return [FunctionTool(name="ping", description="p", handler=lambda: "pong")]

    policy = tmp_path / "p.yaml"
    policy.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: n\n    type: tool\n    tool: ping\n    output_key: v\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyDenied):
        run_workflow_file(
            wf, settings=tmp_settings, persist=False, extra_packs=[PingPack()], policy=policy
        )


def test_malformed_policy_on_run_fails_closed(tmp_path: Path, tmp_settings) -> None:
    policy = tmp_path / "p.yaml"
    policy.write_text("version: 1\nbogus: true\n", encoding="utf-8")
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: n\n    type: tool\n    tool: calc\n    arguments: {expression: '1'}\n",
        encoding="utf-8",
    )
    with pytest.raises(PolicyError):
        run_workflow_file(wf, settings=tmp_settings, persist=False, policy=policy)


def test_path_scope_denies_escape() -> None:
    policy = Policy(tools={"write_file": ToolRule(paths=["out/**"])})
    state = RunState.start("t", {})
    decision = evaluate(
        ToolRequest(
            name="write_file", arguments={"path": "../secret", "content": "x"}, node_id="n"
        ),
        state,
        policy,
    )
    assert decision.action == "deny"


def test_egress_allowlist() -> None:
    policy = Policy(tools={"http_get": ToolRule(allow_hosts=["docs.example.com"])})
    state = RunState.start("t", {})
    denied = evaluate(
        ToolRequest(name="http_get", arguments={"url": "https://evil.test/x"}, node_id="n"),
        state,
        policy,
    )
    assert denied.action == "deny"
    allowed = evaluate(
        ToolRequest(name="http_get", arguments={"url": "https://docs.example.com/x"}, node_id="n"),
        state,
        policy,
    )
    assert allowed.action == "allow"


def test_pin_change_gates() -> None:
    policy = Policy(tools={"mcp:*": ToolRule(on_description_change="gate")})
    state = RunState.start("t", {})
    decision = evaluate(
        ToolRequest(name="srv.tool", arguments={}, node_id="n"),
        state,
        policy,
        pin_changed=True,
    )
    assert decision.action == "gate"
    assert "description" in decision.reason


def test_known_secret_refused_in_model_request(tmp_path: Path, tmp_settings) -> None:
    from readyagents.errors import PolicyDenied
    from readyagents.firewall.secrets_scan import scan_messages
    from readyagents.llm.base import Message

    secret = "sk-firewalltestsecret99"
    with pytest.raises(PolicyDenied, match="secret"):
        scan_messages(
            [Message(role="user", content=f"use {secret} please")],
            [secret],
            node_id="draft",
        )


def test_policy_explain_cli(tmp_path: Path) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: n\n    type: tool\n    tool: calc\n    arguments: {expression: '1'}\n",
        encoding="utf-8",
    )
    policy = tmp_path / "p.yaml"
    policy.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    result = _runner.invoke(app, ["policy", "explain", str(wf), "--policy", str(policy), "--json"])
    assert result.exit_code == 0, result.stdout
    assert "deny" in result.stdout
