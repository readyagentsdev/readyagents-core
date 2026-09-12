"""Completeness, drift, overdue reviews, unused candidates. Never deletes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.registry.periods import is_overdue, is_stale, past_date
from readyagents.registry.scan import RegistryEntry, load_entries
from readyagents.registry.schema import RegistryConfig
from readyagents.registry.store import load_config


@dataclass
class CheckReport:
    missing: list[dict[str, Any]] = field(default_factory=list)
    drift: list[dict[str, Any]] = field(default_factory=list)
    overdue: list[dict[str, Any]] = field(default_factory=list)
    unused: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "missing": list(self.missing),
            "drift": list(self.drift),
            "overdue": list(self.overdue),
            "unused": list(self.unused),
            "violations": list(self.violations),
        }


def check(
    *,
    settings: Settings | None = None,
    authorizer: Any = None,
    actor: str | None = None,
    now: datetime | None = None,
    enforce: bool = False,
) -> CheckReport:
    settings = settings or get_settings()
    if authorizer is not None:
        authorizer.check(actor, "registry.check", "registry")
    cfg = load_config(settings)
    entries = load_entries(settings=settings)
    report = CheckReport()
    for entry in entries:
        _fill_entry(report, entry, cfg, now=now)
    if enforce:
        from readyagents.registry.enforce import raise_first_violation

        raise_first_violation(report)
    return report


def material_drift(entry: RegistryEntry) -> list[str]:
    snap = entry.declared.review_snapshot or {}
    if not snap:
        return []
    current = entry.derived.drift_key()
    changes: list[str] = []
    for key in ("egress_hosts", "tools", "models", "data_classes_from_nodes"):
        left = {str(item) for item in (snap.get(key) or [])}
        right = {str(item) for item in (current.get(key) or [])}
        if left != right:
            changes.append(key)
    return changes


def _fill_entry(
    report: CheckReport,
    entry: RegistryEntry,
    cfg: RegistryConfig,
    *,
    now: datetime | None,
) -> None:
    missing = entry.declared.missing_fields()
    if missing:
        report.missing.append({"agent_id": entry.agent_id, "fields": missing})
    changes = material_drift(entry)
    if changes:
        report.drift.append({"agent_id": entry.agent_id, "changes": changes})
    review = entry.declared.review
    if review is not None and review.cadence and is_overdue(review.last, review.cadence, now=now):
        report.overdue.append(
            {
                "agent_id": entry.agent_id,
                "last": review.last,
                "cadence": review.cadence,
            }
        )
    unused_reason = None
    if is_stale(entry.derived.last_run, cfg.unused_after, now=now):
        unused_reason = "unused"
    if past_date(entry.declared.decommission_after, now=now):
        unused_reason = "decommission_date"
    if unused_reason:
        report.unused.append(
            {
                "agent_id": entry.agent_id,
                "last_run": entry.derived.last_run,
                "reason": unused_reason,
            }
        )
    from readyagents.registry.enforce import violation_rows

    report.violations.extend(violation_rows(entry, cfg))
