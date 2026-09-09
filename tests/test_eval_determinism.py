"""Loader and freeze→eval CI pins. Drive the shipped CLI, not a reimplementation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ConfigError
from readyagents.testing.eval import EvalCase, load_eval_suite, run_eval
from readyagents.testing.helpers import ScriptedLLM
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import NodeResult, RunState

_runner = CliRunner()
_REPO = Path(__file__).resolve().parents[1]


def _cli_env(monkeypatch, tmp_path: Path, tmp_settings) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_API_KEY",
        "READYAGENTS_ANTHROPIC_API_KEY",
        "READYAGENTS_OPENAI_COMPAT_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()


def _json_from_cli(text: str) -> dict:
    for i, ch in enumerate(text):
        if ch in "{[":
            data = json.loads(text[i:])
            assert isinstance(data, dict)
            return data
    raise AssertionError(f"no JSON in:\n{text}")


def _freeze_calc_pipeline(tmp_path: Path, tmp_settings, monkeypatch) -> Path:
    _cli_env(monkeypatch, tmp_path, tmp_settings)
    examples = tmp_path / "examples"
    examples.mkdir()
    shutil.copy(_REPO / "examples" / "calc_pipeline.yaml", examples / "calc_pipeline.yaml")
    monkeypatch.chdir(tmp_path)
    ran = _runner.invoke(app, ["run", "examples/calc_pipeline.yaml", "--record", "--json"])
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    run_id = str(_json_from_cli(ran.stdout)["run_id"])
    frozen = _runner.invoke(app, ["runs", "freeze", run_id, "--out", "frozen-calc"])
    assert frozen.exit_code == 0, frozen.stdout + frozen.stderr
    dest = tmp_path / "frozen-calc"
    assert (dest / "case.yaml").is_file()
    return dest


def _load_case(dest: Path) -> dict:
    data = yaml.safe_load((dest / "case.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data.get("cases")
    return data


def _write_case(dest: Path, data: dict) -> None:
    (dest / "case.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )


def test_legacy_suite_omits_new_fields() -> None:
    cases = load_eval_suite(_REPO / "examples" / "eval" / "pass.yaml")
    assert cases[0].expect_determinism is None
    assert cases[0].expect_nodes is None
    assert cases[0].expect_tools is None
    assert cases[0].expect_usage is None
    report = run_eval(cases)
    assert report.ok


def test_load_eval_suite_new_fields(tmp_path: Path) -> None:
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        """
cases:
  - name: pins
    workflow:
      name: tiny
      nodes:
        - id: t
          type: transform
          template: "hello-eval"
          output_key: summary
    expect_status: succeeded
    expect_contains:
      summary: hello-eval
    expect_determinism:
      unsealable: []
      misses: []
    expect_nodes: []
    expect_tools:
      - name: calc
        arguments: {expr: "1+1"}
    expect_usage:
      prompt_tokens: {max: 8}
