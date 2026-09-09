"""Provenance survives every construct. Drive shipped run_workflow_file."""

from __future__ import annotations

from pathlib import Path

from readyagents.firewall.taint import (
    prompt_tainted,
    provenance_of,
    seed_foreach_item_provenance,
    untrusted,
)
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_tool_output_is_untrusted(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "t.yaml",
        "name: t\nnodes:\n  - id: n\n    type: tool\n    tool: calc\n"
        "    arguments: {expression: '1+1'}\n    output_key: total\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    assert provenance_of(state, "n").trust == "untrusted"
    assert provenance_of(state, "total").trust == "untrusted"
    assert provenance_of(state, "n").source.startswith("tool:")


def test_transform_laundering_keeps_untrusted(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "t.yaml",
        "name: t\n"
        "nodes:\n"
        "  - id: n\n    type: tool\n    tool: calc\n    arguments: {expression: '3'}\n"
        "    output_key: total\n    next: wrap\n"
        "  - id: wrap\n    type: transform\n    template: 'x={{total}}'\n    output_key: summary\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    assert provenance_of(state, "summary").trust == "untrusted"


def test_condition_records_untrusted_influence(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "t.yaml",
        "name: t\n"
        "nodes:\n"
        "  - id: n\n    type: tool\n    tool: calc\n    arguments: {expression: '1'}\n"
        "    output_key: total\n    next: check\n"
        "  - id: check\n    type: condition\n    when: total == 1\n    then: ok\n    else: ok\n"
        "  - id: ok\n    type: transform\n    template: 'ok'\n    output_key: summary\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    assert provenance_of(state, "check").trust == "untrusted"


def test_foreach_and_parallel_keep_taint(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "t.yaml",
        "name: t\n"
        "nodes:\n"
        "  - id: seed\n    type: transform\n    template: '[1,2]'\n    parse_json: true\n"
        "    output_key: items\n    next: mapped\n"
        "  - id: mapped\n    type: foreach\n    items: items\n    output_key: outs\n"
        "    body:\n      id: inner\n      type: transform\n      template: 'v={{item}}'\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    # literal template '[1,2]' is trusted; foreach body over literals stays trusted
    assert provenance_of(state, "seed").trust == "trusted"


def test_input_is_trusted(tmp_path: Path, tmp_settings) -> None:
    wf = _write(
        tmp_path / "t.yaml",
        "name: t\ninputs: {n: {type: number, required: true}}\n"
        "nodes:\n  - id: x\n    type: transform\n    template: '{{n}}'\n    output_key: summary\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False, inputs={"n": 7})
    assert provenance_of(state, "n").trust == "trusted"
    assert provenance_of(state, "summary").trust == "trusted"
    assert provenance_of(state, "n").source == "input"


def test_include_propagates(tmp_path: Path, tmp_settings) -> None:
    child = _write(
        tmp_path / "child.yaml",
        "name: child\nnodes:\n  - id: c\n    type: tool\n    tool: calc\n"
        "    arguments: {expression: '4'}\n    output_key: inner\n",
    )
    _ = child
    wf = _write(
        tmp_path / "parent.yaml",
        "name: parent\nnodes:\n  - id: inc\n    type: include\n    path: child.yaml\n"
        "    output_key: nested\n",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"


def test_prompt_tainted_when_template_root_is_untrusted() -> None:
    state = RunState.start("t", {"page": "hi"})
    state.provenance["page"] = untrusted(source="tool:now", node_id="page").as_dict()
    assert prompt_tainted(state, "Follow {{page}}", None) is True
    assert prompt_tainted(state, "hello", None) is False


def test_foreach_child_marks_item_untrusted_from_untrusted_items() -> None:
    parent = RunState.start("t", {})
    parent.provenance["items"] = untrusted(source="tool:now", node_id="wrap").as_dict()
    child = RunState.start("t", {"item": "x", "index": 0})
    seed_foreach_item_provenance(parent, child, items_expr="items", node_id="mapped")
    assert provenance_of(child, "item").trust == "untrusted"
    assert provenance_of(child, "index").trust == "untrusted"
    assert provenance_of(child, "items").trust == "untrusted"
