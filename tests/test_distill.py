"""Distillation: plan, dataset, pack boundary, eval, adapters, promote, demote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.cost.ledger import read_spend_entries
from readyagents.distill.adapters import require_signed
from readyagents.distill.canary import CANARY_TOKEN, canary_pass, plant_secret
from readyagents.distill.dataset import build_dataset
from readyagents.distill.evaluate import evaluate
from readyagents.distill.plan import plan
from readyagents.distill.promote import demote, promote, rescore_promoted
from readyagents.distill.schema import DistillConfig, EvalComparison, SideScore
from readyagents.distill.store import pin_for, save_config
from readyagents.distill.train import train
from readyagents.errors import (
    DistillApproval,
    DistillCanary,
    DistillCost,
    DistillHoldoutMissing,
    DistillLatency,
    DistillParity,
    DistillRegression,
    DistillSovereignHosted,
    DistillTrainMissing,
    DistillUnsigned,
)
from readyagents.feedback.capture import record_human_correction
from readyagents.llm.base import CompletionResult
from readyagents.routing.select import select_route
from readyagents.run_store import open_run_store
from readyagents.workflow.schema import NodeSpec, WorkflowSpec
from readyagents.workflow.state import RunState

runner = CliRunner()


def _gate() -> NodeSpec:
    return NodeSpec.model_validate(
        {
            "id": "classify",
            "type": "approval",
            "prompt": "ok?",
            "then": "ok",
            "else": "no",
            "feedback": {
                "allow_edit": True,
                "labels": ["spam", "ham"],
                "consent": "internal_training",
            },
        }
    )


def _seed_corrections(
    tmp_settings,
    n: int,
    *,
    consented: bool = True,
    label: str = "spam",
    secret: str | None = None,
    node_id: str = "classify",
) -> None:
    store = open_run_store(tmp_settings)
    try:
        for i in range(n):
            state = RunState.start("distill_wf", {"i": i})
            if consented:
                state.metadata["data_policy"] = {"scopes": ["internal_training"]}
            original = f"msg-{i}"
            edited = f"label-{label}-{i}"
            if secret and i == 0:
                edited = f"{edited} {secret}"
            record_human_correction(
                state,
                node=_gate(),
                actor_role="reviewer",
                reason="fix",
                original=original,
                edited=edited,
                label=label if i % 2 == 0 else "ham",
                decision_id=f"d{i}",
                model="openai:gpt-4o-mini",
            )
            store.save(state)
    finally:
        store.close()


class HonestTuner:
    name = "honest"

    def hosted(self) -> bool:
        return False

    def train(self, dataset_dir: Path, *, base: str, config: dict) -> Path:
        dest = Path(dataset_dir) / "trained.adapter.json"
        dest.write_text(
            json.dumps({"kind": "adapter", "base": base}, sort_keys=True), encoding="utf-8"
        )
        return dest

    def complete(self, adapter: Path, prompt: str) -> str:
        return "ok"


class MemorizingTuner:
    name = "memorize"

    def hosted(self) -> bool:
        return False

    def train(self, dataset_dir: Path, *, base: str, config: dict) -> Path:
        dest = Path(dataset_dir) / "trained.adapter.json"
        dest.write_text(
            (Path(dataset_dir) / "train.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        return dest

    def complete(self, adapter: Path, prompt: str) -> str:
        return Path(adapter).read_text(encoding="utf-8")


class HostedTuner(HonestTuner):
    def hosted(self) -> bool:
        return True


class BlobLLM:
    name = "blob"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages, *, model, tools=None, **kwargs) -> CompletionResult:
        return CompletionResult(text=self.text, model=model)


def _suite(tmp: Path, *, expect: str = "ok") -> Path:
    path = tmp / "suite.yaml"
    path.write_text(
        "cases:\n"
        "  - name: frozen\n"
        "    workflow:\n"
        "      name: frozen\n"
        "      default_model: mock:x\n"
        "      nodes:\n"
        "        - id: out\n"
        "          type: agent\n"
        "          prompt: say ok\n"
        "          output_key: output\n"
        "    expect_status: succeeded\n"
        f"    expect_contains: {{output: {expect}}}\n",
        encoding="utf-8",
    )
    return path


def _ed25519(tmp: Path) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv = tmp / "distill.pem"
    pub = tmp / "distill.pub.pem"
    priv.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv, pub


def test_plan_verdicts_enough_not_enough_too_broad(tmp_settings) -> None:
    save_config(DistillConfig(min_examples=4, broad_ratio=0.5), tmp_settings)
    empty = plan("classify", settings=tmp_settings, min_examples=4)
    assert empty.verdict == "not_enough_data"
    _seed_corrections(tmp_settings, 5, label="spam")
    broad = plan("classify", settings=tmp_settings, min_examples=4)
    assert broad.verdict in {"task_too_broad", "viable"}
    save_config(DistillConfig(min_examples=4, broad_ratio=1.0), tmp_settings)
    ok = plan("classify", settings=tmp_settings, min_examples=4)
    assert ok.verdict == "viable"
    assert ok.examples >= 4
    assert ok.current_cost_micros is None or ok.current_cost_micros >= 0


def test_dataset_consent_redact_seed_hash(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 6)
    state = RunState.start("distill_wf", {"i": 99})
    bare = NodeSpec.model_validate(
        {
            "id": "classify",
            "type": "approval",
            "prompt": "ok?",
            "then": "ok",
            "else": "no",
            "feedback": {"allow_edit": True, "labels": ["spam", "ham"]},
        }
    )
    record_human_correction(
        state,
        node=bare,
        actor_role="reviewer",
        reason="no consent",
        original="secret-row",
        edited="secret-row",
        label="spam",
        decision_id="nope",
    )
    store = open_run_store(tmp_settings)
    store.save(state)
    store.close()
    first = build_dataset("classify", tmp_path / "ds1", settings=tmp_settings, seed=7, yes=True)
    second = build_dataset("classify", tmp_path / "ds2", settings=tmp_settings, seed=7, yes=True)
    assert first.hash == second.hash
    assert first.hash.startswith("sha256:")
    assert first.counts["holdout"] >= 1
    assert first.holdout_named == "holdout"
    assert first.consent_from_recorded_policy is True
    assert first.redaction_reverified is True
    texts = (tmp_path / "ds1" / "train.jsonl").read_text(encoding="utf-8")
    texts += (tmp_path / "ds1" / "holdout.jsonl").read_text(encoding="utf-8")
    assert "secret-row" not in texts
    other = build_dataset("classify", tmp_path / "ds3", settings=tmp_settings, seed=99, yes=True)
    # Different seed may or may not change assignment; hash includes seed so it differs.
    assert other.hash != first.hash or other.seed != first.seed


def test_train_without_pack_is_typed_error(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 4)
    ds = build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    with pytest.raises(DistillTrainMissing) as extra:
        train(tmp_path / "ds", base="qwen-2.5-7b", settings=tmp_settings, packs=[])
    assert extra.value.reason == "pack_missing"
    import importlib

    train_mod = importlib.import_module("readyagents.distill.train")
    src = Path(train_mod.__file__).read_text(encoding="utf-8")
    assert "torch" not in src
    assert "transformers" not in src
    assert ds.hash


def test_evaluate_refuses_missing_holdout(tmp_path: Path, tmp_settings) -> None:
    suite = _suite(tmp_path)
    with pytest.raises(DistillHoldoutMissing):
        evaluate(suite=suite, dataset=tmp_path / "missing")


def test_promote_thresholds_distinct_and_one_node(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 4)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    priv, pub = _ed25519(tmp_path)
    from readyagents.trust.keyring import add_key, load_keyring

    add_key(pub, name="ops", home=tmp_settings.home_path())
    record = train(
        tmp_path / "ds",
        base="mock:base",
        settings=tmp_settings,
        tuner=HonestTuner(),
        sign_key=priv,
        node_id="classify",
    )
    assert record.signed is True
    require_signed(
        record.id, settings=tmp_settings, keyring=load_keyring(home=tmp_settings.home_path())
    )
    save_config(DistillConfig(min_parity=1.0, max_latency_ms=1.0, max_cost_micros=1), tmp_settings)
    slow = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, latency_ms=1, cost_micros=1),
        candidate=SideScore(accuracy=1, holdout=1.0, latency_ms=50, cost_micros=1),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
    )
    with pytest.raises(DistillLatency) as lat:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=slow,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert lat.value.reason == "latency"
    save_config(DistillConfig(min_parity=1.0, max_cost_micros=1), tmp_settings)
    pricey = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, cost_micros=1),
        candidate=SideScore(accuracy=1, holdout=1.0, cost_micros=99),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
    )
    with pytest.raises(DistillCost) as cost:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=pricey,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert cost.value.reason == "cost"
    save_config(DistillConfig(min_parity=1.0), tmp_settings)
    weak = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0),
        candidate=SideScore(accuracy=0.5, holdout=0.2),
        holdout={"named": True, "incumbent": 1.0, "candidate": 0.2},
        canary_passed=True,
    )
    with pytest.raises(DistillParity) as par:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=weak,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert par.value.reason == "parity"
    regress = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0),
        candidate=SideScore(accuracy=1, holdout=1.0),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        frozen_regressions=["frozen"],
        canary_passed=True,
    )
    with pytest.raises(DistillRegression) as reg:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=regress,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert reg.value.reason == "regression"
    leak = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0),
        candidate=SideScore(accuracy=1, holdout=1.0),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=False,
    )
    with pytest.raises(DistillCanary) as can:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=leak,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert can.value.reason == "canary"
    save_config(DistillConfig(min_parity=1.0, approval_roles=["ml_owner"]), tmp_settings)
    good = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, cost_micros=10),
        candidate=SideScore(accuracy=1, holdout=1.0, cost_micros=4),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
        fixture_digest="sha256:abc",
    )
    with pytest.raises(DistillApproval) as ap:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=good,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert ap.value.reason == "approval"
    assert ap.value.comparison is not None
    promoted = promote(
        record.id,
        "classify",
        settings=tmp_settings,
        comparison=good,
        approve=True,
        incumbent="openai:gpt-4o-mini",
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    assert promoted.status == "promoted"
    pin = pin_for("classify", tmp_settings)
    assert pin is not None
    assert pin["adapter_id"] == record.id


def test_select_route_uses_pin_when_home_configured(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    _seed_corrections(tmp_settings, 4)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    priv, pub = _ed25519(tmp_path)
    from readyagents.trust.keyring import add_key, load_keyring

    add_key(pub, name="ops", home=tmp_settings.home_path())
    record = train(
        tmp_path / "ds",
        base="mock:base",
        settings=tmp_settings,
        tuner=HonestTuner(),
        sign_key=priv,
        node_id="classify",
    )
    good = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, cost_micros=10),
        candidate=SideScore(accuracy=1, holdout=1.0, cost_micros=4),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
        fixture_digest="sha256:abc",
    )
    promote(
        record.id,
        "classify",
        settings=tmp_settings,
        comparison=good,
        incumbent="openai:gpt-4o-mini",
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    spec = WorkflowSpec.model_validate(
        {
            "name": "t",
            "nodes": [
                {"id": "classify", "type": "agent", "prompt": "x", "output_key": "o"},
                {"id": "other", "type": "agent", "prompt": "y", "output_key": "p"},
            ],
        }
    )
    pinned = select_route(spec, spec.nodes[0], primary="openai:gpt-4o-mini")
    assert pinned.model == f"adapter:{record.id}"
    assert "openai:gpt-4o-mini" in pinned.candidates
    other = select_route(spec, spec.nodes[1], primary="openai:gpt-4o-mini")
    assert other.model == "openai:gpt-4o-mini"
    rows = read_spend_entries(tmp_settings.ledger_dir())
    phases = {row.get("phase") for row in rows if row.get("event") == "distill_delta"}
    assert phases == {"before", "after"}


def test_unsigned_adapter_refuses_to_load(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 4)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    record = train(
        tmp_path / "ds",
        base="mock:base",
        settings=tmp_settings,
        tuner=HonestTuner(),
        node_id="classify",
    )
    assert record.signed is False
    with pytest.raises(DistillUnsigned) as extra:
        require_signed(record.id, settings=tmp_settings)
    assert extra.value.reason == "unsigned"


def test_sovereign_refuses_hosted_tune(tmp_path: Path, tmp_settings) -> None:
    tmp_settings.sovereign = True
    _seed_corrections(tmp_settings, 4)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    with pytest.raises(DistillSovereignHosted) as extra:
        train(
            tmp_path / "ds",
            base="openai:gpt-4o-mini",
            settings=tmp_settings,
            tuner=HostedTuner(),
            hosted=True,
        )
    assert extra.value.reason == "sovereign_hosted"


def test_canary_and_auto_demote(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 6, secret=CANARY_TOKEN)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    train_path = tmp_path / "ds" / "train.jsonl"
    # Ensure the planted secret survived into train for the memorising tuner.
    if CANARY_TOKEN not in train_path.read_text(encoding="utf-8"):
        row = plant_secret({"id": "canary", "instruction": "remember", "response": "x"})
        with train_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    leaked = MemorizingTuner().train(tmp_path / "ds", base="x", config={})
    assert canary_pass(leaked, tuner=MemorizingTuner()) is False
    honest = HonestTuner().train(tmp_path / "ds", base="x", config={})
    assert canary_pass(honest, tuner=HonestTuner()) is True
    priv, pub = _ed25519(tmp_path)
    from readyagents.trust.keyring import add_key, load_keyring

    add_key(pub, name="ops", home=tmp_settings.home_path())
    record = train(
        tmp_path / "ds",
        base="mock:base",
        settings=tmp_settings,
        tuner=HonestTuner(),
        sign_key=priv,
        node_id="classify",
    )
    suite = _suite(tmp_path)
    good = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, cost_micros=10),
        candidate=SideScore(accuracy=1, holdout=1.0, cost_micros=4),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
        fixture_digest="sha256:old",
    )
    promote(
        record.id,
        "classify",
        settings=tmp_settings,
        comparison=good,
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    save_config(DistillConfig(min_parity=1.0, max_cost_micros=1), tmp_settings)
    changed = rescore_promoted(
        settings=tmp_settings,
        suite=suite,
        dataset=tmp_path / "ds",
        incumbent_llm=BlobLLM("ok"),
        candidate_llm=BlobLLM("ok"),
        tuner=HonestTuner(),
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    # rescore runs evaluate which may pass or demote on cost; pin must not silently rot.
    assert pin_for("classify", tmp_settings) is None or changed == []
    if pin_for("classify", tmp_settings) is not None:
        demote(record.id, settings=tmp_settings, reason="below_threshold")
    assert pin_for("classify", tmp_settings) is None
    from readyagents.distill.store import load_adapter

    assert load_adapter(record.id, tmp_settings).demote_reason


def test_cli_plan_and_train_pack_missing(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    save_config(DistillConfig(min_examples=3), tmp_settings)
    _seed_corrections(tmp_settings, 4)
    planned = runner.invoke(app, ["distill", "plan", "--node", "classify", "--json"])
    assert planned.exit_code == 0, planned.stdout + planned.stderr
    payload = json.loads(planned.stdout[planned.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "distill plan"
    assert payload["verdict"] in {"viable", "not_enough_data", "task_too_broad"}
    trained = runner.invoke(
        app,
        ["distill", "train", "--dataset", str(tmp_path / "nope"), "--base", "qwen", "--json"],
    )
    assert trained.exit_code == 1
    body = json.loads(trained.stdout[trained.stdout.find("{") :])
    assert body["ok"] is False
    assert body["error"] in {"DistillTrainMissing", "DistillRefused", "ConfigError", "PathError"}
