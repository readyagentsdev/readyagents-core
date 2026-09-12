"""Frozen dataset manifest, adapter record, and eval-comparison contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DATASET_SCHEMA = "readyagents.distill.dataset/1"
ADAPTER_SCHEMA = "readyagents.distill.adapter/1"
COMPARISON_SCHEMA = "readyagents.distill.comparison/1"
VERDICTS = ("viable", "not_enough_data", "task_too_broad")
HOLDOUT_NAME = "holdout"
DEFAULT_SPLIT = (0.8, 0.1, 0.1)
DEFAULT_MIN_EXAMPLES = 10
DEFAULT_BROAD_RATIO = 0.85


class DistillConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_examples: int = Field(default=DEFAULT_MIN_EXAMPLES, ge=1)
    broad_ratio: float = Field(default=DEFAULT_BROAD_RATIO, gt=0, le=1)
    min_parity: float = Field(default=1.0, ge=0)
    max_latency_ms: float | None = Field(default=None, ge=0)
    max_cost_micros: int | None = Field(default=None, ge=0)
    approval_roles: list[str] = Field(default_factory=list)
    hosted_tune: bool = False


class SplitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    train: float = 0.8
    validation: float = 0.1
    holdout: float = 0.1


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())

    schema_id: str = Field(default=DATASET_SCHEMA, alias="schema")
    node_id: str
    format: str = "sft"
    seed: int = 1
    split: SplitSpec = Field(default_factory=SplitSpec)
    counts: dict[str, int] = Field(default_factory=dict)
    hash: str = ""
    consent_from_recorded_policy: bool = True
    redaction_reverified: bool = True
    holdout_named: str = HOLDOUT_NAME
    created_at: str = ""
    path: str = ""


class SideScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accuracy: float = 0.0
    passed: int = 0
    failed: int = 0
    determinism: dict[str, int] = Field(default_factory=dict)
    trajectory: list[str] = Field(default_factory=list)
    latency_ms: float | None = None
    cost_micros: int = 0
    holdout: float | None = None


class EvalComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())

    schema_id: str = Field(default=COMPARISON_SCHEMA, alias="schema")
    incumbent: SideScore = Field(default_factory=SideScore)
    candidate: SideScore = Field(default_factory=SideScore)
    holdout: dict[str, Any] = Field(default_factory=dict)
    frozen_regressions: list[str] = Field(default_factory=list)
    canary_passed: bool | None = None
    fixture_digest: str = ""


class AdapterRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())

    schema_id: str = Field(default=ADAPTER_SCHEMA, alias="schema")
    id: str
    version: int = 1
    node_id: str
    base_model: str
    dataset_hash: str
    training_config: dict[str, Any] = Field(default_factory=dict)
    eval: EvalComparison | None = None
    digest: str = ""
    signed: bool = False
    path: str = ""
    status: str = "candidate"
    incumbent: str | None = None
    promoted_at: str | None = None
    demoted_at: str | None = None
    demote_reason: str | None = None
    fixture_digest: str = ""


class PlanReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    verdict: str
    examples: int = 0
    consented: int = 0
    diversity: float = 0.0
    current_cost_micros: int | None = None
    current_latency_ms: float | None = None
    estimated_saving_micros: int | None = None
    reason: str = ""
    hardware: str = (
        "training runs on operator hardware through an optional pack; core has no GPU extra"
    )
