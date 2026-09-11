"""Pydantic models for YAML/JSON workflow definitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from readyagents.contracts.spec import ContractSpec
from readyagents.errors import WorkflowError


class ApprovalNotifySpec(BaseModel):
    """Opt-in pause notification. Failure never changes run state."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="file, command, or webhook.")
    path: str | None = Field(default=None, description="JSONL path for kind=file.")
    command: list[str] = Field(default_factory=list, description="Argv for kind=command.")
    url: str | None = Field(default=None, description="HTTPS URL for kind=webhook.")

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"file", "command", "webhook"}:
            raise ValueError("notify.kind must be file, command, or webhook")
        return cleaned


class NodeType(StrEnum):
    agent = "agent"
    tool = "tool"
    condition = "condition"
    transform = "transform"
    approval = "approval"
    parallel = "parallel"
    include = "include"
    foreach = "foreach"
    a2a = "a2a"
    memory = "memory"
    code = "code"
    team = "team"
    document = "document"
    transcribe = "transcribe"
    ingest = "ingest"
    table = "table"
    classify = "classify"
    wait = "wait"


class RetrySpec(BaseModel):
    """Retry policy for a node."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(
        default=1,
        ge=1,
        le=20,
        description="Maximum attempts including the first try (1–20).",
    )
    backoff_seconds: float = Field(
        default=1.0,
        ge=0,
        description="Initial backoff in seconds after a failed attempt.",
    )
    backoff_multiplier: float = Field(
        default=2.0,
        ge=1.0,
        description="Multiplier applied to backoff after each failed attempt.",
    )


class BudgetSpec(BaseModel):
    """Optional token/cost budget for LLM calls in this workflow."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Maximum LLM tokens for the run. Further calls raise BudgetExceeded.",
    )
    max_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="Maximum estimated LLM cost in USD for the run.",
    )
    max_media_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Maximum ingested media bytes for the run.",
    )


class RunawaySpec(BaseModel):
    """Optional per-run loop/call/wall-clock guards. Distinct from BudgetSpec."""

    model_config = ConfigDict(extra="forbid")

    max_model_calls: int | None = Field(
        default=None,
        ge=1,
        description="Maximum LLM complete() attempts for the run.",
    )
    max_tool_rounds: int | None = Field(
        default=None,
        ge=1,
        description="Maximum agent tool-call rounds across the whole run.",
    )
    max_wall_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Maximum wall-clock seconds for the run.",
    )


class ContextSpec(BaseModel):
    """Declared compaction for oversized memory content. Never implicit."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Token budget for this node's memory payload.",
    )
    on_exceed: str = Field(
        default="truncate",
        description="truncate, summarize, or fail when the budget is exceeded.",
    )
    summarize_model: str | None = Field(
        default=None,
        description="Optional provider:model used when on_exceed is summarize.",
    )

    @field_validator("on_exceed")
    @classmethod
    def _exceed(cls, value: str) -> str:
        cleaned = (value or "truncate").strip().lower()
        if cleaned not in {"truncate", "summarize", "fail"}:
            raise ValueError("context.on_exceed must be truncate, summarize, or fail")
        return cleaned


class CircuitSpec(BaseModel):
    """Process-local circuit breaker for LLM providers."""

    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(
        default=3,
        ge=1,
        description="Consecutive failures before the breaker opens.",
    )
    cooldown_seconds: float = Field(
        default=60.0,
        ge=0,
        description="Seconds to skip a model after the breaker opens.",
    )


_ROUTING_STRATEGIES = frozenset(
    {
        "cheapest_capable",
        "fastest",
        "highest_quality",
        "local_only",
        "pin",
    }
)


def _normalize_strategy(value: str) -> str:
    text = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "highestquality": "highest_quality",
        "localonly": "local_only",
        "cheapestcapable": "cheapest_capable",
        "explicit": "pin",
        "explicit_pinning": "pin",
        "explicit_pin": "pin",
    }
    return aliases.get(text, text)


class RouteMatch(BaseModel):
    """AND of declared match fields. Empty match is a catch-all."""

    model_config = ConfigDict(extra="forbid")

    node: str | None = Field(default=None, description="Match this node id.")
    node_tag: str | None = Field(default=None, description="Match when the tag is on the node.")
    role: str | None = Field(default=None, description="Match this node role.")
    taint: str | None = Field(
        default=None,
        description="trusted or untrusted. Indeterminate taint matches untrusted.",
    )

    @field_validator("taint")
    @classmethod
    def _taint(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        cleaned = str(value).strip().lower()
        if cleaned not in {"trusted", "untrusted"}:
            raise ValueError("routing match.taint must be trusted or untrusted")
        return cleaned


class RouteRequire(BaseModel):
    """Declared capability constraints. Unsatisfiable is a typed error, never a downgrade."""

    model_config = ConfigDict(extra="forbid")

    tool_calling: bool | None = None
    structured_output: bool | None = None
    media: bool | None = None
    streaming: bool | None = None
    local: bool | None = None
    min_context_window: int | None = Field(default=None, ge=1)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.tool_calling:
            data["tool_calling"] = True
        if self.structured_output:
            data["structured_output"] = True
        if self.media:
            data["media"] = True
        if self.streaming:
            data["streaming"] = True
        if self.local:
            data["local"] = True
        if self.min_context_window is not None:
            data["min_context_window"] = int(self.min_context_window)
        return data


class RouteBudget(BaseModel):
    """Per-route spend/token ceiling. Applied on top of the run-level cap."""

    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)


class RouteRule(BaseModel):
    """One routing rule. First matching rule wins."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Optional rule id recorded on the route.")
    match: RouteMatch | None = None
    strategy: str | None = Field(
        default=None,
        description=(
            "cheapest_capable, fastest, highest_quality, local_only, or pin. "
            "Declared attributes only; never inferred output quality."
        ),
    )
    pin: str | None = Field(
        default=None,
        description="Explicit provider:model pin. Implies strategy pin.",
    )
    require: RouteRequire | None = None
    pool: list[str] = Field(
        default_factory=list,
        description="Candidate model refs for this rule. Empty uses workflow pool or catalog.",
    )

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        cleaned = _normalize_strategy(str(value))
        if cleaned not in _ROUTING_STRATEGIES:
            raise ValueError(
                "routing strategy must be cheapest_capable, fastest, "
                "highest_quality, local_only, or pin"
            )
        return cleaned

    @model_validator(mode="after")
    def _pin_or_strategy(self) -> RouteRule:
        if self.pin and not self.strategy:
            self.strategy = "pin"
        if not self.strategy and not self.pin:
            raise ValueError("routing rule requires strategy or pin")
        if self.strategy == "pin" and not (self.pin or "").strip():
            raise ValueError("routing strategy pin requires pin")
        if self.pin:
            self.pin = self.pin.strip()
        return self


