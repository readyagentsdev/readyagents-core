"""Core Decider protocol, Jev HTTP client, shim, and registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from readyagents.config import Settings
from readyagents.decide import (
    Answer,
    DecideError,
    Decision,
    Question,
    get_decider,
    validate_questions,
)
from readyagents.decide.jev import (
    PINNED_JEV_MODEL,
    JevDecider,
    reset_transport,
    use_transport,
)
from readyagents.decide.shim import ShimDecider
from readyagents.testing import FakeDecider, ScriptedLLM

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "decide"
SECRET = "sk-test-typesafe-not-a-real-key"


def _questions() -> dict[str, Question]:
    return {
        "department": Question(
            type="choice",
            instructions="Which team should handle this",
            criteria={
                "billing": "Payment or subscription issues",
                "technical": "Bugs or integration problems",
                "sales": "Pricing or account questions",
            },
        ),
        "frustration": Question(
            type="score",
            instructions="How frustrated the customer appears",
            criteria=[
                "Calm, just stating facts",
                "Frustrated but civil",
                "Very angry, strong language",
            ],
        ),
        "is_urgent": Question(
            type="noul",
            instructions="The message conveys urgency or time-sensitivity",
        ),
    }


def _noul_only() -> dict[str, Question]:
    return {
        "is_urgent": Question(
            type="noul",
            instructions="The message conveys urgency or time-sensitivity",
        )
    }


def _choice_only() -> dict[str, Question]:
    return {
        "department": Question(
            type="choice",
            instructions="Which team should handle this",
            criteria={
                "billing": "Payment or subscription issues",
                "technical": "Bugs or integration problems",
                "sales": "Pricing or account questions",
            },
        )
    }


def _load_fixture(name: str) -> Any:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    if name.endswith("malformed.json"):
        return text.encode("utf-8")
    data = json.loads(text)
    data.pop("_provenance", None)
    return json.dumps(data).encode("utf-8")


def test_question_wire_criteria_shape() -> None:
    choice = Question(
        type="choice",
        instructions="pick",
        criteria={"a": "A", "b": "B"},
    )
    score = Question(type="score", instructions="rate", criteria=["low", "high"])
    noul = Question(type="noul", instructions="true?")
    assert isinstance(choice.wire()["criteria"], dict)
    assert isinstance(score.wire()["criteria"], list)
    assert "criteria" not in noul.wire()


def test_validate_questions_rejects() -> None:
    with pytest.raises(DecideError, match="at least one"):
        validate_questions({})
    with pytest.raises(DecideError, match="mapping"):
        validate_questions(
            {
                "q": Question(type="choice", instructions="pick", criteria=["a", "b"]),
            }
        )
    with pytest.raises(DecideError, match="ordered list"):
        validate_questions(
            {"q": Question(type="score", instructions="rate", criteria={"a": "A", "b": "B"})}
        )
    too_many = {f"opt{i}": str(i) for i in range(256)}
    with pytest.raises(DecideError, match="2–255"):
        validate_questions({"q": Question(type="choice", instructions="pick", criteria=too_many)})
    with pytest.raises(DecideError, match="2–255"):
        validate_questions(
            {"q": Question(type="choice", instructions="pick", criteria={"only": "one"})}
        )
    with pytest.raises(DecideError, match="non-empty instructions"):
        validate_questions({"q": Question(type="noul", instructions="  ")})
    with pytest.raises(DecideError, match="template-safe"):
        validate_questions({"not-ok": Question(type="noul", instructions="true?")})


def test_answer_value_and_effective_confidence() -> None:
    assert Answer(type="noul", noul=0.7).value == 0.7
    assert Answer(type="choice", choice="billing").value == "billing"
    assert Answer(type="score", score=1.035).value == 1.035
    assert Answer(type="noul", noul=0.01).effective_confidence() == pytest.approx(0.98)
    assert Answer(type="noul", noul=0.99).effective_confidence() == pytest.approx(0.98)
    assert Answer(type="noul", noul=0.5).effective_confidence() == pytest.approx(0.0)
    supplied = Answer(type="noul", noul=0.01, confidence=0.42)
    assert supplied.effective_confidence() == pytest.approx(0.42)


def test_decision_as_dict_frozen_shape() -> None:
    decision = Decision(
        answers={
            "is_urgent": Answer(type="noul", noul=0.999, confidence=0.998),
            "department": Answer(
                type="choice",
                choice="billing",
                confidence=0.596,
                probabilities={"billing": 0.84, "technical": 0.159, "sales": 0.001},
            ),
        },
        model="jev-1.13.0",
        decider="jev",
        usage={
            "input_tokens": 312,
            "output_tokens": 48,
            "prompt_tokens": 312,
            "completion_tokens": 48,
            "total_tokens": 360,
        },
    )
    blob = decision.as_dict(min_confidence=0.85)
    assert blob["decider"] == "jev"
    assert blob["model"] == "jev-1.13.0"
    assert blob["min_confidence"] == 0.85
    assert blob["low_confidence"] == ["department"]
    urgent = blob["answers"]["is_urgent"]
    assert urgent == {
        "type": "noul",
        "value": 0.999,
        "noul": 0.999,
        "confidence": 0.998,
        "probabilities": {},
        "legend": {},
    }
    dept = blob["answers"]["department"]
    assert dept["value"] == "billing"
    assert dept["choice"] == "billing"
    assert dept["confidence"] == 0.596
    assert dept["probabilities"]["billing"] == 0.84
    assert "legend" in dept
    assert blob["usage"]["input_tokens"] == 312


def test_jev_request_body_and_bearer() -> None:
    captured: dict[str, Any] = {}

    def exchange(url, *, method, body, headers, timeout):
        captured["url"] = url
        captured["method"] = method
        captured["body"] = json.loads(body.decode("utf-8"))
        captured["headers"] = dict(headers)
        return 200, _load_fixture("quickstart_response.json"), {}

    token = use_transport(exchange)
    try:
        decider = JevDecider(SECRET, sleep=lambda _s: None)
        decision = decider.decide(
            state="checkout is down",
            questions=_questions(),
            model="jev-latest",
        )
    finally:
        reset_transport(token)
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["headers"]["User-Agent"].startswith("readyagents/")
    assert captured["body"]["model"] == "jev-latest"
    assert captured["body"]["state"] == "checkout is down"
    assert captured["body"]["questions"]["department"]["criteria"]["billing"]
    assert isinstance(captured["body"]["questions"]["frustration"]["criteria"], list)
    assert decision.model == "jev-1.13.0"
    assert decision.decider == "jev"
    assert decision.answers["department"].choice == "billing"
    assert decision.answers["is_urgent"].noul == 0.999
    assert decision.answers["is_urgent"].confidence is None
    assert decision.answers["is_urgent"].effective_confidence() == pytest.approx(0.998)
    assert decision.usage["prompt_tokens"] == 312
    assert decision.usage["completion_tokens"] == 48
    assert decision.usage["input_tokens"] == 312


def test_jev_extra_answer_key_ignored() -> None:
    token = use_transport(lambda url, **kw: (200, _load_fixture("extra_key.json"), {}))
    try:
        decision = JevDecider(SECRET, sleep=lambda _s: None).decide(
            state="x", questions=_noul_only(), model=PINNED_JEV_MODEL
        )
    finally:
        reset_transport(token)
    assert "is_urgent" in decision.answers
    assert "unexpected" not in decision.answers


def test_jev_missing_requested_key_raises() -> None:
    token = use_transport(lambda url, **kw: (200, _load_fixture("missing_key.json"), {}))
    try:
        with pytest.raises(DecideError, match="missing requested answer"):
            JevDecider(SECRET, sleep=lambda _s: None).decide(
                state="x", questions=_noul_only(), model=PINNED_JEV_MODEL
            )
    finally:
        reset_transport(token)


def test_jev_retries_429_then_succeeds() -> None:
    calls = {"n": 0}

    def exchange(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, b'{"error":"slow down"}', {"Retry-After": "0"}
        return 200, _load_fixture("noul_no_confidence.json"), {}

    token = use_transport(exchange)
    try:
        decision = JevDecider(SECRET, sleep=lambda _s: None).decide(
            state="x", questions=_noul_only(), model=PINNED_JEV_MODEL
        )
    finally:
        reset_transport(token)
    assert calls["n"] == 2
    assert decision.answers["is_urgent"].noul == 0.01
    assert decision.answers["is_urgent"].effective_confidence() == pytest.approx(0.98)


def test_jev_5xx_exhausts_retries() -> None:
    def exchange(url, **kw):
        return 503, b"unavailable", {}

    token = use_transport(exchange)
    try:
        with pytest.raises(DecideError, match="HTTP 503") as info:
            JevDecider(SECRET, max_retries=2, sleep=lambda _s: None).decide(
                state="x", questions=_noul_only(), model=PINNED_JEV_MODEL
            )
    finally:
        reset_transport(token)
    assert info.value.status == 503
    assert SECRET not in str(info.value)


def test_jev_non_json_raises() -> None:
    token = use_transport(lambda url, **kw: (200, _load_fixture("malformed.json"), {}))
    try:
        with pytest.raises(DecideError, match="not JSON"):
            JevDecider(SECRET, sleep=lambda _s: None).decide(
                state="x", questions=_noul_only(), model=PINNED_JEV_MODEL
            )
    finally:
        reset_transport(token)


def test_jev_choice_outside_space_raises() -> None:
    token = use_transport(lambda url, **kw: (200, _load_fixture("out_of_space.json"), {}))
    try:
        with pytest.raises(DecideError, match="outside the declared space"):
            JevDecider(SECRET, sleep=lambda _s: None).decide(
                state="x", questions=_choice_only(), model=PINNED_JEV_MODEL
            )
    finally:
        reset_transport(token)


def test_shim_declared_space_from_scripted_llm() -> None:
    llm = ScriptedLLM().enqueue('{"department": "technical", "is_urgent": 0.8}')
    decision = ShimDecider(llm).decide(
        state="the API is returning 500s",
        questions={
            "department": _choice_only()["department"],
            "is_urgent": _noul_only()["is_urgent"],
        },
        model="openai:gpt-4o-mini",
    )
    assert decision.decider == "shim"
    assert decision.answers["department"].choice == "technical"
    assert decision.answers["is_urgent"].noul == 0.8
    assert decision.answers["department"].confidence is None
    assert decision.low_confidence_keys(0.01) == ["department", "is_urgent"]


def test_shim_oov_and_parse_failure_raise() -> None:
    llm = ScriptedLLM().enqueue('{"department": "legal"}')
    with pytest.raises(DecideError, match="outside the declared space"):
        ShimDecider(llm).decide(state="x", questions=_choice_only(), model="scripted")
    llm = ScriptedLLM().enqueue("not json at all")
    with pytest.raises(DecideError, match="not JSON"):
        ShimDecider(llm).decide(state="x", questions=_choice_only(), model="scripted")


def test_registry_table(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("READYAGENTS_TYPESAFE_API_KEY", raising=False)
    empty = Settings(
        home=tmp_settings.home,
        workspace=tmp_settings.workspace,
        typesafe_api_key=None,
        openai_api_key=None,
        _env_file=(),  # type: ignore[call-arg]
    )
    llm = ScriptedLLM()
    decider, model = get_decider(
        "jev:jev-1.13.0",
        settings=Settings(
            home=tmp_settings.home,
            workspace=tmp_settings.workspace,
            typesafe_api_key=SECRET,
            _env_file=(),  # type: ignore[call-arg]
        ),
    )
    assert isinstance(decider, JevDecider)
    assert model == "jev-1.13.0"

    decider, model = get_decider(
        "jev",
        settings=Settings(
            home=tmp_settings.home,
            workspace=tmp_settings.workspace,
            typesafe_api_key=SECRET,
            _env_file=(),  # type: ignore[call-arg]
        ),
    )
    assert isinstance(decider, JevDecider)
    assert model == PINNED_JEV_MODEL

    decider, model = get_decider("shim", settings=empty, llm=llm)
    assert isinstance(decider, ShimDecider)

    decider, model = get_decider(None, settings=empty, llm=llm)
    assert isinstance(decider, ShimDecider)

    keyed = Settings(
        home=tmp_settings.home,
        workspace=tmp_settings.workspace,
        typesafe_api_key=SECRET,
        _env_file=(),  # type: ignore[call-arg]
    )
    decider, model = get_decider(None, settings=keyed)
    assert isinstance(decider, JevDecider)
    assert model == PINNED_JEV_MODEL

    with pytest.raises(DecideError, match="No API key"):
        get_decider("jev", settings=empty)

    with pytest.raises(DecideError, match="Offline replay"):
        get_decider("jev", settings=keyed, offline=True)

    # offline raises before reading a key: a settings getter that blows up must
    # not be reached.
    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"offline must not read {name}")

    with pytest.raises(DecideError, match="Offline replay"):
        get_decider("jev", settings=Boom(), offline=True)  # type: ignore[arg-type]


def test_registry_alias_warns_with_min_confidence(
    tmp_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    keyed = Settings(
        home=tmp_settings.home,
        workspace=tmp_settings.workspace,
        typesafe_api_key=SECRET,
        _env_file=(),  # type: ignore[call-arg]
    )
    with caplog.at_level("WARNING"):
        get_decider("jev:jev-latest", settings=keyed, min_confidence=0.85)
    assert "moving alias" in caplog.text


@pytest.mark.parametrize("decider_name", ["jev", "shim", "fake"])
def test_decider_conformance(decider_name: str) -> None:
    questions = _questions()
    state = "checkout is down, customers cannot pay"

    if decider_name == "jev":
        token = use_transport(
            lambda url, **kw: (200, _load_fixture("quickstart_response.json"), {})
        )
        try:
            decision = JevDecider(SECRET, sleep=lambda _s: None).decide(
                state=state, questions=questions, model=PINNED_JEV_MODEL
            )
        finally:
            reset_transport(token)
    elif decider_name == "shim":
        llm = ScriptedLLM().enqueue(
            json.dumps({"department": "billing", "frustration": 1.0, "is_urgent": 0.9})
        )
        decision = ShimDecider(llm).decide(state=state, questions=questions, model="scripted")
    else:
        decision = FakeDecider().decide(state=state, questions=questions, model="fake")

    assert set(decision.answers) == set(questions)
    assert decision.decider == decider_name
    assert isinstance(decision.model, str) and decision.model
    for key, question in questions.items():
        answer = decision.answers[key]
        if question.type == "choice":
            assert answer.choice in question.criteria
        elif question.type == "score":
            assert 0.0 <= float(answer.score) <= float(len(question.criteria) - 1)
        else:
            assert 0.0 <= float(answer.noul) <= 1.0

    with pytest.raises(DecideError):
        if decider_name == "jev":
            token = use_transport(lambda url, **kw: (200, b"{}", {}))
            try:
                JevDecider(SECRET, sleep=lambda _s: None).decide(
                    state=state, questions={}, model=PINNED_JEV_MODEL
                )
            finally:
                reset_transport(token)
        elif decider_name == "shim":
            ShimDecider(ScriptedLLM()).decide(state=state, questions={}, model="x")
        else:
            FakeDecider().decide(state=state, questions={}, model="fake")


def _triage_spec(**extra):
    node = {
        "id": "triage",
        "type": "decide",
        "state": "{{message}}",
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which team",
                "criteria": {
                    "billing": "pay",
                    "technical": "bugs",
                    "sales": "price",
                },
            },
            "is_urgent": {"type": "noul", "instructions": "urgent?"},
        },
        "output_key": "triage",
        "route_on": "department",
        "routes": {
            "billing": "billing_queue",
            "technical": "page_oncall",
            "sales": "sales_inbox",
        },
        "default": "human_review",
    }
    node.update(extra)
    return {
        "name": "triage",
        "inputs": {"message": "checkout is down"},
        "start": "triage",
        "nodes": [
            node,
            {
                "id": "human_review",
                "type": "transform",
                "template": "human",
                "output_key": "summary",
            },
            {"id": "page_oncall", "type": "transform", "template": "page", "output_key": "summary"},
            {
                "id": "billing_queue",
                "type": "transform",
                "template": "bill",
                "output_key": "summary",
            },
            {
                "id": "sales_inbox",
                "type": "transform",
                "template": "sales",
                "output_key": "summary",
            },
            {"id": "done", "type": "transform", "template": "{{summary}}", "output_key": "result"},
        ],
    }


def _patch_decider(monkeypatch, decision: Decision) -> None:
    fake = FakeDecider()
    fake.enqueue(decision)

    def getter(ref=None, **kwargs):
        return fake, decision.model

    monkeypatch.setattr("readyagents.decide.node.get_decider", getter)


def test_decide_dry_run_no_call_zero_spend(tmp_settings, tmp_path: Path) -> None:
    meter = __import__("readyagents.cost.meter", fromlist=["SpendMeter"]).SpendMeter()
    fake = FakeDecider()

    def getter(**kwargs):
        raise AssertionError("dry-run must not construct a decider")

    import readyagents.decide.node as node_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(node_mod, "get_decider", getter)
    try:
        from readyagents.testing.helpers import run_workflow_spec

        state = run_workflow_spec(
            _triage_spec(),
            pin_home=tmp_settings.home_path(),
            workflow_dir=tmp_path,
            dry_run=True,
            spend_meter=meter,
        )
    finally:
        monkeypatch.undo()
    assert state.status == "succeeded"
    assert meter.model_calls == 0
    assert fake.calls == []


def test_decide_routes_choice_and_default(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    from readyagents.testing.helpers import run_workflow_spec

    decision = Decision(
        answers={
            "department": Answer(type="choice", choice="technical", confidence=0.9),
            "is_urgent": Answer(type="noul", noul=0.99, confidence=0.98),
        },
        model="fake",
        decider="fake",
        usage={"prompt_tokens": 4, "completion_tokens": 0, "total_tokens": 4},
    )
    _patch_decider(monkeypatch, decision)
    meter = __import__("readyagents.cost.meter", fromlist=["SpendMeter"]).SpendMeter()
    state = run_workflow_spec(
        _triage_spec(),
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        spend_meter=meter,
    )
    assert state.status == "succeeded"
    assert state.output_keys["summary"] == "page"
    assert state.node_outputs["triage"]["routed"] == "page_oncall"
    assert state.node_outputs["triage"]["answers"]["department"]["value"] == "technical"
    assert meter.model_calls == 1

    _patch_decider(
        monkeypatch,
        Decision(
            answers={
                "department": Answer(type="choice", choice="legal", confidence=0.9),
                "is_urgent": Answer(type="noul", noul=0.1, confidence=0.8),
            },
            model="fake",
            decider="fake",
        ),
    )
    # choice "legal" is invalid at the Decider boundary; FakeDecider default
    # would still be in-space. Here we inject an out-of-space answer to hit default.
    state = run_workflow_spec(
        _triage_spec(),
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
    )
    assert state.output_keys["summary"] == "human"


def test_decide_noul_then_else(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    from readyagents.testing.helpers import run_workflow_spec

    spec = {
        "name": "noul",
        "start": "gate",
        "nodes": [
            {
                "id": "gate",
                "type": "decide",
                "state": "urgent ping",
                "questions": {"is_urgent": {"type": "noul", "instructions": "urgent?"}},
                "route_on": "is_urgent",
                "threshold": 0.9,
                "then": "hot",
                "else": "cold",
                "output_key": "d",
            },
            {"id": "hot", "type": "transform", "template": "HOT", "output_key": "out"},
            {"id": "cold", "type": "transform", "template": "COLD", "output_key": "out"},
        ],
    }
    _patch_decider(
        monkeypatch,
        Decision(
            answers={"is_urgent": Answer(type="noul", noul=0.95, confidence=0.9)},
            model="fake",
            decider="fake",
        ),
    )
    state = run_workflow_spec(spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path)
    assert state.output_keys["out"] == "HOT"
    _patch_decider(
        monkeypatch,
        Decision(
            answers={"is_urgent": Answer(type="noul", noul=0.1, confidence=0.8)},
            model="fake",
            decider="fake",
        ),
    )
    state = run_workflow_spec(spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path)
    assert state.output_keys["out"] == "COLD"


def test_decide_min_confidence_and_fail(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    from readyagents.errors import NodeError
    from readyagents.testing.helpers import run_workflow_spec

    spec = _triage_spec(min_confidence=0.85, on_low_confidence="human_review")
    _patch_decider(
        monkeypatch,
        Decision(
            answers={
                "department": Answer(type="choice", choice="billing", confidence=0.4),
                "is_urgent": Answer(type="noul", noul=0.99, confidence=0.98),
            },
            model="fake",
            decider="fake",
        ),
    )
    state = run_workflow_spec(spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path)
    assert state.output_keys["summary"] == "human"
    assert state.node_outputs["triage"]["low_confidence"] == ["department"]

    fail_spec = _triage_spec(min_confidence=0.85, on_low_confidence="fail")
    _patch_decider(
        monkeypatch,
        Decision(
            answers={
                "department": Answer(type="choice", choice="billing", confidence=0.4),
                "is_urgent": Answer(type="noul", noul=0.99, confidence=0.98),
            },
            model="fake",
            decider="fake",
        ),
    )
    with pytest.raises((DecideError, NodeError), match="confidence below"):
        run_workflow_spec(fail_spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path)


def test_shim_min_confidence_always_low(tmp_settings, tmp_path: Path) -> None:
    from readyagents.testing.helpers import run_workflow_spec

    llm = ScriptedLLM().enqueue('{"department": "technical", "is_urgent": 0.99}')
    spec = _triage_spec(decider="shim", min_confidence=0.01, on_low_confidence="human_review")
    state = run_workflow_spec(
        spec, pin_home=tmp_settings.home_path(), workflow_dir=tmp_path, llm=llm
    )
    assert state.output_keys["summary"] == "human"
    assert set(state.node_outputs["triage"]["low_confidence"]) >= {"department", "is_urgent"}


def test_decide_schema_validate_errors() -> None:
    from pydantic import ValidationError

    from readyagents.workflow.schema import WorkflowSpec

    def boom(nodes):
        WorkflowSpec.model_validate({"name": "x", "nodes": nodes})

    with pytest.raises(ValidationError, match="require 'questions'"):
        boom([{"id": "n", "type": "decide", "state": "x"}])
    with pytest.raises(ValidationError, match="require 'state'"):
        boom(
            [
                {
                    "id": "n",
                    "type": "decide",
                    "questions": {"q": {"type": "noul", "instructions": "t"}},
                }
            ]
        )
    with pytest.raises(ValidationError, match="not a question"):
        boom(
            [
                {
                    "id": "n",
                    "type": "decide",
                    "state": "x",
                    "questions": {"q": {"type": "noul", "instructions": "t"}},
                    "route_on": "missing",
                    "then": "n",
                    "else": "n",
                }
            ]
        )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        boom(
            [
                {
                    "id": "n",
                    "type": "decide",
                    "state": "x",
                    "questions": {
                        "q": {
                            "type": "choice",
                            "instructions": "t",
                            "criteria": {"a": "A", "b": "B"},
                        }
                    },
                    "route_on": "q",
                    "routes": {"a": "n"},
                    "then": "n",
                }
            ]
        )
    with pytest.raises(ValidationError, match="on_low_confidence requires min_confidence"):
        boom(
            [
                {
                    "id": "n",
                    "type": "decide",
                    "state": "x",
                    "questions": {"q": {"type": "noul", "instructions": "t"}},
                    "on_low_confidence": "n",
                }
            ]
        )
    with pytest.raises(ValidationError, match="not criteria"):
        boom(
            [
                {
                    "id": "n",
                    "type": "decide",
                    "state": "x",
                    "questions": {
                        "q": {
                            "type": "choice",
                            "instructions": "t",
                            "criteria": {"a": "A", "b": "B"},
                        }
                    },
                    "route_on": "q",
                    "routes": {"nope": "n"},
                    "default": "n",
                }
            ]
        )


def test_replay_edited_routes_and_raised_min_confidence(
    tmp_settings, tmp_path: Path, monkeypatch
) -> None:
    from readyagents.replay.cassette import Cassette
    from readyagents.testing.helpers import run_workflow_spec

    decision = Decision(
        answers={
            "department": Answer(type="choice", choice="billing", confidence=0.7),
            "is_urgent": Answer(type="noul", noul=0.2, confidence=0.6),
        },
        model="fake",
        decider="fake",
    )
    _patch_decider(monkeypatch, decision)
    tape = Cassette.new(run_id="r1", workflow="triage")
    first = run_workflow_spec(
        _triage_spec(),
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        cassette=tape,
        recording=True,
    )
    assert first.output_keys["summary"] == "bill"
    path = tmp_path / "c.json"
    tape.save(path, root=tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("offline replay must not construct a decider")

    monkeypatch.setattr("readyagents.decide.node.get_decider", boom)
    loaded = Cassette.load(path)
    rerouted = _triage_spec()
    rerouted["nodes"][0]["routes"] = {
        "billing": "sales_inbox",
        "technical": "page_oncall",
        "sales": "billing_queue",
    }
    second = run_workflow_spec(
        rerouted,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        cassette=loaded,
        offline=True,
    )
    assert second.output_keys["summary"] == "sales"

    loaded2 = Cassette.load(path)
    gated = _triage_spec(min_confidence=0.85, on_low_confidence="human_review")
    third = run_workflow_spec(
        gated,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        cassette=loaded2,
        offline=True,
    )
    assert third.output_keys["summary"] == "human"


def test_decide_example_validates_and_dry_runs() -> None:
    from typer.testing import CliRunner

    from readyagents.cli import app

    runner = CliRunner()
    path = str(Path(__file__).resolve().parents[1] / "examples" / "decide_triage.yaml")
    v = runner.invoke(app, ["validate", path])
    assert v.exit_code == 0, v.stdout + v.stderr
    d1 = runner.invoke(app, ["run", path, "--dry-run", "--no-persist"])
    assert d1.exit_code == 0, d1.stdout + d1.stderr
    d2 = runner.invoke(app, ["run", path, "--dry-run", "--no-persist"])
    assert d2.exit_code == 0, d2.stdout + d2.stderr


def test_redactor_applies_to_vendor_body_not_cassette_digest(
    tmp_settings, tmp_path: Path, monkeypatch
) -> None:
    """ctx.redactor mutates the request body; record/replay digest the pre-redact state."""
    from readyagents.policy import REDACTED, Redactor
    from readyagents.replay.cassette import Cassette
    from readyagents.testing.helpers import run_workflow_spec

    email = "ada@x.test"
    captured: dict[str, Any] = {}

    def exchange(url, *, method, body, headers, timeout):
        captured["body"] = json.loads(body.decode("utf-8"))
        payload = {
            "model": "jev-1.13.0",
            "answers": {
                "department": {"type": "choice", "choice": "technical", "confidence": 0.9},
                "is_urgent": {"type": "noul", "noul": 0.99, "confidence": 0.98},
            },
            "usage": {"input_tokens": 8, "output_tokens": 1},
        }
        return 200, json.dumps(payload).encode("utf-8"), {}

    jev = JevDecider(SECRET, sleep=lambda _s: None)
    monkeypatch.setattr("readyagents.decide.node.get_decider", lambda *a, **k: (jev, "jev-1.13.0"))
    spec = _triage_spec()
    spec["inputs"]["message"] = f"checkout is down, ping {email}"
    tape = Cassette.new(run_id="r1", workflow="triage")
    token = use_transport(exchange)
    try:
        first = run_workflow_spec(
            spec,
            pin_home=tmp_settings.home_path(),
            workflow_dir=tmp_path,
            cassette=tape,
            recording=True,
            redactor=Redactor(),
        )
    finally:
        reset_transport(token)
    assert first.output_keys["summary"] == "page"
    sent = json.dumps(captured["body"])
    assert email not in sent
    assert REDACTED in sent

    path = tmp_path / "c.json"
    tape.save(path, root=tmp_path)
    assert email not in path.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise AssertionError("offline replay must not construct a decider")

    monkeypatch.setattr("readyagents.decide.node.get_decider", boom)
    loaded = Cassette.load(path)
    second = run_workflow_spec(
        spec,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        cassette=loaded,
        offline=True,
        redactor=Redactor(),
    )
    assert second.output_keys["summary"] == "page"


def test_decide_cancellation_before_call(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    from readyagents.errors import CancellationRequested
    from readyagents.testing.helpers import run_workflow_spec
    from readyagents.workflow.cancellation import CancellationToken

    token = CancellationToken()
    token.request(reason="stop")

    def getter(*_a, **_k):
        raise AssertionError("cancellation must run before constructing a decider")

    monkeypatch.setattr("readyagents.decide.node.get_decider", getter)
    with pytest.raises(CancellationRequested):
        run_workflow_spec(
            _triage_spec(),
            pin_home=tmp_settings.home_path(),
            workflow_dir=tmp_path,
            cancellation=token,
        )
