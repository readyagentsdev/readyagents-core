"""Adversarial distillation: canary leak, consent bypass, forged adapter, sovereign, overclaim."""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.distill.adapters import require_signed
from readyagents.distill.canary import CANARY_TOKEN, canary_pass, plant_secret
from readyagents.distill.dataset import build_dataset
from readyagents.distill.evaluate import evaluate
from readyagents.distill.plan import plan
from readyagents.distill.promote import demote, promote
from readyagents.distill.schema import DistillConfig, EvalComparison, SideScore
from readyagents.distill.store import pin_for, save_config
from readyagents.distill.train import train
from readyagents.errors import DistillCanary, DistillSovereignHosted, DistillUnsigned
from readyagents.feedback.capture import record_human_correction
from readyagents.run_store import open_run_store
from readyagents.workflow.schema import NodeSpec
from readyagents.workflow.state import RunState

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()

_NEAR_NEGATION = re.compile(
    r"\b(not|never|no|without|nor|neither|cannot|can't|don't|doesn't|"
    r"isn't|won't|do not|does not|is not|are not|was not|were not|"
    r"not a|no quality)\b",
    re.I,
)
_FORBIDDEN_OVERCLAIM = (
    ("state of the art", re.compile(r"state\s+of\s+the\s+art", re.I)),
    ("beats GPT", re.compile(r"beats\s+GPT", re.I)),
    ("leaderboard", re.compile(r"\bleaderboard\b", re.I)),
    ("SOTA", re.compile(r"\bSOTA\b")),
    ("we are compliant", re.compile(r"we\s+are\s+compliant", re.I)),
)


def _gate(*, consent: str | None = "internal_training") -> NodeSpec:
    feedback: dict = {
        "allow_edit": True,
        "labels": ["spam", "ham"],
    }
    if consent is not None:
        feedback["consent"] = consent
    return NodeSpec.model_validate(
        {
            "id": "classify",
            "type": "approval",
            "prompt": "ok?",
            "then": "ok",
            "else": "no",
            "feedback": feedback,
        }
    )


def _seed_corrections(
    tmp_settings,
    n: int,
    *,
    consented: bool = True,
    label: str = "spam",
    secret: str | None = None,
    original_prefix: str = "msg",
    node_id: str = "classify",
) -> None:
    store = open_run_store(tmp_settings)
    try:
        for i in range(n):
            state = RunState.start("distill_wf", {"i": i})
            if consented:
                state.metadata["data_policy"] = {"scopes": ["internal_training"]}
                node = _gate(consent="internal_training")
            else:
                # No consent on the node so capture does not invent data_policy scopes.
                node = _gate(consent=None)
            original = f"{original_prefix}-{i}"
            edited = f"label-{label}-{i}"
            if secret and i == 0:
                edited = f"{edited} {secret}"
            record_human_correction(
                state,
                node=node,
                actor_role="reviewer",
                reason="fix",
                original=original,
                edited=edited,
                label=label if i % 2 == 0 else "ham",
                decision_id=f"d{i}",
                model="openai:gpt-4o-mini",
            )
            assert node.id == node_id
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


def _plain(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text)
    return text.replace("\u2014", "-").replace("\u2013", "-")


def _is_negated(plain: str, start: int, end: int) -> bool:
    before = plain[max(0, start - 500) : start]
    after = plain[end : min(len(plain), end + 48)]
    near = before[-180:] + " " + after[:32]
    return _NEAR_NEGATION.search(near) is not None


def _unreleased_distill_bullet() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    marker = "## Unreleased"
    start = text.find(marker)
    assert start >= 0, "CHANGELOG missing Unreleased section"
    rest = text[start:]
    next_h2 = re.search(r"\n## (?!Unreleased)", rest)
    section = rest[: next_h2.start()] if next_h2 else rest
    match = re.search(
        r"(?ms)^-\s+\*\*Local distillation\.\*\*.*?(?=^\-\s+\*\*|\Z)",
        section,
    )
    assert match, "CHANGELOG Unreleased missing Local distillation bullet"
    return match.group(0)


def _good_comparison() -> EvalComparison:
    return EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0, cost_micros=10),
        candidate=SideScore(accuracy=1, holdout=1.0, cost_micros=4),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=True,
        fixture_digest="sha256:abc",
    )


# --- 1) MEMORISATION CANARY -------------------------------------------------


