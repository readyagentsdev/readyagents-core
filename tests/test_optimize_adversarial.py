"""Adversarial optimize: secrets, redaction, hostile candidates, spend cap, hold-out."""

from __future__ import annotations

import json
from pathlib import Path

from readyagents.errors import OptimizeRefused, OptimizeStopSpend
from readyagents.llm.base import CompletionResult, Message
from readyagents.optimize.loop import optimize_workflow
from readyagents.prompts.registry import add_version, get_prompt, register_literals
from readyagents.prompts.safety import is_config_shaped
from readyagents.testing.eval import EvalCase


class SpyLLM:
    """Records every generation payload; scores based on prompt keywords."""

    name = "scripted"

    def __init__(self, candidates: list[str], *, cost_micros: int = 0) -> None:
        self.candidates = list(candidates)
        self.cost_micros = cost_micros
        self.payloads: list[str] = []
        self.generation_calls = 0

    def complete(
        self, messages: list[Message], *, model: str, **kwargs: object
    ) -> CompletionResult:
        blob = " ".join(str(getattr(row, "content", "") or "") for row in messages)
        self.payloads.append(blob)
        if "Propose n improved" in blob or '"candidates"' in blob or "failing_cases" in blob:
            self.generation_calls += 1
            text = json.dumps({"candidates": self.candidates})
        elif "urgent-rule" in blob:
            text = "urgent"
        else:
            text = "other"
        return CompletionResult(
            text=text,
            model=model,
            usage={"cost_micros": self.cost_micros},
        )


def _flow(tmp_path: Path) -> Path:
    path = tmp_path / "classify.yaml"
    path.write_text(
        "name: classify\nrequired_inputs: [text]\nstart: draft\n"
        "nodes:\n  - id: draft\n    type: agent\n"
        "    prompt: 'Label: {{text}}'\n    output_key: label\n",
        encoding="utf-8",
    )
    return path


