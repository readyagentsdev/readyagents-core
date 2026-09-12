"""Shipped prompt registry and optimize loop: eval scorer, budgets, promotion, rollback."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import (
    OptimizeRefused,
    OptimizeStopIterations,
    OptimizeStopSpend,
    OptimizeStopWall,
)
from readyagents.llm.base import CompletionResult, Message
from readyagents.optimize.loop import optimize_workflow
from readyagents.optimize.score import score_suite
from readyagents.prompts.layout import content_hash
from readyagents.prompts.registry import (
    add_version,
    diff_versions,
    get_prompt,
    register_literals,
    rollback,
    sidecar_path,
)
from readyagents.testing.eval import EvalCase, run_eval
from readyagents.testing.helpers import ScriptedLLM, run_workflow_file_test

runner = CliRunner()
_ROOT = Path(__file__).resolve().parents[1]


class KeywordLLM:
    """Scripted scorer/generator: prompt text changes the completion."""

    name = "scripted"

    def __init__(self, *, cost_micros: int = 0, candidates: list[str] | None = None) -> None:
        self.cost_micros = cost_micros
        self.candidates = list(candidates or [])
        self.calls: list[list[Message]] = []

    def complete(
        self, messages: list[Message], *, model: str, **kwargs: object
    ) -> CompletionResult:
        self.calls.append(list(messages))
        blob = " ".join(str(getattr(row, "content", "") or "") for row in messages)
        if '"candidates"' in blob or "Propose n improved" in blob:
            text = json.dumps({"candidates": self.candidates})
        elif "always-other" in blob:
            text = "other"
        elif "urgent-rule" in blob:
            text = "urgent"
        else:
            text = "other"
        return CompletionResult(
            text=text,
            model=model,
            usage={"cost_micros": self.cost_micros, "prompt_tokens": 1, "completion_tokens": 1},
        )


def _flow(tmp_path: Path, prompt: str = "Label the ticket: {{text}}") -> Path:
    path = tmp_path / "classify.yaml"
    path.write_text(
        "\n".join(
            [
                "name: classify",
                "required_inputs: [text]",
                "start: draft",
                "nodes:",
                "  - id: draft",
                "    type: agent",
                "    prompt: |",
                f"      {prompt}",
                "    output_key: label",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _cases(workflow: Path) -> list[EvalCase]:
    return [
        EvalCase(
            name="train-down",
            workflow=workflow,
            inputs={"text": "production is down"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
            expect_nodes=["draft"],
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


def test_literal_auto_register_no_yaml_rewrite(tmp_path: Path) -> None:
    path = _flow(tmp_path)
    before = path.read_bytes()
    registry = register_literals(path)
    assert path.read_bytes() == before
    row = get_prompt(path, "draft")
    assert row.version == 1
    assert row.content_hash == content_hash(row.text)
    assert "Label the ticket" in row.text
    assert sidecar_path(path).is_file()
    changed = add_version(
        path, "draft", row.text + "\nBe brief.", source="candidate", activate=False
    )
    assert changed.content_hash != row.content_hash
    assert changed.content_hash == content_hash(changed.text)
    assert path.read_bytes() == before
    obj = registry.prompts["draft"]
    assert obj.id == "draft"


def test_score_suite_uses_run_eval_nodes_and_usage(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    llm = KeywordLLM()
    cases = _cases(path)
    cases.append(
        EvalCase(
            name="nodes-miss",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_nodes=["no-such-node"],
        )
    )
    cases.append(
        EvalCase(
            name="usage-ceil",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_usage={"prompt_tokens": {"max": 0}},
        )
    )
    snap, _ = score_suite(cases, settings=tmp_settings, llm=llm, prompt_text=path.read_text())
    # run_eval must fail expect_nodes / expect_usage; a status-only stub would pass them.
    assert "nodes-miss" in snap.failed_names
    assert "usage-ceil" in snap.failed_names
    via_eval = run_eval(
        [c for c in cases if c.name in {"nodes-miss", "usage-ceil"}],
        settings=tmp_settings,
        llm=llm,
    )
    assert not via_eval.ok


def test_cassette_scoring_zero_spend(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    llm = ScriptedLLM().enqueue("urgent", usage={"cost_micros": 0, "prompt_tokens": 2})
    state = run_workflow_file_test(
        path,
        inputs={"text": "production is down"},
        llm=llm,
        settings=tmp_settings,
        persist=False,
        record=True,
    )
    cassette = Path(str(state.metadata.get("cassette") or ""))
    assert cassette.is_file()
    case = EvalCase(
        name="replay",
        workflow=path,
        inputs={"text": "production is down"},
        cassette=cassette,
        expect_status="succeeded",
    )
    snap, _ = score_suite([case], settings=tmp_settings)
    assert snap.spend_usd == 0.0
    assert snap.total == 1


def _record_cassette(path: Path, text: str, tmp_settings) -> Path:
    llm = ScriptedLLM().enqueue("other", usage={"cost_micros": 0})
    state = run_workflow_file_test(
        path,
        inputs={"text": text},
        llm=llm,
        settings=tmp_settings,
        persist=False,
        record=True,
    )
    cassette = Path(str(state.metadata.get("cassette") or ""))
    assert cassette.is_file()
    return cassette


def test_candidate_outscores_cassette_baseline_without_score_llm(
    tmp_path: Path, tmp_settings
) -> None:
    path = _flow(tmp_path)
    before = path.read_bytes()
    cases = [
        EvalCase(
            name="train-down",
            workflow=path,
            inputs={"text": "production is down"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
            cassette=_record_cassette(path, "production is down", tmp_settings),
        ),
        EvalCase(
            name="train-outage",
            workflow=path,
            inputs={"text": "checkout is down"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
            cassette=_record_cassette(path, "checkout is down", tmp_settings),
        ),
        EvalCase(
            name="hold-hello",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            cassette=_record_cassette(path, "hello", tmp_settings),
        ),
    ]
    better = "urgent-rule. Label the ticket: {{text}}"
    gen = KeywordLLM(candidates=[better])
    report = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        llm=gen,
        score_llm=None,
        settings=tmp_settings,
        resume=False,
    )
    assert path.read_bytes() == before
    assert report.promoted is True
    assert report.best_score > report.baseline_score
    assert report.baseline_score == 0.0
    scoring_calls = [
        msgs
        for msgs in gen.calls
        if "urgent-rule" in " ".join(str(getattr(m, "content", "") or "") for m in msgs)
        and "failing_cases" not in " ".join(str(getattr(m, "content", "") or "") for m in msgs)
    ]
    assert scoring_calls, "candidate scoring must use the generation provider, not cassette replay"


def test_failure_payload_includes_redacted_diffs(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    better = "urgent-rule. Label the ticket: {{text}}"
    gen = KeywordLLM(candidates=[better])
    optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=1.1,
        candidates=1,
        llm=gen,
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    blobs = [
        " ".join(str(getattr(m, "content", "") or "") for m in msgs)
        for msgs in gen.calls
        if any("failing_cases" in str(getattr(m, "content", "") or "") for m in msgs)
    ]
    assert blobs
    data = json.loads(blobs[0][blobs[0].find("{") : blobs[0].rfind("}") + 1])
    rows = data.get("failing_cases") or []
    assert rows
    row = rows[0]
    assert "inputs" in row and "text" in row["inputs"]
    assert "expected" in row and "actual" in row
    assert "contains" in row["expected"] or "status" in row["expected"]
    assert row["reason"]


def test_held_out_score_is_adopted_not_baseline(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    train = _cases(path)[:2]
    hold = [
        EvalCase(
            name="hold-hello",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_contains={"label": "urgent"},
        )
    ]
    better = "urgent-rule. Label the ticket: {{text}}"
    report = optimize_workflow(
        path,
        train,
        hold_out=hold,
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        llm=KeywordLLM(candidates=[better]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert report.promoted is True
    assert report.held_out["baseline"] == 0.0
    assert report.held_out["score"] == 1.0
    assert report.held_out["score"] != report.held_out["baseline"]


def test_optimize_promotes_above_threshold_and_holdout(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    before = path.read_bytes()
    better = "urgent-rule. Label the ticket: {{text}}"
    gen = KeywordLLM(candidates=[better])
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        llm=gen,
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert path.read_bytes() == before
    assert report.stop_reason == "iterations"
    assert isinstance(report.stop, OptimizeStopIterations)
    assert report.promoted is True
    assert report.held_out.get("score") is not None
    assert report.delta >= 0.05
    active = get_prompt(path, "draft")
    assert "urgent-rule" in active.text
    assert active.content_hash == content_hash(active.text)


def test_below_threshold_not_adopted(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    better = "urgent-rule. Label the ticket: {{text}}"
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=1.1,
        candidates=1,
        llm=KeywordLLM(candidates=[better]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert report.promoted is False
    assert get_prompt(path, "draft").version == 1


def test_frozen_fixture_regression_is_named(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    hostile = "always-other. Label the ticket: {{text}}"
    frozen = [
        EvalCase(
            name="frozen-hello",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_contains={"label": "other"},
        )
    ]
    # A candidate that improves train (urgent-rule) is fine; always-other
    # fails train. Use a candidate that boosts train but flips frozen.
    boost = "urgent-rule. always-other. Label: {{text}}"
    # KeywordLLM: "always-other" is checked first, so this candidate
    # scores "other" everywhere — train fails, frozen passes. Invert:
    # frozen expects "urgent" on hello so always-other regresses it? Baseline
    # also returns other for hello without urgent-rule. So baseline frozen fails
    # too and is not a regression (only newly failing names count).
    frozen_ok = [
        EvalCase(
            name="frozen-hello",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_contains={"label": "other"},
        )
    ]
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        frozen=frozen_ok,
        llm=KeywordLLM(candidates=[boost]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    # boost contains always-other so train does not improve; not adopted.
    assert report.promoted is False
    # True regression: urgent-rule improves train (down -> urgent) and
    # frozen-hello still other. Not a regression. Need frozen that fails
    # only with urgent-rule: expect label other on production-is-down.
    frozen_strict = [
        EvalCase(
            name="frozen-down",
            workflow=path,
            inputs={"text": "production is down"},
            expect_status="succeeded",
            expect_contains={"label": "other"},
        )
    ]
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        frozen=frozen_strict,
        llm=KeywordLLM(candidates=["urgent-rule. Label the ticket: {{text}}"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert report.promoted is False
    assert any("frozen-down" in name for name in report.regressions)
    del frozen, hostile


def test_holdout_regression_refuses_promotion(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    # Baseline hold (hello -> other) passes. urgent-rule returns urgent
    # everywhere, so a hold-out that requires "other" regresses.
    hold = [
        EvalCase(
            name="hold-hello",
            workflow=path,
            inputs={"text": "hello"},
            expect_status="succeeded",
            expect_contains={"label": "other"},
        )
    ]
    train = _cases(path)[:2]
    report = optimize_workflow(
        path,
        train,
        hold_out=hold,
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        llm=KeywordLLM(candidates=["urgent-rule. Label the ticket: {{text}}"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert report.promoted is False
    assert report.held_out.get("regression") is True


def test_missing_holdout_refused(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    one = [_cases(path)[0]]
    try:
        optimize_workflow(
            path,
            one,
            node="draft",
            max_iterations=1,
            llm=KeywordLLM(candidates=["x"]),
            score_llm=KeywordLLM(),
            settings=tmp_settings,
            resume=False,
        )
        raise AssertionError("expected OptimizeRefused")
    except OptimizeRefused as extra:
        assert extra.reason == "holdout"


def test_typed_budget_stops(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    cases = _cases(path)
    wall = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=8,
        max_wall_seconds=0,
        llm=KeywordLLM(candidates=["urgent-rule"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert wall.stop_reason == "wall"
    assert isinstance(wall.stop, OptimizeStopWall)
    spend = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=8,
        max_spend=0.0,
        llm=KeywordLLM(candidates=["urgent-rule"], cost_micros=1_000_000),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert spend.stop_reason == "spend"
    assert isinstance(spend.stop, OptimizeStopSpend)
    assert spend.spend_usd == 0.0
    iters = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=1,
        llm=KeywordLLM(candidates=["urgent-rule"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert iters.stop_reason == "iterations"
    assert isinstance(iters.stop, OptimizeStopIterations)
    assert wall.stop_reason != spend.stop_reason != iters.stop_reason


def test_resume_does_not_respend_completed_iteration(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    cases = _cases(path)
    gen = KeywordLLM(candidates=["urgent-rule. Label {{text}}"], cost_micros=100)
    first = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=1,
        min_improvement=1.0,
        llm=gen,
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert first.stop_reason == "iterations"
    n_first = len(gen.calls)
    second = optimize_workflow(
        path,
        cases,
        node="draft",
        max_iterations=2,
        min_improvement=1.0,
        llm=gen,
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=True,
    )
    assert second.stop_reason == "iterations"
    assert len(second.iterations) == 2
    assert len(gen.calls) == n_first + 1


def test_history_diff_rollback_exact(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        llm=KeywordLLM(candidates=["urgent-rule. Label the ticket: {{text}}"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    before = get_prompt(path, "draft")
    assert before.version == 2
    diff = diff_versions(path, "draft", left=1, right=2)
    assert "urgent-rule" in diff
    restored = rollback(path, "draft")
    assert restored.version == 1
    assert restored.content_hash == content_hash(restored.text)
    original = register_literals(path)
    v1 = original.prompts["draft"].version(1)
    assert v1 is not None
    assert restored.text == v1.text
    assert restored.content_hash == v1.content_hash
    assert path.read_bytes().find(b"urgent-rule") < 0


def test_approval_payload_has_diff_and_delta(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path)
    report = optimize_workflow(
        path,
        _cases(path),
        node="draft",
        max_iterations=1,
        min_improvement=0.05,
        candidates=1,
        require_approval=True,
        llm=KeywordLLM(candidates=["urgent-rule. Label the ticket: {{text}}"]),
        score_llm=KeywordLLM(),
        settings=tmp_settings,
        resume=False,
    )
    assert report.promoted is False
    assert report.approval is not None
    assert "diff" in report.approval
    assert "delta" in report.approval
    assert report.approval["delta"] >= 0.05
    assert get_prompt(path, "draft").version == 1


def test_cli_optimize_json_no_double_ok(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    src = _ROOT / "examples" / "optimize" / "classify.yaml"
    dest = tmp_path / "classify.yaml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    def _record(text: str) -> Path:
        llm = ScriptedLLM().enqueue("other", usage={"cost_micros": 0})
        state = run_workflow_file_test(
            dest,
            inputs={"text": text},
            llm=llm,
            settings=tmp_settings,
            persist=False,
            record=True,
        )
        cassette = Path(str(state.metadata.get("cassette") or ""))
        assert cassette.is_file()
        return cassette

    c1 = _record("production is down")
    c2 = _record("checkout is down")
    c3 = _record("hello")
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "\n".join(
            [
                "cases:",
                "  - name: train-down",
                "    workflow: classify.yaml",
                f"    cassette: {c1}",
                "    inputs: {text: production is down}",
                "    expect_status: succeeded",
                "    expect_contains: {label: urgent}",
                "  - name: train-outage",
                "    workflow: classify.yaml",
                f"    cassette: {c2}",
                "    inputs: {text: checkout is down}",
                "    expect_status: succeeded",
                "    expect_contains: {label: urgent}",
                "  - name: hold-hello",
                "    workflow: classify.yaml",
                f"    cassette: {c3}",
                "    inputs: {text: hello}",
                "    expect_status: succeeded",
                "",
            ]
        ),
        encoding="utf-8",
    )
    gen = KeywordLLM(candidates=["urgent-rule. Label the ticket: {{text}}"])
    provider_calls = {"n": 0}

    def _provider(model_ref=None, **kwargs):
        del model_ref, kwargs
        provider_calls["n"] += 1
        return gen, "scripted"

    monkeypatch.setattr("readyagents.llm.registry.get_provider", _provider)
    first = runner.invoke(app, ["optimize", "--help"])
    second = runner.invoke(app, ["optimize", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    plist = runner.invoke(app, ["prompts", "--help"])
    plist2 = runner.invoke(app, ["prompts", "--help"])
    assert plist.exit_code == 0
    assert plist2.exit_code == 0
    ran = runner.invoke(
        app,
        [
            "optimize",
            str(dest),
            "--eval",
            str(suite),
            "--max-iterations",
            "1",
            "--min-improvement",
            "0.05",
            "--model",
            "mock:scripted",
            "--json",
        ],
    )
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    payload = json.loads(ran.stdout[ran.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "optimize"
    assert payload["stop_reason"] == "iterations"
    assert "held_out" in payload
    assert "score" in payload["held_out"]
    assert payload["promoted"] is True
    assert payload["best_score"] > payload["baseline_score"]
    again = runner.invoke(
        app,
        [
            "optimize",
            str(dest),
            "--eval",
            str(suite),
            "--max-iterations",
            "1",
            "--min-improvement",
            "0.05",
            "--model",
            "mock:scripted",
            "--no-resume",
            "--json",
        ],
    )
    assert again.exit_code == 0, again.stdout + again.stderr
    blob = json.loads(again.stdout[again.stdout.find("{") :])
    assert blob["stop_reason"] == payload["stop_reason"]
    assert "held_out" in blob
    hist = runner.invoke(app, ["prompts", "history", str(dest), "--id", "draft", "--json"])
    assert hist.exit_code == 0, hist.stdout + hist.stderr
    clear_settings_cache()
