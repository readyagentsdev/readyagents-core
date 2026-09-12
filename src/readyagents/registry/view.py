"""Fleet list/show/stats. Default view redacts endpoints and secretish tools."""

from __future__ import annotations

from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.registry.scan import RegistryEntry, get_entry, load_entries


def list_agents(
    *,
    settings: Settings | None = None,
    owner: str | None = None,
    tier: str | None = None,
    model: str | None = None,
    kind: str | None = None,
    stale: bool | None = None,
    min_spend_micros: int | None = None,
    max_spend_micros: int | None = None,
    min_health: float | None = None,
    max_health: float | None = None,
    redact: bool = True,
    authorizer: Any = None,
    actor: str | None = None,
    unused_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.list", "registry")
        if not redact:
            authorizer.check(actor, "registry.unredact", "registry")
    rows: list[dict[str, Any]] = []
    for entry in load_entries(settings=settings):
        if not _match(
            entry,
            owner=owner,
            tier=tier,
            model=model,
            kind=kind,
            stale=stale,
            unused_ids=unused_ids,
            min_spend_micros=min_spend_micros,
            max_spend_micros=max_spend_micros,
            min_health=min_health,
            max_health=max_health,
        ):
            continue
        rows.append(_summary(entry, redact=redact))
    return rows


def show_agent(
    agent_id: str,
    *,
    settings: Settings | None = None,
    redact: bool = True,
    authorizer: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.show", agent_id)
        if not redact:
            authorizer.check(actor, "registry.unredact", agent_id)
    entry = get_entry(agent_id, settings=settings)
    if entry is None:
        raise RegistryRefused(f"unknown agent {agent_id}", reason="missing")
    return entry.as_dict(redact=redact)


def stats(
    *,
    settings: Settings | None = None,
    owner: str | None = None,
    tier: str | None = None,
    model: str | None = None,
    kind: str | None = None,
    stale: bool | None = None,
    min_spend_micros: int | None = None,
    max_spend_micros: int | None = None,
    min_health: float | None = None,
    max_health: float | None = None,
    unused_ids: set[str] | None = None,
    redact: bool = True,
    authorizer: Any = None,
    actor: str | None = None,
) -> dict[str, Any]:
    del redact
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.stats", "registry")
    entries = [
        entry
        for entry in load_entries(settings=settings)
        if _match(
            entry,
            owner=owner,
            tier=tier,
            model=model,
            kind=kind,
            stale=stale,
            unused_ids=unused_ids,
            min_spend_micros=min_spend_micros,
            max_spend_micros=max_spend_micros,
            min_health=min_health,
            max_health=max_health,
        )
    ]
    by_tier: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    spend = 0
    missing = 0
    for entry in entries:
        row_tier = entry.declared.risk_tier or "undeclared"
        by_tier[row_tier] = by_tier.get(row_tier, 0) + 1
        role = entry.declared.owner.role if entry.declared.owner else "undeclared"
        by_owner[role] = by_owner.get(role, 0) + 1
        by_kind[entry.derived.kind] = by_kind.get(entry.derived.kind, 0) + 1
        spend += int(entry.derived.spend_micros or 0)
        if entry.declared.missing_fields():
            missing += 1
    from readyagents.registry.check import check as run_check

    report = run_check(settings=settings)
    kept = {entry.agent_id for entry in entries}
    return {
        "count": len(entries),
        "by_tier": by_tier,
        "by_owner": by_owner,
        "by_kind": by_kind,
        "spend_micros": spend,
        "missing_metadata": missing,
        "overdue": sum(1 for row in report.overdue if row.get("agent_id") in kept),
        "unused_candidates": sum(1 for row in report.unused if row.get("agent_id") in kept),
        "drift": sum(1 for row in report.drift if row.get("agent_id") in kept),
    }


def _summary(entry: RegistryEntry, *, redact: bool) -> dict[str, Any]:
    body = entry.as_dict(redact=redact)
    derived = body["derived"]
    return {
        "agent_id": entry.agent_id,
        "name": derived.get("name"),
        "kind": derived.get("kind"),
        "owner": entry.declared.owner.role if entry.declared.owner else None,
        "risk_tier": entry.declared.risk_tier,
        "models": derived.get("models"),
        "run_count": derived.get("run_count"),
        "spend_micros": derived.get("spend_micros"),
        "health_score": derived.get("health_score"),
        "last_run": derived.get("last_run"),
        "missing": body["missing"],
        "egress_hosts": derived.get("egress_hosts"),
    }


def _match(
    entry: RegistryEntry,
    *,
    owner: str | None,
    tier: str | None,
    model: str | None,
    kind: str | None,
    stale: bool | None,
    unused_ids: set[str] | None,
    min_spend_micros: int | None = None,
    max_spend_micros: int | None = None,
    min_health: float | None = None,
    max_health: float | None = None,
) -> bool:
    if owner:
        role = entry.declared.owner.role if entry.declared.owner else ""
        if role != owner:
            return False
    if tier and (entry.declared.risk_tier or "") != tier:
        return False
    if kind and entry.derived.kind != kind:
        return False
    if model and model not in entry.derived.models:
        return False
    spend = int(entry.derived.spend_micros or 0)
    if min_spend_micros is not None and spend < int(min_spend_micros):
        return False
    if max_spend_micros is not None and spend > int(max_spend_micros):
        return False
    score = entry.derived.health_score
    if min_health is not None:
        if score is None or float(score) < float(min_health):
            return False
    if max_health is not None:
        if score is None or float(score) > float(max_health):
            return False
    if stale is True:
        if unused_ids is not None:
            return entry.agent_id in unused_ids
        return entry.derived.run_count == 0
    if stale is False:
        if unused_ids is not None:
            return entry.agent_id not in unused_ids
        return entry.derived.run_count > 0
    return True