class RoutingSpec(BaseModel):
    """Optional workflow routing policy. Absent means byte-identical legacy selection."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, description="Routing policy version. Must be 1.")
    rules: list[RouteRule] = Field(default_factory=list)
    budgets: dict[str, RouteBudget] = Field(
        default_factory=dict,
        description="Keyed by node id, tag, or rule id.",
    )
    pool: list[str] = Field(
        default_factory=list,
        description="Default candidate pool when a rule omits pool.",
    )
    capability_matrix: str | None = Field(
        default=None,
        description="Optional override path for the capability matrix (schema-validated).",
    )

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if int(value) != 1:
            raise ValueError("routing.version must be 1")
        return int(value)


class MCPServerSpec(BaseModel):
    """Stdio MCP server launched for this workflow."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="Executable to launch (no shell).")
    args: list[str] = Field(default_factory=list, description="Arguments passed to the executable.")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the server process.",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory; must stay inside the workspace.",
    )


class MediaPolicySpec(BaseModel):
    """Optional per-workflow media caps. Absent: engine defaults. Not a quality claim."""

    model_config = ConfigDict(extra="forbid")

    max_bytes_per_part: int | None = Field(default=None, ge=1)
    max_run_bytes: int | None = Field(default=None, ge=1)
    max_width: int | None = Field(default=None, ge=1)
    max_height: int | None = Field(default=None, ge=1)
    max_pixels: int | None = Field(default=None, ge=1)
    max_pages: int | None = Field(default=None, ge=1)
    max_duration_ms: int | None = Field(default=None, ge=1)
    max_bytes_per_page: int | None = Field(default=None, ge=1)
    downscale_max_edge: int | None = Field(default=None, ge=1)
    dpi: int | None = Field(default=None, ge=1)
    redact: dict[str, Any] | None = Field(
        default=None,
        description="Optional declared regions/classes applied before persist/send.",
    )


