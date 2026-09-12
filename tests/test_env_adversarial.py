"""Adversarial suite for V2-21 environments. Drive shipped APIs; fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.env.promote import promote, rollback_env
from readyagents.env.release import deploy, verify_release
from readyagents.env.run import run_in_environment
from readyagents.env.schema import EnvGatesSpec, EnvironmentSpec, load_env_file, refuse_secrets
from readyagents.env.store import EnvStore
from readyagents.errors import (
    ApprovalRequired,
    EnvGateApproval,
    EnvGateBenchmark,
    EnvGateEval,
    EnvGateFixtures,
    EnvGateHealth,
    EnvRefused,
)
from readyagents.run_store import JsonRunStore
from readyagents.trust.digest import digest_canonical, inspect_workflow
from readyagents.trust.keyring import add_key
from readyagents.workflow.state import RunState

runner = CliRunner()

_RUN_JSON_KEYS = frozenset(
    {
        "ok",
        "command",
        "record_version",
        "run_id",
        "workflow",
        "status",
        "started_at",
        "finished_at",
        "pending_node",
        "pending",
        "inputs",
        "outputs",
        "output_keys",
        "node_outputs",
        "node_results",
        "metadata",
        "errors",
        "usage",
        "provenance",
    }
)

_GATE_TYPES = (EnvGateEval, EnvGateFixtures, EnvGateBenchmark, EnvGateHealth, EnvGateApproval)


def _flow(tmp: Path, token: str, name: str = "echo.yaml") -> Path:
    path = tmp / name
    path.write_text(
        "name: echo\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        f"    template: '{token}'\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    return path


def _ed25519_pem(tmp: Path, name: str = "key") -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv_path = tmp / f"{name}.pem"
    pub_path = tmp / f"{name}.pub.pem"
    priv_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def _pass_hooks() -> dict:
    return {
        "eval": lambda: True,
        "fixtures": lambda: True,
        "benchmark": lambda: True,
        "health": lambda: 1.0,
    }


def _gated_spec() -> EnvironmentSpec:
    return EnvironmentSpec(
        gates=EnvGatesSpec(
            eval={"must_pass": True},
            fixtures={"no_regression": True},
            benchmark={"tolerance": "10%"},
            health={"min_score": 0.95},
            approval={"roles": ["release_manager"]},
        )
    )


def _write_env(tmp: Path, extra_prod: str = "") -> Path:
    path = tmp / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  staging: {}\n"
        "  prod:\n"
        "    secrets: prod\n"
        "    budget: {max_cost_usd: 50.00}\n"
        "    gates:\n"
        "      approval: {roles: [release_manager]}\n"
        f"{extra_prod}",
        encoding="utf-8",
    )
    return path


def _pointer_blob(store: EnvStore, name: str) -> str:
    current = store.current(name)
    return json.dumps(current, sort_keys=True, default=str)


def _payload(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


def test_promote_to_prod_refuses_without_approval_decision(tmp_path: Path, tmp_settings) -> None:
    """Privilege escalation: gates.approval.roles blocks promote without a decision."""
    _write_env(tmp_path)
    staging = _flow(tmp_path, "STAGING", "s.yaml")
    prod = _flow(tmp_path, "PROD", "p.yaml")
    deploy(staging, "staging", spec=EnvironmentSpec(), settings=tmp_settings, actor="ops")
    prod_pointer = deploy(prod, "prod", spec=EnvironmentSpec(), settings=tmp_settings, actor="ops")
    store = EnvStore(tmp_settings)
    before = _pointer_blob(store, "prod")
    spec = _gated_spec()
    hooks = _pass_hooks()

    with pytest.raises(ApprovalRequired) as missing:
        promote(
            staging,
            source="staging",
            target="prod",
            spec=spec,
            settings=tmp_settings,
            actor="release_manager",
            hooks=hooks,
        )
    assert missing.value.node_id == "promote"
    assert not isinstance(missing.value, _GATE_TYPES)
    assert "UNTRUSTED RELEASE DIFF" in missing.value.prompt
    assert _pointer_blob(store, "prod") == before
    assert store.current("prod") is not None
    assert store.current("prod")["digest"] == prod_pointer["digest"]

    with pytest.raises(ApprovalRequired) as empty:
        promote(
            staging,
            source="staging",
            target="prod",
            spec=spec,
            settings=tmp_settings,
            actor="ops",
            decisions={},
            hooks=hooks,
        )
    assert empty.value.node_id == "promote"
    assert not isinstance(empty.value, _GATE_TYPES)
    assert _pointer_blob(store, "prod") == before

    moved = promote(
        staging,
        source="staging",
        target="prod",
        spec=spec,
        settings=tmp_settings,
        actor="ops",
        decisions={"promote": "approve"},
        hooks=hooks,
    )
    assert moved["digest"] != prod_pointer["digest"]
    assert store.current("prod")["digest"] == moved["digest"]


def test_promote_wrong_node_or_force_flag_does_not_escalate(tmp_path: Path, tmp_settings) -> None:
    """--approve for another node, empty token, or force=True must not skip the gate."""
    _write_env(tmp_path)
    staging = _flow(tmp_path, "STAGING", "s.yaml")
    prod = _flow(tmp_path, "PROD", "p.yaml")
    deploy(staging, "staging", spec=EnvironmentSpec(), settings=tmp_settings)
    deploy(prod, "prod", spec=EnvironmentSpec(), settings=tmp_settings)
    store = EnvStore(tmp_settings)
    before = _pointer_blob(store, "prod")
    spec = _gated_spec()
    hooks = _pass_hooks()
    base = dict(
        source="staging",
        target="prod",
        spec=spec,
        settings=tmp_settings,
        actor="attacker",
        hooks=hooks,
    )

    for decisions in (
        {"other_node": "approve"},
        {"rollback": "approve"},
        {"env": "approve"},
        {"promote": ""},
        {"promote": "reject"},
    ):
        with pytest.raises(ApprovalRequired, match="UNTRUSTED RELEASE DIFF") as extra:
            promote(staging, decisions=decisions, **base)
        assert extra.value.node_id == "promote", decisions
        assert not isinstance(extra.value, _GATE_TYPES)
        assert _pointer_blob(store, "prod") == before, decisions

    try:
        promote(staging, decisions=None, force=True, **base)
    except TypeError:
        pass
    except ApprovalRequired as extra:
        assert extra.node_id == "promote"
        assert not isinstance(extra, _GATE_TYPES)
    else:
        raise AssertionError("force=True must not promote")
    assert _pointer_blob(store, "prod") == before


def test_signed_manifest_digest_tamper_refuses(tmp_path: Path, tmp_settings) -> None:
    _write_env(tmp_path)
    flow = _flow(tmp_path, "PINNED")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(
        flow, "prod", spec=EnvironmentSpec(), settings=tmp_settings, sign_key=priv, actor="ops"
    )
    verify_release(pointer, settings=tmp_settings, require_signed=True)
    manifest = Path(pointer["path"]) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["digest"] = "sha256:" + ("0" * 64)
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"
    with pytest.raises(EnvRefused) as run_err:
        run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert run_err.value.reason == "tamper"


def test_signed_manifest_pins_tamper_refuses(tmp_path: Path, tmp_settings) -> None:
    _write_env(tmp_path)
    flow = _flow(tmp_path, "PINNED")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(flow, "prod", spec=EnvironmentSpec(), settings=tmp_settings, sign_key=priv)
    manifest = Path(pointer["path"]) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pins = dict(data["pins"])
    pins["workflow"] = "sha256:" + ("1" * 64)
    data["pins"] = pins
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"
    with pytest.raises(EnvRefused) as run_err:
        run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert run_err.value.reason == "tamper"


def test_signed_manifest_pins_and_digest_together_still_refuses(
    tmp_path: Path, tmp_settings
) -> None:
    _write_env(tmp_path)
    flow = _flow(tmp_path, "PINNED")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(flow, "prod", spec=EnvironmentSpec(), settings=tmp_settings, sign_key=priv)
    manifest = Path(pointer["path"]) / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    pins = dict(data["pins"])
    pins["workflow"] = "sha256:" + ("2" * 64)
    data["pins"] = pins
    data["digest"] = digest_canonical(pins)
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"
    with pytest.raises(EnvRefused) as run_err:
        run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert run_err.value.reason == "tamper"


def test_snapshot_workflow_byte_tamper_refuses(tmp_path: Path, tmp_settings) -> None:
    _write_env(tmp_path)
    flow = _flow(tmp_path, "PINNED")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(flow, "prod", spec=EnvironmentSpec(), settings=tmp_settings, sign_key=priv)
    wf = Path(pointer["path"]) / "workflow.yaml"
    wf.write_text(wf.read_text(encoding="utf-8").replace("PINNED", "EVIL"), encoding="utf-8")
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"
    with pytest.raises(EnvRefused) as run_err:
        run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert run_err.value.reason == "tamper"


def test_delete_sig_and_rewrite_pins_is_still_tamper(tmp_path: Path, tmp_settings) -> None:
    """Unsigned forge: drop .sig, rewrite pins+digest to match mutated snapshot bytes."""
    from readyagents.env.release import _pins_from_snapshot

    _write_env(tmp_path)
    flow = _flow(tmp_path, "PINNED")
    priv, pub = _ed25519_pem(tmp_path)
    add_key(pub, name="ops", home=tmp_settings.home_path())
    pointer = deploy(flow, "prod", spec=EnvironmentSpec(), settings=tmp_settings, sign_key=priv)
    folder = Path(pointer["path"])
    sig = folder / "manifest.json.sig"
    assert sig.is_file()
    wf = folder / "workflow.yaml"
    wf.write_text(wf.read_text(encoding="utf-8").replace("PINNED", "EVIL"), encoding="utf-8")
    live = _pins_from_snapshot(folder)
    assert live["workflow"] == inspect_workflow(wf).digest
    manifest = folder / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["pins"] = live
    data["digest"] = digest_canonical(live)
    data["signed"] = False
    manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    sig.unlink()
    assert not sig.is_file()
    with pytest.raises(EnvRefused) as extra:
        verify_release(pointer, settings=tmp_settings)
    assert extra.value.reason == "tamper"
    with pytest.raises(EnvRefused) as run_err:
        run_in_environment(flow, "prod", settings=tmp_settings, persist=True)
    assert run_err.value.reason == "tamper"
    assert data["digest"] != pointer["digest"]


def test_forced_rollback_to_looser_release_requires_authorisation(
    tmp_path: Path, tmp_settings
) -> None:
    _write_env(tmp_path)
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    loose = _flow(tmp_path, "LOOSE", "loose.yaml")
    tight = _flow(tmp_path, "TIGHT", "tight.yaml")
    deploy(loose, "prod", spec=loaded.environments["prod"], settings=tmp_settings, actor="ops")
    v2 = deploy(tight, "prod", spec=loaded.environments["prod"], settings=tmp_settings, actor="ops")
    gated = EnvironmentSpec(gates=EnvGatesSpec(approval={"roles": ["release_manager"]}))
    store = EnvStore(tmp_settings)
    before = _pointer_blob(store, "prod")
    assert store.current("prod")["digest"] == v2["digest"]

    with pytest.raises(ApprovalRequired) as extra:
        rollback_env("prod", spec=gated, settings=tmp_settings, actor="ops")
    assert extra.value.node_id == "rollback"
    assert _pointer_blob(store, "prod") == before
    still = run_in_environment(tight, "prod", settings=tmp_settings, persist=True)
    assert still.output_keys["out"] == "TIGHT"
    assert still.metadata["release"] == v2["digest"]

    with pytest.raises(ApprovalRequired):
        rollback_env(
            "prod",
            spec=gated,
            settings=tmp_settings,
            actor="ops",
            decisions={"promote": "approve"},
        )
    assert _pointer_blob(store, "prod") == before
    assert (
        run_in_environment(tight, "prod", settings=tmp_settings, persist=True).output_keys["out"]
        == "TIGHT"
    )

    pointer = rollback_env(
        "prod",
        spec=gated,
        settings=tmp_settings,
        actor="ops",
        decisions={"rollback": "approve"},
        reason="manual",
    )
    assert pointer.get("rollback_reason") == "manual"
    rolled = run_in_environment(tight, "prod", settings=tmp_settings, persist=True)
    assert rolled.output_keys["out"] == "LOOSE"
    assert rolled.metadata["release"] != v2["digest"]


def test_guard_error_rate_rollback_is_not_the_forced_rollback_attack(
    tmp_path: Path, tmp_settings
) -> None:
    """Declared error_rate guard may roll back without HITL; that is not the attack."""
    _write_env(
        tmp_path,
        extra_prod="    rollback:\n      on: {error_rate_above: 0.05, window: 20}\n",
    )
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    spec = loaded.environments["prod"]
    assert spec.gates and spec.gates.approval
    v1 = _flow(tmp_path, "SAFE", "safe.yaml")
    v2 = _flow(tmp_path, "BAD", "bad.yaml")
    safe = deploy(v1, "prod", spec=spec, settings=tmp_settings)
    bad = deploy(v2, "prod", spec=spec, settings=tmp_settings)
    store = EnvStore(tmp_settings)
    env_store = JsonRunStore(store.runs_dir("prod"))
    for i in range(8):
        failed = RunState.start("echo", {}, run_id=f"fail{i}")
        failed.status = "failed"
        env_store.save(failed)
    state = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert state.output_keys["out"] == "BAD"
    assert state.metadata.get("rollback")
    assert store.current("prod")["digest"] == safe["digest"]
    nxt = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert nxt.output_keys["out"] == "SAFE"
    assert store.current("prod")["digest"] == safe["digest"]
    third = run_in_environment(v2, "prod", settings=tmp_settings, persist=True)
    assert third.output_keys["out"] == "SAFE"
    assert store.current("prod")["digest"] == safe["digest"]
    assert store.current("prod")["digest"] != bad["digest"]
    prev = store.previous("prod")
    assert prev is None or prev.get("digest") != bad["digest"]


def test_secret_literals_in_env_yaml_are_typed_refuse(tmp_path: Path, tmp_settings) -> None:
    cases = [
        {"environments": {"prod": {"token": "sk-abc12345xxxx"}}},
        {"environments": {"prod": {"notes": "ghp_abcdefghijk"}}},
        {"environments": {"prod": {"account": "AKIAIOSFODNN7EXAMPLE"}}},
        {
            "environments": {
                "prod": {"key": "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK8notreal\n"}
            }
        },
        {"environments": {"prod": {"password": "hunter2-is-not-a-scope!"}}},
        {"environments": {"prod": {"api_key": "n0t-a-path-or-scope!!"}}},
    ]
    for raw in cases:
        with pytest.raises(EnvRefused) as extra:
            refuse_secrets(raw)
        assert extra.value.reason == "secret", raw

    pem = tmp_path / "readyagents.env.yaml"
    pem.write_text(
        "version: 1\nenvironments:\n  prod:\n    token: sk-abcdefghijklmnop\n",
        encoding="utf-8",
    )
    with pytest.raises(EnvRefused) as loaded:
        load_env_file(pem, settings=tmp_settings)
    assert loaded.value.reason == "secret"


def test_scope_names_and_relative_paths_still_load(tmp_path: Path, tmp_settings) -> None:
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "prod.yaml").write_text("version: 1\ndefault: allow\n", encoding="utf-8")
    path = tmp_path / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  prod:\n"
        "    secrets: prod\n"
        "    policy: policy/prod.yaml\n"
        "  dev:\n"
        "    secrets: dev\n"
        "    policy: policy/prod.yaml\n",
        encoding="utf-8",
    )
    loaded = load_env_file(path, settings=tmp_settings)
    assert loaded is not None
    assert loaded.environments["prod"].secrets == "prod"
    assert loaded.environments["prod"].policy == "policy/prod.yaml"
    assert loaded.environments["dev"].secrets == "dev"
    refuse_secrets(
        {
            "environments": {
                "prod": {"secrets": "prod", "policy": "policy/prod.yaml"},
                "dev": {"secrets": "dev"},
            }
        }
    )


def test_nested_secret_smuggling_refuses_when_secret_value_matches(
    tmp_path: Path, tmp_settings
) -> None:
    with pytest.raises(EnvRefused) as listed:
        refuse_secrets({"environments": {"prod": {"gates": {"eval": {"tags": ["sk-abcdefghi"]}}}}})
    assert listed.value.reason == "secret"

    with pytest.raises(EnvRefused) as comment:
        refuse_secrets(
            {
                "environments": {
                    "prod": {"rollback": {"on": {"error_rate_above": 0.05, "note": "sk-abcdefghi"}}}
                }
            }
        )
    assert comment.value.reason == "secret"

    with pytest.raises(EnvRefused) as as_key:
        refuse_secrets({"environments": {"prod": {"sk-abcdefghijklmnop": "routing/prod.yaml"}}})
    assert as_key.value.reason == "secret"

    nested = tmp_path / "readyagents.env.yaml"
    nested.write_text(
        "version: 1\n"
        "environments:\n"
        "  prod:\n"
        "    gates:\n"
        "      eval:\n"
        "        suite: evals/prod.yaml\n"
        "        tags:\n"
        "          - sk-nestedlistsecret\n",
        encoding="utf-8",
    )
    with pytest.raises(EnvRefused) as extra:
        load_env_file(nested, settings=tmp_settings)
    assert extra.value.reason == "secret"


def test_shadow_cost_is_not_billed_to_primary_budget(tmp_path: Path, tmp_settings) -> None:
    path = tmp_path / "readyagents.env.yaml"
    path.write_text(
        "version: 1\n"
        "environments:\n"
        "  prod:\n"
        "    budget: {max_cost_usd: 50.00}\n"
        "    shadow:\n"
        "      budget: {max_cost_usd: 0.01}\n",
        encoding="utf-8",
    )
    loaded = load_env_file(settings=tmp_settings)
    assert loaded is not None
    spec = loaded.environments["prod"]
    assert spec.budget is not None and spec.shadow is not None and spec.shadow.budget is not None
    assert spec.budget.max_cost_usd != spec.shadow.budget.max_cost_usd
    v1 = _flow(tmp_path, "PRIMARY", "p.yaml")
    v2 = _flow(tmp_path, "SHADOW", "s.yaml")
    deploy(v1, "prod", spec=spec, settings=tmp_settings)
    control = run_in_environment(v1, "prod", settings=tmp_settings, persist=True, run_id="control")
    control_cost = int((control.usage or {}).get("cost_micros") or 0)
    deploy(v2, "prod", spec=spec, settings=tmp_settings, as_candidate=True)
    state = run_in_environment(v1, "prod", settings=tmp_settings, persist=True, run_id="primary1")
    assert state.output_keys["out"] == "PRIMARY"
    assert state.metadata.get("shadow_unused") is True
    assert "shadow_cost_micros" in state.metadata
    primary_cost = int((state.usage or {}).get("cost_micros") or 0)
    shadow_cost = int(state.metadata["shadow_cost_micros"] or 0)
    assert primary_cost == control_cost
    assert "shadow_cost_micros" not in (state.usage or {})
    assert state.metadata.get("shadow_cost_micros") == shadow_cost

    store = EnvStore(tmp_settings)
    primary_dir = store.runs_dir("prod")
    shadow_dir = store.env_dir("prod") / "shadow-runs"
    primary_files = list(primary_dir.glob("*.json"))
    shadow_files = list(shadow_dir.glob("*.json"))
    assert shadow_files
    primary_ids = {path.stem for path in primary_files}
    shadow_ids = {path.stem for path in shadow_files}
    assert "primary1" in primary_ids
    assert shadow_ids.isdisjoint(primary_ids)
    billed = 0
    for item in primary_files:
        blob = json.loads(item.read_text(encoding="utf-8"))
        assert blob.get("outputs", {}).get("out") != "SHADOW"
        assert blob.get("metadata", {}).get("unused") is not True
        assert blob.get("metadata", {}).get("shadow") is not True
        billed += int((blob.get("usage") or {}).get("cost_micros") or 0)
        if item.stem == "primary1":
            meta = blob.get("metadata") or {}
            assert int(meta.get("shadow_cost_micros") or 0) == shadow_cost
            assert blob.get("outputs", {}).get("out") == "PRIMARY"
            assert int((blob.get("usage") or {}).get("cost_micros") or 0) == primary_cost
    shadow_billed = 0
    for item in shadow_files:
        blob = json.loads(item.read_text(encoding="utf-8"))
        assert blob.get("metadata", {}).get("unused") is True
        assert blob.get("outputs", {}).get("out") == "SHADOW"
        assert item.parent == shadow_dir
        shadow_billed += int((blob.get("usage") or {}).get("cost_micros") or 0)
    assert billed == control_cost + primary_cost
    assert billed != billed + shadow_billed or shadow_billed == 0
    default_runs = tmp_settings.runs_dir()
    assert not (default_runs / "primary1.json").is_file()
    assert spec.budget.max_cost_usd == 50.00
    assert spec.shadow.budget.max_cost_usd == 0.01


def test_run_without_env_does_not_grow_json_envelope_keys(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow(tmp_path, "plain")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    first = runner.invoke(app, ["run", str(flow), "--json", "--no-persist"])
    assert first.exit_code == 0, first.stdout + first.stderr
    payload = _payload(first.stdout)
    assert payload["ok"] is True
    assert payload["command"] == "run"
    extra = set(payload) - _RUN_JSON_KEYS
    assert extra == set(), extra
    assert "environment" not in (payload.get("metadata") or {})
    assert "release" not in (payload.get("metadata") or {})
