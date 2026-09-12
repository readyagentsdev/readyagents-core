"""Declared metadata, derived facts, and registry config. Tiers are not in workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from readyagents.errors import RegistryRefused

TIERS = ("high", "medium", "low")
DEFAULT_DATA_CLASSES = (
    "customer_pii",
    "payment_metadata",
    "public",
    "internal",
    "secrets",
)
DECLARED_FIELDS = (
    "owner",
    "backup_owner",
    "purpose",
    "risk_tier",
    "data_classes",
    "retention",
    "review",
    "decommission_after",
)


class OwnerRole(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, description="Role name, never a personal contact.")


class ReviewSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: str | None = None
    last: str | None = None


class TierRequirements(BaseModel):
    """Requirements live in registry config, never in workflow YAML."""

    model_config = ConfigDict(extra="forbid")

    approval_gate: bool = False
    evidence_pack: bool = False
    signed_release: bool = False
    min_review_days: int | None = Field(default=None, ge=1)


class RegistryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    roots: list[str] = Field(default_factory=list)
    enforce: bool = False
    unused_after: str = "90d"
    data_classes: list[str] = Field(default_factory=lambda: list(DEFAULT_DATA_CLASSES))
    tiers: dict[str, TierRequirements] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if int(value) != 1:
            raise ValueError("registry config version must be 1")
        return int(value)


class DeclaredAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    owner: OwnerRole | None = None
    backup_owner: OwnerRole | None = None
    purpose: str | None = None
    risk_tier: str | None = None
    data_classes: list[str] = Field(default_factory=list)
    retention: str | None = None
    review: ReviewSpec | None = None
    decommission_after: str | None = None
    review_snapshot: dict[str, Any] = Field(default_factory=dict)

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if self.owner is None:
            missing.append("owner")
        if self.backup_owner is None:
            missing.append("backup_owner")
        if not (self.purpose or "").strip():
            missing.append("purpose")
        if not self.risk_tier:
            missing.append("risk_tier")
        if not self.data_classes:
            missing.append("data_classes")
        if not self.retention:
            missing.append("retention")
        if self.review is None or not self.review.cadence:
            missing.append("review")
        if not self.decommission_after:
            missing.append("decommission_after")
        return missing


@dataclass
class DerivedFacts:
    kind: str
    path: str
    name: str
    version: str | None
    digest: str
    node_types: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    egress_hosts: list[str] = field(default_factory=list)
    approval_gates: bool = False
    memory_or_knowledge: bool = False
    policy: str | None = None
    budget_max_cost_usd: float | None = None
    run_count: int = 0
    spend_micros: int = 0
    failure_rate: float = 0.0
    health_score: float | None = None
    first_run: str | None = None
    last_run: str | None = None
    signed_release: bool = False
    evidence_runs: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
            "node_types": list(self.node_types),
            "tools": list(self.tools),
            "connectors": list(self.connectors),
            "models": list(self.models),
            "egress_hosts": list(self.egress_hosts),
            "approval_gates": self.approval_gates,
            "memory_or_knowledge": self.memory_or_knowledge,
            "policy": self.policy,
            "budget_max_cost_usd": self.budget_max_cost_usd,
            "run_count": self.run_count,
            "spend_micros": self.spend_micros,
            "failure_rate": self.failure_rate,
            "health_score": self.health_score,
            "first_run": self.first_run,
            "last_run": self.last_run,
            "signed_release": self.signed_release,
            "evidence_runs": self.evidence_runs,
        }

    def drift_key(self) -> dict[str, Any]:
        return {
            "egress_hosts": list(self.egress_hosts),
            "data_classes_from_nodes": list(self.node_types),
            "tools": list(self.tools),
            "models": list(self.models),
        }


def validate_tier(value: str) -> str:
    token = str(value or "").strip().lower()
    if token not in TIERS:
        raise RegistryRefused(f"unknown risk tier {value!r}", reason="tier")
    return token


def validate_data_classes(values: list[str], allowed: list[str]) -> list[str]:
    out: list[str] = []
    allow = {item.lower() for item in allowed}
    for raw in values:
        token = str(raw).strip().lower()
        if token not in allow:
            raise RegistryRefused(f"unknown data class {raw!r}", reason="data_class")
        out.append(token)
    return out
