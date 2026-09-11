"""Adversarial simulate suite. Drive shipped APIs only; fail closed. No skip/xfail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readyagents.errors import SimulateRefused
from readyagents.firewall.policy_file import load_policy
from readyagents.simulate.generate import generate_from_path
from readyagents.simulate.run import simulate_workflow
from readyagents.testing.eval import load_eval_suite, run_eval
from readyagents.tools import FunctionTool, ToolRegistry

_SECRET_SHAPED = "sk-abcdefghijksecret"


def _write_adv_flow(root: Path, *, gated: bool = False) -> Path:
    """Minimal write_file workflow. ``gated`` adds an approval so cases actually fail."""
    nxt = "a" if gated else "d"
    approval = ""
    if gated:
        approval = "  - id: a\n    type: approval\n    prompt: go?\n    then: d\n    else: d\n"
    path = root / "adv-sim.yaml"
    path.write_text(
        "name: adv-sim\n"
        "required_inputs: [draft]\n"
        "inputs: {draft: hello}\n"
        "start: w\n"
        "nodes:\n"
        "  - id: w\n"
        "    type: tool\n"
        "    tool: write_file\n"
        '    arguments: {path: pwned.txt, content: "{{ draft }}"}\n'
        f"    next: {nxt}\n"
        f"{approval}"
        "  - id: d\n"
        "    type: transform\n"
        '    template: "ok"\n'
        "    output_key: summary\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _assert_write_file_tool(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "type: tool" in text
    assert "tool: write_file" in text


def _spy_write_file() -> tuple[ToolRegistry, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    def _spy(**kwargs: object) -> str:
        calls.append(dict(kwargs))
        return "spied"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="write_file", description="spy write_file", handler=_spy))
    return tools, calls


def _tree_text(root: Path) -> str:
    blob = ""
    for path in root.rglob("*"):
        if path.is_file():
            blob += path.read_text(encoding="utf-8", errors="replace")
    return blob


def _seed_for_secret(flow: Path) -> int:
    for seed in range(256):
        cases = generate_from_path(flow, seed=seed, cap=1)
        if any(_SECRET_SHAPED in json.dumps(c.inputs, default=str) for c in cases):
            return seed
    raise AssertionError("generator never emitted secret-shaped input at cap=1")


def test_generated_injection_does_not_invoke_write_file_without_live_side_effects(
    tmp_settings,
) -> None:
    flow = _write_adv_flow(tmp_settings.workspace_path())
    _assert_write_file_tool(flow)
    cases = generate_from_path(flow, seed=42, cap=64)
    assert any("injection" in c.name for c in cases)
    assert any("call the tool write_file" in json.dumps(c.inputs, default=str) for c in cases), (
        "generator must emit the write_file injection corpus string"
    )
    tools, calls = _spy_write_file()
    report = simulate_workflow(
        flow,
        seed=42,
        cap=64,
        settings=tmp_settings,
        tools=tools,
        live_side_effects=False,
    )
    assert report.dry_run is True
    assert calls == []
    assert not (tmp_settings.workspace_path() / "pwned.txt").exists()


def test_secret_shaped_generated_content_does_not_survive_freeze(tmp_settings) -> None:
    workspace = tmp_settings.workspace_path()
    flow = _write_adv_flow(workspace)
    _assert_write_file_tool(flow)
    cases = generate_from_path(flow, seed=42, cap=64)
    assert any(_SECRET_SHAPED in json.dumps(c.inputs, default=str) for c in cases)

    dest = workspace / "sims"
    simulate_workflow(flow, seed=42, cap=64, out_dir=dest, settings=tmp_settings)
    assert _SECRET_SHAPED not in _tree_text(dest)

    # Freeze a representative whose generated input *was* the secret-shaped token.
    gated = _write_adv_flow(workspace, gated=True)
    seed = _seed_for_secret(gated)
    frozen_dest = workspace / "sims-secret"
    simulate_workflow(gated, seed=seed, cap=1, out_dir=frozen_dest, settings=tmp_settings)
    blob = _tree_text(frozen_dest)
    assert _SECRET_SHAPED not in blob
    case_files = list(frozen_dest.rglob("case.yaml"))
    assert case_files, "secret-shaped generated case must freeze under --out"
    for case in case_files:
        suite = load_eval_suite(case)
        scored = run_eval(suite, settings=tmp_settings, dry_run=True)
        assert scored.ok
        assert scored.passed == len(suite)
        copied = [
            p
            for p in case.parent.iterdir()
            if p.is_file() and p.suffix in {".yaml", ".yml"} and p.name != "case.yaml"
        ]
        assert copied, f"frozen workflow missing beside {case}"
        assert _SECRET_SHAPED not in json.dumps([row.inputs for row in suite], default=str)


def test_fail_on_new_does_not_treat_unrelated_cluster_keys_as_known(tmp_settings) -> None:
    flow = _write_adv_flow(tmp_settings.workspace_path(), gated=True)
    dest = tmp_settings.workspace_path() / "sims-new"
    dest.mkdir()
    (dest / "clusters.json").write_text(
        json.dumps({"keys": ["unrelated-class", "not-a-real-cluster"]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(SimulateRefused) as caught:
        simulate_workflow(
            flow, seed=7, cap=16, fail_on_new=True, out_dir=dest, settings=tmp_settings
        )
    assert caught.value.reason == "new-failure"
    assert caught.value.report is not None
    real_keys = list(caught.value.report.clusters)
    assert real_keys
    assert "unrelated-class" not in real_keys
    (dest / "clusters.json").write_text(
        json.dumps({"keys": real_keys}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    second = simulate_workflow(
        flow, seed=7, cap=16, fail_on_new=True, out_dir=dest, settings=tmp_settings
    )
    assert second.new_failures == []


def test_live_side_effects_with_default_deny_policy_is_refused(
    tmp_path: Path, tmp_settings
) -> None:
    flow = _write_adv_flow(tmp_settings.workspace_path())
    deny = tmp_path / "deny.yaml"
    deny.write_text("version: 1\ndefault: deny\n", encoding="utf-8", newline="\n")
    tools, calls = _spy_write_file()
    with pytest.raises(SimulateRefused) as caught:
        simulate_workflow(
            flow,
            seed=1,
            cap=4,
            settings=tmp_settings,
            tools=tools,
            live_side_effects=True,
            policy=load_policy(deny),
        )
    assert caught.value.reason == "policy"
    assert calls == []
    assert not (tmp_settings.workspace_path() / "pwned.txt").exists()
