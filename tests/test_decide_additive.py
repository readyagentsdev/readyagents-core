"""Adding decide must not change existing public names or node classifications."""

from __future__ import annotations

from pathlib import Path

from readyagents import __all__ as TOP_LEVEL
from readyagents.replay.cassette import classify_node_type
from readyagents.workflow.schema import NodeType


def test_decide_names_are_not_top_level() -> None:
    assert "get_decider" not in TOP_LEVEL
    assert "Decider" not in TOP_LEVEL
    assert "Decision" not in TOP_LEVEL
    from readyagents.decide import get_decider

    assert callable(get_decider)


def test_existing_classify_node_type_values_unchanged() -> None:
    expected = {
        "transform": "recomputed",
        "condition": "recomputed",
        "foreach": "recomputed",
        "parallel": "recomputed",
        "include": "recomputed",
        "approval": "recomputed",
        "converse": "recomputed",
        "tool": "unsealable",
        "agent": "unsealable",
        "code": "sealed",
        "document": "sealed",
        "transcribe": "sealed",
        "table": "sealed",
        "browser": "sealed",
        "classify": "unsealable",
        "wait": "unsealable",
        "skill": "unsealable",
        "ingest": "unsealable",
        "memory": "unsealable",
        "a2a": "unsealable",
        "team": "unsealable",
        "decide": "sealed",
    }
    for kind, bucket in expected.items():
        assert classify_node_type(kind) == bucket, kind
    assert NodeType.decide.value == "decide"
    assert classify_node_type("decide") == "sealed"


def test_doctor_typesafe_row_is_informational() -> None:
    from readyagents.doctor import run_doctor

    report = run_doctor()
    row = report["keys"]["typesafe"]
    assert row["required"] is False
    assert "configured" in row
    if not row["configured"]:
        assert report["ok"] is True or all(item["id"] != "typesafe" for item in report["findings"])


def test_generated_schema_still_validates_existing_examples() -> None:
    from readyagents.workflow.runner import load_workflow

    root = Path(__file__).resolve().parents[1] / "examples"
    skip = {"readyagents.policy.yaml"}
    for path in sorted(root.glob("*.yaml")):
        if path.name in skip:
            continue
        load_workflow(path)


def test_prices_table_lists_jev() -> None:
    from readyagents.cost.prices import load_price_table

    table = load_price_table()
    for key in ("typesafe:jev-1.13.0", "jev-1.13.0", "typesafe:jev-latest", "typesafe:jev-preview"):
        rate = table.models[key]
        assert rate.input == 0.042
        assert rate.output == 0.0
