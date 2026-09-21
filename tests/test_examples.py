"""Shipped examples stay in the wheel and reachable via `new` (H-06)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ConfigError
from readyagents.examples import list_examples, materialize_example, resolve_example

runner = CliRunner()

ROOT = Path(__file__).resolve().parents[1]
SKIP_NAMES = {"__pycache__", ".DS_Store"}


def _repo_example_files() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "examples").rglob("*"):
        if path.is_dir() or path.name in SKIP_NAMES or path.suffix in {".pyc", ".pyo"}:
            continue
        found.add(path.relative_to(ROOT).as_posix())
    return found


def test_force_include_matches_repo_examples() -> None:
    """Every repo example must be force-included into the wheel, and no stale entries."""
    with open(ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    manifest = {k for k in force_include if k.startswith("examples/")}
    targets = {v for k, v in force_include.items() if k.startswith("examples/")}
    repo_files = _repo_example_files()
    assert manifest == repo_files, (
        f"missing from wheel manifest: {sorted(repo_files - manifest)}; "
        f"stale manifest entries: {sorted(manifest - repo_files)}"
    )
    assert targets == {f"readyagents/{f}" for f in repo_files}


def test_list_examples_matches_repo() -> None:
    assert [f"examples/{p}" for p in list_examples()] == sorted(_repo_example_files())


def test_resolve_prefers_yaml_for_shared_stem() -> None:
    relpath, content = resolve_example("calc_pipeline")
    assert relpath == "calc_pipeline.yaml"
    assert content == (ROOT / "examples" / "calc_pipeline.yaml").read_bytes()


def test_resolve_accepts_full_relative_path() -> None:
    relpath, _ = resolve_example("bench/suite.yaml")
    assert relpath == "bench/suite.yaml"


def test_resolve_unknown_example_errors() -> None:
    with pytest.raises(ConfigError, match="Unknown example"):
        resolve_example("does-not-exist")


def test_materialize_writes_workflow_and_schema(tmp_path: Path) -> None:
    written = materialize_example("calc_pipeline", tmp_path / "f")
    names = sorted(p.name for p in written)
    assert names == ["workflow.schema.json", "workflow.yaml"]
    assert (tmp_path / "f" / "workflow.yaml").read_bytes() == (
        ROOT / "examples" / "calc_pipeline.yaml"
    ).read_bytes()


def test_materialize_refuses_overwrite(tmp_path: Path) -> None:
    materialize_example("calc_pipeline", tmp_path / "f")
    with pytest.raises(ConfigError, match="Refusing to overwrite"):
        materialize_example("calc_pipeline", tmp_path / "f")


def test_new_list_examples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "--list-examples"])
    assert result.exit_code == 0, result.output
    assert "calc_pipeline.yaml" in result.output
    assert "bench/suite.yaml" in result.output


def test_new_from_example_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Acceptance flow: materialize calc_pipeline and run it keyless."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    created = runner.invoke(app, ["new", "f", "--from-example", "calc_pipeline"])
    assert created.exit_code == 0, created.output
    assert (tmp_path / "f" / "workflow.yaml").exists()
    run = runner.invoke(app, ["run", "f/workflow.yaml"])
    assert run.exit_code == 0, run.output
    assert "succeeded" in run.output


def test_new_from_example_rejects_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["new", "f", "--from-example", "calc_pipeline", "--template", "basic"]
    )
    assert result.exit_code != 0
    assert "Cannot combine" in result.output


def test_new_from_example_unknown_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "f", "--from-example", "does-not-exist"])
    assert result.exit_code != 0
    assert "Unknown example" in result.output


def test_shim_keyless_heuristic_without_llm() -> None:
    """Cold-pip: shim must not raise when no LLM is configured."""
    from readyagents.decide.base import questions_from_mapping
    from readyagents.decide.shim import ShimDecider

    questions = questions_from_mapping(
        {
            "department": {
                "type": "choice",
                "instructions": "Which team",
                "criteria": {
                    "billing": "Payment issues",
                    "technical": "Bugs or checkout errors",
                    "sales": "Pricing",
                },
            },
            "is_urgent": {"type": "noul", "instructions": "Urgency"},
        }
    )
    decision = ShimDecider(None).decide(
        state="production checkout returning 500s; please escalate",
        questions=questions,
        model="shim",
    )
    assert decision.decider == "shim"
    assert decision.answers["department"].choice == "technical"
    assert decision.low_confidence_keys(0.85) == ["department", "is_urgent"]


def test_new_from_example_decide_triage_completes_after_approval(
    tmp_path: Path, monkeypatch
) -> None:
    """Cold copy of decide_triage finishes after the human gate is approved."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    for key in ("TYPESAFE_API_KEY", "READYAGENTS_TYPESAFE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    created = runner.invoke(app, ["new", "f", "--from-example", "decide_triage"])
    assert created.exit_code == 0, created.output
    result = runner.invoke(
        app,
        ["run", "f/workflow.yaml", "--approve", "human_review", "--no-persist"],
    )
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    assert "Missing template variable" not in result.output
    assert "triage complete:" in result.output
    assert "Manual routing" in result.output


