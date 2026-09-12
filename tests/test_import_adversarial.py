"""Adversarial importer suite: no exec, no parser bombs, no secret leaks, no overstated fidelity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from import_cases import crewai_export, langgraph_export, n8n_export, trigger_export
from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import (
    ImportBoundDepth,
    ImportBoundNodes,
    ImportBoundOversized,
    ImportRefused,
)
from readyagents.importers.bounds import MAX_BYTES, MAX_DEPTH, MAX_NODES
from readyagents.importers.crewai import parse_crewai
from readyagents.importers.langgraph import parse_langgraph
from readyagents.importers.n8n import parse_n8n
from readyagents.importers.service import import_workflow
from readyagents.importers.trigger import parse_trigger

runner = CliRunner()

_SECRET = "sk-abcdefghijklmnop"
_MISSING = "definitely_missing_langgraph_pkg_xyz"


def _cli_env(tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()


def _nest(levels: int) -> dict:
    node: dict = {"v": 1}
    for _ in range(levels):
        node = {"c": node}
    return node


def _pwn_exec(marker: Path) -> str:
    return f"exec(\"open({str(marker)!r}, 'w').write('pwned')\")\n"


def _pwn_eval(marker: Path) -> str:
    return f"eval(\"open({str(marker)!r}, 'w').write('pwned')\")\n"


def _write_payload_module(tmp_path: Path, marker: Path) -> str:
    name = "pwn_on_import_mod"
    (tmp_path / f"{name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('pwned', encoding='utf-8')\n"
        "raise RuntimeError('payload module imported')\n",
        encoding="utf-8",
    )
    return name


# --- 1. Code execution via parse ---


def test_exec_open_write_is_refused_and_does_not_run(tmp_path: Path, tmp_settings) -> None:
    marker = tmp_path / "pwned-exec.txt"
    src = _pwn_exec(marker) + "g.add_node('x', lambda s: s)\n"
    path = tmp_path / "evil.py"
    path.write_text(src, encoding="utf-8")
    with pytest.raises(ImportRefused) as extra:
        parse_langgraph(src)
    assert extra.value.reason == "ast"
    with pytest.raises(ImportRefused) as extra:
        import_workflow("langgraph", path, out=tmp_path / "out", settings=tmp_settings)
    assert extra.value.reason == "ast"
    assert not marker.exists()


def test_eval_open_write_is_refused_and_does_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "pwned-eval.txt"
    src = _pwn_eval(marker) + "g.add_node('x', lambda s: s)\n"
    with pytest.raises(ImportRefused) as extra:
        parse_langgraph(src)
    assert extra.value.reason == "ast"
    assert not marker.exists()


def test_crewai_exec_open_write_is_refused_and_does_not_run(tmp_path: Path, tmp_settings) -> None:
    marker = tmp_path / "pwned-crew.txt"
    src = _pwn_exec(marker) + "Agent(role='Writer', goal='Draft')\n"
    path = tmp_path / "crew.py"
    path.write_text(src, encoding="utf-8")
    with pytest.raises(ImportRefused) as extra:
        parse_crewai(src, filename="crew.py")
    assert extra.value.reason == "ast"
    with pytest.raises(ImportRefused) as extra:
        import_workflow("crewai", path, out=tmp_path / "out", settings=tmp_settings)
    assert extra.value.reason == "ast"
    assert not marker.exists()


def test_module_level_open_write_is_not_executed(tmp_path: Path, tmp_settings) -> None:
    marker = tmp_path / "pwned-toplevel.txt"
    src = f"open({str(marker)!r}, 'w').write('pwned')\ng.add_node('x', lambda s: s)\n"
    path = tmp_path / "graph.py"
    path.write_text(src, encoding="utf-8")
    graph = parse_langgraph(src)
    assert graph.nodes
    result = import_workflow("langgraph", path, out=tmp_path / "out", settings=tmp_settings)
    assert result.workflow_path.is_file()
    assert not marker.exists()


def test_import_of_raising_payload_module_is_not_executed(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pwned-import.txt"
    mod = _write_payload_module(tmp_path, marker)
    monkeypatch.syspath_prepend(str(tmp_path))
    src = f"import {mod}\ng.add_node('x', lambda s: s)\n"
    path = tmp_path / "graph.py"
    path.write_text(src, encoding="utf-8")
    try:
        graph = parse_langgraph(src)
    except ImportError as extra:
        raise AssertionError("importer executed import (ImportError)") from extra
    except RuntimeError as extra:
        raise AssertionError("importer executed payload module") from extra
    assert graph.nodes
    try:
        import_workflow("langgraph", path, out=tmp_path / "out", settings=tmp_settings)
    except ImportError as extra:
        raise AssertionError("import_workflow executed import") from extra
    except RuntimeError as extra:
        raise AssertionError("import_workflow executed payload module") from extra
    assert not marker.exists()


def test_crewai_import_of_raising_payload_module_is_not_executed(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "pwned-crew-import.txt"
    mod = _write_payload_module(tmp_path, marker)
    monkeypatch.syspath_prepend(str(tmp_path))
    src = f"import {mod}\nfrom crewai import Agent\nAgent(role='Writer', goal='Draft')\n"
    path = tmp_path / "crew.py"
    path.write_text(src, encoding="utf-8")
    try:
        graph = parse_crewai(src, filename="crew.py")
    except ImportError as extra:
        raise AssertionError("crewai parser executed import") from extra
    except RuntimeError as extra:
        raise AssertionError("crewai parser executed payload module") from extra
    assert any(n.kind == "agent" for n in graph.nodes)
    try:
        import_workflow("crewai", path, out=tmp_path / "out", settings=tmp_settings)
    except (ImportError, RuntimeError) as extra:
        raise AssertionError("crewai import_workflow executed payload") from extra
    assert not marker.exists()


def test_missing_module_import_still_ast_parses() -> None:
    src = f"import {_MISSING}\ng.add_node('x', lambda s: s)\n"
    try:
        graph = parse_langgraph(src)
    except ImportError as extra:
        raise AssertionError("importer raised ImportError for a missing import") from extra
    assert graph.nodes
    crew = f"import {_MISSING}\nAgent(role='Writer', goal='Draft')\n"
    try:
        parsed = parse_crewai(crew, filename="crew.py")
    except ImportError as extra:
        raise AssertionError("crewai importer raised ImportError for a missing import") from extra
    assert any(n.kind == "agent" for n in parsed.nodes)


def test_crewai_yaml_python_tag_does_not_execute(tmp_path: Path) -> None:
    marker = tmp_path / "pwned-yaml.txt"
    blob = (
        "name: research\nversion: 1\nagents:\n  writer:\n    role: Writer\n"
        f"    goal: !!python/object/apply:pathlib.Path.write_text [{str(marker)!r}, pwned]\n"
    )
    with pytest.raises(ImportRefused):
        parse_crewai(blob, filename="crew.yaml")
    assert not marker.exists()


# --- 2. Parser bombs ---


@pytest.mark.parametrize("parser", [parse_n8n, parse_langgraph, parse_crewai, parse_trigger])
def test_oversized_source_is_typed_refuse(parser) -> None:
    blob = "x" * (MAX_BYTES + 1)
    assert len(blob.encode("utf-8")) > 1_048_576
    with pytest.raises(ImportBoundOversized):
        parser(blob)


def test_nesting_deeper_than_max_depth_is_typed_refuse() -> None:
    extra = _nest(MAX_DEPTH + 5)
    n8n = json.dumps({"name": "d", "nodes": [], "extra": extra})
    with pytest.raises(ImportBoundDepth):
        parse_n8n(n8n)
    trigger = json.dumps({"version": "1", "trigger": {}, "steps": [], "extra": extra})
    with pytest.raises(ImportBoundDepth):
        parse_trigger(trigger)
    crew = {
        "name": "d",
        "version": "1",
        "agents": {"writer": {"role": "Writer"}},
        "extra": extra,
    }
    import yaml

    with pytest.raises(ImportBoundDepth):
        parse_crewai(yaml.safe_dump(crew), filename="crew.yaml")


def test_node_count_over_max_is_typed_refuse() -> None:
    nodes = [
        {"id": str(i), "name": f"n{i}", "type": "n8n-nodes-base.noOp", "parameters": {}}
        for i in range(MAX_NODES + 5)
    ]
    with pytest.raises(ImportBoundNodes):
        parse_n8n(json.dumps({"name": "bomb", "nodes": nodes, "connections": {}}))
    lg = "g = object()\n" + "".join(
        f"g.add_node('n{i}', lambda s: s)\n" for i in range(MAX_NODES + 5)
    )
    with pytest.raises(ImportBoundNodes):
        parse_langgraph(lg)
    agents = {f"a{i}": {"role": f"r{i}"} for i in range(MAX_NODES + 5)}
    crew = {"name": "bomb", "version": "1", "agents": agents}
    import yaml

    with pytest.raises(ImportBoundNodes):
        parse_crewai(yaml.safe_dump(crew), filename="crew.yaml")
    steps = [{"id": str(i), "type": "webhook"} for i in range(MAX_NODES + 5)]
    with pytest.raises(ImportBoundNodes):
        parse_trigger(json.dumps({"version": "1", "trigger": {"app": "webhook"}, "steps": steps}))


# --- 3. Secret leakage ---


def test_n8n_parameter_secret_not_in_yaml_or_report(tmp_path: Path, tmp_settings) -> None:
    data = json.loads(n8n_export())
    data["nodes"][1]["parameters"] = {
        "apiKey": _SECRET,
        "headers": {"Authorization": f"Bearer {_SECRET}"},
        "note": "ok",
    }
    src = tmp_path / "secret.json"
    src.write_text(json.dumps(data), encoding="utf-8")
    result = import_workflow("n8n", src, out=tmp_path / "out", settings=tmp_settings)
    yaml_text = result.workflow_path.read_text(encoding="utf-8")
    report = result.report_path.read_text(encoding="utf-8")
    mermaid = result.graph_path.read_text(encoding="utf-8")
    assert _SECRET not in yaml_text
    assert _SECRET not in report
    assert _SECRET not in mermaid
    assert _SECRET not in json.dumps(result.workflow)
    assert _SECRET not in json.dumps(result.report.as_dict())
    blob = " ".join(result.report.warnings).lower()
    assert "secret" in blob
    assert "export file itself is a secret" in " ".join(result.report.warnings)
    assert "export file itself is a secret" in report


def test_langgraph_string_constant_secret_not_copied(tmp_path: Path, tmp_settings) -> None:
    src = (
        "from langgraph.graph import StateGraph, START, END\n"
        f"API_KEY = '{_SECRET}'\n"
        "g = StateGraph(dict)\n"
        f"g.add_node('greet', lambda s: s)\n"
        f"g.add_node('{_SECRET}', lambda s: s)\n"
        "g.add_edge(START, 'greet')\n"
        f"g.add_conditional_edges('greet', lambda s: s, {{'ok': '{_SECRET}', 'no': 'greet'}})\n"
    )
    path = tmp_path / "graph.py"
    path.write_text(src, encoding="utf-8")
    result = import_workflow("langgraph", path, out=tmp_path / "out", settings=tmp_settings)
    yaml_text = result.workflow_path.read_text(encoding="utf-8")
    report = result.report_path.read_text(encoding="utf-8")
    assert _SECRET not in yaml_text
    assert _SECRET not in report
    assert _SECRET not in json.dumps(result.workflow)
    assert _SECRET not in json.dumps(result.report.as_dict())
    assert "export file itself is a secret" in " ".join(result.report.warnings)


# --- 4. Overstated fidelity ---


def _status_for(result, *, kind: str | None = None, structural: str | None = None) -> list[str]:
    rows = result.report.nodes
    if kind is not None:
        rows = [n for n in rows if n.kind == kind]
    if structural is not None:
        rows = [n for n in rows if n.structural == structural]
    return [n.status for n in rows]


def test_approximated_mappings_are_not_reported_translated(tmp_path: Path, tmp_settings) -> None:
    n8n_src = tmp_path / "n.json"
    n8n_src.write_text(n8n_export(), encoding="utf-8")
    n8n = import_workflow("n8n", n8n_src, out=tmp_path / "n-out", settings=tmp_settings)
    assert _status_for(n8n, kind="n8n-nodes-base.if")
    assert "translated" not in _status_for(n8n, kind="n8n-nodes-base.if")
    assert set(_status_for(n8n, kind="n8n-nodes-base.if")) == {"approximated"}

    lg_src = tmp_path / "g.py"
    lg_src.write_text(langgraph_export(), encoding="utf-8")
    lg = import_workflow("langgraph", lg_src, out=tmp_path / "g-out", settings=tmp_settings)
    assert _status_for(lg, kind="condition")
    assert "translated" not in _status_for(lg, kind="condition")
    assert set(_status_for(lg, kind="condition")) == {"approximated"}

    crew_src = tmp_path / "c.yaml"
    crew_src.write_text(crewai_export(), encoding="utf-8")
    crew = import_workflow("crewai", crew_src, out=tmp_path / "c-out", settings=tmp_settings)
    assert _status_for(crew, kind="agent")
    assert "translated" not in _status_for(crew, kind="agent")
    assert set(_status_for(crew, kind="agent")) == {"approximated"}

    trig_src = tmp_path / "z.json"
    trig_src.write_text(trigger_export(), encoding="utf-8")
    trig = import_workflow("trigger", trig_src, out=tmp_path / "z-out", settings=tmp_settings)
    assert _status_for(trig, kind="filter")
    assert "translated" not in _status_for(trig, kind="filter")
    assert set(_status_for(trig, kind="filter")) == {"approximated"}


def test_structural_nodes_are_approximated_not_translated(tmp_path: Path, tmp_settings) -> None:
    n8n_src = tmp_path / "n.json"
    n8n_src.write_text(n8n_export(), encoding="utf-8")
    n8n = import_workflow("n8n", n8n_src, out=tmp_path / "n-out", settings=tmp_settings)
    kinds = {row.structural for row in n8n.report.nodes if row.structural}
    assert {"branch", "loop", "parallel", "subworkflow", "error"} <= kinds

    lg_src = tmp_path / "g.py"
    lg_src.write_text(langgraph_export(), encoding="utf-8")
    lg = import_workflow("langgraph", lg_src, out=tmp_path / "g-out", settings=tmp_settings)
    assert any(row.structural == "branch" for row in lg.report.nodes)

    crew_text = crewai_export() + "  ask:\n    description: confirm\n    human_input: true\n"
    crew_src = tmp_path / "h.yaml"
    crew_src.write_text(crew_text, encoding="utf-8")
    crew = import_workflow("crewai", crew_src, out=tmp_path / "h-out", settings=tmp_settings)
    assert any(row.structural == "human" for row in crew.report.nodes)

    trig_src = tmp_path / "z.json"
    trig_src.write_text(trigger_export(), encoding="utf-8")
    trig = import_workflow("trigger", trig_src, out=tmp_path / "z-out", settings=tmp_settings)
    tstruct = {row.structural for row in trig.report.nodes if row.structural}
    assert {"branch", "loop", "parallel", "subworkflow", "error"} <= tstruct

    for result in (n8n, lg, crew, trig):
        for row in result.report.nodes:
            if row.structural:
                assert row.status == "approximated", (row.id, row.kind, row.status, row.structural)
                assert row.status != "translated"


def test_report_equivalence_false_and_statement_requires_testing(
    tmp_path: Path, tmp_settings
) -> None:
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    result = import_workflow("n8n", src, out=tmp_path / "out", settings=tmp_settings)
    payload = result.report.as_dict()
    assert payload["equivalence"] is False
    statement = (result.report.statement + " " + payload["statement"]).lower()
    assert "must be tested" in statement
    assert "structural" in statement
    markdown = result.report_path.read_text(encoding="utf-8").lower()
    assert "must be tested" in markdown
    assert "structural" in markdown


# --- overwrite / --explain ---


def test_overwrite_without_force_refused(tmp_path: Path, tmp_settings) -> None:
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    dest = tmp_path / "out"
    first = import_workflow("n8n", src, out=dest, settings=tmp_settings)
    original = first.workflow_path.read_text(encoding="utf-8")
    first.workflow_path.write_text(original + "# sentinel\n", encoding="utf-8")
    with pytest.raises(ImportRefused) as extra:
        import_workflow("n8n", src, out=dest, settings=tmp_settings)
    assert extra.value.reason == "exists"
    assert "# sentinel" in first.workflow_path.read_text(encoding="utf-8")


def test_cli_overwrite_without_force_refused(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, tmp_settings, monkeypatch)
    src = tmp_path / "n.json"
    src.write_text(n8n_export(), encoding="utf-8")
    dest = tmp_path / "imported-cli"
    first = runner.invoke(app, ["import", "n8n", str(src), "--out", str(dest)])
    assert first.exit_code == 0, first.stdout + first.stderr
    wf = dest / "workflow.yaml"
    text = wf.read_text(encoding="utf-8")
    wf.write_text(text + "# sentinel\n", encoding="utf-8")
    second = runner.invoke(app, ["import", "n8n", str(src), "--out", str(dest)])
    assert second.exit_code != 0
    assert "# sentinel" in wf.read_text(encoding="utf-8")


def test_explain_writes_no_workflow_yaml(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, tmp_settings, monkeypatch)
    dest = tmp_path / "explain-out"
    first = runner.invoke(app, ["import", "--explain", "n8n", "--out", str(dest)])
    second = runner.invoke(app, ["import", "--explain", "langgraph"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    assert not dest.exists()
    assert not (tmp_path / "workflow.yaml").exists()
    assert list(tmp_path.rglob("workflow.yaml")) == []
    assert "n8n-nodes-base.set" in first.stdout
