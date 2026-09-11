"""Fail-closed team shape. Nested teams and unknown strategies refuse at validate."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STRATEGIES = frozenset({"route", "plan_then_execute", "debate", "pipeline"})
MAX_ROUNDS = 32
MAX_MEMBERS = 16


class ScratchpadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)


class TeamScratchpadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=65_536, ge=64, le=1_048_576)


class TeamTerminateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=8, ge=1, le=MAX_ROUNDS)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_spend: float | None = Field(default=None, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    goal: str | None = None

    @model_validator(mode="after")
    def _spend(self) -> TeamTerminateSpec:
        if self.max_cost_usd is None and self.max_spend is not None:
            self.max_cost_usd = self.max_spend
        return self


class TeamSupervisorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model: str | None = None
    system: str | None = None


class TeamMemberSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    role: str = ""
    type: str = "agent"
    prompt: str | None = None
    system: str | None = None
    persona: str | None = None
    model: str | None = None
    tools: list[str] = Field(default_factory=list)
    scratchpad: ScratchpadGrant | None = None
    approver_roles: list[str] = Field(default_factory=list)
    max_cost_usd: float | None = Field(default=None, ge=0)
    template: str | None = None
    parse_json: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid member id '{value}'")
        return value

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned == "team":
            raise ValueError("nested type: team members are not supported")
        if cleaned not in {"agent", "approval", "transform"}:
            raise ValueError(
                f"team member type must be agent, approval, or transform, not {value!r}"
            )
        return cleaned

    @model_validator(mode="after")
    def _typed(self) -> TeamMemberSpec:
        if self.type == "transform" and not self.template:
            raise ValueError("transform members require template")
        if self.type == "approval" and not (self.prompt or "").strip():
            self.prompt = f"Approve {self.role or self.id}"
        return self

    def system_prompt(self) -> str | None:
        return self.system or self.persona


class TeamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str
    supervisor: TeamSupervisorSpec
    members: list[TeamMemberSpec]
    scratchpad: TeamScratchpadSpec = Field(default_factory=TeamScratchpadSpec)
    terminate: TeamTerminateSpec = Field(default_factory=TeamTerminateSpec)

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in STRATEGIES:
            raise ValueError(f"strategy must be one of {sorted(STRATEGIES)}")
        return cleaned

    @model_validator(mode="after")
    def _closed(self) -> TeamSpec:
        if not self.members:
            raise ValueError("team members must be a non-empty closed set")
        if len(self.members) > MAX_MEMBERS:
            raise ValueError(f"team members exceed {MAX_MEMBERS}")
        ids = [m.id for m in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("team member ids must be unique")
        if self.strategy == "debate" and len(self.members) < 2:
            raise ValueError("debate requires at least two members")
        declared = set(self.scratchpad.keys)
        for member in self.members:
            grant = member.scratchpad
            if grant is None:
                continue
            for key in list(grant.read) + list(grant.write):
                if declared and key not in declared:
                    raise ValueError(
                        f"member '{member.id}' scratchpad key '{key}' is not in team keys"
                    )
        return self

    def member_map(self) -> dict[str, TeamMemberSpec]:
        return {m.id: m for m in self.members}

    def member_ids(self) -> list[str]:
        return [m.id for m in self.members]


def parse_team(node: Any) -> TeamSpec:
    node_id = str(getattr(node, "id", "") or "")
    raw = {
        "strategy": getattr(node, "strategy", None),
        "supervisor": getattr(node, "supervisor", None),
        "members": getattr(node, "members", None),
        "scratchpad": getattr(node, "scratchpad", None) or {},
        "terminate": getattr(node, "terminate", None) or {},
    }
    if not raw["strategy"] or not raw["supervisor"] or not raw["members"]:
        raise ValueError(f"Node '{node_id}': team nodes require strategy, supervisor, and members")
    try:
        return TeamSpec.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Node '{node_id}': {exc}") from exc
