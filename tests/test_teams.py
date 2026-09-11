"""Shipped type: team path: strategies, terminate, scratchpad, resume, replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import (
    ApprovalRequired,
    TeamRoundsExceeded,
    TeamScratchpadDenied,
    TeamSpendExceeded,
    TeamUnknownMember,
    TeamWallExceeded,
)
from readyagents.replay.cassette import Cassette
from readyagents.replay.fork import reconstruct_after
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


def test_record_offline_replay(tmp_settings, tmp_path: Path) -> None:
    path = tmp_path / "team.yaml"
    path.write_text(
        json.dumps(
            _team(
                strategy="pipeline",
                members=[
                    {
                        "id": "one",
                        "type": "transform",
                        "template": '{"scratchpad": {"findings": ["r"]}}',
                        "parse_json": True,
                        "scratchpad": {"read": [], "write": ["findings"]},
                    }
                ],
            )
        ),
        encoding="utf-8",
    )
    first = run_workflow_file(path, settings=tmp_settings, persist=True, record=True)
    assert first.status == "succeeded"
    cassette_path = Path(first.metadata["cassette"])
    tape = Cassette.load(cassette_path)
    replayed = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=False,
        offline=True,
        cassette_path=cassette_path,
    )
    assert replayed.status == "succeeded"
    assert replayed.output_keys["out"]["scratchpad"]["findings"] == ["r"]
    assert tape.entries is not None


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