class NodeSpec(BaseModel):
    """One node in the workflow graph."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(min_length=1, description="Unique node id (letters, numbers, _ or -).")
    type: str = Field(
        description=(
            "Node kind. Built-ins: agent, tool, condition, transform, approval, "
            "parallel, include, foreach, a2a, memory, code, team, document, "
            "transcribe, ingest, table, classify, wait. Packs may add types."
        )
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Soft timeout in seconds for this node.",
    )
    retry: RetrySpec | None = Field(default=None, description="Retry policy for this node.")
    next: str | None = Field(default=None, description="Default successor node id.")
    output_key: str | None = Field(
        default=None,
        description="Alias for this node's output in templates ({{key}}).",
    )
    description: str | None = Field(
        default=None,
        description="Human-readable note; ignored at runtime.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Optional tags for routing match.node_tag.",
    )
    role: str | None = Field(
        default=None,
        description="Optional role for routing match.role.",
    )

    # agent
    prompt: str | None = Field(
        default=None,
        description="Agent or approval prompt. Templates allowed.",
    )
    system: str | None = Field(default=None, description="Optional system prompt for agent nodes.")
    model: str | None = Field(
        default=None,
        description="LLM ref for this agent (provider:model). Overrides default_model.",
    )

    # tool
    tool: str | None = Field(
        default=None,
        description="Registry tool name (calc, read_file, server.tool, …).",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments passed to the tool. Values may be templates.",
    )

    # condition
    when: str | None = Field(default=None, description="Condition expression (not Python eval).")
    then: str | None = Field(
        default=None,
        description="Successor node id when the condition/approval is true.",
    )
    else_: str | None = Field(
        default=None,
        alias="else",
        description="Successor node id when the condition/approval is false.",
    )

    # transform
    template: str | None = Field(default=None, description="Transform output template.")
    source: str | dict[str, Any] | None = Field(
        default=None,
        description="Transform/document path, or ingest {kind, path, glob}.",
    )
    json_path: str | None = Field(default=None, description="JSON path applied during a transform.")
    parse_json: bool = Field(default=False, description="Parse the transform result as JSON.")

    # parallel
    branches: list[NodeSpec] = Field(
        default_factory=list,
        description="Child nodes run concurrently. Each branch needs a unique id.",
    )

    # include (sub-workflow)
    path: str | None = Field(
        default=None,
        description="Workspace-relative path of the included workflow file.",
    )
    call_inputs: dict[str, Any] = Field(
        default_factory=dict,
        alias="inputs",
        description="Inputs passed to the included workflow.",
    )

    # agent extras
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Extra provider:model refs if this agent's primary model fails.",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema object; agent output is parsed and validated.",
    )
    cache: bool | None = Field(
        default=None,
        description="Override workflow/settings LLM cache for this agent node.",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Allowlist of registry tool names this agent may call.",
    )
    max_tool_rounds: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Cap on agent tool-call rounds (default 8, max 20).",
    )

    # foreach
    items: str | None = Field(
        default=None,
        description="Template or input name of the list to iterate.",
    )
    max_items: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Maximum items this foreach will process (1–100).",
    )
    scale_items: int | None = Field(
        default=None,
        ge=101,
        le=100000,
        description="Opt-in foreach cap above 100. Default 32/100 is unchanged when omitted.",
    )
    concurrency: int | None = Field(
        default=None,
        ge=1,
        le=8,
        description="Opt-in foreach workers (1–8). Omitted means sequential.",
    )
    body: NodeSpec | None = Field(default=None, description="Node executed once per item.")
    rationale_key: str | None = Field(
        default=None,
        description="Optional output field that is the human-readable justification.",
    )

    # enterprise HITL (approval nodes only; ignored defaults keep 1.0 gates)
    approvals_required: int | None = Field(
        default=None,
        ge=1,
        le=32,
        description="Quorum size. Omitted means a single decision resolves the gate.",
    )
    distinct_actors: bool | None = Field(
        default=None,
        description="Refuse the same identity twice. Defaults true when quorum > 1.",
    )
    deny_actor: list[str] = Field(
        default_factory=list,
        description="Identities that may not vote (typically the run initiator).",
    )
    approver_roles: list[str] = Field(
        default_factory=list,
        description="Roles that may vote. Recorded on the pause as the eligible set.",
    )
    require: str | None = Field(
        default=None,
        description="any (default) or all across declared approver_roles.",
    )
    expires_in: str | None = Field(
        default=None,
        description="Lazy deadline (30m, 4h, 1d). Evaluated on resume/decide/status.",
    )
    on_expire: str | None = Field(
        default=None,
        description="reject, escalate, or fail. approve is refused at validation.",
    )
    escalate_to: list[str] = Field(
        default_factory=list,
        description="Workflow-declared roles used when on_expire is escalate.",
    )
    require_reason: bool = Field(
        default=False,
        description="Refuse a vote that omits a reason.",
    )
    reject_short_circuit: bool = Field(
        default=True,
        description="A single reject settles the gate (default).",
    )
    recommendation: str | None = Field(
        default=None,
        description="Model recommendation; a contradicting vote is recorded as override.",
    )
    notify: list[ApprovalNotifySpec] = Field(
        default_factory=list,
        description="Opt-in file/command/webhook pause notifications.",
    )

    # a2a (remote agent delegation)
    agent_url: str | None = Field(
        default=None,
        description="Remote A2A agent URL (card origin). type: a2a only.",
    )
    message: str | None = Field(
        default=None,
        description="Message submitted to the remote A2A agent. Templates allowed.",
    )
    on_input_required: str | None = Field(
        default=None,
        description="When the remote agent pauses: gate (default) or fail.",
    )
    token: str | None = Field(
        default=None,
        description="Optional bearer for the remote agent. Prefer READYAGENTS_A2A_TOKEN.",
    )

    # memory
    op: str | None = Field(
        default=None,
        description="memory op: write, read, search, or forget.",
    )
    scope: str | None = Field(
        default=None,
        description="Explicit memory scope (workflow:, ns:, or subject:). Templates allowed.",
    )
    scope_pattern: str | None = Field(
        default=None,
        description="Declared scope pattern the interpolated scope must match.",
    )
    query: str | None = Field(
        default=None,
        description="Search query for memory op=search. Templates allowed.",
    )
    text: str | None = Field(
        default=None,
        description="Payload for memory op=write. Templates allowed.",
    )
    ttl: str | None = Field(
        default=None,
        description="Memory TTL duration (30m, 4h, 90d).",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description="Max records or hits returned by memory read/search.",
    )
    embed: bool = Field(
        default=False,
        description="Request BYOK embedding search/write. Degrades to keyword if unavailable.",
    )
    context: ContextSpec | None = Field(
        default=None,
        description="Declared compaction budget for this memory node.",
    )

    # code (sandboxed Python)
    isolation: str | None = Field(
        default=None,
        description="code isolation: subprocess (default) or container (optional pack).",
    )
    source_from: str | None = Field(
        default=None,
        description="Output key whose text is the Python source for a code node.",
    )
    allow_imports: list[str] = Field(
        default_factory=list,
        description="Import allowlist for a code node. Empty means the default stdlib subset.",
    )
    network: bool = Field(
        default=False,
        description="If true, the code child may import network modules. Default denied.",
    )
    filesystem: dict[str, Any] | None = Field(
        default=None,
        description="code filesystem grants: {read: [...], write: [...]} under the workspace.",
    )
    limits: dict[str, Any] | None = Field(
        default=None,
        description="code limits: cpu_seconds, memory_mb, wall_seconds, output_bytes, nproc.",
    )
    require_isolation: str | None = Field(
        default=None,
        description="Minimum isolation tier. container fails closed without a runtime pack.",
    )
    contract: ContractSpec | None = Field(
        default=None,
        description=(
            "Opt-in output contract: structural schema, named content rules, "
            "and a declared action (fail, repair, fallback, gate, redact_and_continue)."
        ),
    )
    strategy: str | None = Field(
        default=None,
        description="team strategy: route, plan_then_execute, debate, or pipeline.",
    )
    supervisor: dict[str, Any] | None = Field(
        default=None,
        description="team supervisor: {prompt, model, system}.",
    )
    members: list[Any] | None = Field(
        default=None,
        description="Closed team member set. Nested type: team is refused.",
    )
    scratchpad: dict[str, Any] | None = Field(
        default=None,
        description="team scratchpad: {keys: [...]} plus per-member read/write grants.",
    )
    terminate: dict[str, Any] | None = Field(
        default=None,
        description="team terminate: max_rounds, max_cost_usd, max_wall_seconds, goal.",
    )
    media: list[Any] = Field(
        default_factory=list,
        description="Agent-only. Declared media parts (templates or hash refs) to attach.",
    )
    render: dict[str, Any] | None = Field(
        default=None,
        description="document render caps: dpi, max_pages, max_bytes_per_page.",
    )
    media_redact: dict[str, Any] | None = Field(
        default=None,
        description="Declared regions/classes redacted before persist, record, or send.",
    )
    chunk: dict[str, Any] | None = Field(
        default=None,
        description="ingest chunk: {strategy, max_chars, overlap}.",
    )
    on_change: str | None = Field(
        default=None,
        description="ingest versioning: supersede or keep_versions.",
    )
    freshness: dict[str, Any] | None = Field(
        default=None,
        description="Optional staleness gate: {max_age: 30d}.",
    )
    blend: dict[str, Any] | None = Field(
        default=None,
        description="Optional hybrid retrieval weights: {bm25, embedding}.",
    )
    column_schema: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description="Declared table column types: {name: int|str|float|bool}.",
    )
    columns: list[str] = Field(
        default_factory=list,
        description="table select column names.",
    )
    keys: list[str] = Field(
        default_factory=list,
        description="table dedupe/join/aggregate key columns.",
    )
    keep: str | None = Field(
        default=None,
        description="dedupe keep: first or last.",
    )
    how: str | None = Field(
        default=None,
        description="join how: inner or left.",
    )
    on: list[str] = Field(
        default_factory=list,
        description="join key column names shared by both tables.",
    )
    right: str | None = Field(
        default=None,
        description="Right table template for join/union.",
    )
    metrics: dict[str, Any] | None = Field(
        default=None,
        description="aggregate metrics: {column: sum|count|avg|min|max}.",
    )
    by: list[str] = Field(
        default_factory=list,
        description="sort column names.",
    )
    descending: bool = Field(
        default=False,
        description="sort descending when true.",
    )
    derive: dict[str, Any] | None = Field(
        default=None,
        description="derive: {name, expr}.",
    )
    rules: list[Any] | None = Field(
        default=None,
        description="classify deterministic rules: [{when, label}].",
    )
    model_for_remainder: dict[str, Any] | None = Field(
        default=None,
        description="classify remainder: {model, batch, labels}.",
    )
    on_row_error: str | None = Field(
        default=None,
        description="Row error policy: fail, skip, or quarantine.",
    )
    until: str | None = Field(
        default=None,
        description="Wait deadline: duration (72h) or ISO timestamp. Required on type: wait.",
    )
    for_event: dict[str, Any] | None = Field(
        default=None,
        description="Wait for a named signed event: {name, match}.",
    )
    for_file: dict[str, Any] | None = Field(
        default=None,
        description="Wait for a confined path: {path, on: created|changed}.",
    )
    for_run: dict[str, Any] | None = Field(
        default=None,
        description="Wait for another run: {run_id, status}.",
    )
    whichever: str | None = Field(
        default=None,
        description="Wait combination: first (default) or all.",
    )
    on_deadline: str | None = Field(
        default=None,
        description="Wait deadline action: fail, continue, escalate, or branch.",
    )
    default: Any = Field(
        default=None,
        description="Wait on_deadline: continue payload. Never grants an approval.",
    )

    @field_validator("for_file", mode="before")
    @classmethod
    def _wait_file_yaml_on(cls, value: Any) -> Any:
        """YAML 1.1 treats unquoted `on` as boolean True; accept it as the key 'on'."""
        if not isinstance(value, dict):
            return value
        remapped: dict[Any, Any] = {}
        for key, item in value.items():
            if key is True:
                remapped["on"] = item
            else:
                remapped[key] = item
        return remapped

    @field_validator("id")
    @classmethod
    def _id_token(cls, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid node id '{value}' (use letters, numbers, _ or -)")
        return value

    @field_validator("type")
    @classmethod
    def _type_token(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid node type '{value}'")
        return cleaned

    @field_validator("tools")
    @classmethod
    def _tools_tokens(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = str(raw).strip()
            if not name:
                raise ValueError("tool names must be non-empty")
            token = name.replace("_", "").replace("-", "").replace(".", "")
            if not token.isalnum():
                raise ValueError(f"Invalid tool name '{name}' (use letters, numbers, _, -, .)")
            if name in seen:
                raise ValueError(f"Duplicate tool name '{name}'")
            seen.add(name)
            cleaned.append(name)
        return cleaned

    @model_validator(mode="after")
    def _type_fields(self) -> NodeSpec:
        t = self.type
        if t != NodeType.agent.value and self.tools:
            raise ValueError(f"Node '{self.id}': 'tools' is only valid on agent nodes")
        if t == NodeType.agent.value and not self.prompt:
            raise ValueError(f"Node '{self.id}': agent nodes require 'prompt'")
        if t == NodeType.tool.value and not self.tool:
            raise ValueError(f"Node '{self.id}': tool nodes require 'tool'")
        if t == NodeType.condition.value:
            if not self.when:
                raise ValueError(f"Node '{self.id}': condition nodes require 'when'")
            if not self.then and not self.else_:
                raise ValueError(f"Node '{self.id}': condition nodes require 'then' and/or 'else'")
        if t == NodeType.transform.value:
            if self.template is None and not self.json_path and not self.parse_json:
                raise ValueError(
                    f"Node '{self.id}': transform nodes require "
                    "'template', 'json_path', or parse_json"
                )
        if t == NodeType.approval.value:
            if not self.prompt:
                raise ValueError(f"Node '{self.id}': approval nodes require 'prompt'")
            if not self.then and not self.else_ and not self.next:
                raise ValueError(
                    f"Node '{self.id}': approval nodes require 'then', 'else', or 'next'"
                )
            self._validate_hitl()
        elif t == NodeType.wait.value:
            if not (self.until or "").strip():
                raise ValueError(f"Node '{self.id}': wait nodes require 'until' (a deadline)")
            which = str(self.whichever or "first").strip().lower()
            if which not in {"first", "all"}:
                raise ValueError(f"Node '{self.id}': whichever must be first or all")
            self.whichever = which
            action = str(self.on_deadline or "fail").strip().lower()
            if action not in {"fail", "continue", "escalate", "branch"}:
                raise ValueError(
                    f"Node '{self.id}': on_deadline must be fail, continue, escalate, or branch"
                )
            self.on_deadline = action
            if action == "escalate" and not self.escalate_to:
                raise ValueError(f"Node '{self.id}': on_deadline: escalate requires escalate_to")
        elif self._hitl_declared():
            raise ValueError(
                f"Node '{self.id}': quorum/expiry/delegation fields "
                "are only valid on approval nodes"
            )
        if t == NodeType.parallel.value and not self.branches:
            raise ValueError(f"Node '{self.id}': parallel nodes require 'branches'")
        if t == NodeType.include.value and not self.path:
            raise ValueError(f"Node '{self.id}': include nodes require 'path'")
        if t == NodeType.foreach.value:
            if not self.items:
                raise ValueError(f"Node '{self.id}': foreach nodes require 'items'")
            if self.body is None:
                raise ValueError(f"Node '{self.id}': foreach nodes require 'body'")
            if self.body.type == NodeType.foreach.value:
                raise ValueError(f"Node '{self.id}': nested foreach is not supported")
        if t == NodeType.a2a.value:
            if not (self.agent_url or "").strip():
                raise ValueError(f"Node '{self.id}': a2a nodes require 'agent_url'")
            mode = (self.on_input_required or "gate").strip().lower()
            if mode not in {"gate", "fail"}:
                raise ValueError(f"Node '{self.id}': on_input_required must be 'gate' or 'fail'")
            self.on_input_required = mode
        if t == NodeType.memory.value:
            op = (self.op or "").strip().lower()
            if op not in {"write", "read", "search", "forget"}:
                raise ValueError(
                    f"Node '{self.id}': memory nodes require op write, read, search, or forget"
                )
            self.op = op
            if not (self.scope or "").strip():
                raise ValueError(f"Node '{self.id}': memory nodes require 'scope'")
            if op == "write" and not (self.text or self.source or self.prompt):
                raise ValueError(f"Node '{self.id}': memory write requires 'text'")
            if op == "search" and not (self.query or self.prompt):
                raise ValueError(f"Node '{self.id}': memory search requires 'query'")
            if self.ttl:
                from readyagents.approvals.gate import parse_expires_in

                parse_expires_in(self.ttl)
        if t == NodeType.team.value:
            from readyagents.team.spec import parse_team

            parse_team(self)
        elif self.strategy or self.members or self.supervisor:
            raise ValueError(
                f"Node '{self.id}': strategy/members/supervisor are only valid on team nodes"
            )
        if t == NodeType.document.value:
            if not isinstance(self.source, str) or not self.source.strip():
                raise ValueError(f"Node '{self.id}': document nodes require 'source'")
        if t == NodeType.transcribe.value:
            if not isinstance(self.source, str) or not self.source.strip():
                raise ValueError(f"Node '{self.id}': transcribe nodes require 'source'")
        if t == NodeType.ingest.value:
            if not self.source:
                raise ValueError(f"Node '{self.id}': ingest nodes require 'source'")
            if not (self.scope or "").strip():
                raise ValueError(f"Node '{self.id}': ingest nodes require 'scope'")
            if self.chunk:
                strategy = str(self.chunk.get("strategy") or "").strip().lower().replace("-", "_")
                if strategy and strategy not in {"fixed", "paragraph", "heading", "row_group"}:
                    raise ValueError(
                        f"Node '{self.id}': chunk.strategy must be "
                        "fixed, paragraph, heading, or row_group"
                    )
            change = str(self.on_change or "supersede").strip().lower()
            if change not in {"supersede", "keep_versions"}:
                raise ValueError(f"Node '{self.id}': on_change must be supersede or keep_versions")
            self.on_change = change
        if t == NodeType.table.value:
            op = (self.op or "").strip().lower()
            allowed = {
                "read",
                "write",
                "select",
                "filter",
                "join",
                "aggregate",
                "sort",
                "dedupe",
                "union",
                "derive",
            }
            if op not in allowed:
                raise ValueError(
                    f"Node '{self.id}': table nodes require op read, write, "
                    "select, filter, join, aggregate, sort, dedupe, union, or derive"
                )
            self.op = op
            if op == "read" and not self.source:
                raise ValueError(f"Node '{self.id}': table read requires 'source'")
            if op == "filter" and not self.when:
                raise ValueError(f"Node '{self.id}': table filter requires 'when'")
            keep = str(self.keep or "first").strip().lower()
            if keep not in {"first", "last"}:
                raise ValueError(f"Node '{self.id}': keep must be first or last")
            self.keep = keep
            how = str(self.how or "inner").strip().lower()
            if how not in {"inner", "left"}:
                raise ValueError(f"Node '{self.id}': how must be inner or left")
            self.how = how
        if t == NodeType.classify.value:
            if not self.source:
                raise ValueError(f"Node '{self.id}': classify nodes require 'source'")
            err = str(self.on_row_error or "fail").strip().lower()
            if err not in {"fail", "skip", "quarantine"}:
                raise ValueError(
                    f"Node '{self.id}': on_row_error must be fail, skip, or quarantine"
                )
            self.on_row_error = err
        if self.media and t != NodeType.agent.value:
            raise ValueError(f"Node '{self.id}': 'media' is only valid on agent nodes")
        if self.render is not None and t != NodeType.document.value:
            raise ValueError(f"Node '{self.id}': 'render' is only valid on document nodes")
        return self

    def _hitl_declared(self) -> bool:
        if self.approvals_required is not None:
            return True
        if self.distinct_actors is not None:
            return True
        if self.deny_actor or self.approver_roles or self.escalate_to or self.notify:
            return True
        if self.expires_in or self.on_expire or self.require_reason or self.recommendation:
            return True
        if self.require not in (None, "any"):
            return True
        if self.reject_short_circuit is False:
            return True
        return False

    def _validate_hitl(self) -> None:
        if self.require is not None and self.require.strip().lower() not in {"any", "all"}:
            raise ValueError(f"Node '{self.id}': require must be 'any' or 'all'")
        if self.require is not None:
            self.require = self.require.strip().lower()
        expire = (self.on_expire or "").strip().lower()
        if self.on_expire is not None:
            if expire == "approve":
                raise ValueError(
                    f"Node '{self.id}': on_expire: approve is refused "
                    "(an attacker who can stall a gate must never gain an approval)"
                )
            if expire not in {"reject", "escalate", "fail"}:
                raise ValueError(f"Node '{self.id}': on_expire must be reject, escalate, or fail")
            self.on_expire = expire
        if self.expires_in:
            from readyagents.approvals.gate import parse_expires_in

            parse_expires_in(self.expires_in)
        if expire == "escalate" and not self.escalate_to:
            raise ValueError(f"Node '{self.id}': on_expire: escalate requires escalate_to")
        for spec in self.notify:
            if spec.kind == "file" and not spec.path:
                raise ValueError(f"Node '{self.id}': file notify requires path")
            if spec.kind == "command" and not spec.command:
                raise ValueError(f"Node '{self.id}': command notify requires command")
            if spec.kind == "webhook" and not spec.url:
                raise ValueError(f"Node '{self.id}': webhook notify requires url")


class TriggerAcceptsSpec(BaseModel):
    """Which event kind and payload shape a trigger accepts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: str = Field(description="webhook, file, queue, or schedule.")
    payload_schema: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description="JSON Schema fragment for the event object (type: object).",
    )

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        cleaned = str(value or "").strip().lower()
        if cleaned not in {"webhook", "file", "queue", "schedule"}:
            raise ValueError("accepts.kind must be webhook, file, queue, or schedule")
        return cleaned

    @field_validator("payload_schema")
    @classmethod
    def _schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("accepts.schema must be a mapping")
        kind = value.get("type")
        if kind is not None and str(kind).strip().lower() != "object":
            raise ValueError("accepts.schema type must be object")
        required = value.get("required")
        if required is not None:
            if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
                raise ValueError("accepts.schema required must be a list of strings")
        return value


