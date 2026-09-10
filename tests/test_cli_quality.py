from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_validate_ok_fixture() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "ok.yaml"
    result = runner.invoke(app, ["validate", str(fixture)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    assert "fixture_ok" in result.stdout
    assert "1 node(s)" in result.stdout
    assert "start=t" in result.stdout


def test_validate_calc_pipeline() -> None:
    result = runner.invoke(app, ["validate", "examples/calc_pipeline.yaml"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    assert "calc_pipeline" in result.stdout
    assert "add" in result.stdout
    assert "stamp" in result.stdout


def test_validate_invalid_workflow(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\nnodes:\n  - id: a\n    type: transform\n    template: x\n    next: missing\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "WorkflowError" in text
    assert "Invalid workflow" in text


def test_validate_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "ConfigError" in text
    assert "Workflow file not found" in text
    assert "nope.yaml" in text


def test_validate_shows_else_routing() -> None:
    result = runner.invoke(app, ["validate", "examples/approval_gate.yaml"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "then:receipt" in result.stdout
    assert "else:denied" in result.stdout


def test_validate_json_ok() -> None:
    result = runner.invoke(app, ["validate", "examples/calc_pipeline.yaml", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "\x1b" not in result.stdout
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is True
    assert data["command"] == "validate"
    assert data["name"] == "calc_pipeline"
    assert data["start"]
    assert data["node_count"] >= 1
    ids = [n["id"] for n in data["nodes"]]
    assert "add" in ids
    assert "stamp" in ids


def test_validate_json_cycle(tmp_path: Path) -> None:
    path = tmp_path / "cycle.yaml"
    path.write_text(
        """
name: loop
nodes:
  - id: a
    type: transform
    template: "1"
    next: b
  - id: b
    type: transform
    template: "2"
    next: a
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path), "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["command"] == "validate"
    assert data["error"] == "WorkflowError"
    assert "Cycle" in data["message"]


def test_validate_json_invalid(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: bad\nnodes:\n  - id: a\n    type: transform\n    template: x\n    next: missing\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(path), "--json"])
    assert result.exit_code == 1
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["error"] == "WorkflowError"
    assert "Invalid workflow" in data["message"]


def test_validate_help_lists_json() -> None:
    result = runner.invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    assert "--json" in _plain(result.stdout)


def test_run_missing_required_input(tmp_path: Path) -> None:
    wf = tmp_path / "need.yaml"
    wf.write_text(
        """
name: need
required_inputs: [topic]
nodes:
  - id: t
    type: transform
    template: "{{topic}}"
    output_key: v
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert result.exit_code == 1
    text = result.stdout + result.stderr
    assert "WorkflowError" in text
    assert "Missing required inputs: topic" in text
    assert "--input topic=..." in text
    js = runner.invoke(app, ["run", str(wf), "--json", "--no-persist"])
    assert js.exit_code == 1
    data = _json_from_cli(js.stdout)
    assert isinstance(data, dict)
    assert data["error"] == "WorkflowError"
    assert "--input topic=..." in str(data["message"])
    ok = runner.invoke(app, ["run", str(wf), "--input", "topic=hello", "--no-persist"])
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    assert "hello" in ok.stdout


def test_init_writes_template_when_no_example(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / ".env"
    result = runner.invoke(app, ["init", "--dest", str(dest)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert dest.is_file()
    text = dest.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in text
    assert "ANTHROPIC_API_KEY=" in text
    assert "READYAGENTS_DEFAULT_MODEL=" in text
    assert "sk-" not in text
    assert "Wrote" in result.stdout
    assert "readyagents new" in result.stdout
    assert "readyagents doctor" in result.stdout


def test_init_copies_env_example(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    example = tmp_path / ".env.example"
    example.write_text("# copied-from-example\nOPENAI_API_KEY=\n", encoding="utf-8")
    dest = tmp_path / ".env"
    result = runner.invoke(app, ["init", "--dest", str(dest)])
    assert result.exit_code == 0, result.stdout + result.stderr
    copied = dest.read_text(encoding="utf-8")
    assert copied == example.read_text(encoding="utf-8")
    assert "copied-from-example" in copied
    assert "sk-" not in copied
    assert "Wrote" in result.stdout


def test_init_does_not_overwrite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dest = tmp_path / ".env"
    dest.write_text("KEEP=1\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--dest", str(dest)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert dest.read_text(encoding="utf-8") == "KEEP=1\n"
    assert "already exists" in result.stdout
    assert "left unchanged" in result.stdout


def test_init_default_dest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout + result.stderr
    dest = tmp_path / ".env"
    assert dest.is_file()
    assert "OPENAI_API_KEY=" in dest.read_text(encoding="utf-8")
    assert "sk-" not in dest.read_text(encoding="utf-8")


def test_reject_oneshot_cli() -> None:
    result = runner.invoke(
        app, ["run", "examples/approval_gate.yaml", "--reject", "gate", "--no-persist"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "approval_gate denied" in result.stdout
    assert "approval_gate ok" not in result.stdout


def test_reject_resume_cli(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = _repo_root() / "examples" / "approval_gate.yaml"
    paused = runner.invoke(app, ["run", str(example)])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    text = paused.stdout + paused.stderr
    assert "ApprovalRequired" in text or "Approval required" in text
    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    run_id = _run_id(listed.stdout + paused.stdout)
    rejected = runner.invoke(app, ["resume", run_id, "--reject", "gate"])
    assert rejected.exit_code == 0, rejected.stdout + rejected.stderr
    assert "succeeded" in rejected.stdout
    assert "approval_gate denied" in rejected.stdout
    clear_settings_cache()


def test_mcp_help_lists_serve() -> None:
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout


def _json_from_cli(text: str) -> object:
    import json as json_mod

    for i, ch in enumerate(text):
        if ch in "{[":
            return json_mod.loads(text[i:])
    raise AssertionError(f"no JSON in:\n{text}")


def test_failed_run_prints_timeline_and_resume(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    wf = tmp_path / "boom.yaml"
    wf.write_text(
        """
name: boom
inputs:
  expr: "1 / 0"
nodes:
  - id: prep
    type: transform
    template: "start"
    output_key: tag
    next: go
  - id: go
    type: tool
    tool: calc
    arguments:
      expression: "{{expr}}"
    output_key: n
""",
        encoding="utf-8",
    )
    failed = runner.invoke(app, ["run", str(wf)])
    assert failed.exit_code == 1, failed.stdout + failed.stderr
    text = failed.stdout + failed.stderr
    assert "NodeError" in text
    assert "division by zero" in text
    assert "prep" in failed.stdout
    assert "go" in failed.stdout
    assert "run_id:" in text
    assert "readyagents resume" in text
    run_id = _run_id(text)
    resumed = runner.invoke(app, ["resume", run_id, "--input", "expr=2 + 2"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    assert "succeeded" in resumed.stdout
    assert "4" in resumed.stdout
    clear_settings_cache()


def test_run_json_success() -> None:
    result = runner.invoke(app, ["run", "examples/calc_pipeline.yaml", "--json", "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["status"] == "succeeded"
    assert data["workflow"] == "calc_pipeline"
    assert "run_id" in data
    assert "sum" in data.get("output_keys") or "sum" in data.get("outputs", {})


def test_run_json_failed(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    wf = tmp_path / "boom.yaml"
    wf.write_text(
        """
name: boom
nodes:
  - id: go
    type: tool
    tool: calc
    arguments:
      expression: "1 / 0"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(wf), "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["error"] == "NodeError"
    assert data["status"] == "failed"
    assert data["run_id"]
    assert isinstance(data.get("run"), dict)
    assert data["run"]["status"] == "failed"
    assert data["run"]["pending_node"] == "go"
    clear_settings_cache()


def test_runs_list_prints_run_id_once(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    example = Path(__file__).resolve().parents[1] / "examples" / "calc_pipeline.yaml"
    ran = runner.invoke(app, ["run", str(example)])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    listed_json = runner.invoke(app, ["runs", "list", "--json"])
    assert listed_json.exit_code == 0, listed_json.stdout
    payload = _json_from_cli(listed_json.stdout)
    assert isinstance(payload, list)
    assert payload
    run_id = payload[0]["run_id"]
    assert run_id in {item["run_id"] for item in payload}

    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0, listed.stdout
    assert listed.stdout.count(run_id) == 1
    assert listed.stdout.count("run_id:") == 1
    assert "calc_pipeline" in listed.stdout
    clear_settings_cache()


def test_include_child_approval_pause_resume_cli(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    (tmp_path / "child.yaml").write_text(
        """
name: nested_gate
start: gate
nodes:
  - id: gate
    type: approval
    prompt: "Allow nested?"
    then: ok
    else: hold
  - id: ok
    type: transform
    template: nested-ok
    output_key: verdict
  - id: hold
    type: transform
    template: nested-no
    output_key: verdict
""",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.yaml"
    parent.write_text(
        """
name: parent_include_gate
start: nested
nodes:
  - id: nested
    type: include
    path: child.yaml
    output_key: child
    next: wrap
  - id: wrap
    type: transform
    template: "parent ok: {{child.verdict}}"
    output_key: summary
""",
        encoding="utf-8",
    )
    paused = runner.invoke(app, ["run", str(parent), "--json"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    data = _json_from_cli(paused.stdout)
    assert isinstance(data, dict)
    assert data["error"] == "ApprovalRequired"
    assert data["status"] == "paused"
    assert data["node_id"] == "gate"
    run_id = data["run_id"]
    assert run_id
    assert data.get("run", {}).get("pending_node") == "nested"
    assert data.get("run", {}).get("run_id") == run_id

    resumed = runner.invoke(app, ["resume", run_id, "--approve", "gate", "--json"])
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    done = _json_from_cli(resumed.stdout)
    assert isinstance(done, dict)
    assert done["status"] == "succeeded"
    assert done["run_id"] == run_id
    assert done.get("output_keys", {}).get("summary") == "parent ok: nested-ok"
    clear_settings_cache()


def test_run_json_paused_approval() -> None:
    result = runner.invoke(app, ["run", "examples/approval_gate.yaml", "--json", "--no-persist"])
    assert result.exit_code == 2, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["error"] == "ApprovalRequired"
    assert data["ok"] is False
    assert data["command"] == "run"
    assert data["status"] == "paused"
    assert data["node_id"] == "gate"
    assert data["run_id"]
    assert data.get("run", {}).get("status") == "paused"
    assert "\x1b" not in result.stdout


def test_run_json_preserves_dry_run_markup() -> None:
    result = runner.invoke(
        app,
        ["run", "examples/support_triage.yaml", "--dry-run", "--json", "--no-persist"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    blob = result.stdout
    assert "[dry-run]" in blob
    # Rich markup would eat `[dry-run]` as a tag; raw JSON must keep the brackets.
    assert "dry-run" in blob


def test_dry_run_does_not_write_file(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    target = tmp_path / "stamp.txt"
    wf = tmp_path / "stamp.yaml"
    wf.write_text(
        """
name: stamp
start: write
nodes:
  - id: write
    type: tool
    tool: write_file
    arguments:
      path: stamp.txt
      content: should-not-exist
    output_key: written
""",
        encoding="utf-8",
    )
    dry = runner.invoke(app, ["run", str(wf), "--dry-run", "--json", "--no-persist"])
    assert dry.exit_code == 0, dry.stdout + dry.stderr
    assert not target.exists()
    data = _json_from_cli(dry.stdout)
    assert isinstance(data, dict)
    assert data["status"] == "succeeded"
    written = data["output_keys"]["written"]
    assert isinstance(written, str)
    assert written.startswith("[dry-run]")
    assert "write_file" in written

    real = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert real.exit_code == 0, real.stdout + real.stderr
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "should-not-exist"
    clear_settings_cache()


def test_workspace_yaml_cannot_escape(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    leaked = tmp_path.parent / f"ra-leaked-{tmp_path.name}.txt"
    wf = tmp_path / "escape.yaml"
    wf.write_text(
        f"""
name: escape
workspace: {tmp_path.parent}
start: write
nodes:
  - id: write
    type: tool
    tool: write_file
    arguments:
      path: ra-leaked-{tmp_path.name}.txt
      content: leaked
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert result.exit_code == 1, result.stdout + result.stderr
    text = result.stdout + result.stderr
    assert "ConfigError" in text
    assert "outside the workspace" in text
    assert not leaked.exists()
    clear_settings_cache()


def test_workspace_subdir_still_writes(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    (tmp_path / "out").mkdir()
    wf = tmp_path / "sub.yaml"
    wf.write_text(
        """
name: sub
workspace: out
start: write
nodes:
  - id: write
    type: tool
    tool: write_file
    arguments:
      path: stamp.txt
      content: ok
    output_key: written
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert (tmp_path / "out" / "stamp.txt").read_text(encoding="utf-8") == "ok"
    clear_settings_cache()


def test_file_tools_sandbox_to_workflow_dir(tmp_path: Path, monkeypatch) -> None:
    """read_file resolves next to the workflow file, even if cwd is elsewhere."""
    clear_settings_cache()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "note.txt").write_text("from-workflow-dir", encoding="utf-8")
    wf = proj / "flow.yaml"
    wf.write_text(
        """
name: wfdir
start: r
nodes:
  - id: r
    type: tool
    tool: read_file
    arguments:
      path: note.txt
    output_key: text
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.delenv("READYAGENTS_WORKSPACE", raising=False)
    result = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "from-workflow-dir" in result.stdout
    clear_settings_cache()


def test_file_tools_ignore_process_cwd(tmp_path: Path, monkeypatch) -> None:
    """A file that exists only in cwd is not readable."""
    clear_settings_cache()
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "cwd_only.txt").write_text("from-cwd", encoding="utf-8")
    wf = proj / "flow.yaml"
    wf.write_text(
        """
name: cwdtrap
start: r
nodes:
  - id: r
    type: tool
    tool: read_file
    arguments:
      path: cwd_only.txt
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.delenv("READYAGENTS_WORKSPACE", raising=False)
    result = runner.invoke(app, ["run", str(wf), "--no-persist"])
    assert result.exit_code == 1, result.stdout + result.stderr
    text = result.stdout + result.stderr
    assert "cwd_only.txt" in text
    clear_settings_cache()


def test_code_review_from_other_cwd(tmp_path: Path, monkeypatch) -> None:
    """Stranger case: code_review from outside the repo root."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.delenv("READYAGENTS_WORKSPACE", raising=False)
    root = Path(__file__).resolve().parents[1]
    result = runner.invoke(
        app,
        ["run", str(root / "examples" / "code_review.yaml"), "--dry-run", "--no-persist"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    clear_settings_cache()


def test_run_missing_file_exits_1_not_pause_2(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(app, ["run", str(missing), "--no-persist"])
    assert result.exit_code == 1, result.stdout + result.stderr
    text = result.stdout + result.stderr
    assert "ConfigError" in text
    assert "Workflow file not found" in text
    assert "nope.yaml" in text

    paused = runner.invoke(app, ["run", "examples/approval_gate.yaml", "--no-persist"])
    assert paused.exit_code == 2, paused.stdout + paused.stderr
    assert "ApprovalRequired" in paused.stdout + paused.stderr


def test_run_help_lists_json() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--json" in _plain(result.stdout)


def test_run_accepts_log_level() -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "examples/calc_pipeline.yaml",
            "--log-level",
            "DEBUG",
            "--no-persist",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "succeeded" in result.stdout
    assert "calc_pipeline ok" in result.stdout
    help_r = runner.invoke(app, ["run", "--help"])
    assert help_r.exit_code == 0
    assert "--log-level" in _plain(help_r.stdout)


def test_mcp_serve_invokes_stdio(monkeypatch) -> None:
    # Live `mcp serve` blocks on stdio (server.run(transport="stdio")). Patch the
    # transport so the CLI wiring is covered without hanging the suite.
    called = {"n": 0}

    def fake_serve() -> None:
        called["n"] += 1

    monkeypatch.setattr("readyagents.mcp.server.serve_stdio", fake_serve)
    result = runner.invoke(app, ["mcp", "serve"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert called["n"] == 1


def test_packs_json_empty() -> None:
    result = runner.invoke(app, ["packs", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "\x1b" not in result.stdout
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is True
    assert data["command"] == "packs"
    assert data["packs"] == []


def test_packs_help_lists_json() -> None:
    result = runner.invoke(app, ["packs", "--help"])
    assert result.exit_code == 0
    assert "--json" in _plain(result.stdout)


def test_runs_show_json_missing_id(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    result = runner.invoke(app, ["runs", "show", "does-not-exist", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["command"] == "runs show"
    assert data["error"] == "ConfigError"
    assert data["run_id"] == "does-not-exist"
    assert "not found" in data["message"].lower()


def test_eval_json_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["eval", str(tmp_path / "nope.yaml"), "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["command"] == "eval"
    assert data["error"] == "ConfigError"
    assert "message" in data
    assert "nope.yaml" in data["message"]


def test_eval_json_pass_envelope() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/pass.yaml", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is True
    assert data["command"] == "eval"
    assert data["passed"] >= 1
    assert data["failed"] == 0
    assert isinstance(data["results"], list)
    row = data["results"][0]
    assert "name" in row
    assert "passed" in row
    assert "reason" in row


def test_eval_json_fail_envelope() -> None:
    result = runner.invoke(app, ["eval", "examples/eval/fail.yaml", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["command"] == "eval"
    assert data["failed"] >= 1
    assert data["results"]


def test_json_envelope_run_resume_eval() -> None:
    ran = runner.invoke(app, ["run", "examples/calc_pipeline.yaml", "--json", "--no-persist"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    assert "\x1b" not in ran.stdout
    data = _json_from_cli(ran.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is True
    assert data["command"] == "run"
    assert data["status"] == "succeeded"
    assert data["run_id"]
    paused = runner.invoke(app, ["run", "examples/approval_gate.yaml", "--json"])
    assert paused.exit_code == 2
    pdata = _json_from_cli(paused.stdout)
    assert pdata["ok"] is False
    assert pdata["command"] == "run"
    assert pdata["run_id"]
    resumed = runner.invoke(
        app, ["resume", pdata["run_id"], "--approve", "gate", "--json", "--no-persist"]
    )
    assert resumed.exit_code == 0, resumed.stdout + resumed.stderr
    rdata = _json_from_cli(resumed.stdout)
    assert rdata["ok"] is True
    assert rdata["command"] == "resume"
    assert rdata["run_id"] == pdata["run_id"]
    ev = runner.invoke(app, ["eval", "examples/eval/pass.yaml", "--json"])
    assert ev.exit_code == 0, ev.stdout + ev.stderr
    edata = _json_from_cli(ev.stdout)
    assert edata["ok"] is True
    assert edata["command"] == "eval"
    assert "passed" in edata
    assert "failed" in edata
    assert "results" in edata


def test_eval_help_lists_json() -> None:
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "--json" in _plain(result.stdout)


def test_runs_inspect_json_missing_id(tmp_path: Path, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    result = runner.invoke(app, ["runs", "inspect", "nope", "--json"])
    assert result.exit_code == 1, result.stdout + result.stderr
    data = _json_from_cli(result.stdout)
    assert isinstance(data, dict)
    assert data["ok"] is False
    assert data["run_id"] == "nope"