def test_memorisation_canary_honest_vs_leaky(tmp_path: Path, tmp_settings) -> None:
    _seed_corrections(tmp_settings, 6, secret=CANARY_TOKEN)
    ds = build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)
    assert ds.hash
    train_path = tmp_path / "ds" / "train.jsonl"
    blob = train_path.read_text(encoding="utf-8")
    if CANARY_TOKEN not in blob:
        row = plant_secret({"id": "canary", "instruction": "remember", "response": "x"})
        with train_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    assert CANARY_TOKEN in train_path.read_text(encoding="utf-8")

    leaked = MemorizingTuner().train(tmp_path / "ds", base="x", config={})
    # Distinct path: HonestTuner also writes trained.adapter.json under the dataset.
    leaked_copy = tmp_path / "leaked.adapter.json"
    leaked_copy.write_bytes(Path(leaked).read_bytes())
    assert canary_pass(leaked_copy, tuner=MemorizingTuner()) is False

    honest = HonestTuner().train(tmp_path / "ds", base="x", config={})
    assert canary_pass(honest, tuner=HonestTuner()) is True

    suite = _suite(tmp_path)
    report = evaluate(
        suite=suite,
        dataset=tmp_path / "ds",
        adapter=leaked_copy,
        tuner=MemorizingTuner(),
        settings=tmp_settings,
    )
    assert report.canary_passed is False


def test_promote_refuses_failed_canary(tmp_path: Path, tmp_settings) -> None:
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
    leak = EvalComparison(
        incumbent=SideScore(accuracy=1, holdout=1.0),
        candidate=SideScore(accuracy=1, holdout=1.0),
        holdout={"named": True, "incumbent": 1.0, "candidate": 1.0},
        canary_passed=False,
    )
    with pytest.raises(DistillCanary) as caught:
        promote(
            record.id,
            "classify",
            settings=tmp_settings,
            comparison=leak,
            keyring=load_keyring(home=tmp_settings.home_path()),
        )
    assert caught.value.reason == "canary"
    assert pin_for("classify", tmp_settings) is None


# --- 2) CONSENT BYPASS ------------------------------------------------------


def test_consent_bypass_scope_arg_cannot_admit_unconsented(tmp_path: Path, tmp_settings) -> None:
    marker = "UNCONSENTED_ORIGINAL_LEAK_TOKEN"
    _seed_corrections(
        tmp_settings,
        3,
        consented=False,
        original_prefix=marker,
    )
    # Also seed consented rows so the dataset is non-empty and splits exist.
    _seed_corrections(tmp_settings, 4, consented=True, original_prefix="ok")

    planned = plan(
        "classify",
        settings=tmp_settings,
        scope="internal_training",
        min_examples=1,
    )
    assert planned.consented >= 1

    manifest = build_dataset(
        "classify",
        tmp_path / "ds",
        settings=tmp_settings,
        seed=1,
        yes=True,
        scope="internal_training",
    )
    assert manifest.consent_from_recorded_policy is True
    assert manifest.counts.get("excluded_unconsented", 0) >= 1
    texts = (tmp_path / "ds" / "train.jsonl").read_text(encoding="utf-8")
    texts += (tmp_path / "ds" / "holdout.jsonl").read_text(encoding="utf-8")
    texts += (tmp_path / "ds" / "validation.jsonl").read_text(encoding="utf-8")
    assert marker not in texts
    assert "UNCONSENTED_ORIGINAL_LEAK_TOKEN-0" not in texts


def test_build_dataset_has_no_force_or_include_unconsented() -> None:
    params = inspect.signature(build_dataset).parameters
    assert "force" not in params
    assert "include_unconsented" not in params
    help_text = runner.invoke(app, ["distill", "dataset", "--help"])
    assert help_text.exit_code == 0
    assert "--force" not in help_text.stdout
    assert "--include-unconsented" not in help_text.stdout
    assert "--allow-unconsented" not in help_text.stdout


# --- 3) UNSIGNED / FORGED ADAPTER -------------------------------------------


def test_unsigned_adapter_require_signed_raises(tmp_path: Path, tmp_settings) -> None:
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
    with pytest.raises(DistillUnsigned) as caught:
        require_signed(record.id, settings=tmp_settings)
    assert caught.value.reason == "unsigned"


def test_tampered_signed_adapter_refused(tmp_path: Path, tmp_settings) -> None:
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
    keyring = load_keyring(home=tmp_settings.home_path())
    require_signed(record.id, settings=tmp_settings, keyring=keyring)

    artifact = Path(record.path)
    artifact.write_bytes(artifact.read_bytes() + b"\nTAMPERED\n")
    with pytest.raises(DistillUnsigned) as caught:
        require_signed(record.id, settings=tmp_settings, keyring=keyring)
    assert caught.value.reason == "unsigned"


# --- 4) SOVEREIGN LEAKAGE ---------------------------------------------------