def _cases(workflow: Path, *, secret_input: bool = False) -> list[EvalCase]:
    secret_text = "sk-abcdefghijksecret down"
    return [
        EvalCase(
            name="train-down",
            workflow=workflow,
            inputs={"text": secret_text if secret_input else "production is down"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
        ),
        EvalCase(
            name="train-outage",
            workflow=workflow,
            inputs={"text": "checkout is down"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
        ),
        EvalCase(
            name="hold-hello",
            workflow=workflow,
            inputs={"text": "hello"},
            expect_status="succeeded",
        ),
    ]


def test_secret_in_case_is_blocked_and_not_sent(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    spy = SpyLLM(["urgent-rule. Label: {{text}}"])
    report = optimize_workflow(
        path,
        _cases(path, secret_input=True),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        llm=spy,
        score_llm=spy,
        settings=tmp_settings,
        secrets=["sk-abcdefghijksecret"],
        resume=False,
    )
    assert any(row.get("reason") == "secret" for row in report.blocked_cases)
    joined = "\n".join(spy.payloads)
    assert "sk-abcdefghijksecret" not in joined
    assert report.blocked_cases[0]["name"] == "train-down"


def test_redaction_applied_before_reflection(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "classify.yaml"
    path.write_text(
        "name: classify\nrequired_inputs: [text]\nstart: draft\n"
        "nodes:\n  - id: draft\n    type: agent\n"
        "    prompt: 'Label for user@example.com: {{text}}'\n    output_key: label\n",
        encoding="utf-8",
    )
    spy = SpyLLM(["urgent-rule. Label: {{text}}"])
    optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=1.0,
        llm=spy,
        score_llm=spy,
        settings=tmp_settings,
        resume=False,
    )
    gen = [blob for blob in spy.payloads if "failing_cases" in blob]
    assert gen
    assert "user@example.com" not in gen[0]
    assert "[redacted]" in gen[0]


def test_hostile_candidate_stored_inert_not_executed(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    yaml_bytes = path.read_bytes()
    hostile = "name: pwned\nstart: x\nnodes:\n  - id: x\n    type: agent\n    prompt: hi\n"
    assert is_config_shaped(hostile)
    spy = SpyLLM([hostile])
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.0,
        llm=spy,
        score_llm=spy,
        settings=tmp_settings,
        resume=False,
    )
    assert path.read_bytes() == yaml_bytes
    assert "name: pwned" not in path.read_text(encoding="utf-8")
    stored = None
    for row in report.iterations:
        for cand in row.candidates:
            if cand.config_shaped:
                stored = cand
    assert stored is not None
    assert stored.text == hostile
    # Registry stores the string; loading the sidecar must not run it as a workflow.
    register_literals(path)
    row = get_prompt(path, "draft", version=stored.version)
    assert row.text == hostile
    from readyagents.workflow.runner import load_workflow

    spec = load_workflow(path)
    assert spec.name == "classify"
    assert spec.start == "draft"


def test_spend_after_cap_does_not_continue(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    spy = SpyLLM(["urgent-rule. Label: {{text}}"], cost_micros=500_000)
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=8,
        max_spend=0.0001,
        llm=spy,
        score_llm=SpyLLM([]),
        settings=tmp_settings,
        resume=False,
    )
    assert report.stop_reason == "spend"
    assert isinstance(report.stop, OptimizeStopSpend)
    # First generation may spend; a second generation after the cap is a failure.
    assert spy.generation_calls <= 1


def test_promotion_without_holdout_refused(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    try:
        optimize_workflow(
            path,
            _cases(path)[:1],
            node="draft",
            max_iterations=1,
            llm=SpyLLM(["x"]),
            score_llm=SpyLLM([]),
            settings=tmp_settings,
            resume=False,
        )
        raise AssertionError("hold-out must be mandatory")
    except OptimizeRefused as extra:
        assert extra.reason == "holdout"


def test_add_version_does_not_rewrite_yaml(tmp_path: Path) -> None:
    path = _flow(tmp_path)
    before = path.read_bytes()
    register_literals(path)
    add_version(path, "draft", "nodes:\n  - id: hack\n    type: tool\n", source="candidate")
    assert path.read_bytes() == before


def test_token_usage_without_cost_micros_stops_spend(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)

    class TokenLLM(SpyLLM):
        def complete(self, messages, *, model, **kwargs):
            blob = " ".join(str(getattr(row, "content", "") or "") for row in messages)
            self.payloads.append(blob)
            self.generation_calls += 1
            return CompletionResult(
                text=json.dumps({"candidates": self.candidates}),
                model=model,
                usage={"prompt_tokens": 2000, "completion_tokens": 1000},
            )

    spy = TokenLLM(["urgent-rule. Label: {{text}}"])
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=8,
        max_spend=0.0001,
        llm=spy,
        score_llm=SpyLLM([]),
        settings=tmp_settings,
        resume=False,
        model="openai:gpt-4o-mini",
    )
    assert report.stop_reason == "spend"
    assert report.spend_usd > 0.0
    assert spy.generation_calls == 1


def test_resume_after_spend_does_not_regenerate(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    spy = SpyLLM(["urgent-rule. Label: {{text}}"], cost_micros=500_000)
    first = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=8,
        max_spend=0.0001,
        llm=spy,
        score_llm=SpyLLM([]),
        settings=tmp_settings,
        resume=False,
    )
    assert first.stop_reason == "spend"
    assert spy.generation_calls == 1
    second = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=8,
        max_spend=0.0001,
        llm=spy,
        score_llm=SpyLLM([]),
        settings=tmp_settings,
        resume=True,
    )
    assert second.stop_reason == "spend"
    assert spy.generation_calls == 1


def test_secret_blocked_holdout_refuses_promotion(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    hold = [
        EvalCase(
            name="hold-secret",
            workflow=path,
            inputs={"text": "sk-abcdefghijksecret"},
            expect_status="succeeded",
        )
    ]
    try:
        optimize_workflow(
            path,
            _cases(path)[:2],
            hold_out=hold,
            node="draft",
            max_iterations=1,
            min_improvement=0.05,
            llm=SpyLLM(["urgent-rule. Label: {{text}}"]),
            score_llm=SpyLLM(["urgent-rule. Label: {{text}}"]),
            settings=tmp_settings,
            resume=False,
        )
        raise AssertionError("blocked hold-out must refuse")
    except OptimizeRefused as extra:
        assert extra.reason == "holdout"


def test_ghp_token_redacted_in_reflection(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "classify.yaml"
    path.write_text(
        "name: classify\nrequired_inputs: [text]\nstart: draft\n"
        "nodes:\n  - id: draft\n    type: agent\n"
        "    prompt: 'token ghp_abcdefghijk999 Label: {{text}}'\n    output_key: label\n",
        encoding="utf-8",
    )
    spy = SpyLLM(["urgent-rule. Label: {{text}}"])
    optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=1.1,
        llm=spy,
        score_llm=spy,
        settings=tmp_settings,
        resume=False,
    )
    gen = [blob for blob in spy.payloads if "failing_cases" in blob]
    assert gen
    assert "ghp_abcdefghijk999" not in gen[0]


def test_sidecar_uses_file_stem_when_name_differs(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "support.yaml"
    path.write_text(
        "name: support-triage\nrequired_inputs: [text]\nstart: draft\n"
        "nodes:\n  - id: draft\n    type: agent\n"
        "    prompt: 'Label: {{text}}'\n    output_key: label\n",
        encoding="utf-8",
    )
    register_literals(path)
    add_version(path, "draft", "urgent-rule. Label: {{text}}", source="candidate", activate=True)
    assert (tmp_path / "support.prompts.json").is_file()
    from readyagents.testing.helpers import run_workflow_file_test

    state = run_workflow_file_test(
        path,
        inputs={"text": "down"},
        llm=SpyLLM([]),
        settings=tmp_settings,
        persist=False,
    )
    label = str(state.output_keys.get("label") or state.node_outputs.get("draft") or "")
    assert "urgent" in label
