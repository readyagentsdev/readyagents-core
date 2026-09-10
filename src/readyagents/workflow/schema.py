"""Pydantic models for YAML/JSON workflow definitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class NodeSpec(BaseModel):
    """One node in the workflow graph."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(min_length=1, description="Unique node id (letters, numbers, _ or -).")
    type: str = Field(
        description=(
            "Node kind. Built-ins: agent, tool, condition, transform, approval, "
            "parallel, include, foreach, a2a, memory. Packs may add types."
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
    source: str | None = Field(
        default=None,
        description="Optional input value or template for a transform.",
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