def test_sovereign_refuses_hosted_tuner_and_has_no_live_client(
    tmp_path: Path, tmp_settings
) -> None:
    tmp_settings.sovereign = True
    _seed_corrections(tmp_settings, 4)
    build_dataset("classify", tmp_path / "ds", settings=tmp_settings, seed=1, yes=True)

    with pytest.raises(DistillSovereignHosted) as via_method:
        train(
            tmp_path / "ds",
            base="openai:gpt-4o-mini",
            settings=tmp_settings,
            tuner=HostedTuner(),
        )
    assert via_method.value.reason == "sovereign_hosted"

    with pytest.raises(DistillSovereignHosted) as via_flag:
        train(
            tmp_path / "ds",
            base="openai:gpt-4o-mini",
            settings=tmp_settings,
            tuner=HonestTuner(),
            hosted=True,
        )
    assert via_flag.value.reason == "sovereign_hosted"

    train_mod = importlib.import_module("readyagents.distill.train")
    src = Path(train_mod.__file__).read_text(encoding="utf-8")
    assert "openai.fine_tuning" not in src
    assert "anthropic.fine_tuning" not in src


# --- 5) OVERCLAIM -----------------------------------------------------------


def test_docs_and_changelog_do_not_overclaim_distillation() -> None:
    doc = (ROOT / "docs" / "distillation.md").read_text(encoding="utf-8")
    bullet = _unreleased_distill_bullet()
    sources = {
        "docs/distillation.md": doc,
        "CHANGELOG Unreleased distillation": bullet,
    }
    hits: list[str] = []
    for label, raw in sources.items():
        plain = _plain(raw)
        for name, pattern in _FORBIDDEN_OVERCLAIM:
            for match in pattern.finditer(plain):
                if _is_negated(plain, match.start(), match.end()):
                    continue
                lo = max(0, match.start() - 60)
                hi = min(len(plain), match.end() + 40)
                snippet = re.sub(r"\s+", " ", plain[lo:hi]).strip()
                hits.append(f"{label}: {name!r} in …{snippet}…")
    assert not hits, "overclaim:\n" + "\n".join(hits)

    doc_plain = _plain(doc).lower()
    bullet_plain = _plain(bullet).lower()
    for body in (doc_plain, bullet_plain):
        assert "core never trains" in body or "core never trains" in body.replace("`", "")
        assert "holdout" in body
        assert (
            "not a quality claim" in body
            or "no quality claim" in body
            or "mandatory holdout" in body
            or "holdout is mandatory" in body
            or "mandatory holdout" in body.replace("**", "")
        )
    # Docs must state holdout is mandatory / not a quality claim beyond fixtures.
    assert "not a quality claim" in doc_plain or "not a quality claim" in bullet_plain
    assert "mandatory" in doc_plain or "mandatory holdout" in bullet_plain
    assert "core never trains" in doc_plain
    assert "core never trains" in bullet_plain


# --- CLI surfaces -----------------------------------------------------------


def test_cli_distill_and_models_adapters(tmp_path: Path, tmp_settings, monkeypatch) -> None:
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

    ds = runner.invoke(
        app,
        [
            "distill",
            "dataset",
            "--node",
            "classify",
            "--out",
            str(tmp_path / "cli-ds"),
            "--yes",
            "--json",
        ],
    )
    assert ds.exit_code == 0, ds.stdout + ds.stderr
    ds_body = json.loads(ds.stdout[ds.stdout.find("{") :])
    assert ds_body["ok"] is True
    assert ds_body["command"] == "distill dataset"

    listed = runner.invoke(app, ["models", "adapters", "list", "--json"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    adapters = json.loads(listed.stdout[listed.stdout.find("{") :])
    assert adapters["ok"] is True
    assert adapters["command"] == "models adapters list"
    assert "adapters" in adapters

    priv, pub = _ed25519(tmp_path)
    from readyagents.trust.keyring import add_key, load_keyring

    add_key(pub, name="ops", home=tmp_settings.home_path())
    record = train(
        tmp_path / "cli-ds",
        base="mock:base",
        settings=tmp_settings,
        tuner=HonestTuner(),
        sign_key=priv,
        node_id="classify",
    )
    promote(
        record.id,
        "classify",
        settings=tmp_settings,
        comparison=_good_comparison(),
        keyring=load_keyring(home=tmp_settings.home_path()),
    )
    assert pin_for("classify", tmp_settings) is not None
    demoted = demote(record.id, settings=tmp_settings, reason="adversarial")
    assert demoted.status == "demoted"
    assert demoted.demote_reason == "adversarial"
    assert pin_for("classify", tmp_settings) is None

    shown = runner.invoke(app, ["models", "adapters", "show", record.id, "--json"])
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    show_body = json.loads(shown.stdout[shown.stdout.find("{") :])
    assert show_body["ok"] is True
    assert show_body["adapter"]["id"] == record.id
    assert show_body["adapter"]["status"] == "demoted"
