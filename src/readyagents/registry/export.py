"""Annex VIII-shaped draft export. A draft record, not a legal filing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.errors import RegistryRefused
from readyagents.registry.card import assemble_card
from readyagents.registry.scan import RegistryEntry, get_entry
from readyagents.workflow.runner import confine_under

DRAFT_DISCLAIMER = (
    "DRAFT — assembled from local evidence, not a legal filing. "
    "Not an Article 49 registration and not a claim of EU AI Act compliance."
)
DRAFT_FORMAT = "annex-viii-draft"


def annex_viii(
    agent_id: str,
    *,
    settings: Settings | None = None,
    authorizer: Any = None,
    actor: str | None = None,
    redact: bool = True,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.export", agent_id)
        if not redact:
            authorizer.check(actor, "registry.unredact", agent_id)
    entry = get_entry(agent_id, settings=settings)
    if entry is None:
        raise RegistryRefused(f"unknown agent {agent_id}", reason="missing")
    return assemble_annex(entry, settings=settings, redact=redact)


def assemble_annex(
    entry: RegistryEntry,
    *,
    settings: Any = None,
    redact: bool = True,
) -> dict[str, Any]:
    declared = entry.declared
    derived = entry.as_dict(redact=redact)["derived"]
    card = assemble_card(entry, settings=settings, redact=redact)
    unknowns: list[str] = []

    def named(value: Any, field: str) -> Any:
        if value in (None, "", [], {}):
            unknowns.append(field)
            return f"unknown: {field}"
        return value

    record = {
        "disclaimer": DRAFT_DISCLAIMER,
        "format": DRAFT_FORMAT,
        "legal_filing": False,
        "article_49_registration": False,
        "compliance_claim": False,
        "header": DRAFT_DISCLAIMER,
        "provider": {
            "name": named(None, "provider.name"),
            "address": named(None, "provider.address"),
            "contact": named(None, "provider.contact"),
            "authorised_representative": named(None, "provider.authorised_representative"),
        },
        "system": {
            "name": derived.get("name"),
            "agent_id": entry.agent_id,
            "type": derived.get("kind"),
            "version": named(derived.get("version"), "system.version"),
            "intended_purpose": named(declared.purpose, "intended_purpose"),
            "status": "local inventory entry",
        },
        "instructions_for_use_url": named(None, "instructions_for_use_url"),
        "data": {
            "classes": named(declared.data_classes, "data.classes"),
            "retention": named(declared.retention, "data.retention"),
        },
        "human_oversight": card.get("human_oversight"),
        "risk_tier_declared": named(declared.risk_tier, "risk_tier"),
        "models": derived.get("models") or named(None, "models"),
        "member_state": named(None, "member_state"),
        "notified_body": named(None, "notified_body"),
        "eu_declaration_of_conformity": named(None, "eu_declaration_of_conformity"),
        "unknowns": sorted(set(unknowns) | set(card.get("unknowns") or [])),
    }
    return record


def write_annex_viii(
    agent_id: str,
    dest: Path | str,
    *,
    settings: Settings | None = None,
    authorizer: Any = None,
    actor: str | None = None,
    redact: bool = True,
    yes: bool = False,
) -> Path:
    settings = settings or get_settings()
    record = annex_viii(
        agent_id,
        settings=settings,
        authorizer=authorizer,
        actor=actor,
        redact=redact,
    )
    workspace = settings.workspace_path()
    target = Path(dest)
    if target.suffix.lower() not in {".json", ".md", ".yaml", ".yml"}:
        target = target / f"annex-viii-draft-{agent_id}.json"
    confined = confine_under(target, workspace, what="registry export")
    if "draft" not in confined.name.lower():
        raise RegistryRefused(
            "annex-viii export filename must contain 'draft' "
            "(this is a draft record, not a legal filing)",
            reason="draft_label",
        )
    if not yes:
        raise RegistryRefused(
            f"registry export is a reconnaissance document; pass --yes to write {confined}",
            reason="confirm",
        )
    confined.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(confined, body, encoding="utf-8", newline="\n")
    return confined
