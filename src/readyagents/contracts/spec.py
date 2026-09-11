"""Fail-closed output-contract shape. Unknown actions and judge-only contracts refuse."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ACTIONS = frozenset({"fail", "repair", "fallback", "gate", "redact_and_continue"})
STRUCTURAL_REPAIR = "repair"
MAX_REPAIRS = 5
RULE_KINDS = (
    "deny_regex",
    "deny",
    "require_citation",
    "language",
    "max_chars",
    "pii",
    "judge",
)


def _action(value: str | None, *, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field} is required")
    cleaned = str(value).strip().lower()
    if cleaned not in ACTIONS:
        raise ValueError(
            f"{field} must be one of {sorted(ACTIONS)}; unknown action {value!r} fails closed"
        )
    return cleaned


class JudgeSpec(BaseModel):
    """Opt-in model-graded check. Never the only rule on a contract."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, description="provider:model for the judge call.")
    rubric: str = Field(min_length=1, description="Fixed rubric. Graded text is untrusted.")
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)


class ContractRule(BaseModel):
    """One named deterministic (or opt-in judge) content rule."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="Recorded name when the rule fires.")
    deny_regex: str | None = None
    deny: str | None = Field(default=None, description="Literal substring deny.")
    require_citation: Any = None
    language: str | None = None
    max_chars: Any = None
    pii: Any = None
    judge: JudgeSpec | None = None
    on_fail: str = Field(default="fail", description="Declared action when this rule fires.")

    @field_validator("on_fail")
    @classmethod
    def _on_fail(cls, value: str) -> str:
        cleaned = _action(value, field="on_fail")
        if cleaned == STRUCTURAL_REPAIR:
            raise ValueError("rule on_fail cannot be repair; repair is structural only")
        return cleaned

    @model_validator(mode="after")
    def _one_kind(self) -> ContractRule:
        present = [k for k in RULE_KINDS if getattr(self, k) not in (None, False)]
        if self.pii is False:
            present = [k for k in present if k != "pii"]
        if len(present) != 1:
            raise ValueError(
                "each contract rule must declare exactly one of " + ", ".join(RULE_KINDS)
            )
        if self.deny_regex is not None:
            from readyagents.contracts.regex import compile_bounded

            compile_bounded(self.deny_regex)
        if self.max_chars is not None:
            _normalize_max_chars(self.max_chars)
        if self.require_citation is not None:
            _normalize_citation(self.require_citation)
        return self

    def kind(self) -> str:
        for key in RULE_KINDS:
            if getattr(self, key) not in (None, False):
                return key
        return "rule"

    def recorded_name(self) -> str:
        return (self.name or self.kind()).strip() or self.kind()


class ContractSpec(BaseModel):
    """Node `contract:` block. extra=forbid so unknown keys fail at validate."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_body: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description="JSON Schema for the node output. Structural checks run first.",
    )
    rules: list[ContractRule] = Field(default_factory=list)
    on_invalid: str = Field(description="Action when the structural schema is not met.")
    max_repairs: int | None = Field(default=None, ge=1, le=MAX_REPAIRS)
    on_exhausted: str | None = None
    on_refusal: str | None = None

    @field_validator("on_invalid")
    @classmethod
    def _on_invalid(cls, value: str) -> str:
        return _action(value, field="on_invalid")

    @field_validator("on_exhausted")
    @classmethod
    def _on_exhausted(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _action(value, field="on_exhausted")
        if cleaned == STRUCTURAL_REPAIR:
            raise ValueError("on_exhausted cannot be repair")
        return cleaned

    @field_validator("on_refusal")
    @classmethod
    def _on_refusal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _action(value, field="on_refusal")
        if cleaned == STRUCTURAL_REPAIR:
            raise ValueError("on_refusal cannot be repair")
        return cleaned

    @model_validator(mode="after")
    def _closed(self) -> ContractSpec:
        if self.schema_body is None and not self.rules:
            raise ValueError("contract requires schema and/or rules")
        if self.on_invalid == STRUCTURAL_REPAIR:
            if self.max_repairs is None:
                raise ValueError("max_repairs is required when on_invalid is repair")
            if self.on_exhausted is None:
                raise ValueError("on_exhausted is required when on_invalid is repair")
        elif self.max_repairs is not None:
            raise ValueError("max_repairs is only valid with on_invalid: repair")
        judges = [r for r in self.rules if r.judge is not None]
        deterministic = [r for r in self.rules if r.judge is None]
        if judges and not deterministic and self.schema_body is None:
            raise ValueError("judge cannot be the only contract check")
        return self


def parse_contract(raw: Any, *, node_id: str) -> ContractSpec:
    if raw is None:
        raise ValueError(f"Node '{node_id}': contract is empty")
    if not isinstance(raw, dict):
        raise ValueError(f"Node '{node_id}': contract must be a mapping")
    try:
        return ContractSpec.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Node '{node_id}': {exc}") from exc


def _normalize_max_chars(raw: Any) -> dict[str, Any]:
    if isinstance(raw, int):
        if raw < 1:
            raise ValueError("max_chars value must be >= 1")
        return {"value": raw, "field": None}
    if isinstance(raw, dict):
        value = raw.get("value")
        if not isinstance(value, int) or value < 1:
            raise ValueError("max_chars.value must be an integer >= 1")
        extra = set(raw) - {"value", "field"}
        if extra:
            raise ValueError(f"max_chars unknown keys: {sorted(extra)}")
        return {"value": value, "field": raw.get("field")}
    raise ValueError("max_chars must be an integer or {field, value}")


def _normalize_citation(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        return {"from": raw.strip()}
    if isinstance(raw, dict):
        source = raw.get("from") or raw.get("field")
        if not source:
            raise ValueError("require_citation requires from:")
        extra = set(raw) - {"from", "field"}
        if extra:
            raise ValueError(f"require_citation unknown keys: {sorted(extra)}")
        return {"from": str(source)}
    raise ValueError("require_citation must be a field name or {from: field}")