""",
        encoding="utf-8",
    )
    cases = load_eval_suite(suite)
    assert cases[0].expect_determinism == {"unsealable": [], "misses": []}
    assert cases[0].expect_nodes == []
    assert cases[0].expect_tools == [{"name": "calc", "arguments": {"expr": "1+1"}}]
    assert cases[0].expect_usage == {"prompt_tokens": {"max": 8}}


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "expect_determinism: {nope: []}\n",
            "unknown bucket",
        ),
        (
            "expect_tools: [{arguments: {x: 1}}]\n",
            "needs a name",
        ),
        (
            "expect_usage: {prompt_tokens: {max: 1, min: 0}}\n",
            "unknown keys",
        ),
        (
            "expect_usage: {tokens: {max: 1}}\n",
            "unknown metric",
        ),
        (
            "expect_nodes: [1]\n",
            "list of strings",
        ),
    ],
)
def test_load_eval_suite_rejects_bad_pins(tmp_path: Path, body: str, match: str) -> None:
    suite = tmp_path / "bad.yaml"
    suite.write_text(
        "cases:\n  - name: bad\n    workflow: {name: t, nodes: "
        "[{id: t, type: transform, template: x, output_key: s}]}\n    "
        + body,
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=match):
        load_eval_suite(suite)


def test_freeze_eval_cli_emits_and_scores_pins(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    dest = _freeze_calc_pipeline(tmp_path, tmp_settings, monkeypatch)
    text = (dest / "case.yaml").read_text(encoding="utf-8")
    assert "expect_determinism:" in text
    assert "expect_nodes:" in text
    assert "expect_tools:" in text
    assert "unsealable: []" in text or "unsealable:[]" in text.replace(" ", "")
    data = _load_case(dest)
    case = data["cases"][0]
    assert "summary" in (case.get("expect_contains") or {})
    assert case["expect_determinism"]["unsealable"] == []
    assert case["expect_determinism"]["misses"] == []
    assert "add" in case["expect_nodes"]
    assert any(item.get("name") == "calc" for item in case["expect_tools"])
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml")])
    assert scored.exit_code == 0, scored.stdout + scored.stderr


def test_eval_cli_fails_when_determinism_drifts(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    dest = _freeze_calc_pipeline(tmp_path, tmp_settings, monkeypatch)
    data = _load_case(dest)
    data["cases"][0]["expect_determinism"]["unsealable"] = ["stamp"]
    _write_case(dest, data)
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml"), "--json"])
    assert scored.exit_code == 1, scored.stdout + scored.stderr
    payload = _json_from_cli(scored.stdout)
    reason = payload["results"][0]["reason"]
    assert "determinism.unsealable" in reason


def test_eval_cli_fails_when_recomputed_class_drifts(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    dest = _freeze_calc_pipeline(tmp_path, tmp_settings, monkeypatch)
    data = _load_case(dest)
    data["cases"][0]["expect_determinism"]["recomputed"] = ["stamp"]
    _write_case(dest, data)
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml"), "--json"])
    assert scored.exit_code == 1, scored.stdout + scored.stderr
    reason = _json_from_cli(scored.stdout)["results"][0]["reason"]
    assert "determinism.recomputed" in reason
    assert data["cases"][0]["expect_contains"]


def test_eval_cli_fails_when_nodes_drift(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    dest = _freeze_calc_pipeline(tmp_path, tmp_settings, monkeypatch)
    data = _load_case(dest)
    nodes = list(data["cases"][0]["expect_nodes"])
    data["cases"][0]["expect_nodes"] = [n for n in nodes if n != "pick"]
    _write_case(dest, data)
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml"), "--json"])
    assert scored.exit_code == 1, scored.stdout + scored.stderr
    reason = _json_from_cli(scored.stdout)["results"][0]["reason"]
    assert "nodes" in reason


def test_eval_cli_fails_when_tools_drift(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    dest = _freeze_calc_pipeline(tmp_path, tmp_settings, monkeypatch)
    data = _load_case(dest)
    data["cases"][0]["expect_tools"] = [
        item for item in data["cases"][0]["expect_tools"] if item.get("name") != "calc"
    ]
    _write_case(dest, data)
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml"), "--json"])
    assert scored.exit_code == 1, scored.stdout + scored.stderr
    reason = _json_from_cli(scored.stdout)["results"][0]["reason"]
    assert "tool[" in reason


def test_eval_cli_fails_when_usage_exceeds_max(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    _cli_env(monkeypatch, tmp_path, tmp_settings)
    workflow = tmp_path / "agent.yaml"
    workflow.write_text(
        "name: usage-agent\n"
        "nodes:\n"
        "  - id: a\n"
        "    type: agent\n"
        "    prompt: hi\n"
        "    model: mock:m\n"
        "    output_key: summary\n",
        encoding="utf-8",
    )
    inner = ScriptedLLM()
    inner.enqueue(
        "hello-frozen-agent",
        model="m",
        usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    )
    state = run_workflow_file(
        workflow, settings=tmp_settings, persist=True, record=True, llm=inner
    )
    frozen = _runner.invoke(app, ["runs", "freeze", state.run_id, "--out", "frozen-usage"])
    assert frozen.exit_code == 0, frozen.stdout + frozen.stderr
    dest = tmp_path / "frozen-usage"
    data = _load_case(dest)
    assert data["cases"][0]["expect_usage"]["prompt_tokens"]["max"] == 10
    passing = _runner.invoke(app, ["eval", str(dest / "case.yaml")])
    assert passing.exit_code == 0, passing.stdout + passing.stderr
    data["cases"][0]["expect_usage"]["prompt_tokens"]["max"] = 1
    _write_case(dest, data)
    scored = _runner.invoke(app, ["eval", str(dest / "case.yaml"), "--json"])
    assert scored.exit_code == 1, scored.stdout + scored.stderr
    reason = _json_from_cli(scored.stdout)["results"][0]["reason"]
    assert "usage.prompt_tokens" in reason and "max 1" in reason


def test_score_missing_determinism_metadata_fails() -> None:
    from readyagents.testing.eval import _score

    state = RunState.start("tiny", {})
    state.status = "succeeded"
    state.results.append(NodeResult(node_id="t", type="transform", status="ok"))
    case = EvalCase(
        name="meta",
        workflow={"name": "tiny", "nodes": []},
        expect_determinism={"unsealable": []},
    )
    ok, reason = _score(state, case)
    assert not ok
    assert reason == "determinism metadata missing"


def test_score_tool_arguments_without_cassette_fail() -> None:
    from readyagents.testing.eval import _score

    state = RunState.start("tiny", {})
    state.status = "succeeded"
    state.results.append(
        NodeResult(
            node_id="t",
            type="agent",
            status="ok",
            tool_rounds=[{"name": "calc"}],
        )
    )
    case = EvalCase(
        name="args",
        workflow={"name": "tiny", "nodes": []},
        expect_tools=[{"name": "calc", "arguments": {"expression": "1+1"}}],
    )
    ok, reason = _score(state, case)
    assert not ok
    assert "require a cassette" in reason
