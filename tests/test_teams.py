"""Shipped type: team path: strategies, terminate, scratchpad, resume, replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.cost.ledger import read_spend_entries
from readyagents.errors import (
    ApprovalRequired,
    TeamRoundsExceeded,
    TeamScratchpadDenied,
    TeamSpendExceeded,
    TeamUnknownMember,
    TeamWallExceeded,
)
from readyagents.replay.cassette import Cassette
from readyagents.replay.fork import fork_run, reconstruct_after
from readyagents.testing import ScriptedLLM, run_workflow_spec
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()


def _team(
    *,
    strategy: str = "route",
    members: list[dict],
    terminate: dict | None = None,
    scratchpad: dict | None = None,
    extra_nodes: list[dict] | None = None,
) -> dict:
    team_node = {
        "id": "crew",
        "type": "team",
        "strategy": strategy,
        "supervisor": {"prompt": "Route to a declared member.", "model": "mock:sup"},
        "members": members,
        "scratchpad": scratchpad or {"keys": ["findings", "open_questions"]},
        "terminate": terminate or {"max_rounds": 8},
        "output_key": "out",
    }
    nodes = list(extra_nodes or [])
    nodes.append(team_node)
    return {"name": "team_flow", "nodes": nodes}


def _agent_member(
    mid: str, *, writes: list[str] | None = None, reads: list[str] | None = None
) -> dict:
    return {
        "id": mid,
        "role": mid,
        "type": "agent",
        "prompt": f"You are {mid}.",
        "model": f"mock:{mid}",
        "scratchpad": {"read": reads or ["open_questions"], "write": writes or ["findings"]},
    }


def test_route_terminates() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "go", "done": false}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["one"]}}', model="a")
    llm.enqueue('{"done": true}', model="sup")
    state = run_workflow_spec(_team(members=[_agent_member("a")]), llm=llm)
    assert state.status == "succeeded"
    assert state.output_keys["out"]["stop"] == "done"
    assert state.output_keys["out"]["scratchpad"]["findings"] == ["one"]
    assert state.metadata["teams"]["crew"]["handoffs"]


def test_plan_then_execute_terminates() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"plan": ["a", "b"], "reason": "order"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["x"]}}', model="a")
    llm.enqueue("ok-b", model="b")
    state = run_workflow_spec(
        _team(
            strategy="plan_then_execute",
            members=[_agent_member("a"), _agent_member("b", writes=[], reads=["findings"])],
        ),
        llm=llm,
    )
    assert state.status == "succeeded"
    assert state.output_keys["out"]["rounds"] == 2


def test_debate_and_pipeline_terminate() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "first"}', model="sup")
    llm.enqueue("arg-a", model="a")
    llm.enqueue('{"next": "b", "reason": "second"}', model="sup")
    llm.enqueue("arg-b", model="b")
    llm.enqueue('{"done": true}', model="sup")
    state = run_workflow_spec(
        _team(
            strategy="debate",
            members=[_agent_member("a"), _agent_member("b", writes=[], reads=["findings"])],
        ),
        llm=llm,
    )
    assert state.status == "succeeded"

    state = run_workflow_spec(
        _team(
            strategy="pipeline",
            members=[
                {
                    "id": "one",
                    "type": "transform",
                    "template": '{"scratchpad": {"findings": ["p"]}}',
                    "parse_json": True,
                    "scratchpad": {"read": [], "write": ["findings"]},
                },
                {
                    "id": "two",
                    "type": "transform",
                    "template": '{"ok": true}',
                    "parse_json": True,
                    "scratchpad": {"read": ["findings"], "write": []},
                },
            ],
        ),
        llm=ScriptedLLM(),
    )
    assert state.status == "succeeded"
    assert state.output_keys["out"]["scratchpad"]["findings"] == ["p"]


def test_hallucinated_member_is_typed() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "ghost", "reason": "nope"}', model="sup")
    with pytest.raises(TeamUnknownMember) as exc:
        run_workflow_spec(_team(members=[_agent_member("a")]), llm=llm)
    assert exc.value.member_id == "ghost"


def test_max_rounds_distinct() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue("ok", model="a")
    with pytest.raises(TeamRoundsExceeded) as exc:
        run_workflow_spec(
            _team(members=[_agent_member("a")], terminate={"max_rounds": 1}),
            llm=llm,
        )
    assert exc.value.limit == 1


def test_spend_and_wall_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        '{"next": "a", "reason": "x"}',
        model="sup",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "cost_micros": 50},
    )
    llm.enqueue(
        "ok",
        model="a",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "cost_micros": 50},
    )
    with pytest.raises(TeamSpendExceeded):
        run_workflow_spec(
            _team(
                members=[_agent_member("a")],
                terminate={"max_rounds": 8, "max_cost_usd": 0.00005},
            ),
            llm=llm,
        )

    n = {"i": 0}

    def fake_now() -> float:
        n["i"] += 1
        return 0.0 if n["i"] < 3 else 10.0

    monkeypatch.setattr("readyagents.team.node._NOW", fake_now)
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue("ok", model="a")
    with pytest.raises(TeamWallExceeded):
        run_workflow_spec(
            _team(
                members=[_agent_member("a")],
                terminate={"max_rounds": 8, "max_wall_seconds": 1},
            ),
            llm=llm,
        )


def test_goal_predicate_stops_early() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["hit"]}}', model="a")
    state = run_workflow_spec(
        _team(
            members=[_agent_member("a")],
            terminate={"max_rounds": 8, "goal": "{{ scratchpad.findings | len }}"},
        ),
        llm=llm,
    )
    assert state.output_keys["out"]["stop"] == "goal"
    assert len(llm.calls) == 2


def test_scratchpad_grants_and_taint() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad": {"open_questions": ["q"]}}', model="a")
    with pytest.raises(TeamScratchpadDenied):
        run_workflow_spec(_team(members=[_agent_member("a")]), llm=llm)

    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad_read": ["findings"]}', model="a")
    with pytest.raises(TeamScratchpadDenied):
        run_workflow_spec(
            _team(members=[_agent_member("a", reads=["open_questions"], writes=["findings"])]),
            llm=llm,
        )

    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["ok"]}}', model="a")
    llm.enqueue('{"done": true}', model="sup")
    state = run_workflow_spec(_team(members=[_agent_member("a")]), llm=llm)
    assert state.output_keys["out"]["taint"]["findings"] == "a"
    assert state.provenance["scratchpad.findings"]["node_id"] == "a"


_LEAK_TEMPLATES = (
    '{{ scratchpad.secret | default "" }}',
    '{{ teams.crew.scratchpad.secret | default "" }}',
    "{{ teams }}",
)


@pytest.mark.parametrize("template", _LEAK_TEMPLATES)
def test_scratchpad_template_cannot_read_ungranted_key(template: str) -> None:
    secret = "classified-token-9f3a"
    spec = _team(
        strategy="pipeline",
        scratchpad={"keys": ["public", "secret"]},
        members=[
            {
                "id": "writer",
                "type": "transform",
                "template": json.dumps({"scratchpad": {"secret": secret, "public": "ok"}}),
                "parse_json": True,
                "scratchpad": {"read": [], "write": ["public", "secret"]},
            },
            {
                "id": "reader",
                "type": "transform",
                "template": template,
                "scratchpad": {"read": ["public"], "write": []},
            },
        ],
    )
    state = run_workflow_spec(spec, llm=ScriptedLLM())
    last = state.output_keys["out"]["last"]
    assert secret not in str(last)

    llm = ScriptedLLM()
    llm.enqueue('{"next": "writer", "reason": "w"}', model="sup")
    llm.enqueue(json.dumps({"scratchpad": {"secret": secret, "public": "ok"}}), model="writer")
    llm.enqueue('{"next": "reader", "reason": "r"}', model="sup")
    llm.enqueue("ok", model="reader")
    llm.enqueue('{"done": true}', model="sup")
    run_workflow_spec(
        _team(
            scratchpad={"keys": ["public", "secret"]},
            members=[
                {
                    "id": "writer",
                    "type": "agent",
                    "prompt": "write",
                    "model": "mock:writer",
                    "scratchpad": {"read": [], "write": ["public", "secret"]},
                },
                {
                    "id": "reader",
                    "type": "agent",
                    "prompt": f"Repeat {template}",
                    "model": "mock:reader",
                    "scratchpad": {"read": ["public"], "write": []},
                },
            ],
        ),
        llm=llm,
    )
    for call in llm.calls:
        if call["model"] != "reader":
            continue
        blob = " ".join(str(getattr(m, "content", m)) for m in call["messages"])
        assert secret not in blob


def test_per_member_usage_sums() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        '{"next": "a", "reason": "x"}',
        model="sup",
        usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    )
    llm.enqueue(
        '{"scratchpad": {"findings": ["u"]}}',
        model="a",
        usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
    )
    llm.enqueue('{"done": true}', model="sup", usage={"prompt_tokens": 1, "total_tokens": 1})
    state = run_workflow_spec(_team(members=[_agent_member("a")]), llm=llm)
    members = state.output_keys["out"]["usage"]
    summed = sum(int(row.get("total_tokens") or 0) for row in members.values())
    assert summed == int(state.usage.get("total_tokens") or 0)
    assert summed == 9


def test_per_member_spend_ledger(tmp_settings, tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    spec = _team(members=[_agent_member("a")])
    spec["nodes"][0]["members"][0]["role"] = "researcher"
    path.write_text(json.dumps(spec), encoding="utf-8")
    llm = ScriptedLLM()
    llm.enqueue(
        '{"next": "a", "reason": "x"}',
        model="sup",
        usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3, "cost_micros": 10},
    )
    llm.enqueue(
        '{"scratchpad": {"findings": ["u"]}}',
        model="a",
        usage={"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5, "cost_micros": 20},
    )
    llm.enqueue(
        '{"done": true}',
        model="sup",
        usage={"prompt_tokens": 1, "total_tokens": 1, "cost_micros": 5},
    )
    state = run_workflow_file(path, settings=tmp_settings, persist=True, llm=llm)
    assert state.status == "succeeded"
    entries = read_spend_entries(tmp_settings.ledger_dir())
    row = next(item for item in entries if item.get("run_id") == state.run_id)
    by_member = row["by_member"]
    by_role = row["by_role"]
    assert by_member["a"]["role"] == "researcher"
    assert by_member["a"]["team"] == "crew"
    assert by_member["supervisor"]["role"] == "supervisor"
    token_sum = sum(int(item.get("total_tokens") or 0) for item in by_member.values())
    cost_sum = sum(int(item.get("cost_micros") or 0) for item in by_member.values())
    tools_sum = sum(int(item.get("tool_calls") or 0) for item in by_member.values())
    team_usage = state.output_keys["out"]["usage"]
    assert token_sum == int(state.usage.get("total_tokens") or 0)
    assert token_sum == int(row["total_tokens"])
    assert token_sum == sum(int(item.get("total_tokens") or 0) for item in team_usage.values())
    assert cost_sum == int(state.usage.get("cost_micros") or 0)
    assert cost_sum == sum(int(item.get("cost_micros") or 0) for item in team_usage.values())
    assert tools_sum == sum(int(item.get("tool_calls") or 0) for item in team_usage.values())
    role_tokens = sum(int(item.get("total_tokens") or 0) for item in by_role.values())
    role_cost = sum(int(item.get("cost_micros") or 0) for item in by_role.values())
    role_tools = sum(int(item.get("tool_calls") or 0) for item in by_role.values())
    assert "researcher" in by_role and "supervisor" in by_role
    assert role_tokens == token_sum
    assert role_cost == cost_sum
    assert role_tools == tools_sum
    spend = state.metadata.get("spend") or {}
    assert spend.get("by_member") == by_member
    assert spend.get("by_role") == by_role


def test_member_unknown_tool_denied() -> None:
    from readyagents.llm.base import ToolCall

    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue(
        "call",
        model="a",
        tool_calls=[ToolCall(id="1", name="http_get", arguments={"url": "https://x.test"})],
    )
    spec = _team(members=[_agent_member("a")])
    spec["nodes"][0]["members"][0]["tools"] = ["calc"]
    with pytest.raises(Exception) as exc:
        run_workflow_spec(spec, llm=llm)
    assert "http_get" in str(exc.value).lower() or "unknown" in str(exc.value).lower()


def test_approval_member_pauses_and_resumes() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    spec = _team(
        members=[
            {
                "id": "sign_off",
                "role": "human",
                "type": "approval",
                "prompt": "Approve the draft.",
                "scratchpad": {"read": ["findings"], "write": []},
            }
        ]
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_spec(spec, llm=llm)
    assert paused.value.node_id == "sign_off"
    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    llm.enqueue('{"done": true}', model="sup")
    state = run_workflow_spec(spec, llm=llm, decisions={"sign_off": "approve"})
    assert state.status == "succeeded"


def test_member_budget_slice_enforced() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue(
        '{"scratchpad": {"findings": ["c"]}}',
        model="a",
        usage={"prompt_tokens": 1, "cost_micros": 50},
    )
    llm.enqueue('{"next": "a", "reason": "again"}', model="sup")
    spec = _team(members=[_agent_member("a")])
    spec["nodes"][0]["members"][0]["max_cost_usd"] = 0.00005
    with pytest.raises(TeamSpendExceeded):
        run_workflow_spec(spec, llm=llm)


def test_paused_team_resumes_mid_conversation(tmp_settings, tmp_path: Path) -> None:
    path = tmp_path / "pause.yaml"
    path.write_text(
        json.dumps(
            _team(
                members=[
                    {
                        "id": "sign_off",
                        "role": "human",
                        "type": "approval",
                        "prompt": "Approve the draft.",
                        "scratchpad": {"read": ["findings"], "write": []},
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True, llm=llm)
    assert paused.value.state.metadata["teams"]["crew"]["pending_member"] == "sign_off"
    resume_llm = ScriptedLLM()
    resume_llm.enqueue('{"done": true}', model="sup")
    state = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        llm=resume_llm,
        decisions={"sign_off": "approve"},
        resume_state=paused.value.state,
    )
    assert state.status == "succeeded"
    assert any(h.get("to") == "sign_off" for h in state.metadata["teams"]["crew"]["handoffs"])


def test_offline_replay_does_not_complete(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "rec.yaml"
    spec = _team(members=[_agent_member("a")])
    path.write_text(json.dumps(spec), encoding="utf-8")
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["r"]}}', model="a")
    llm.enqueue('{"done": true}', model="sup")
    first = run_workflow_file(path, settings=tmp_settings, persist=True, record=True, llm=llm)
    cassette_path = Path(first.metadata["cassette"])
    tape = Cassette.load(cassette_path)
    llm_rows = [row for row in tape.entries.values() if row.get("kind") == "llm"]
    nodes = {str(row.get("node_id")) for row in llm_rows}
    assert "crew__supervisor" in nodes
    assert "a" in nodes
    assert len(llm_rows) >= 3

    def boom(*_a, **_k):
        raise AssertionError("offline replay must not call complete()")

    monkeypatch.setattr("readyagents.llm.base.LLMProvider.complete", boom, raising=False)
    monkeypatch.setattr("readyagents.testing.helpers.ScriptedLLM.complete", boom)
    replayed = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=False,
        offline=True,
        cassette_path=cassette_path,
        llm=ScriptedLLM(),
    )
    assert replayed.status == "succeeded"
    assert replayed.output_keys["out"]["scratchpad"]["findings"] == ["r"]


def test_nested_team_fails_at_validate() -> None:
    with pytest.raises((ValidationError, ValueError)):
        WorkflowSpec.model_validate(
            _team(
                members=[
                    {
                        "id": "inner",
                        "type": "team",
                        "prompt": "no",
                    }
                ]
            )
        )


def test_record_offline_replay(
    tmp_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "team.yaml"
    spec = _team(members=[_agent_member("a")])
    path.write_text(json.dumps(spec), encoding="utf-8")
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "go"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["r"]}}', model="a")
    llm.enqueue('{"done": true}', model="sup")
    first = run_workflow_file(path, settings=tmp_settings, persist=True, record=True, llm=llm)
    assert first.status == "succeeded"
    cassette_path = Path(first.metadata["cassette"])
    tape = Cassette.load(cassette_path)
    llm_rows = [row for row in tape.entries.values() if row.get("kind") == "llm"]
    nodes = {str(row.get("node_id")) for row in llm_rows}
    assert "crew__supervisor" in nodes
    assert "a" in nodes
    assert len(llm_rows) >= 3

    def boom(*_a, **_k):
        raise AssertionError("offline replay must not call complete()")

    monkeypatch.setattr("readyagents.llm.base.LLMProvider.complete", boom, raising=False)
    monkeypatch.setattr("readyagents.testing.helpers.ScriptedLLM.complete", boom)
    replayed = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=False,
        offline=True,
        cassette_path=cassette_path,
        llm=ScriptedLLM(),
    )
    assert replayed.status == "succeeded"
    assert replayed.output_keys["out"]["scratchpad"]["findings"] == ["r"]

    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home))
    clear_settings_cache()
    cli = runner.invoke(
        app, ["runs", "replay", first.run_id, "--offline", "--json", "--no-persist"]
    )
    assert cli.exit_code == 0, cli.stdout + cli.stderr
    payload = json.loads(cli.stdout[cli.stdout.find("{") :])
    assert payload.get("ok") is True
    clear_settings_cache()


def test_fork_restores_scratchpad_and_handoff() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a", "reason": "x"}', model="sup")
    llm.enqueue('{"scratchpad": {"findings": ["forked"]}}', model="a")
    llm.enqueue('{"done": true}', model="sup")
    spec = _team(
        members=[_agent_member("a")],
        extra_nodes=[{"id": "setup", "type": "transform", "template": "ready", "next": "crew"}],
    )
    spec["start"] = "setup"
    state = run_workflow_spec(spec, llm=llm)
    child = reconstruct_after(state, "crew")
    assert child.metadata["teams"]["crew"]["scratchpad"]["findings"] == ["forked"]
    assert child.metadata["teams"]["crew"]["handoffs"]


def test_fork_from_paused_mid_team_checkpoint() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    spec = _team(
        members=[
            {
                "id": "sign_off",
                "role": "human",
                "type": "approval",
                "prompt": "Approve the draft.",
                "scratchpad": {"read": ["findings"], "write": []},
            }
        ]
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_spec(spec, llm=llm)
    state = paused.value.state
    assert state.results == []
    assert "crew" not in state.node_outputs
    bucket = state.metadata["teams"]["crew"]
    assert bucket["pending_member"] == "sign_off"
    child = reconstruct_after(state, "crew")
    assert child.metadata["forked_from"] == state.run_id
    assert child.metadata["forked_at_node"] == "crew"
    assert child.metadata["teams"]["crew"]["pending_member"] == "sign_off"
    assert child.metadata["teams"]["crew"]["scratchpad"] == bucket["scratchpad"]
    assert child.metadata["teams"]["crew"]["handoffs"] == bucket["handoffs"]

    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    with_setup = _team(
        members=[
            {
                "id": "sign_off",
                "role": "human",
                "type": "approval",
                "prompt": "Approve the draft.",
                "scratchpad": {"read": ["findings"], "write": []},
            }
        ],
        extra_nodes=[{"id": "setup", "type": "transform", "template": "ready", "next": "crew"}],
    )
    with_setup["start"] = "setup"
    with pytest.raises(ApprovalRequired) as paused_setup:
        run_workflow_spec(with_setup, llm=llm)
    parent = paused_setup.value.state
    assert all(row.node_id != "crew" for row in parent.results)
    forked = reconstruct_after(parent, "crew")
    assert forked.node_outputs["setup"] == "ready"
    assert forked.metadata["teams"]["crew"]["handoffs"]
    assert forked.metadata["teams"]["crew"]["pending_member"] == "sign_off"


def test_persist_then_cli_fork_from_paused_team(
    tmp_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pause.yaml"
    spec = _team(
        members=[
            {
                "id": "sign_off",
                "role": "human",
                "type": "approval",
                "prompt": "Approve the draft.",
                "scratchpad": {"read": ["findings"], "write": []},
            }
        ],
        extra_nodes=[{"id": "setup", "type": "transform", "template": "ready", "next": "crew"}],
    )
    spec["start"] = "setup"
    path.write_text(json.dumps(spec), encoding="utf-8")
    llm = ScriptedLLM()
    llm.enqueue('{"next": "sign_off", "reason": "human"}', model="sup")
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True, llm=llm)
    parent = paused.value.state
    run_path = tmp_settings.runs_dir() / f"{parent.run_id}.json"
    assert run_path.is_file()
    disk = json.loads(run_path.read_text(encoding="utf-8"))
    assert disk["metadata"]["teams"]["crew"]["pending_member"] == "sign_off"
    assert disk["metadata"]["teams"]["crew"]["handoffs"]

    with pytest.raises(ApprovalRequired) as forked:
        fork_run(parent.run_id, "crew", settings=tmp_settings, persist=True)
    child = forked.value.state
    assert child.metadata["forked_from"] == parent.run_id
    assert child.metadata["teams"]["crew"]["pending_member"] == "sign_off"
    assert child.metadata["teams"]["crew"]["handoffs"]
    assert (
        child.metadata["teams"]["crew"]["scratchpad"]
        == parent.metadata["teams"]["crew"]["scratchpad"]
    )

    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home))
    clear_settings_cache()
    result = runner.invoke(app, ["runs", "fork", parent.run_id, "--from-node", "crew", "--json"])
    assert result.exit_code == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    record = payload.get("run") or {}
    meta = record.get("metadata") or {}
    assert meta.get("forked_from") == parent.run_id
    assert meta["teams"]["crew"]["pending_member"] == "sign_off"
    assert meta["teams"]["crew"]["handoffs"]
    clear_settings_cache()


def test_cli_team_example_twice() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "team_pipeline.yaml"
    first = runner.invoke(app, ["run", str(example), "--json", "--no-persist"])
    second = runner.invoke(app, ["run", str(example), "--json", "--no-persist"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert a["ok"] is True and b["ok"] is True
    assert a["output_keys"]["out"]["scratchpad"] == b["output_keys"]["out"]["scratchpad"]
    assert a["run_id"] != b["run_id"]
