from __future__ import annotations

from pathlib import Path

from readyagents.replay.cassette import Cassette
from readyagents.replay.freeze import FREEZE_WARNING, freeze_run
from readyagents.testing.eval import load_eval_suite, run_eval
from readyagents.workflow.runner import run_workflow_file


def test_freeze_round_trip_eval(tmp_path: Path, tmp_settings) -> None:
    workflow = tmp_path / "flow.yaml"
    workflow.write_text(
        "name: freeze-me\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: hello-frozen\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(workflow, settings=tmp_settings, persist=True, record=True)
    cassette = Cassette.load(state.metadata["cassette"])
    dest = tmp_path / "fixture"
    freeze_run(
        state,
        cassette,
        out_dir=dest,
        workspace=tmp_path,
        allow_unsealed=True,
    )
    assert (dest / "cassette.json").is_file()
    assert (dest / "case.yaml").is_file()
    readme = (dest / "README.md").read_text(encoding="utf-8")
    assert "recorded model" in readme.lower() or FREEZE_WARNING.split()[0] in readme
    assert FREEZE_WARNING.split()[0] in readme or "WARNING" in readme
    cases = load_eval_suite(dest / "case.yaml")
    report = run_eval(cases, settings=tmp_settings)
    assert report.ok, [row.reason for row in report.results]


def test_diff_identical_and_changed(tmp_settings, tmp_path: Path) -> None:
    from readyagents.replay.diff import diff_runs

    workflow = tmp_path / "d.yaml"
    workflow.write_text(
        "name: diff-me\n"
        "inputs: {n: 1}\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: 'n={{n}}'\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    a = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 1})
    b = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 1})
    same = diff_runs(a, b)
    assert same["identical"] is True
    c = run_workflow_file(workflow, settings=tmp_settings, persist=True, inputs={"n": 2})
    changed = diff_runs(a, c)
    assert changed["identical"] is False
    assert changed["first_divergence"]["node_id"] == "t"
    assert (tmp_settings.runs_dir() / f"{a.run_id}.json").is_file()
