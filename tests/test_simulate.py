"""Shipped simulate: generate, coverage, dry-run, cluster, freeze, fail-on, personas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import SimulateRefused
from readyagents.firewall.detect import detect_injection, injection_examples
from readyagents.firewall.policy_file import load_policy
from readyagents.simulate.generate import generate_from_path
from readyagents.simulate.personas import generate_personas
from readyagents.simulate.run import simulate_workflow
from readyagents.testing.eval import load_eval_suite, run_eval
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import load_workflow

runner = CliRunner()


def _flow(tmp: Path, *, extra_required: str | None = None) -> Path:
    required = "[draft]" if extra_required is None else f"[draft, {extra_required}]"
    path = tmp / "sim.yaml"
    path.write_text(
        "name: sim-fixture\n"
        f"required_inputs: {required}\n"
        "inputs:\n"
        "  draft: hello\n"
        "  flag: yes\n"
        "  items: []\n"
        "start: gate\n"
        "nodes:\n"
        "  - id: gate\n"
        "    type: condition\n"
        "    when: \"{{ flag }} == 'yes'\"\n"
        "    then: approve\n"
        "    else: skip\n"
        "  - id: approve\n"
        "    type: approval\n"
        "    prompt: go?\n"
        "    then: loop\n"
        "    else: skip\n"
        "  - id: loop\n"
        "    type: foreach\n"
        "    items: items\n"
        "    body:\n"
        "      id: work\n"
        "      type: tool\n"
        "      tool: calc\n"
        "      arguments:\n"
        "        expression: '1+1'\n"
        "    next: write\n"
        "  - id: write\n"
        "    type: tool\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      path: out.txt\n"
        "      content: '{{ draft }}'\n"
        "    next: done\n"
        "  - id: skip\n"
        "    type: transform\n"
        "    template: skipped\n"
        "    output_key: summary\n"
        "  - id: done\n"
        "    type: transform\n"
        "    template: done\n"
        "    output_key: summary\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_generate_seed_stable_and_declaration_driven(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    a = generate_from_path(flow, seed=42, cap=40)
    b = generate_from_path(flow, seed=42, cap=40)
    assert [(c.name, c.inputs, c.decisions) for c in a] == [
        (c.name, c.inputs, c.decisions) for c in b
    ]
    names = {c.name for c in a}
    tags = {c.tag for c in a}
    assert any("empty" in n for n in names)
    assert any("maximal" in n for n in names)
    assert any("wrong-type" in n for n in names)
    assert any("unicode" in n for n in names)
    assert any("control" in n for n in names)
    assert any("injection" in n for n in names)
    assert "branch" in tags or any(n.startswith("branch-") for n in names)
    blobs = json.dumps([c.inputs for c in a], default=str)
    assert any(example[:12] in blobs for example in injection_examples() if example.strip())
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = generate_from_path(_flow(other_dir, extra_required="note"), seed=42, cap=40)
    assert {c.name for c in a} != {c.name for c in other}


def test_generate_needs_no_model(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    cases = generate_from_path(flow, seed=1, cap=8)
    assert cases
    assert all(c.inputs is not None for c in cases)


def test_simulate_coverage_and_dry_run(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    report = simulate_workflow(flow, seed=42, cap=24, settings=tmp_settings)
    assert report.dry_run is True
    assert report.spend_usd == 0.0
    cov = report.coverage
    assert "reached" in cov and "unreached" in cov
    assert cov["declared"] >= 1
    # Honest: do not require everything reached.
    assert isinstance(cov["unreached"], list)
    assert not (tmp_settings.workspace_path() / "out.txt").exists()


def test_live_side_effects_require_policy(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    with pytest.raises(SimulateRefused) as caught:
        simulate_workflow(
            flow, seed=1, cap=4, settings=tmp_settings, live_side_effects=True, policy=None
        )
    assert caught.value.reason == "policy"
    deny = tmp_path / "deny.yaml"
    deny.write_text("version: 1\ndefault: deny\n", encoding="utf-8")
    with pytest.raises(SimulateRefused) as denied:
        simulate_workflow(
            flow,
            seed=1,
            cap=4,
            settings=tmp_settings,
            live_side_effects=True,
            policy=load_policy(deny),
        )
    assert denied.value.reason == "policy"
    allow = tmp_path / "allow.yaml"
    allow.write_text(
        "version: 1\ndefault: deny\ntools:\n  write_file: {}\n  http_get: {}\n  calc: {}\n",
        encoding="utf-8",
    )
    report = simulate_workflow(
        flow,
        seed=1,
        cap=4,
        settings=tmp_settings,
        live_side_effects=True,
        policy=load_policy(allow),
    )
    assert report.dry_run is False


def test_cluster_freeze_and_fail_on(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_settings.workspace_path())
    dest = tmp_settings.workspace_path() / "sims"
    first = simulate_workflow(flow, seed=7, cap=16, out_dir=dest, settings=tmp_settings)
    assert first.failed >= 1
    assert first.clusters
    assert (dest / "clusters.json").is_file()
    frozen_dirs = [p for p in dest.iterdir() if p.is_dir() and p.name.startswith("fail-")]
    assert frozen_dirs
    case = frozen_dirs[0].joinpath("case.yaml")
    assert case.is_file()
    blob = ""
    for path in dest.rglob("*"):
        if path.is_file():
            blob += path.read_text(encoding="utf-8", errors="replace")
    assert "sk-abcdefghijksecret" not in blob
    suite = load_eval_suite(case)
    scored = run_eval(suite, settings=tmp_settings, dry_run=True)
    assert scored.passed + scored.failed == len(suite)
    # Second run: same classes are not new.
    second = simulate_workflow(
        flow, seed=7, cap=16, out_dir=dest, settings=tmp_settings, fail_on_new=True
    )
    assert second.new_failures == []
    # Distinct seed/cap can still be known if shapes match; force a new class via empty known.
    dest2 = tmp_settings.workspace_path() / "sims2"
    dest2.mkdir()
    (dest2 / "clusters.json").write_text('{"keys": []}\n', encoding="utf-8")
    with pytest.raises(SimulateRefused) as caught:
        simulate_workflow(
            flow, seed=7, cap=16, out_dir=dest2, settings=tmp_settings, fail_on_new=True
        )
    assert caught.value.reason == "new-failure"


def test_personas_opt_in_metered_sovereign(tmp_path: Path, tmp_settings) -> None:
    wf = load_workflow(_flow(tmp_path))
    llm = ScriptedLLM()
    llm.enqueue(text='{"draft": "hostile"}', usage={"cost_micros": 100000})
    llm.enqueue(text='{"draft": "confused"}', usage={"cost_micros": 100000})
    with pytest.raises(SimulateRefused) as sov:
        generate_personas(
            wf,
            model="mock:x",
            personas=["hostile", "confused"],
            llm=llm,
            max_spend=1.0,
            sovereign=True,
        )
    assert sov.value.reason == "sovereign"
    cases, spend = generate_personas(
        wf,
        model="mock:x",
        personas=["hostile", "confused"],
        llm=llm,
        max_spend=0.05,
        sovereign=False,
    )
    assert len(cases) == 1
    assert spend <= 0.15
    report = simulate_workflow(
        _flow(tmp_settings.workspace_path()),
        seed=1,
        cap=4,
        settings=tmp_settings,
        model=None,
        llm=llm,
    )
    assert report.spend_usd == 0.0


def test_cli_simulate_help_twice_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    first = runner.invoke(app, ["simulate", "--help"])
    second = runner.invoke(app, ["simulate", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    flow = _flow(tmp_path)
    ran = runner.invoke(
        app,
        ["simulate", str(flow), "--seed", "3", "--cases", "8", "--json", "--deterministic-only"],
    )
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    payload = json.loads(ran.stdout[ran.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "simulate"
    assert payload["dry_run"] is True
    assert "reached" in payload["coverage"]
    assert "unreached" in payload["coverage"]
    clear_settings_cache()


def test_injection_examples_hit_firewall() -> None:
    hits = [detect_injection(s).reasons for s in injection_examples()]
    assert any(h for h in hits)