class TriggerBudgetSpec(BaseModel):
    """Per-trigger spend ceiling. Distinct from the workflow budget."""

    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)


class TriggerSpec(BaseModel):
    """Declared event contract. Core validates; core starts no listener."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(description="Trigger name recorded on run provenance.")
    accepts: TriggerAcceptsSpec
    require_signature: bool = Field(default=False)
    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="Map event fields to workflow inputs via {{ event.* }} templates.",
    )
    idempotency_key: str = Field(description="Required template; missing is a schema error.")
    idempotency_window: str = Field(default="24h")
    budget: TriggerBudgetSpec | None = None
    concurrency: int = Field(default=1, ge=1, le=64)
    on_ceiling: str = Field(
        default="defer",
        description="When concurrency is full: drop (refuse) or defer (bounded queue).",
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        token = str(value or "").strip()
        if not token.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid trigger name '{value}'")
        return token

    @field_validator("idempotency_key")
    @classmethod
    def _idem(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("idempotency_key is required")
        return text

    @field_validator("idempotency_window")
    @classmethod
    def _window(cls, value: str) -> str:
        from readyagents.approvals.gate import parse_expires_in

        text = str(value or "").strip() or "24h"
        parse_expires_in(text)
        return text

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: dict[str, str]) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("inputs must be a mapping of name to template string")
        out: dict[str, str] = {}
        for key, item in value.items():
            if not str(key).strip():
                raise ValueError("trigger input names must be non-empty")
            if not isinstance(item, str):
                raise ValueError(f"trigger input '{key}' mapping must be a string template")
            out[str(key)] = item
        return out

    @field_validator("on_ceiling")
    @classmethod
    def _ceiling(cls, value: str) -> str:
        cleaned = str(value or "defer").strip().lower()
        if cleaned not in {"drop", "defer"}:
            raise ValueError("on_ceiling must be drop or defer")
        return cleaned


class EdgeSpec(BaseModel):
    """Optional explicit edge between two nodes."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", description="Source node id.")
    to: str = Field(description="Destination node id.")
    when: str | None = Field(default=None, description="Optional condition for taking this edge.")


