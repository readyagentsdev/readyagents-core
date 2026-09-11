"""Adversarial corpus for type: team. Separate from the team-node author tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from readyagents.errors import (
    TeamRoundsExceeded,
    TeamScratchpadDenied,
    TeamSpendExceeded,
    TeamUnknownMember,
)
from readyagents.testing import ScriptedLLM, run_workflow_spec
from readyagents.workflow.schema import WorkflowSpec


def _team(members: list[dict], **kwargs: object) -> dict:
    node = {
        "id": "crew",
        "type": "team",
        "strategy": kwargs.get("strategy") or "route",
        "supervisor": {"prompt": "Route.", "model": "mock:sup"},
        "members": members,
        "scratchpad": {"keys": ["findings", "open_questions"]},
        "terminate": kwargs.get("terminate") or {"max_rounds": 8},
        "output_key": "out",
    }
    return {"name": "adv", "nodes": [node]}


def test_hallucinated_member_id_fails_closed() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "root", "reason": "wider"}', model="sup")
    with pytest.raises(TeamUnknownMember):
        run_workflow_spec(
            _team(
                [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "x",
                        "model": "mock:a",
                        "tools": ["calc"],
                        "scratchpad": {"read": [], "write": ["findings"]},
                    }
                ]
            ),
            llm=llm,
        )


def test_round_inflation_stopped() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a"}', model="sup")
    llm.enqueue("x", model="a")
    with pytest.raises(TeamRoundsExceeded):
        run_workflow_spec(
            _team(
                [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "x",
                        "model": "mock:a",
                        "scratchpad": {"read": [], "write": ["findings"]},
                    }
                ],
                terminate={"max_rounds": 1},
            ),
            llm=llm,
        )


def test_spend_inflation_stopped() -> None:
    llm = ScriptedLLM()
    llm.enqueue(
        '{"next": "a"}',
        model="sup",
        usage={"prompt_tokens": 1, "cost_micros": 100},
    )
    llm.enqueue("x", model="a", usage={"prompt_tokens": 1, "cost_micros": 100})
    with pytest.raises(TeamSpendExceeded):
        run_workflow_spec(
            _team(
                [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "x",
                        "model": "mock:a",
                        "scratchpad": {"read": [], "write": ["findings"]},
                    }
                ],
                terminate={"max_rounds": 8, "max_cost_usd": 0.00005},
            ),
            llm=llm,
        )


def test_scratchpad_write_escape() -> None:
    llm = ScriptedLLM()
    llm.enqueue('{"next": "a"}', model="sup")
    llm.enqueue('{"scratchpad": {"open_questions": ["nope"]}}', model="a")
    with pytest.raises(TeamScratchpadDenied):
        run_workflow_spec(
            _team(
                [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "x",
                        "model": "mock:a",
                        "scratchpad": {"read": ["open_questions"], "write": ["findings"]},
                    }
                ]
            ),
            llm=llm,
        )


def test_nested_team_refused() -> None:
    with pytest.raises((ValidationError, ValueError)):
        WorkflowSpec.model_validate(_team([{"id": "inner", "type": "team", "prompt": "no"}]))


def test_unknown_strategy_refused() -> None:
    with pytest.raises((ValidationError, ValueError)):
        WorkflowSpec.model_validate(
            _team(
                [
                    {
                        "id": "a",
                        "type": "agent",
                        "prompt": "x",
                        "scratchpad": {"read": [], "write": ["findings"]},
                    }
                ],
                strategy="auction",
            )
        )
