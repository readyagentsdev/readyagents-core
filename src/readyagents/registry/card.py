"""Model-card-shaped document assembled from evidence. Unknowns are named, never guessed."""

from __future__ import annotations

from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.registry.scan import RegistryEntry, get_entry


def model_card(
    agent_id: str,
    *,
    settings: Settings | None = None,
    authorizer: Any = None,
    actor: str | None = None,
    redact: bool = True,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.card", agent_id)
        if not redact:
            authorizer.check(actor, "registry.unredact", agent_id)
    entry = get_entry(agent_id, settings=settings)
    if entry is None:
        raise RegistryRefused(f"unknown agent {agent_id}", reason="missing")
    return assemble_card(entry, settings=settings, redact=redact)


def assemble_card(
    entry: RegistryEntry,
    *,
    settings: Any = None,
    redact: bool = True,
) -> dict[str, Any]:
    declared = entry.declared
    derived = entry.as_dict(redact=redact)["derived"]
    unknowns: list[str] = []

    def named(value: Any, field: str) -> Any:
        if value in (None, "", [], {}):
            unknowns.append(field)
            return f"unknown: {field}"
        return value

    owner = declared.owner.role if declared.owner else None
    backup = declared.backup_owner.role if declared.backup_owner else None
    oversight = "approval gate" if entry.derived.approval_gates else None
    fingerprints = _fingerprints(entry.derived.name, settings)
    card = {
        "kind": "model_card",
        "disclaimer": (
            "Assembled from local evidence. Unknown fields are named, not guessed. "
            "Not a claim of EU AI Act compliance or certification."
        ),
        "agent_id": entry.agent_id,
        "name": derived.get("name"),
        "purpose": named(declared.purpose, "purpose"),
        "owner": named(owner, "owner"),
        "backup_owner": named(backup, "backup_owner"),
        "models": named(derived.get("models"), "models"),
        "data_classes": named(declared.data_classes, "data_classes"),
        "risk_tier": named(declared.risk_tier, "risk_tier"),
        "limitations": [],
        "evaluation_results": named(_eval_results(entry), "evaluation_results"),
        "known_failure_fingerprints": named(fingerprints, "known_failure_fingerprints"),
        "human_oversight": named(oversight, "human_oversight"),
        "retention": named(declared.retention, "retention"),
        "review": named(
            declared.review.model_dump() if declared.review else None,
            "review",
        ),
    }
    card["limitations"] = [f"unknown: {name}" for name in unknowns] or [
        "none named from local evidence"
    ]
    card["unknowns"] = list(unknowns)
    return card


def _eval_results(entry: RegistryEntry) -> dict[str, Any] | None:
    if entry.derived.run_count <= 0:
        return None
    return {
        "run_count": entry.derived.run_count,
        "failure_rate": entry.derived.failure_rate,
        "health_score": entry.derived.health_score,
        "spend_micros": entry.derived.spend_micros,
    }


def _fingerprints(name: str, settings: Any) -> list[str] | None:
    if settings is None:
        return None
    try:
        from readyagents.health.query import query_health
        from readyagents.run_store import open_run_store

        store = open_run_store(settings)
        try:
            report = query_health(store, workflow=name)
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        rows: list[str] = []
        for row in report.clusters[:8]:
            fp = getattr(row, "fingerprint", None)
            if fp is not None:
                rows.append(fp.id)
        return rows or None
    except Exception:  # noqa: BLE001
        return None
