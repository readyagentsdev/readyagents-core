from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from readyagents import __version__
from readyagents.cli import app
from readyagents.config import clear_settings_cache

runner = CliRunner()


def _plain(text: str) -> str:
    """Strip ANSI and whitespace so Rich wrapping cannot hide tokens."""
    return re.sub(r"\s+", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _run_id(text: str) -> str:
    match = re.search(r"run_id:\s*([0-9a-f]{16,})", text)
    if match:
        return match.group(1)
    match = re.search(r"Run ([0-9a-f]{16,}) —", text)
    if match:
        return match.group(1)
    raise AssertionError(f"no run id in:\n{text}")


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "validate" in result.stdout
    assert "eval" in result.stdout


def test_readme_cli_table_lists_help_commands() -> None:
    """Front-door README CLI section must name the verbs shipped `--help` prints."""
    root = runner.invoke(app, ["--help"])
    runs = runner.invoke(app, ["runs", "--help"])
    approvals = runner.invoke(app, ["approvals", "--help"])
    assert root.exit_code == 0, root.stdout + root.stderr
    assert runs.exit_code == 0, runs.stdout + runs.stderr
    assert approvals.exit_code == 0, approvals.stdout + approvals.stderr
    help_text = root.stdout + runs.stdout + approvals.stdout
    readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    table = readme.split("## CLI", 1)[1].split("## Examples", 1)[0]
    for token in (
        "connectors",
        "sign",
        "verify",
        "lock",
        "sbom",
        "trust",
        "fork",
        "freeze",
        "serve",
        "batch",
    ):
        assert token in help_text, token
        assert token in table, token


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_packs_none_installed() -> None:
    result = runner.invoke(app, ["packs"])
    assert result.exit_code == 0
    assert "No packs installed" in result.stdout


def test_run_connector_demo_with_pack() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "examples/connector_demo.yaml",
            "--pack",
            "examples/packs/connector_pack.py",
            "--no-persist",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "connector_demo ok" in result.stdout
    assert "hello" in result.stdout


def test_run_connector_demo_without_pack() -> None:
    result = runner.invoke(app, ["run", "examples/connector_demo.yaml", "--no-persist"])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "connector_ping" in text


def test_packs_lists_local_pack() -> None:
    result = runner.invoke(app, ["packs", "--pack", "examples/packs/connector_pack.py"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "example-connector" in result.stdout


def test_pack_path_escape_refused(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    result = runner.invoke(
        app,
        ["packs", "--pack", "/etc/passwd"],
    )
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "ConfigError" in text or "PathError" in text
    assert "outside" in text.lower()
    parent = tmp_path.parent / "escaped_pack.py"
    parent.write_text("def get_pack():\n    return None\n", encoding="utf-8")
    rel = runner.invoke(app, ["packs", "--pack", "../escaped_pack.py"])
    assert rel.exit_code == 1
    assert "outside" in (rel.stdout + rel.stderr).lower()
    clear_settings_cache()


def test_run_list_dir_example() -> None:
    result = runner.invoke(app, ["run", "examples/list_dir.yaml", "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "list_dir ok:" in result.stdout
    dry = runner.invoke(app, ["run", "examples/list_dir.yaml", "--dry-run", "--no-persist"])
    assert dry.exit_code == 0, dry.stdout + dry.stderr
    assert "list_dir ok:" in dry.stdout
    assert "succeeded" in dry.stdout


def test_run_calc_pipeline() -> None:
    result = runner.invoke(app, ["run", "examples/calc_pipeline.yaml", "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "calc_pipeline ok" in result.stdout
    assert re.search(r"run_id:\s*[0-9a-f]{16,}", result.stdout)


def test_run_agent_workflow_without_keys_fails_byok(tmp_path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "READYAGENTS_OPENAI_API_KEY",
        "READYAGENTS_ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_COMPAT_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    example = Path(__file__).resolve().parents[1] / "examples" / "research_brief.yaml"
    result = runner.invoke(
        app,
        ["run", str(example), "--no-persist", "--input", "topic=x"],
    )
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "BYOK" in text
    assert "OPENAI_API_KEY" in text
    clear_settings_cache()


def test_help_lists_new_and_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "new" in result.stdout
    assert "runs" in result.stdout
    assert "resume" in result.stdout
    assert "decide" in result.stdout


def test_new_writes_starter_tree(tmp_path: Path) -> None:
    dest = tmp_path / "my-flow"
    result = runner.invoke(app, ["new", "my-flow", "--dest", str(dest)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (dest / "workflow.yaml").is_file()
    assert (dest / "README.md").is_file()
    assert (dest / ".env.example").is_file()
    workflow = (dest / "workflow.yaml").read_text(encoding="utf-8")
    readme = (dest / "README.md").read_text(encoding="utf-8")
    env = (dest / ".env.example").read_text(encoding="utf-8")
    assert "type: approval" not in workflow
    assert "readyagents new" in readme or "readyagents run" in readme
    assert "OPENAI_API_KEY=" in env
    assert "sk-" not in env
    from readyagents.workflow.runner import load_workflow

    spec = load_workflow(dest / "workflow.yaml")
    assert spec.start
    assert not any(n.type == "approval" for n in spec.nodes)
    ran = runner.invoke(app, ["run", str(dest / "workflow.yaml"), "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "succeeded" in ran.stdout


def test_new_template_approval(tmp_path: Path) -> None:
    dest = tmp_path / "gated"
    result = runner.invoke(app, ["new", "gated", "--dest", str(dest), "--template", "approval"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "type: approval" in (dest / "workflow.yaml").read_text(encoding="utf-8")
    gated = runner.invoke(
        app, ["run", str(dest / "workflow.yaml"), "--approve", "gate", "--no-persist"]
    )
    assert gated.exit_code == 0, gated.stdout + gated.stderr
    assert "succeeded" in gated.stdout


def test_approval_pauses_then_resume_cli(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = Path(__file__).resolve().parents[1] / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    text = paused.stdout + paused.stderr
    assert "ApprovalRequired" in text or "Approval required" in text
    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert "approval_gate" in listed.stdout
    run_id = _run_id(listed.stdout + paused.stdout)
    shown = runner.invoke(app, ["runs", "show", run_id])
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    assert run_id in shown.stdout
    assert "add" in shown.stdout
    resumed = runner.invoke(app, ["resume", run_id, "--approve", "gate"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    assert "succeeded" in resumed.stdout
    assert "approval_gate ok" in resumed.stdout
    clear_settings_cache()


def test_runs_list_show_after_calc(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = Path(__file__).resolve().parents[1] / "examples" / "calc_pipeline.yaml"
    ran = runner.invoke(app, ["run", str(example)])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0, listed.stdout
    assert "calc_pipeline" in listed.stdout
    run_id = _run_id(listed.stdout + ran.stdout)
    shown = runner.invoke(app, ["runs", "show", run_id])
    assert shown.exit_code == 0, shown.stdout
    assert "add" in shown.stdout
    assert "stamp" in shown.stdout
    inspect = runner.invoke(app, ["runs", "inspect", run_id])
    assert inspect.exit_code == 0
    assert run_id in inspect.stdout
    listed_json = runner.invoke(app, ["runs", "list", "--json"])
    assert listed_json.exit_code == 0, listed_json.stdout
    assert run_id in listed_json.stdout
    assert '"workflow"' in listed_json.stdout
    shown_json = runner.invoke(app, ["runs", "show", run_id, "--json"])
    assert shown_json.exit_code == 0, shown_json.stdout
    assert run_id in shown_json.stdout
    assert "node_results" in shown_json.stdout
    replayed = runner.invoke(app, ["runs", "replay", run_id])
    assert replayed.exit_code == 0, replayed.stdout + replayed.stderr
    assert "succeeded" in replayed.stdout
    assert "calc_pipeline ok" in replayed.stdout
    clear_settings_cache()


def test_new_template_basic(tmp_path: Path) -> None:
    dest = tmp_path / "basic-flow"
    result = runner.invoke(app, ["new", "basic-flow", "--dest", str(dest), "--template", "basic"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = (dest / "workflow.yaml").read_text(encoding="utf-8")
    assert "type: approval" not in text
    ran = runner.invoke(app, ["run", str(dest / "workflow.yaml"), "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "succeeded" in ran.stdout


def test_new_template_research(tmp_path: Path) -> None:
    dest = tmp_path / "research-flow"
    result = runner.invoke(
        app, ["new", "research-flow", "--dest", str(dest), "--template", "research"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    wf = dest / "workflow.yaml"
    assert "type: parallel" in wf.read_text(encoding="utf-8")
    ran = runner.invoke(app, ["run", str(wf), "--approve", "publish", "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "succeeded" in ran.stdout
    assert "published" in ran.stdout


def test_new_template_pipeline(tmp_path: Path) -> None:
    dest = tmp_path / "pipe"
    result = runner.invoke(app, ["new", "pipe", "--dest", str(dest), "--template", "pipeline"])
    assert result.exit_code == 0, result.stdout + result.stderr
    ran = runner.invoke(app, ["run", str(dest / "workflow.yaml"), "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "pipeline ok" in ran.stdout


def test_new_template_review(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "rev"
    (tmp_path / "README.md").write_text("sample file", encoding="utf-8")
    result = runner.invoke(app, ["new", "rev", "--dest", str(dest), "--template", "review"])
    assert result.exit_code == 0, result.stdout + result.stderr
    ran = runner.invoke(
        app,
        [
            "run",
            str(dest / "workflow.yaml"),
            "--input",
            "path=README.md",
            "--approve",
            "gate",
            "--no-persist",
        ],
    )
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "accepted" in ran.stdout


def test_new_template_foreach(tmp_path: Path) -> None:
    dest = tmp_path / "each"
    result = runner.invoke(app, ["new", "each", "--dest", str(dest), "--template", "foreach"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (dest / "workflow.yaml").is_file()
    assert (dest / "README.md").is_file()
    assert (dest / ".env.example").is_file()
    text = (dest / "workflow.yaml").read_text(encoding="utf-8")
    assert "type: foreach" in text
    ran = runner.invoke(app, ["run", str(dest / "workflow.yaml"), "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "succeeded" in ran.stdout


def test_new_template_agent_tools(tmp_path: Path) -> None:
    dest = tmp_path / "tools"
    result = runner.invoke(app, ["new", "tools", "--dest", str(dest), "--template", "agent-tools"])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = (dest / "workflow.yaml").read_text(encoding="utf-8")
    assert "tools:" in text
    assert "calc" in text
    ran = runner.invoke(app, ["run", str(dest / "workflow.yaml"), "--dry-run", "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "succeeded" in ran.stdout


def test_new_template_gated_pauses_without_write(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / "gated"
    result = runner.invoke(app, ["new", "gated", "--dest", str(dest), "--template", "gated"])
    assert result.exit_code == 0, result.stdout + result.stderr
    wf = dest / "workflow.yaml"
    assert "type: approval" in wf.read_text(encoding="utf-8")
    paused = runner.invoke(app, ["run", str(wf)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    assert not (dest / "gated.txt").exists()
    assert not (tmp_path / "gated.txt").exists()
    approved = runner.invoke(app, ["run", str(wf), "--approve", "gate", "--no-persist"])
    assert approved.exit_code == 0, approved.stdout + approved.stderr
    assert "succeeded" in approved.stdout
    written = dest / "gated.txt"
    cwd_written = tmp_path / "gated.txt"
    assert written.is_file() or cwd_written.is_file()
    clear_settings_cache()


def test_new_default_template_is_pipeline(tmp_path: Path) -> None:
    dest = tmp_path / "def"
    result = runner.invoke(app, ["new", "def", "--dest", str(dest)])
    assert result.exit_code == 0, result.stdout + result.stderr
    text = (dest / "workflow.yaml").read_text(encoding="utf-8")
    assert "json_get" in text or "pipeline" in text.lower() or "extracted" in text


def test_new_refuses_overwrite(tmp_path: Path) -> None:
    dest = tmp_path / "existing"
    dest.mkdir()
    marker = "KEEP-THIS-WORKFLOW"
    (dest / "workflow.yaml").write_text(marker, encoding="utf-8")
    result = runner.invoke(app, ["new", "existing", "--dest", str(dest)])
    assert result.exit_code == 1, result.stdout + result.stderr
    text = _plain(result.stdout + result.stderr)
    assert "Refusingtooverwrite" in text
    assert "workflow.yaml" in text
    assert (dest / "workflow.yaml").read_text(encoding="utf-8") == marker
    assert not (dest / "README.md").exists()
    assert not (dest / ".env.example").exists()

    env_dest = tmp_path / "has-env"
    env_dest.mkdir()
    (env_dest / ".env.example").write_text("KEEP-ENV", encoding="utf-8")
    env_result = runner.invoke(app, ["new", "has-env", "--dest", str(env_dest)])
    assert env_result.exit_code == 1
    assert (env_dest / ".env.example").read_text(encoding="utf-8") == "KEEP-ENV"
    assert not (env_dest / "workflow.yaml").exists()


def test_new_unknown_template(tmp_path: Path) -> None:
    result = runner.invoke(app, ["new", "x", "--dest", str(tmp_path / "x"), "--template", "nope"])
    assert result.exit_code == 1
    assert "Unknown template" in result.stdout + result.stderr


def test_include_demo_cli() -> None:
    result = runner.invoke(
        app, ["run", "examples/include_demo.yaml", "--input", "n=7", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "include_demo ok: 17" in result.stdout


def test_composed_gate_oneshot() -> None:
    result = runner.invoke(
        app, ["run", "examples/composed_gate.yaml", "--approve", "gate", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "composed_gate ok" in result.stdout


def test_runs_report_html(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = Path(__file__).resolve().parents[1] / "examples" / "calc_pipeline.yaml"
    ran = runner.invoke(app, ["run", str(example)])
    assert ran.exit_code == 0, ran.stdout
    listed = runner.invoke(app, ["runs", "list", "--json"])
    import json as json_mod

    payload = json_mod.loads(listed.stdout[listed.stdout.find("[") :])
    run_id = payload[0]["run_id"]
    out = tmp_path / "report.html"
    report = runner.invoke(app, ["runs", "report", run_id, "--out", str(out)])
    assert report.exit_code == 0, report.stdout + report.stderr
    assert out.is_file()
    html = out.read_text(encoding="utf-8")
    assert run_id in html
    assert "add" in html
    assert "calc_pipeline" in html
    clear_settings_cache()


def test_fanout_gate_oneshot() -> None:
    result = runner.invoke(
        app, ["run", "examples/fanout_gate.yaml", "--approve", "gate", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "fanout_gate ok" in result.stdout


def test_list_status_filter(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = Path(__file__).resolve().parents[1] / "examples" / "calc_pipeline.yaml"
    ran = runner.invoke(app, ["run", str(example)])
    assert ran.exit_code == 0, ran.stdout
    listed = runner.invoke(app, ["runs", "list", "--status", "succeeded", "--json"])
    assert listed.exit_code == 0
    assert "calc_pipeline" in listed.stdout
    empty = runner.invoke(app, ["runs", "list", "--status", "paused", "--json"])
    assert empty.exit_code == 0
    assert empty.stdout.strip() == "[]" or "[]" in empty.stdout
    clear_settings_cache()


def test_dry_run_support_triage() -> None:
    result = runner.invoke(
        app,
        ["run", "examples/support_triage.yaml", "--dry-run", "--no-persist"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "[dry-run]" in result.stdout


def test_eval_pass_suite() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/pass.yaml"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "passed=" in result.stdout
    assert "calc_pipeline" in result.stdout


def test_eval_fail_suite() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/fail.yaml"])
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout + result.stderr


def test_eval_pass_suite_json() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/pass.yaml", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["command"] == "eval"
    assert data["passed"] >= 1
    assert data["failed"] == 0
    assert isinstance(data["results"], list)
    assert data["results"]
    assert "name" in data["results"][0]
    assert "passed" in data["results"][0]


def test_eval_fail_suite_json() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/fail.yaml", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert data["command"] == "eval"
    assert data["failed"] >= 1
    assert data["results"]


def test_eval_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1, result.stdout + result.stderr
    text = result.stdout + result.stderr
    assert "ConfigError" in text
    assert "nope.yaml" in text


def test_dry_run_research_brief() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "examples/research_brief.yaml",
            "--dry-run",
            "--no-persist",
            "--input",
            "topic=test",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "[dry-run]" in result.stdout