class WorkflowSpec(BaseModel):
    """A validated workflow document."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(description="Workflow name stored on the run record.")
    version: str = Field(
        default="1",
        description="Free-form workflow version string (not the schema id).",
    )
    description: str | None = Field(default=None, description="Human-readable description.")
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Default inputs. A value may be a scalar or {default, description}.",
    )
    required_inputs: list[str] = Field(
        default_factory=list,
        description="Input keys that must be present after defaults and CLI overrides.",
    )
    start: str | None = Field(
        default=None,
        description="First node id (defaults to the first node).",
    )
    nodes: list[NodeSpec] = Field(description="Nodes in the workflow graph.")
    edges: list[EdgeSpec] = Field(
        default_factory=list,
        description="Optional explicit edges {from, to, when}.",
    )
    mcp_servers: dict[str, MCPServerSpec] = Field(
        default_factory=dict,
        description="Named stdio MCP servers available to this workflow.",
    )
    allow_http: bool = Field(default=False, description="Enable the builtin http_get tool.")
    workspace: str | None = Field(
        default=None,
        description="Sandbox directory; must stay under the configured workspace root.",
    )
    default_model: str | None = Field(
        default=None,
        description="Default LLM ref (provider:model) for agent nodes.",
    )
    budget: BudgetSpec | None = Field(
        default=None,
        description="Optional token/cost budget for LLM calls.",
    )
    runaway: RunawaySpec | None = Field(
        default=None,
        description="Optional per-run model-call, tool-round, and wall-clock guards.",
    )
    fallback_models: list[str] = Field(
        default_factory=list,
        description="Workflow-level fallback LLM refs after the primary fails.",
    )
    routing: RoutingSpec | None = Field(
        default=None,
        description=(
            "Optional model routing policy. Absent: legacy node.model / "
            "default_model / fallback_models selection is unchanged."
        ),
    )
    circuit: CircuitSpec | None = Field(
        default=None,
        description="Process-local LLM circuit breaker.",
    )
    on_pause_url: str | None = Field(
        default=None,
        description="Outbound webhook URL when an approval node pauses.",
    )
    cache_llm: bool | None = Field(
        default=None,
        description="Opt in to the local LLM response cache.",
    )
    redact: bool | None = Field(
        default=None,
        description="Opt in to PII redaction for this workflow.",
    )
    memory_scopes: list[str] = Field(
        default_factory=list,
        description="Declared memory scope patterns this workflow may use.",
    )
    media: MediaPolicySpec | None = Field(
        default=None,
        description="Optional media caps and redaction policy. Absent: engine defaults.",
    )
    triggers: list[TriggerSpec] = Field(
        default_factory=list,
        description=(
            "Optional event contracts that may start this workflow. Core validates "
            "and decides; core starts no listener."
        ),
    )

    @model_validator(mode="after")
    def _graph(self) -> WorkflowSpec:
        if not self.nodes:
            raise ValueError("Workflow must declare at least one node")
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate node ids")
        known = set(ids)
        start = self.start or self.nodes[0].id
        if start not in known:
            raise ValueError(f"start node '{start}' does not exist")
        self.start = start
        for node in self.nodes:
            for ref in (node.next, node.then, node.else_):
                if ref is not None and ref not in known:
                    raise ValueError(f"Node '{node.id}' references unknown node '{ref}'")
            if node.branches:
                branch_ids = [b.id for b in node.branches]
                if len(branch_ids) != len(set(branch_ids)):
                    raise ValueError(f"Node '{node.id}': duplicate parallel branch ids")
        for edge in self.edges:
            if edge.from_ not in known:
                raise ValueError(f"Edge from unknown node '{edge.from_}'")
            if edge.to not in known:
                raise ValueError(f"Edge to unknown node '{edge.to}'")
        self._assert_acyclic()
        names = [t.name for t in self.triggers]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate trigger names")
        return self

    def _assert_acyclic(self) -> None:
        graph: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for node in self.nodes:
            for ref in (node.next, node.then, node.else_):
                if ref is not None:
                    graph[node.id].append(ref)
        for edge in self.edges:
            graph[edge.from_].append(edge.to)

        visiting: set[str] = set()
        done: set[str] = set()

        def dfs(nid: str) -> None:
            visiting.add(nid)
            for nxt in graph[nid]:
                if nxt in visiting:
                    raise ValueError(f"Cycle detected at node '{nxt}'")
                if nxt not in done and nxt in graph:
                    dfs(nxt)
            visiting.remove(nid)
            done.add(nid)

        for nid in graph:
            if nid not in done:
                dfs(nid)

    def node_map(self) -> dict[str, NodeSpec]:
        return {n.id: n for n in self.nodes}

    def input_defaults(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        for key, raw in self.inputs.items():
            if isinstance(raw, dict) and "default" in raw:
                defaults[key] = raw["default"]
            else:
                defaults[key] = raw
        return defaults


def validate_required_inputs(workflow: WorkflowSpec, provided: dict[str, Any]) -> None:
    missing = [name for name in workflow.required_inputs if name not in provided]
    if missing:
        example = " ".join(f"--input {name}=..." for name in missing)
        raise WorkflowError(f"Missing required inputs: {', '.join(missing)}. Pass {example}.")


NodeSpec.model_rebuild()
