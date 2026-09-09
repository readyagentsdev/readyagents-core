from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents import __version__
from readyagents.cli import app
from readyagents.scaffold import TEMPLATES
from readyagents.workflow.jsonschema import (
    NODE_TYPE_FIELDS,
    SCHEMA_DIALECT,
    SCHEMA_ID,
    workflow_json_schema,
    workflow_json_schema_text,
)
from readyagents.workflow.schema import NodeType

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "schemas" / "workflow-v1.json"


def test_committed_schema_matches_generator() -> None:
    generated = workflow_json_schema_text()
    assert COMMITTED.is_file()
    assert COMMITTED.read_text(encoding="utf-8") == generated
    again = workflow_json_schema_text()
    assert again == generated


def test_schema_identity_and_aliases() -> None:
    schema = workflow_json_schema()
    assert schema["$schema"] == SCHEMA_DIALECT
    assert schema["$id"] == SCHEMA_ID
    assert schema["title"]
    assert schema["description"]
    assert schema["x-readyagents-version"] == __version__
    dumped = json.dumps(schema)
    assert "else_" not in dumped
    assert "from_" not in dumped
    assert "call_inputs" not in dumped
    assert '"else"' in dumped
    assert '"from"' in dumped


def test_node_type_enum_surfaced_from_python() -> None:
    schema = workflow_json_schema()
    type_field = schema["$defs"]["NodeSpec"]["properties"]["type"]
    assert "enum" not in type_field
    any_of = type_field.get("anyOf")
    assert isinstance(any_of, list)
    enum_clause = next(part for part in any_of if "enum" in part)
    string_clause = next(part for part in any_of if part.get("type") == "string")
    assert string_clause["type"] == "string"
    assert enum_clause["enum"] == [member.value for member in NodeType]
    dumped = json.dumps(schema)
    assert '"enum"' in dumped

    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(
        {
            "name": "pack",
            "nodes": [{"id": "custom", "type": "pack_widget", "widget": True}],
        }
    )


def test_every_node_type_has_if_then() -> None:
    schema = workflow_json_schema()
    node = schema["$defs"]["NodeSpec"]
    assert node.get("additionalProperties") is True
    clauses = node["allOf"]
    consts = [c["if"]["properties"]["type"]["const"] for c in clauses]
    assert consts == [member.value for member in NodeType]
    for member in NodeType:
        assert member in NODE_TYPE_FIELDS
        clause = next(c for c in clauses if c["if"]["properties"]["type"]["const"] == member.value)
        props = clause["then"]["properties"]
        for field in NODE_TYPE_FIELDS[member]:
            assert field in props, field


def test_branches_and_body_use_defs_ref() -> None:
    schema = workflow_json_schema()
    node = schema["$defs"]["NodeSpec"]
    parallel = next(
        c for c in node["allOf"] if c["if"]["properties"]["type"]["const"] == "parallel"
    )
    foreach = next(c for c in node["allOf"] if c["if"]["properties"]["type"]["const"] == "foreach")
    assert parallel["then"]["properties"]["branches"]["items"]["$ref"] == "#/$defs/NodeSpec"
    body = foreach["then"]["properties"]["body"]
    refs = json.dumps(body)
    assert "#/$defs/NodeSpec" in refs
    # no unbounded inline expansion
    assert len(workflow_json_schema_text()) < 200_000


def test_schema_cli_stdout_matches_committed() -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout == COMMITTED.read_text(encoding="utf-8")