def test_new_from_example_decide_triage_runs_keyless(tmp_path: Path, monkeypatch) -> None:
    """Doctor claim: decide_triage materializes and runs without keys/LLM (pauses at HITL)."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    for key in ("TYPESAFE_API_KEY", "READYAGENTS_TYPESAFE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    created = runner.invoke(app, ["new", "triage", "--from-example", "decide_triage"])
    assert created.exit_code == 0, created.output
    result = runner.invoke(app, ["run", "triage/workflow.yaml", "--no-persist"])
    # Exit 2 = paused at approval (human_review); must not be exit 1 NodeError about LLM.
    assert result.exit_code == 2, result.output
    combined = result.output.lower()
    assert "requires an llm" not in combined
    assert "approval" in combined or "paused" in combined or "human_review" in combined


def test_materialize_include_demo_copies_child_and_runs(tmp_path: Path, monkeypatch) -> None:
    """Pip-road: include_demo materializes parent + child and runs keyless."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    created = runner.invoke(app, ["new", "inc", "--from-example", "include_demo"])
    assert created.exit_code == 0, created.output
    assert (tmp_path / "inc" / "workflow.yaml").is_file()
    assert (tmp_path / "inc" / "included_min.yaml").is_file()
    run = runner.invoke(app, ["run", "inc/workflow.yaml", "--input", "n=5"])
    assert run.exit_code == 0, run.output
    assert "include_demo ok: 15" in run.output
    assert "succeeded" in run.output


def test_materialize_composed_gate_copies_child_and_runs(tmp_path: Path, monkeypatch) -> None:
    """Pip-road: composed_gate materializes include child and runs with --approve."""
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    created = runner.invoke(app, ["new", "cg", "--from-example", "composed_gate"])
    assert created.exit_code == 0, created.output
    assert (tmp_path / "cg" / "included_min.yaml").is_file()
    run = runner.invoke(app, ["run", "cg/workflow.yaml", "--approve", "gate"])
    assert run.exit_code == 0, run.output
    assert "composed_gate ok:" in run.output
    assert "succeeded" in run.output


def test_copy_include_tree_refuses_cycle(tmp_path: Path) -> None:
    from readyagents.errors import TrustError
    from readyagents.examples import copy_include_tree

    root = tmp_path / "ex"
    root.mkdir()
    (root / "loop.yaml").write_text(
        "name: loop\nnodes:\n  - id: again\n    type: include\n    path: loop.yaml\n",
        encoding="utf-8",
    )
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "workflow.yaml").write_bytes((root / "loop.yaml").read_bytes())
    with pytest.raises(TrustError, match="include cycle") as exc:
        copy_include_tree(
            parent_rel="loop.yaml",
            parent_content=(root / "loop.yaml").read_bytes(),
            examples_root=root,
            dest=dest,
        )
    assert exc.value.reason == "cycle"


def test_copy_include_tree_refuses_escape(tmp_path: Path) -> None:
    from readyagents.errors import TrustError
    from readyagents.examples import copy_include_tree

    root = tmp_path / "ex"
    root.mkdir()
    (tmp_path / "outside.yaml").write_text(
        "name: leaked\nnodes:\n  - id: t\n    type: transform\n    template: x\n",
        encoding="utf-8",
    )
    (root / "parent.yaml").write_text(
        "name: parent\nnodes:\n  - id: c\n    type: include\n    path: ../outside.yaml\n",
        encoding="utf-8",
    )
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(TrustError, match="escapes") as exc:
        copy_include_tree(
            parent_rel="parent.yaml",
            parent_content=(root / "parent.yaml").read_bytes(),
            examples_root=root,
            dest=dest,
        )
    assert exc.value.reason == "escape"


def test_copy_include_tree_refuses_depth(tmp_path: Path) -> None:
    from readyagents.errors import TrustError
    from readyagents.examples import _MAX_INCLUDE_DEPTH, copy_include_tree

    root = tmp_path / "ex"
    root.mkdir()
    for i in range(_MAX_INCLUDE_DEPTH + 1):
        nxt = f"d{i + 1}.yaml" if i < _MAX_INCLUDE_DEPTH else None
        if nxt:
            body = f"name: d{i}\nnodes:\n  - id: c\n    type: include\n    path: {nxt}\n"
        else:
            body = "name: leaf\nnodes:\n  - id: t\n    type: transform\n    template: ok\n"
        (root / f"d{i}.yaml").write_text(body, encoding="utf-8")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(TrustError, match="include depth exceeded") as exc:
        copy_include_tree(
            parent_rel="d0.yaml",
            parent_content=(root / "d0.yaml").read_bytes(),
            examples_root=root,
            dest=dest,
        )
    assert exc.value.reason == "depth"
