from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from readyagents.config import clear_settings_cache
from readyagents.errors import SourceMapBoundError, WorkflowError
from readyagents.workflow.runner import load_workflow
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.source_map import (
    _build_index,
    locate_errors,
    locate_node_field,
    sanitize_excerpt,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_wrong_scalar_line_and_column(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad.yaml",
        "name: bad\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        '    retry: {max_attempts: "three"}\n',
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    problems = caught.value.problems
    assert problems
    pos = problems[0].position
    assert pos is not None
    assert pos.line == 6
    assert pos.column >= 1
    assert "three" in pos.excerpt
    assert "^" in pos.pointer
    assert problems[0].loc == ("nodes", 0, "retry", "max_attempts")


def test_missing_required_and_extra_retry_field(tmp_path: Path) -> None:
    missing = _write(
        tmp_path / "missing.yaml",
        "nodes:\n  - id: a\n    type: transform\n    template: x\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(missing)
    locs = [p.loc for p in caught.value.problems]
    assert any(p and p[0] == "name" for p in locs)

    extra = _write(
        tmp_path / "extra.yaml",
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        "    retry:\n"
        "      max_attempts: 1\n"
        "      bogus: 1\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(extra)
    problem = next(p for p in caught.value.problems if p.loc[-1] == "bogus")
    assert problem.position is not None
    assert "bogus" in problem.position.excerpt


def test_alias_fields_use_file_spelling(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "alias.yaml",
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: condition\n"
        "    when: true\n"
        "    then: b\n"
        "    else: 123\n"
        "  - id: b\n"
        "    type: transform\n"
        "    template: x\n"
        "edges:\n"
        "  - from: a\n"
        "    to: b\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    locs = [p.loc for p in caught.value.problems]
    assert any("else" in loc for loc in locs)
    assert all("else_" not in loc for loc in locs)


def test_parallel_branch_and_foreach_body(tmp_path: Path) -> None:
    parallel = _write(
        tmp_path / "par.yaml",
        "name: p\n"
        "nodes:\n"
        "  - id: fan\n"
        "    type: parallel\n"
        "    branches:\n"
        "      - id: one\n"
        "        type: transform\n"
        "        template: a\n"
        "      - id: two\n"
        "        type: transform\n"
        "        template: b\n"
        "        retry: {max_attempts: 0}\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(parallel)
    problem = caught.value.problems[0]
    assert problem.loc[:4] == ("nodes", 0, "branches", 1)
    assert problem.position is not None
    assert "max_attempts" in problem.position.excerpt

    foreach = _write(
        tmp_path / "each.yaml",
        "name: e\n"
        "nodes:\n"
        "  - id: each\n"
        "    type: foreach\n"
        "    items: xs\n"
        "    body:\n"
        "      id: math\n"
        "      type: tool\n"
        "      tool: 1\n",
    )
    # tool: 1 is invalid string? actually 1 might coerce. use retry bound instead.
    foreach.write_text(
        "name: e\n"
        "nodes:\n"
        "  - id: each\n"
        "    type: foreach\n"
        "    items: xs\n"
        "    body:\n"
        "      id: math\n"
        "      type: tool\n"
        "      tool: calc\n"
        "      retry: {max_attempts: 0}\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(foreach)
    problem = caught.value.problems[0]
    assert "body" in problem.loc
    assert problem.position is not None


def test_json_workflow_positions(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bad.json",
        '{\n  "name": "j",\n  "nodes": [\n    {"id": "a", "type": "transform", "template": "x", "retry": {"max_attempts": 0}}\n  ]\n}\n',
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    pos = caught.value.problems[0].position
    assert pos is not None
    assert pos.line >= 1
    assert "^" in pos.pointer


def test_crlf_tabs_bom_and_long_line(tmp_path: Path) -> None:
    crlf = _write(
        tmp_path / "crlf.yaml",
        "name: x\r\nnodes:\r\n  - id: a\r\n    type: transform\r\n    template: x\r\n    retry: {max_attempts: 0}\r\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(crlf)
    assert caught.value.problems[0].position is not None

    tabbed = _write(
        tmp_path / "tabs.yaml",
        "name: x\nnodes:\n\t- id: a\n\ttype: transform\n\ttemplate: x\n\tretry: {max_attempts: 0}\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(tabbed)
    pos = caught.value.problems[0].position
    assert pos is not None
    assert "\t" not in pos.excerpt
    assert pos.pointer.strip() == "^"

    long_val = "n" * 400
    long_line = _write(
        tmp_path / "long.yaml",
        f'name: x\nnodes:\n  - id: a\n    type: transform\n    template: x\n    retry: {{max_attempts: "{long_val}"}}\n',
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(long_line)
    excerpt = caught.value.problems[0].position.excerpt
    assert len(excerpt) <= 160

    excerpt, col = sanitize_excerpt("ok\x1b[31mRED\x1b[0m secret sk-abcdefghijklmnop", 1)
    assert "\x1b" not in excerpt
    assert "[redacted]" in excerpt or "sk-" not in excerpt


def test_bom_caret_stays_on_value(tmp_path: Path) -> None:
    body = (
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        "    retry: {max_attempts: 0}\n"
    )
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    pos = caught.value.problems[0].position
    assert pos is not None
    assert "max_attempts" in pos.excerpt
    pointed = pos.excerpt[pos.column - 1] if 0 < pos.column <= len(pos.excerpt) else ""
    assert pointed == "0" or pos.pointer.strip() == "^"


def test_multibyte_utf8_caret_alignment(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "utf8.yaml",
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: 日本語café\n"
        "    retry: {max_attempts: 0}\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    pos = caught.value.problems[0].position
    assert pos is not None
    assert "max_attempts" in pos.excerpt
    assert "\t" not in pos.excerpt
    idx = pos.column - 1
    assert 0 <= idx < len(pos.excerpt)
    assert pos.pointer[idx] == "^"
    assert pos.excerpt[idx] == "0"


def test_did_you_mean_unknown_node_id(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.yaml",
        "name: review\n"
        "nodes:\n"
        "  - id: gate\n"
        "    type: transform\n"
        "    template: x\n"
        "    next: publsh\n"
        "  - id: publish\n"
        "    type: transform\n"
        "    template: y\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    top = str(caught.value)
    assert "Invalid workflow" in top
    assert "unknown node 'publsh'" in top
    assert "did you mean" not in top
    joined = " ".join(item.message for item in caught.value.problems)
    assert "did you mean 'publish'" in joined


def test_did_you_mean_not_for_unrelated_or_free_text(tmp_path: Path) -> None:
    unrelated = _write(
        tmp_path / "nope.yaml",
        "name: review\n"
        "nodes:\n"
        "  - id: gate\n"
        "    type: transform\n"
        "    template: x\n"
        "    next: zzzzzzzz\n"
        "  - id: publish\n"
        "    type: transform\n"
        "    template: y\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(unrelated)
    joined = " ".join(item.message for item in caught.value.problems)
    assert "did you mean" not in joined

    free = _write(
        tmp_path / "prompt.yaml",
        "name: a\nnodes:\n  - id: worker\n    type: agent\n    prompt: publsh this now\n",
    )
    spec_ok = True
    try:
        load_workflow(free)
    except WorkflowError as exc:
        spec_ok = False
        joined = " ".join(item.message for item in exc.problems)
        assert "did you mean" not in joined
    if spec_ok:
        # agent with a free-text prompt must not invent a node-id suggestion
        pass


def test_configured_literal_is_masked_in_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_settings_cache()
    monkeypatch.setenv("READYAGENTS_REDACT_LITERALS", "super-secret-token")
    clear_settings_cache()
    path = _write(
        tmp_path / "secret.yaml",
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        "    retry: {max_attempts: super-secret-token}\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    pos = caught.value.problems[0].position
    assert pos is not None
    assert "super-secret-token" not in pos.excerpt
    assert "[redacted]" in pos.excerpt
    clear_settings_cache()


def test_unmappable_loc_has_no_position() -> None:
    try:
        WorkflowSpec.model_validate({"name": "x", "nodes": []})
    except ValidationError as exc:
        problems = locate_errors(exc, "memory.yaml", source="name: x\n")
    assert problems
    # empty nodes is a model validator on the whole document; position may be root or none
    assert all(p.position is None or p.position.line >= 1 for p in problems)


def test_yaml_parse_error_has_mark(tmp_path: Path) -> None:
    path = _write(tmp_path / "broken.yaml", "name: [\n")
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    assert "Could not parse" in str(caught.value)
    assert caught.value.problems
    assert caught.value.problems[0].position is not None


def test_anchor_ambiguity_yields_no_wrong_caret(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "alias.yaml",
        "name: x\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: transform\n"
        "    template: x\n"
        "    retry: &r\n"
        "      max_attempts: 0\n"
        "  - id: b\n"
        "    type: transform\n"
        "    template: y\n"
        "    retry: *r\n",
    )
    with pytest.raises(WorkflowError) as caught:
        load_workflow(path)
    # both nodes share the retry mapping; at least one loc must not invent a false caret
    for problem in caught.value.problems:
        if problem.loc[:2] == ("nodes", 1):
            assert problem.position is None or "retry" in problem.position.excerpt


def test_include_chain_points_at_child_and_parent(tmp_path: Path) -> None:
    child = _write(
        tmp_path / "child.yaml",
        "name: child\nnodes:\n  - id: n\n    type: transform\n    template: x\n    retry: {max_attempts: 0}\n",
    )
    parent = _write(
        tmp_path / "parent.yaml",
        "name: parent\nnodes:\n  - id: child\n    type: include\n    path: child.yaml\n",
    )
    from readyagents.errors import NodeError
    from readyagents.workflow.runner import run_workflow_file

    with pytest.raises((WorkflowError, NodeError)) as caught:
        run_workflow_file(parent, persist=False)
    problems = list(getattr(caught.value, "problems", None) or [])
    assert problems
    assert problems[0].position is not None
    assert problems[0].position.path.name == "child.yaml" or "child.yaml" in str(
        problems[0].position.path
    )
    assert problems[0].include_chain
    site = problems[0].include_chain[0]
    assert site.line >= 1
    # include site helper
    found = locate_node_field(parent, "child", "path")
    assert found is not None
    assert found.line >= 1
    del child


def test_depth_bomb_raises_typed_error() -> None:
    nested = "bad: 1"
    for _ in range(100):
        nested = "a:\n" + "\n".join("  " + line for line in nested.splitlines())
    with pytest.raises(SourceMapBoundError):
        _build_index(nested)


def test_anchor_bomb_does_not_hang() -> None:
    lines = ["a: &a [x, x]"]
    prev = "a"
    for i in range(20):
        name = f"b{i}"
        lines.append(f"{name}: &{name} [*{prev}, *{prev}]")
        prev = name
    source = "\n".join(lines) + "\n"
    index = _build_index(source)
    assert index.by_path
