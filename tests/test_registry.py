"""Agent registry: scan, identity, check, enforce, cards, export, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.env.promote import promote
from readyagents.env.release import deploy
from readyagents.env.schema import EnvironmentSpec
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
from readyagents.registry.enforce import enforce_entry, enforce_promote
from readyagents.registry.export import annex_viii, write_annex_viii
from readyagents.registry.scan import RegistryEntry, get_entry, scan
from readyagents.registry.schema import (
    DeclaredAgent,
    DerivedFacts,
    RegistryConfig,
    ReviewSpec,
    TierRequirements,
)
from readyagents.registry.store import save_config
from readyagents.registry.view import list_agents, stats
from readyagents.run_store import open_run_store
from readyagents.workflow.state import RunState

runner = CliRunner()


def _flow(
    tmp: Path,
    name: str = "fleet",
    filename: str = "flow.yaml",
    extra_nodes: str = "",
    header: str = "",
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


def _rich(tmp: Path) -> Path:
    folder = tmp / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rich.yaml"
    path.write_text(
        "name: rich_agent\n"
        'version: "3"\n'
        "default_model: openai:gpt-4o-mini\n"
        "on_pause_url: https://hooks.internal.example/pause\n"
        "budget:\n"
        "  max_cost_usd: 1.5\n"
        "memory_scopes: [workflow:rich]\n"
        "nodes:\n"
        "  - id: talk\n"
        "    type: agent\n"
        "    model: openai:gpt-4o-mini\n"
        "    prompt: hi\n"
        "    output_key: text\n"
        "    next: gate\n"
        "  - id: gate\n"
        "    type: approval\n"
        "    prompt: go?\n"
        "    then: call\n"
        "    else: stop\n"
        "  - id: call\n"
        "    type: tool\n"
        "    tool: rest\n"
        "    arguments:\n"
        "      url: https://api.partner.example/v1\n"
        "    output_key: remote\n"
        "    next: mem\n"
        "  - id: mem\n"
        "    type: memory\n"
        "    op: write\n"
        "    scope: workflow:rich\n"
        "    text: '{{text}}'\n"
        "    output_key: saved\n"
        "  - id: stop\n"
        "    type: transform\n"
        "    template: stop\n"
        "    output_key: out\n",
        encoding="utf-8",
    )
    return path


def _record_run(settings, workflow_name: str, *, status: str, cost_micros: int) -> None:
    store = open_run_store(settings)
    try:
        state = RunState.start(workflow_name, {})
        state.status = status
        state.usage = {"cost_micros": int(cost_micros)}
        store.save(state)
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()


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


def test_scan_declared_roots_only(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path, "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "shadow.yaml").write_text(
        "name: shadow\nnodes:\n  - id: t\n    type: transform\n    template: x\n    output_key: o\n",
        encoding="utf-8",
    )
    _enable(tmp_settings, ["agents"])
    entries = scan(settings=tmp_settings)
    names = {item.derived.name for item in entries}
    assert "inside" in names
    assert "shadow" not in names


def test_scan_does_not_run_or_import_packs(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _flow(tmp_path)
    pack = tmp_path / "agents" / "evilpack.py"
    pack.write_text("raise RuntimeError('pack imported during scan')\n", encoding="utf-8")
    called: list[str] = []
    monkeypatch.setattr(
        "readyagents.workflow.runner.run_workflow_file",
        lambda *a, **k: called.append("run") or None,
    )
    monkeypatch.setattr(
        "readyagents.packs.loader.load_local_packs",
        lambda *a, **k: called.append("pack") or [],
    )
    _enable(tmp_settings)
    entries = scan(settings=tmp_settings)
    kinds = {item.derived.kind for item in entries}
    assert "pack" in kinds
    assert called == []


def test_ids_stable_across_rename_move_version_copy_is_new(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path, "stable", "a.yaml")
    _enable(tmp_settings)
    first = scan(settings=tmp_settings)
    assert len(first) == 1
    agent_id = first[0].agent_id
    assert agent_id.startswith("agt_")

    dest = path.parent / "renamed.yaml"
    path.rename(dest)
    moved = scan(settings=tmp_settings)
    assert {row.agent_id for row in moved} == {agent_id}

    dest.write_text(
        dest.read_text(encoding="utf-8").replace('version: "1"', 'version: "2"'), encoding="utf-8"
    )
    versioned = scan(settings=tmp_settings)
    assert {row.agent_id for row in versioned} == {agent_id}
    assert versioned[0].derived.version == "2"

    copy = dest.parent / "copy.yaml"
    copy.write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
    both = scan(settings=tmp_settings)
    ids = {row.agent_id for row in both}
    assert agent_id in ids
    assert len(ids) == 2


def test_derived_facts_cover_nodes_tools_hosts_gates(tmp_path: Path, tmp_settings) -> None:
    _rich(tmp_path)
    _enable(tmp_settings)
    entries = scan(settings=tmp_settings)
    rich = next(item for item in entries if item.derived.name == "rich_agent")
    facts = rich.derived
    assert "agent" in facts.node_types
    assert "approval" in facts.node_types
    assert "memory" in facts.node_types
    assert "rest" in facts.tools
    assert "rest" in facts.connectors
    assert "openai:gpt-4o-mini" in facts.models
    assert "hooks.internal.example" in facts.egress_hosts
    assert "api.partner.example" in facts.egress_hosts
    assert facts.approval_gates is True
    assert facts.memory_or_knowledge is True
    assert facts.budget_max_cost_usd == 1.5
    assert facts.version == "3"


def test_annotate_missing_only_and_validation(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    missing = annotate(agent_id, {"purpose": "p"}, settings=tmp_settings)
    assert "purpose" not in missing
    assert "owner" in missing
    still = annotate(agent_id, {"purpose": "other"}, settings=tmp_settings)
    assert still == missing
    entry = get_entry(agent_id, settings=tmp_settings)
    assert entry is not None
    assert entry.declared.purpose == "p"
    with pytest.raises(RegistryRefused) as tier:
        annotate(agent_id, {"risk_tier": "critical"}, settings=tmp_settings)
    assert tier.value.reason == "tier"
    with pytest.raises(RegistryRefused) as klass:
        annotate(agent_id, {"data_classes": ["nuclear"]}, settings=tmp_settings)
    assert klass.value.reason == "data_class"
    with pytest.raises(RegistryRefused) as field:
        annotate(agent_id, {"egress_hosts": ["evil.example"]}, settings=tmp_settings)
    assert field.value.reason == "field"


def test_drift_overdue_unused(tmp_path: Path, tmp_settings) -> None:
    path = _flow(tmp_path, "driftme")
    _enable(tmp_settings, unused_after="1d")
    agent_id = scan(settings=tmp_settings)[0].agent_id
    _fill(agent_id, tmp_settings, review={"cadence": "30d", "last": "2020-01-01"})
    report = check(settings=tmp_settings, now=datetime(2026, 9, 1, tzinfo=UTC))
    assert any(row["agent_id"] == agent_id for row in report.overdue)
    assert any(row["agent_id"] == agent_id for row in report.unused)

    path.write_text(
        "name: driftme\n"
        'version: "1"\n'
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: ok\n"
        "    output_key: out\n"
        "    next: egress\n"
        "  - id: egress\n"
        "    type: tool\n"
        "    tool: rest\n"
        "    arguments:\n"
        "      url: https://new.example/api\n"
        "    output_key: remote\n",
        encoding="utf-8",
    )
    scan(settings=tmp_settings)
    drifted = check(settings=tmp_settings)
    assert any(
        row["agent_id"] == agent_id and "egress_hosts" in row["changes"] for row in drifted.drift
    )


def test_tier_requirements_distinct_and_not_from_workflow(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path, "sneaky", header="risk_tier: high\n")
    _enable(tmp_settings)
    entry = scan(settings=tmp_settings)[0]
    assert entry.declared.risk_tier is None
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
    high = RegistryEntry(
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
        enforce_entry(high, cfg)
    assert ap.value.reason == "approval_gate"
    high.derived.approval_gates = True
    with pytest.raises(RegistryTierEvidence) as ev:
        enforce_entry(high, cfg)
    assert ev.value.reason == "evidence_pack"
    high.derived.evidence_runs = 1
    with pytest.raises(RegistryTierSigned) as sg:
        enforce_entry(high, cfg)
    assert sg.value.reason == "signed_release"
    high.derived.signed_release = True
    with pytest.raises(RegistryTierCadence) as cd:
        enforce_entry(high, cfg)
    assert cd.value.reason == "review_cadence"


def test_enforce_gates_promote_when_enabled(tmp_path: Path, tmp_settings) -> None:
    (tmp_path / "readyagents.env.yaml").write_text(
        "version: 1\nenvironments:\n  staging: {}\n  prod: {}\n",
        encoding="utf-8",
    )
    flow = _flow(tmp_path, "promo")
    deploy(flow, "staging", spec=EnvironmentSpec(), settings=tmp_settings)
    _enable(tmp_settings, enforce=True)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    _fill(agent_id, tmp_settings, risk_tier="high")
    with pytest.raises(RegistryTierApproval):
        promote(
            flow,
            source="staging",
            target="prod",
            spec=EnvironmentSpec(),
            settings=tmp_settings,
        )
    save_config(RegistryConfig(roots=["agents"], enforce=False), tmp_settings)
    pointer = promote(
        flow,
        source="staging",
        target="prod",
        spec=EnvironmentSpec(),
        settings=tmp_settings,
    )
    assert pointer.get("digest")


def test_enforce_promote_noop_when_off(tmp_path: Path, tmp_settings) -> None:
    flow = _flow(tmp_path)
    _enable(tmp_settings, enforce=False)
    scan(settings=tmp_settings)
    enforce_promote(flow, settings=tmp_settings)


def test_promote_copy_uses_copy_declared_tier(tmp_path: Path, tmp_settings) -> None:
    (tmp_path / "readyagents.env.yaml").write_text(
        "version: 1\nenvironments:\n  staging: {}\n  prod: {}\n",
        encoding="utf-8",
    )
    original = _flow(tmp_path, "orig", "orig.yaml")
    deploy(original, "staging", spec=EnvironmentSpec(), settings=tmp_settings)
    _enable(tmp_settings, enforce=True)
    orig_id = scan(settings=tmp_settings)[0].agent_id
    _fill(orig_id, tmp_settings, risk_tier="high")
    copied = original.parent / "copy.yaml"
    copied.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    entries = scan(settings=tmp_settings)
    by_name = {Path(item.derived.path).name: item for item in entries}
    assert "orig.yaml" in by_name and "copy.yaml" in by_name
    assert by_name["copy.yaml"].agent_id != by_name["orig.yaml"].agent_id
    _fill(by_name["copy.yaml"].agent_id, tmp_settings, risk_tier="low")
    pointer = promote(
        copied,
        source="staging",
        target="prod",
        spec=EnvironmentSpec(),
        settings=tmp_settings,
    )
    assert pointer.get("digest")
    with pytest.raises(RegistryTierApproval):
        promote(
            original,
            source="staging",
            target="prod",
            spec=EnvironmentSpec(),
            settings=tmp_settings,
        )


def test_enforce_promote_move_without_rescan_keeps_tier(tmp_path: Path, tmp_settings) -> None:
    (tmp_path / "readyagents.env.yaml").write_text(
        "version: 1\nenvironments:\n  staging: {}\n  prod: {}\n",
        encoding="utf-8",
    )
    original = _flow(tmp_path, "movedsrc", "orig.yaml")
    deploy(original, "staging", spec=EnvironmentSpec(), settings=tmp_settings)
    _enable(tmp_settings, enforce=True)
    orig_id = scan(settings=tmp_settings)[0].agent_id
    _fill(orig_id, tmp_settings, risk_tier="high")
    moved = original.parent / "moved.yaml"
    original.rename(moved)
    with pytest.raises(RegistryTierApproval):
        promote(
            moved,
            source="staging",
            target="prod",
            spec=EnvironmentSpec(),
            settings=tmp_settings,
        )


def test_card_and_annex_name_unknowns(tmp_path: Path, tmp_settings) -> None:
    _rich(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    card = model_card(agent_id, settings=tmp_settings)
    assert "unknown: purpose" in str(card["purpose"])
    assert "not a claim" in card["disclaimer"].lower()
    record = annex_viii(agent_id, settings=tmp_settings)
    assert record["legal_filing"] is False
    assert record["article_49_registration"] is False
    assert "DRAFT" in record["disclaimer"]
    assert "not a legal filing" in record["disclaimer"].lower()
    assert "provider.name" in record["unknowns"]
    assert record["provider"]["name"] == "unknown: provider.name"
    dest = write_annex_viii(
        agent_id,
        tmp_path / "annex-viii-draft.json",
        settings=tmp_settings,
        yes=True,
    )
    assert "draft" in dest.name.lower()
    blob = dest.read_text(encoding="utf-8")
    assert "DRAFT" in blob
    assert "api.partner.example" not in blob
    with pytest.raises(RegistryRefused) as lab:
        write_annex_viii(agent_id, tmp_path / "filing.json", settings=tmp_settings, yes=True)
    assert lab.value.reason == "draft_label"


def test_list_redacts_endpoints_rbac_and_stats(tmp_path: Path, tmp_settings) -> None:
    _rich(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    _fill(agent_id, tmp_settings, owner="payments_ops", risk_tier="medium")
    rows = list_agents(settings=tmp_settings, owner="payments_ops", tier="medium")
    assert rows
    assert rows[0]["egress_hosts"] == ["[redacted-endpoint]", "[redacted-endpoint]"] or all(
        host == "[redacted-endpoint]" for host in rows[0]["egress_hosts"]
    )
    raw = list_agents(settings=tmp_settings, redact=False)
    assert "api.partner.example" in raw[0]["egress_hosts"]
    denied = CallbackAuthorizer(lambda actor, action, resource: False)
    with pytest.raises(AuthorizationError):
        scan(settings=tmp_settings, authorizer=denied, actor="intruder")
    summary = stats(settings=tmp_settings)
    assert summary["count"] >= 1
    assert "medium" in summary["by_tier"]


def test_list_and_stats_filters_drop_non_matching_spend_and_health(
    tmp_path: Path, tmp_settings
) -> None:
    _flow(tmp_path, "cheap", "cheap.yaml")
    _flow(tmp_path, "costly", "costly.yaml")
    _record_run(tmp_settings, "cheap", status="succeeded", cost_micros=100)
    _record_run(tmp_settings, "costly", status="failed", cost_micros=9000)
    _enable(tmp_settings)
    scan(settings=tmp_settings)
    rows = list_agents(settings=tmp_settings, kind="workflow")
    assert len(rows) == 2
    spends = sorted(int(row["spend_micros"] or 0) for row in rows)
    low, high = spends[0], spends[1]
    assert low < high
    cheap_rows = list_agents(settings=tmp_settings, kind="workflow", max_spend_micros=low)
    assert {int(row["spend_micros"] or 0) for row in cheap_rows} == {low}
    assert all(int(row["spend_micros"] or 0) <= low for row in cheap_rows)
    costly_rows = list_agents(settings=tmp_settings, kind="workflow", min_spend_micros=high)
    assert {int(row["spend_micros"] or 0) for row in costly_rows} == {high}
    scores = sorted(float(row["health_score"]) for row in rows if row["health_score"] is not None)
    assert len(scores) == 2
    weak, strong = scores[0], scores[1]
    assert weak < strong
    healthy = list_agents(settings=tmp_settings, kind="workflow", min_health=strong)
    assert {float(row["health_score"]) for row in healthy} == {strong}
    assert all(float(row["health_score"]) >= strong for row in healthy)
    sick = list_agents(settings=tmp_settings, kind="workflow", max_health=weak)
    assert {float(row["health_score"]) for row in sick} == {weak}
    sliced = stats(settings=tmp_settings, kind="workflow", max_spend_micros=low)
    assert sliced["count"] == 1
    assert sliced["spend_micros"] == low


def test_export_requires_yes_and_is_confined(tmp_path: Path, tmp_settings) -> None:
    _flow(tmp_path)
    _enable(tmp_settings)
    agent_id = scan(settings=tmp_settings)[0].agent_id
    with pytest.raises(RegistryRefused) as confirm:
        write_annex_viii(
            agent_id, tmp_path / "annex-viii-draft.json", settings=tmp_settings, yes=False
        )
    assert confirm.value.reason == "confirm"
    with pytest.raises(ConfigError):
        write_annex_viii(
            agent_id,
            Path("/tmp/annex-viii-draft.json"),
            settings=tmp_settings,
            yes=True,
        )


def test_cli_scan_list_check_json(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _flow(tmp_path, "cli_agent")
    _enable(tmp_settings)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    scan_result = runner.invoke(app, ["registry", "scan", "--json"])
    assert scan_result.exit_code == 0, scan_result.stdout + scan_result.stderr
    payload = json.loads(scan_result.stdout[scan_result.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "registry scan"
    assert payload["count"] >= 1
    agent_id = payload["agents"][0]["agent_id"]
    listed = runner.invoke(app, ["registry", "list", "--json"])
    assert listed.exit_code == 0
    shown = runner.invoke(app, ["registry", "show", agent_id, "--json"])
    assert shown.exit_code == 0
    checked = runner.invoke(app, ["registry", "check", "--json"])
    assert checked.exit_code == 0
    empty = runner.invoke(app, ["registry", "scan", "--root", "missing-root", "--json"])
    # missing-root under workspace with no files -> zero agents, still ok
    assert empty.exit_code == 0


def test_cli_list_spend_and_health_filters(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _flow(tmp_path, "cheap", "cheap.yaml")
    _flow(tmp_path, "costly", "costly.yaml")
    _record_run(tmp_settings, "cheap", status="succeeded", cost_micros=100)
    _record_run(tmp_settings, "costly", status="failed", cost_micros=9000)
    _enable(tmp_settings)
    scan(settings=tmp_settings)
    rows = list_agents(settings=tmp_settings, kind="workflow")
    spends = sorted(int(row["spend_micros"] or 0) for row in rows)
    low, high = spends[0], spends[1]
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    listed = runner.invoke(
        app, ["registry", "list", "--kind", "workflow", "--max-spend", str(low), "--json"]
    )
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    payload = json.loads(listed.stdout[listed.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["command"] == "registry list"
    assert payload["count"] == 1
    assert int(payload["agents"][0]["spend_micros"]) == low
    scores = sorted(float(row["health_score"]) for row in rows if row["health_score"] is not None)
    strong = scores[-1]
    healthy = runner.invoke(
        app, ["registry", "list", "--kind", "workflow", "--min-health", str(strong), "--json"]
    )
    assert healthy.exit_code == 0, healthy.stdout + healthy.stderr
    body = json.loads(healthy.stdout[healthy.stdout.find("{") :])
    assert body["count"] == 1
    assert float(body["agents"][0]["health_score"]) == strong
    summary = runner.invoke(
        app, ["registry", "stats", "--kind", "workflow", "--min-spend", str(high), "--json"]
    )
    assert summary.exit_code == 0, summary.stdout + summary.stderr
    stats_body = json.loads(summary.stdout[summary.stdout.find("{") :])
    assert stats_body["ok"] is True
    assert stats_body["count"] == 1
    assert int(stats_body["spend_micros"]) == high


def test_cli_no_roots_refused(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()
    result = runner.invoke(app, ["registry", "scan", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    assert payload["ok"] is False
    assert payload["error"] == "RegistryRefused"
    assert payload["reason"] == "no_roots"
