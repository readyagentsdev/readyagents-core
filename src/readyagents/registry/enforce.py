"""Tier requirements live in registry config. Distinct typed reasons. Off by default."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.errors import (
    RegistryTierApproval,
    RegistryTierCadence,
    RegistryTierEvidence,
    RegistryTierSigned,
)
from readyagents.registry.check import CheckReport
from readyagents.registry.discover import as_rel
from readyagents.registry.periods import parse_days
from readyagents.registry.scan import RegistryEntry, load_entries
from readyagents.registry.schema import RegistryConfig, TierRequirements
from readyagents.registry.store import load_config
from readyagents.trust.digest import digest_bytes


def enforce_promote(
    workflow: Path | str,
    *,
    settings: Settings | None = None,
) -> None:
    """No-op unless registry config ``enforce`` is on. Gates the shipped promote path."""
    settings = settings or get_settings()
    cfg = load_config(settings)
    if not cfg.enforce:
        return
    path = Path(workflow)
    rel = as_rel(path, settings)
    digest = ""
    try:
        digest = digest_bytes(path.read_bytes())
    except OSError:
        digest = ""
    for entry in load_entries(settings=settings):
        if entry.derived.path == rel or (digest and entry.derived.digest == digest):
            enforce_entry(entry, cfg)
            return


def enforce_entry(entry: RegistryEntry, cfg: RegistryConfig) -> None:
    req = _requirements(entry, cfg)
    if req is None:
        return
    _approval(entry, req)
    _evidence(entry, req)
    _signed(entry, req)
    _cadence(entry, req)


def violation_rows(entry: RegistryEntry, cfg: RegistryConfig) -> list[dict[str, Any]]:
    req = _requirements(entry, cfg)
    if req is None:
        return []
    rows: list[dict[str, Any]] = []
    checks = (
        (_approval, "approval_gate"),
        (_evidence, "evidence_pack"),
        (_signed, "signed_release"),
        (_cadence, "review_cadence"),
    )
    for fn, reason in checks:
        try:
            fn(entry, req)
        except (
            RegistryTierApproval,
            RegistryTierEvidence,
            RegistryTierSigned,
            RegistryTierCadence,
        ) as extra:
            rows.append(
                {
                    "agent_id": entry.agent_id,
                    "reason": reason,
                    "error": type(extra).__name__,
                    "message": str(extra),
                }
            )
    return rows


def raise_first_violation(report: CheckReport) -> None:
    if not report.violations:
        return
    row = report.violations[0]
    name = str(row.get("error") or "")
    message = str(row.get("message") or "tier requirement unmet")
    mapping = {
        "RegistryTierApproval": RegistryTierApproval,
        "RegistryTierEvidence": RegistryTierEvidence,
        "RegistryTierSigned": RegistryTierSigned,
        "RegistryTierCadence": RegistryTierCadence,
    }
    cls = mapping.get(name)
    if cls is None:
        return
    raise cls(message)


def _requirements(entry: RegistryEntry, cfg: RegistryConfig) -> TierRequirements | None:
    tier = (entry.declared.risk_tier or "").strip().lower()
    if not tier:
        return None
    return cfg.tiers.get(tier)


def _approval(entry: RegistryEntry, req: TierRequirements) -> None:
    if req.approval_gate and not entry.derived.approval_gates:
        raise RegistryTierApproval(
            f"agent {entry.agent_id} requires an approval gate (tier {entry.declared.risk_tier})"
        )


def _evidence(entry: RegistryEntry, req: TierRequirements) -> None:
    if req.evidence_pack and entry.derived.evidence_runs <= 0:
        raise RegistryTierEvidence(
            f"agent {entry.agent_id} requires an evidence pack (tier {entry.declared.risk_tier})"
        )


def _signed(entry: RegistryEntry, req: TierRequirements) -> None:
    if req.signed_release and not entry.derived.signed_release:
        raise RegistryTierSigned(
            f"agent {entry.agent_id} requires a signed release (tier {entry.declared.risk_tier})"
        )


def _cadence(entry: RegistryEntry, req: TierRequirements) -> None:
    if req.min_review_days is None:
        return
    review = entry.declared.review
    if review is None or not review.cadence:
        raise RegistryTierCadence(
            f"agent {entry.agent_id} requires a review cadence of at most "
            f"{req.min_review_days}d (tier {entry.declared.risk_tier})"
        )
    days = parse_days(review.cadence)
    if days > int(req.min_review_days):
        raise RegistryTierCadence(
            f"agent {entry.agent_id} review cadence {review.cadence} exceeds "
            f"{req.min_review_days}d (tier {entry.declared.risk_tier})"
        )
