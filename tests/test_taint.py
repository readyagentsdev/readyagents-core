"""Provenance survives every construct. Drive shipped run_workflow_file."""

from __future__ import annotations

from pathlib import Path

from readyagents.firewall.taint import provenance_of
from readyagents.workflow.runner import run_workflow_file


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
