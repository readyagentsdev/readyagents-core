"""Shipped Agent Skills path: install, type: skill, export, agents-md."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import SkillDrift, SkillPathDenied, SkillRefused
from readyagents.firewall.policy_file import load_policy
from readyagents.firewall.taint import provenance_of
from readyagents.skills.agents_md import generate_agents_md
from readyagents.skills.catalog import disclose_all, get_record
from readyagents.skills.export import export_workflow
from readyagents.skills.install import add_skill
from readyagents.skills.parse import parse_skill_md
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _skill_md(**extra) -> str:
    name = extra.pop("name", "house-writing-style")
    desc = extra.pop(
        "description",
        "Apply house style to a draft. Use when rewriting ReadyAgents prose.",
    )
    tools = extra.pop("allowed-tools", None)
    lines = ["---", f"name: {name}", f"description: {desc}"]
    if tools is not None:
        lines.append(f"allowed-tools: {tools}")
    lines.extend(["---", "", "Do not call tools. Rewrite in short sentences."])
    return "\n".join(lines)


def _write_skill(tmp: Path, *, name: str = "house-writing-style", script: bool = False) -> Path:
    folder = tmp / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(_skill_md(name=name), encoding="utf-8")
    if script:
        (folder / "scripts").mkdir()
        (folder / "scripts" / "apply.py").write_text(
            'result = {"styled": (inputs.get("draft") or "") + " [styled]"}\n',
            encoding="utf-8",
        )
    return folder


def test_frontmatter_constraints_and_directory_match(tmp_path: Path) -> None:
    with pytest.raises(SkillRefused, match="frontmatter"):
        parse_skill_md("no frontmatter")
    with pytest.raises(SkillRefused, match="name"):
        parse_skill_md(_skill_md(name="BAD_NAME"))
    with pytest.raises(SkillRefused, match="match directory"):
        parse_skill_md(_skill_md(name="house-writing-style"), directory_name="other")
    huge = "x" * 2000
    with pytest.raises(SkillRefused, match="description"):
        parse_skill_md(_skill_md(description=huge))
    rec = parse_skill_md(_skill_md(), directory_name="house-writing-style")
    assert rec.name == "house-writing-style"
    assert rec.disclose() == {"name": rec.name, "description": rec.description}


def test_install_indexes_and_progressive_disclosure(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src", script=True)
    home = tmp_settings.home_path()
    row = add_skill(src, home=home)
    assert row["name"] == "house-writing-style"
    assert row["digest"].startswith("sha256:")
    assert "scripts/apply.py" in row["scripts"]
    disclosed = disclose_all(home)
    assert disclosed == [{"name": row["name"], "description": row["description"]}]
    assert "Do not call" not in json.dumps(disclosed)
    listed = get_record(home, "house-writing-style")
    assert listed["signature_status"] == "unsigned"
    assert "instructions" not in listed


def test_skill_node_injects_untrusted_body_and_runs_sandbox_script(
    tmp_path: Path, tmp_settings
) -> None:
    src = _write_skill(tmp_path / "src", script=True)
    home = tmp_settings.home_path()
    add_skill(src, home=home)
    spec = {
        "name": "use-skill",
        "nodes": [
            {
                "id": "apply",
                "type": "skill",
                "skill": "house-writing-style",
                "inputs": {"draft": "{{ draft }}"},
                "output_key": "styled",
            }
        ],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(wf, ToolRegistry(), pin_home=home, default_model="mock:test")
    done = run_workflow(wf, {"draft": "hello"}, ctx)
    assert done.status == "succeeded"
    out = done.output_keys["styled"]
    assert "Do not call" in out["instructions"]
    assert out["disclosed"]["name"] == "house-writing-style"
    assert "Do not call" not in json.dumps(out["disclosed"])
    assert out["script_output"]["styled"] == "hello [styled]"
    assert provenance_of(done, "styled").trust == "untrusted"
    assert provenance_of(done, "apply").trust == "untrusted"


def test_allowed_tools_filtered_by_policy(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src")
    (src / "SKILL.md").write_text(_skill_md(**{"allowed-tools": "calc now"}), encoding="utf-8")
    home = tmp_settings.home_path()
    add_skill(src, home=home)
    policy_path = tmp_path / "p.yaml"
    policy_path.write_text("version: 1\ndefault: deny\ntools:\n  now: {}\n", encoding="utf-8")
    spec = {
        "name": "tools",
        "nodes": [
            {"id": "apply", "type": "skill", "skill": "house-writing-style", "output_key": "s"}
        ],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        pin_home=home,
        policy=load_policy(policy_path),
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx)
    tools = done.output_keys["s"]["tools"]
    assert "now" in tools
    assert "calc" not in tools


def test_install_refuses_zip_slip_symlink_and_oversize(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    zpath = tmp_path / "slip.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../escape/SKILL.md", _skill_md())
    with pytest.raises(SkillPathDenied):
        add_skill(zpath, home=home)
    src = _write_skill(tmp_path / "ok")
    link = tmp_path / "link-skill"
    try:
        link.symlink_to(src)
        linked = True
    except OSError:
        linked = False
    if linked:
        with pytest.raises(SkillPathDenied):
            add_skill(link, home=home)
    big = tmp_path / "big.zip"
    payload = b"x" * (1_048_576 + 10)
    with zipfile.ZipFile(big, "w") as zf:
        zf.writestr("house-writing-style/SKILL.md", _skill_md())
        zf.writestr("house-writing-style/blob.bin", payload)
    # size cap is on archive file size
    if big.stat().st_size <= 1_048_576:
        big.write_bytes(big.read_bytes() + payload)
    with pytest.raises(SkillRefused, match="size"):
        add_skill(big, home=home)


def test_unsigned_required_and_drift_detected(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src")
    home = tmp_settings.home_path()
    with pytest.raises(SkillRefused, match="unsigned"):
        add_skill(src, home=home, require_signature=True)
    add_skill(src, home=home)
    installed = Path(get_record(home, "house-writing-style")["path"])
    (installed / "SKILL.md").write_text(
        _skill_md(description="changed description after install for drift."),
        encoding="utf-8",
    )
    spec = {
        "name": "drift",
        "nodes": [{"id": "apply", "type": "skill", "skill": "house-writing-style"}],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(wf, ToolRegistry(), pin_home=home, default_model="mock:test")
    with pytest.raises(SkillDrift):
        run_workflow(wf, {}, ctx)


def test_export_valid_skill_and_runs_workflow(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    folder = export_workflow(_root() / "examples" / "calc_pipeline.yaml", dest)
    assert folder.name == "calc-pipeline"
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    rec = parse_skill_md(text, directory_name=folder.name)
    assert rec.name == folder.name
    run_sh = (folder / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert "readyagents run" in run_sh
    assert "$DIR/workflow.yaml" in run_sh
    blob = ""
    for path in folder.rglob("*"):
        if path.is_file():
            blob += path.read_text(encoding="utf-8", errors="replace")
    assert "sk-" not in blob
    assert "/Users/" not in blob
    from readyagents.workflow.runner import run_workflow_file

    done = run_workflow_file(folder / "workflow.yaml", persist=False)
    assert done.status == "succeeded"


def test_agents_md_names_run_validate_test() -> None:
    text = generate_agents_md()
    assert "readyagents run" in text
    assert "readyagents validate" in text
    assert "pytest" in text


def test_cli_skills_help_twice_and_agents_md() -> None:
    first = runner.invoke(app, ["skills", "--help"])
    second = runner.invoke(app, ["skills", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    a = runner.invoke(app, ["agents-md", "--json"])
    b = runner.invoke(app, ["agents-md", "--json"])
    assert a.exit_code == 0, a.stdout + a.stderr
    assert b.exit_code == 0
    payload = json.loads(a.stdout[a.stdout.find("{") :])
    assert "readyagents run" in payload["text"]


def test_skill_node_required_at_schema() -> None:
    with pytest.raises(ValidationError, match="skill"):
        WorkflowSpec.model_validate({"name": "bad", "nodes": [{"id": "s", "type": "skill"}]})