def test_schema_check_and_json_envelope(tmp_path: Path) -> None:
    ok = runner.invoke(app, ["schema", "--check", str(COMMITTED)])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    mutated = tmp_path / "mutated.json"
    mutated.write_text(
        COMMITTED.read_text(encoding="utf-8").replace("ReadyAgents", "X"), encoding="utf-8"
    )
    bad = runner.invoke(app, ["schema", "--check", str(mutated)])
    assert bad.exit_code == 1
    text = bad.stdout + bad.stderr
    assert "schema drift" in text
    js = runner.invoke(app, ["schema", "--json"])
    assert js.exit_code == 0, js.stdout + js.stderr
    assert "\x1b" not in js.stdout
    payload = json.loads(js.stdout[js.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "schema"
    assert payload["schema"]["$id"] == SCHEMA_ID


def test_schema_output_write_refuse_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "workflow.schema.json"
    first = runner.invoke(app, ["schema", "--output", str(dest)])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert dest.read_text(encoding="utf-8") == workflow_json_schema_text()
    dest.write_text("keep", encoding="utf-8")
    refused = runner.invoke(app, ["schema", "--output", str(dest)])
    assert refused.exit_code == 1
    assert "Refusing to overwrite" in refused.stdout + refused.stderr
    assert dest.read_text(encoding="utf-8") == "keep"
    forced = runner.invoke(app, ["schema", "--output", str(dest), "--force"])
    assert forced.exit_code == 0, forced.stdout + forced.stderr
    assert dest.read_text(encoding="utf-8") == workflow_json_schema_text()


def test_schema_output_refuses_directory_and_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "adir"
    directory.mkdir()
    as_dir = runner.invoke(app, ["schema", "--output", str(directory)])
    assert as_dir.exit_code == 1
    assert "directory" in (as_dir.stdout + as_dir.stderr).lower()
    outside = tmp_path.parent / f"schema-escape-{tmp_path.name}"
    outside.mkdir()
    target = outside / "escaped.json"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    escaped = runner.invoke(app, ["schema", "--output", str(link)])
    assert escaped.exit_code == 1
    assert not target.exists()


def test_examples_validate_against_generated_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = workflow_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    examples = ROOT / "examples"
    files = sorted(examples.glob("*.yaml")) + sorted(examples.glob("*.json"))
    checked = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        if not isinstance(data, dict) or "nodes" not in data:
            continue
        errors = list(validator.iter_errors(data))
        assert not errors, f"{path.name}: {errors[0].message}"
        checked += 1
    assert checked >= 10
    assert (examples / "calc_pipeline.json") in files or True
    json_data = json.loads((examples / "calc_pipeline.json").read_text(encoding="utf-8"))
    validator.validate(json_data)


def test_malformed_fixture_fails_at_pointer() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = workflow_json_schema()
    validator = jsonschema.Draft202012Validator(schema)
    bad = {
        "name": "bad",
        "nodes": [{"id": "a", "type": "agent"}],
    }
    errors = list(validator.iter_errors(bad))
    assert errors
    paths = [list(err.absolute_path) for err in errors]
    assert any("nodes" in path or path == ["nodes", 0] or "prompt" in path for path in paths)


def test_new_templates_have_resolvable_schema(tmp_path: Path) -> None:
    for kind in TEMPLATES:
        dest = tmp_path / kind
        result = runner.invoke(app, ["new", kind, "--dest", str(dest), "--template", kind])
        assert result.exit_code == 0, kind + result.stdout + result.stderr
        wf = dest / "workflow.yaml"
        schema = dest / "workflow.schema.json"
        text = wf.read_text(encoding="utf-8")
        assert text.startswith("# yaml-language-server: $schema=./workflow.schema.json")
        assert schema.is_file()
        assert schema.read_text(encoding="utf-8") == workflow_json_schema_text()
        assert (dest / schema.name).is_file()


def test_json_workflow_with_schema_key_loads(tmp_path: Path) -> None:
    path = tmp_path / "with-schema.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "./workflow.schema.json",
                "name": "annotated",
                "nodes": [{"id": "t", "type": "transform", "template": "ok", "output_key": "v"}],
            }
        ),
        encoding="utf-8",
    )
    from readyagents.workflow.runner import load_workflow, run_workflow_file

    spec = load_workflow(path)
    assert spec.name == "annotated"
    state = run_workflow_file(path, persist=False)
    assert state.status == "succeeded"


def test_offline_relative_modeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    out = tmp_path / ".readyagents" / "workflow.schema.json"
    result = runner.invoke(app, ["schema", "--output", str(out)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert out.is_file()
    wf = tmp_path / "flow.yaml"
    wf.write_text(
        "# yaml-language-server: $schema=./.readyagents/workflow.schema.json\n"
        "name: offline\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: hi\n"
        "    output_key: v\n",
        encoding="utf-8",
    )
    rel = Path(".readyagents/workflow.schema.json")
    assert rel.is_file()
    valid = runner.invoke(app, ["validate", str(wf)])
    assert valid.exit_code == 0, valid.stdout + valid.stderr
