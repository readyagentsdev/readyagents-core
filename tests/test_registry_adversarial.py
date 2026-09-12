"""Adversarial suite for V2-24 agent registry: escape, recon, tier abuse, overclaim."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import (
    AuthorizationError,
    ConfigError,
    RegistryRefused,
    RegistryTierApproval,
    RegistryTierCadence,
    RegistryTierEvidence,
    RegistryTierSigned,
)
from readyagents.policy import CallbackAuthorizer
from readyagents.registry.annotate import annotate
from readyagents.registry.card import model_card
from readyagents.registry.check import check
from readyagents.registry.discover import discover, iter_under, resolve_roots
from readyagents.registry.enforce import enforce_entry
from readyagents.registry.export import annex_viii, write_annex_viii
from readyagents.registry.scan import RegistryEntry, get_entry, scan
from readyagents.registry.schema import (
    DeclaredAgent,
    DerivedFacts,
    OwnerRole,
    RegistryConfig,
    ReviewSpec,
    TierRequirements,
)
from readyagents.registry.store import load_config, save_config
from readyagents.registry.view import list_agents, show_agent
from readyagents.workflow.runner import confine_under

ROOT = Path(__file__).resolve().parents[1]
SECRET_HOST = "secret-internal.example"
runner = CliRunner()


def _flow(
    tmp: Path,
    name: str = "fleet",
    filename: str = "flow.yaml",
    *,
    header: str = "",
    extra_nodes: str = "",
) -> Path:
    folder = tmp / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(
        f"name: {name}\n"
        f'version: "1"\n'
        f"{header}"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: ok\n"
        "    output_key: out\n"
        f"{extra_nodes}",
        encoding="utf-8",
    )
    return path


def _secret_flow(tmp: Path, name: str = "hooked") -> Path:
    return _flow(
        tmp,
        name=name,
        filename="hooked.yaml",
        extra_nodes=(
            "  - id: call\n"
            "    type: tool\n"
            "    tool: rest\n"
            "    arguments:\n"
            f"      url: https://{SECRET_HOST}/hooks\n"
            "    output_key: remote\n"
        ),
    )


def _enable(tmp_settings, roots: list[str] | None = None, **kwargs: object) -> RegistryConfig:
    cfg = RegistryConfig(roots=list(roots or ["agents"]), **kwargs)  # type: ignore[arg-type]
    save_config(cfg, tmp_settings)
    return cfg


def _fill(agent_id: str, tmp_settings, **overrides: object) -> None:
    updates = {
        "owner": "payments_ops",
        "backup_owner": "payments_backup",
        "purpose": "Triage disputes.",
        "risk_tier": "low",
        "data_classes": ["internal"],
        "retention": "30d",
        "review": "90d",
        "decommission_after": "2027-06-30",
    }
    updates.update(overrides)
    annotate(agent_id, updates, settings=tmp_settings)


def _plain(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text)
    return text.replace("\u2014", "-").replace("\u2013", "-")


def _unreleased_registry_bullet() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.find("## Unreleased")
    assert start >= 0, "CHANGELOG missing Unreleased section"
    section = text[start:]
    end = section.find("\n## ", 1)
    if end > 0:
        section = section[:end]
    marker = "- **Agent registry"
    idx = section.find(marker)
    assert idx >= 0, "CHANGELOG Unreleased missing Agent registry bullet"
    nxt = section.find("\n- ", idx + 1)
    return section[idx:] if nxt < 0 else section[idx:nxt]


# --- 1) SCAN ESCAPE ---------------------------------------------------------


def test_scan_ignores_workflow_outside_declared_roots(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path, "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shadow.yaml").write_text(
        "name: shadow\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: x\n"
        "    output_key: o\n",
        encoding="utf-8",
    )
    _enable(tmp_settings, ["agents"])
    names = {item.derived.name for item in scan(settings=tmp_settings)}
    assert "inside" in names
    assert "shadow" not in names


def test_symlink_escape_not_inventoried(tmp_path: Path, tmp_settings) -> None:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    _flow(tmp_path, "legit")

    outside = tmp_path.parent / f"reg-escape-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    secret = outside / "escaped.yaml"
    secret.write_text(
        "name: escaped\n"
        'version: "1"\n'
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: leak\n"
        "    output_key: out\n",
        encoding="utf-8",
    )

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    sibling_flow = sibling / "sib.yaml"
    sibling_flow.write_text(
        "name: sibling_escape\n"
        'version: "1"\n'
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: x\n"
        "    output_key: out\n",
        encoding="utf-8",
    )

    linked_out = False
    linked_sib = False
    try:
        (agents / "escaped.yaml").symlink_to(secret)
        linked_out = True
    except OSError:
        linked_out = False
    try:
        (agents / "sib.yaml").symlink_to(sibling_flow)
        linked_sib = True
    except OSError:
        linked_sib = False

    _enable(tmp_settings, ["agents"])
    entries = scan(settings=tmp_settings)
    names = {item.derived.name for item in entries}
    assert "legit" in names
    assert "escaped" not in names
    assert "sibling_escape" not in names

    if not linked_out or not linked_sib:
        with pytest.raises(ConfigError):
            confine_under(secret, tmp_settings.workspace_path(), what="registry root")
        with pytest.raises(ConfigError):
            resolve_roots([str(outside)], settings=tmp_settings)
        found = discover(["agents"], settings=tmp_settings)
        for paths in found.values():
            for path in paths:
                assert "escaped" not in path.name
                assert "sib.yaml" not in path.name or path.resolve().is_file()
                assert path.resolve().is_relative_to((tmp_path / "agents").resolve())
                assert outside.resolve() not in path.resolve().parents


def test_dotdot_and_absolute_roots_confined(tmp_path: Path, tmp_settings) -> None:
    leak_dir = tmp_path.parent / f"reg-root-leak-{tmp_path.name}"
    leak_dir.mkdir(exist_ok=True)
    (leak_dir / "leaked.yaml").write_text(
        "name: leaked_root\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: x\n"
        "    output_key: o\n",
        encoding="utf-8",
    )
    _flow(tmp_path, "safe")

    with pytest.raises(ConfigError):
        resolve_roots(["/"], settings=tmp_settings)
    with pytest.raises(ConfigError):
        resolve_roots(["../"], settings=tmp_settings)
    with pytest.raises(ConfigError):
        confine_under(Path("/"), tmp_settings.workspace_path(), what="registry root")
    with pytest.raises(ConfigError):
        confine_under(tmp_path / "..", tmp_settings.workspace_path(), what="registry root")

    _enable(tmp_settings, ["/"])
    with pytest.raises(ConfigError):
        scan(settings=tmp_settings)

    _enable(tmp_settings, ["../"])
    with pytest.raises(ConfigError):
        scan(settings=tmp_settings)

    _enable(tmp_settings, ["agents"])
    names = {item.derived.name for item in scan(settings=tmp_settings)}
    assert "safe" in names
    assert "leaked_root" not in names
    under = iter_under(tmp_path / "agents", pattern="*.yaml")
    assert all(p.resolve().is_relative_to((tmp_path / "agents").resolve()) for p in under)


def test_scan_does_not_execute_workflow_or_import_pack(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flow(tmp_path, "ok")
    pack = tmp_path / "agents" / "evilpack.py"
    pack.write_text("raise RuntimeError('pack imported during scan')\n", encoding="utf-8")
    called: list[str] = []

    def _boom_run(*_a, **_k):
        called.append("run")
        raise AssertionError("run_workflow_file must not be called during scan")

    def _boom_pack(*_a, **_k):
        called.append("pack")
        raise AssertionError("load_local_packs must not be called during scan")

    monkeypatch.setattr("readyagents.workflow.runner.run_workflow_file", _boom_run)
    monkeypatch.setattr("readyagents.packs.loader.load_local_packs", _boom_pack)
    _enable(tmp_settings)
    entries = scan(settings=tmp_settings)
    kinds = {item.derived.kind for item in entries}
    assert "workflow" in kinds
    assert "pack" in kinds
    assert called == []
    # Importing the pack path ourselves must still explode — proving scan did not.
    with pytest.raises(RuntimeError, match="pack imported"):
        namespace: dict[str, object] = {}
        exec(pack.read_text(encoding="utf-8"), namespace, namespace)


# --- 2) RECONNAISSANCE ------------------------------------------------------


def test_default_views_redact_egress_host(tmp_path: Path, tmp_settings) -> None:
    _secret_flow(tmp_path)
    _enable(tmp_settings)
    entry = scan(settings=tmp_settings)[0]
    agent_id = entry.agent_id
    assert SECRET_HOST in entry.derived.egress_hosts

    redacted = entry.as_dict(redact=True)
    assert SECRET_HOST not in json.dumps(redacted)
    assert redacted["derived"]["egress_hosts"] == ["[redacted-endpoint]"]

    rows = list_agents(settings=tmp_settings, redact=True)
    assert rows
    assert SECRET_HOST not in json.dumps(rows)
    assert all(host == "[redacted-endpoint]" for host in rows[0]["egress_hosts"])

    shown = show_agent(agent_id, settings=tmp_settings, redact=True)
    assert SECRET_HOST not in json.dumps(shown)

    card = model_card(agent_id, settings=tmp_settings, redact=True)
    assert SECRET_HOST not in json.dumps(card)

    annex = annex_viii(agent_id, settings=tmp_settings, redact=True)
    assert SECRET_HOST not in json.dumps(annex)


def test_write_annex_default_redact_draft_disclaimer(tmp_path: Path, tmp_settings) -> None:
    _secret_flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    dest = write_annex_viii(
        agent_id,
        tmp_path / "annex-viii-draft.json",
        settings=tmp_settings,
        yes=True,
    )
    assert "draft" in dest.name.lower()
    blob = dest.read_text(encoding="utf-8")
    assert SECRET_HOST not in blob
    payload = json.loads(blob)
    assert "DRAFT" in payload["disclaimer"]
    assert "not a legal filing" in payload["disclaimer"].lower()
    assert payload["legal_filing"] is False


def test_unredact_is_rbac_action(tmp_path: Path, tmp_settings) -> None:
    _secret_flow(tmp_path)
    _enable(tmp_settings)
    scan(settings=tmp_settings)
    authorizer = CallbackAuthorizer(lambda _actor, action, _resource: action != "registry.unredact")
    with pytest.raises(AuthorizationError) as denied:
        list_agents(
            settings=tmp_settings,
            redact=False,
            authorizer=authorizer,
            actor="ops",
        )
    assert denied.value.action == "registry.unredact"
    # Authorised path still returns the real host.
    allowed = CallbackAuthorizer(lambda *_a: True)
    raw = list_agents(
        settings=tmp_settings,
        redact=False,
        authorizer=allowed,
        actor="ops",
    )
    assert SECRET_HOST in raw[0]["egress_hosts"]


def test_owner_is_role_not_email_schema(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    emailish = "alice@evil.example"
    _fill(agent_id, tmp_settings, owner=emailish, backup_owner="backup_role")
    entry = get_entry(agent_id, settings=tmp_settings)
    assert entry is not None
    assert entry.declared.owner is not None
    dumped = entry.declared.owner.model_dump()
    assert set(dumped) == {"role"}
    assert dumped["role"] == emailish
    assert "email" not in OwnerRole.model_fields
    assert "name" not in OwnerRole.model_fields
    assert "role" in OwnerRole.model_fields
    assert entry.declared.owner.role == emailish


# --- 3) TIER MANIPULATION ---------------------------------------------------


def test_workflow_risk_tier_ignored_by_scan_and_enforce(tmp_path: Path, tmp_settings) -> None:
    _flow(
        tmp_path,
        "sneaky",
        header=(
            "risk_tier: high\n"
            "tiers:\n"
            "  high:\n"
            "    approval_gate: false\n"
            "    evidence_pack: false\n"
            "    signed_release: false\n"
            "    min_review_days: 1\n"
            "approval_gate: false\n"
        ),
    )
    _enable(tmp_settings)
    entry = scan(settings=tmp_settings)[0]
    assert entry.declared.risk_tier is None

    # No declared tier => enforce / check --enforce must not treat YAML high as high.
    enforce_entry(entry, load_config(tmp_settings))
    check(settings=tmp_settings, enforce=True)

    cfg_before = load_config(tmp_settings)
    assert cfg_before.tiers["high"].approval_gate is True
    assert cfg_before.tiers["high"].evidence_pack is True
    assert cfg_before.tiers["high"].signed_release is True
    assert cfg_before.tiers["high"].min_review_days == 90

    # Annotating high still uses registry config, not workflow-weakened tiers.
    _fill(agent_id=entry.agent_id, tmp_settings=tmp_settings, risk_tier="high", review="180d")
    loaded = get_entry(entry.agent_id, settings=tmp_settings)
    assert loaded is not None
    cfg_after = load_config(tmp_settings)
    assert cfg_after.tiers["high"].approval_gate is True
    with pytest.raises(RegistryTierApproval) as ap:
        enforce_entry(loaded, cfg_after)
    assert ap.value.reason == "approval_gate"
    with pytest.raises(RegistryTierApproval):
        check(settings=tmp_settings, enforce=True)


def test_each_high_tier_requirement_fails_independently() -> None:
    cfg = RegistryConfig(
        roots=["agents"],
        tiers={
            "high": TierRequirements(
                approval_gate=True,
                evidence_pack=True,
                signed_release=True,
                min_review_days=90,
            )
        },
    )
    entry = RegistryEntry(
        agent_id="agt_test",
        declared=DeclaredAgent(
            agent_id="agt_test",
            risk_tier="high",
            review=ReviewSpec(cadence="180d", last="2026-01-01"),
        ),
        derived=DerivedFacts(
            kind="workflow",
            path="agents/x.yaml",
            name="x",
            version="1",
            digest="abc",
            approval_gates=False,
            evidence_runs=0,
            signed_release=False,
        ),
    )
    with pytest.raises(RegistryTierApproval) as ap:
        enforce_entry(entry, cfg)
    assert ap.value.reason == "approval_gate"

    entry.derived.approval_gates = True
    with pytest.raises(RegistryTierEvidence) as ev:
        enforce_entry(entry, cfg)
    assert ev.value.reason == "evidence_pack"

    entry.derived.evidence_runs = 1
    with pytest.raises(RegistryTierSigned) as sg:
        enforce_entry(entry, cfg)
    assert sg.value.reason == "signed_release"

    entry.derived.signed_release = True
    with pytest.raises(RegistryTierCadence) as cd:
        enforce_entry(entry, cfg)
    assert cd.value.reason == "review_cadence"


# --- 4) OVERCLAIM -----------------------------------------------------------


def test_docs_changelog_annex_do_not_overclaim(tmp_path: Path, tmp_settings) -> None:
    registry_doc = (ROOT / "docs" / "registry.md").read_text(encoding="utf-8")
    bullet = _unreleased_registry_bullet()
    sources = {
        "docs/registry.md": registry_doc,
        "CHANGELOG Unreleased registry": bullet,
    }
    forbidden = (
        re.compile(r"\bis\s+an\s+Article\s+49\b", re.I),
        re.compile(r"\bcompliant with (the )?EU AI Act\b", re.I),
        re.compile(r"\bprovides EU AI Act compliance\b", re.I),
        re.compile(r"\bguarantees (EU AI Act )?compliance\b", re.I),
        re.compile(r"\bcertified\b", re.I),
        re.compile(r"\bthis (export|record|annex)\s+is\s+an?\s+Article\s+49\b", re.I),
        re.compile(r"\bArticle\s+49\s+filing\b(?!\s*,)", re.I),
    )
    near_negation = re.compile(
        r"\b(not|never|no|without|nor|neither|cannot|isn't|are not|is not|not a claim)\b",
        re.I,
    )
    hits: list[str] = []
    for label, raw in sources.items():
        plain = _plain(raw)
        for pattern in forbidden:
            for match in pattern.finditer(plain):
                window = plain[max(0, match.start() - 100) : match.end() + 48]
                if near_negation.search(window):
                    continue
                if "not a claim of" in window.lower() and "compliance" in match.group(0).lower():
                    continue
                if "not a certification" in window.lower():
                    continue
                if "not an article 49" in window.lower() or "not a legal filing" in window.lower():
                    continue
                hits.append(f"{label}: {match.group(0)!r} in …{window.strip()}…")
        lower = plain.lower()
        # Must not affirm compliance / certification / Article 49 filing.
        assert "is an article 49" not in lower
        assert "compliant with the eu ai act" not in lower
        assert "makes you compliant" not in lower
        assert re.search(r"(?<!not a )(?<!not )\bcertification\b", lower) is None or (
            "not a certification" in lower or "not certification" in lower
        )
    assert not hits, "overclaim:\n" + "\n".join(hits)
    assert "draft" in _plain(registry_doc).lower()
    assert "draft" in _plain(bullet).lower()
    assert (
        "not a legal filing" in _plain(bullet).lower()
        or "compliance claim" in _plain(bullet).lower()
    )

    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    record = annex_viii(agent_id, settings=tmp_settings)
    assert record["legal_filing"] is False
    assert record["article_49_registration"] is False
    assert "DRAFT" in record["disclaimer"]
    assert "DRAFT" in record["header"]
    assert "not a legal filing" in record["disclaimer"].lower()
    assert "not a legal filing" in record["header"].lower()
    blob = json.dumps(record).lower()
    assert "is an article 49" not in blob
    assert record.get("compliance_claim") is False


def test_annex_filename_without_draft_refused(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    with pytest.raises(RegistryRefused) as lab:
        write_annex_viii(agent_id, tmp_path / "filing.json", settings=tmp_settings, yes=True)
    assert lab.value.reason == "draft_label"


def test_model_card_disclaimer_and_unknown_fields(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    card = model_card(agent_id, settings=tmp_settings)
    assert "not a claim" in card["disclaimer"].lower()
    assert (
        "compliance" in card["disclaimer"].lower() or "certification" in card["disclaimer"].lower()
    )
    assert card["purpose"] == "unknown: purpose"
    assert card["owner"] == "unknown: owner"
    assert card["risk_tier"] == "unknown: risk_tier"
    for item in card["unknowns"]:
        assert isinstance(item, str)
    for limitation in card["limitations"]:
        assert limitation.startswith("unknown: ") or limitation == "none named from local evidence"


def test_cli_registry_redacts_and_exports_draft(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secret_flow(tmp_path)
    _enable(tmp_settings)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()

    scanned = runner.invoke(app, ["registry", "scan", "--json"])
    assert scanned.exit_code == 0, scanned.stdout + scanned.stderr
    assert SECRET_HOST not in scanned.stdout
    payload = json.loads(scanned.stdout[scanned.stdout.find("{") :])
    agent_id = payload["agents"][0]["agent_id"]

    listed = runner.invoke(app, ["registry", "list", "--json"])
    assert listed.exit_code == 0
    assert SECRET_HOST not in listed.stdout
    assert "[redacted-endpoint]" in listed.stdout

    shown = runner.invoke(app, ["registry", "show", agent_id, "--json"])
    assert shown.exit_code == 0
    assert SECRET_HOST not in shown.stdout

    carded = runner.invoke(app, ["registry", "card", agent_id, "--json"])
    assert carded.exit_code == 0
    assert SECRET_HOST not in carded.stdout

    out = tmp_path / "annex-viii-draft.json"
    exported = runner.invoke(
        app,
        ["registry", "export", agent_id, "--format", "annex-viii", "--out", str(out), "--yes"],
    )
    assert exported.exit_code == 0, exported.stdout + exported.stderr
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert SECRET_HOST not in body
    assert "DRAFT" in body
    assert "not a legal filing" in body.lower()
