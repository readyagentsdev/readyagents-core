"""Typed errors for ReadyAgents Core."""

from __future__ import annotations

from typing import Any


class ReadyAgentsError(Exception):
    """Base error for all ReadyAgents failures."""

    run_id: str | None = None
    state: object | None = None


def missing_extra_message(label: str, extra: str) -> str:
    """Install hint for an optional extra. Distribution name is readyagentsdev."""
    return f"The {label} extra is not installed. Run: pip install 'readyagentsdev[{extra}]'"


class ConfigError(ReadyAgentsError):
    """Invalid configuration, missing settings, or unreadable files."""


class PathError(ConfigError):
    """Path is outside a workspace root or uses a forbidden platform form."""


class AtomicWriteError(ConfigError):
    """Atomic replace failed (sharing violation, leftover temp, or OS error)."""


class WorkflowError(ReadyAgentsError):
    """Invalid workflow definition or graph execution problem."""

    def __init__(self, message: str, *, problems: list[Any] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


class GateExpired(WorkflowError):
    """A gate hit on_expire: fail. Distinct from a human reject."""


class NodeError(ReadyAgentsError):
    """A single node failed after retries / timeout."""

    def __init__(self, node_id: str, message: str, *, cause: BaseException | None = None) -> None:
        self.node_id = node_id
        self.cause = cause
        self.problems = list(getattr(cause, "problems", None) or [])
        super().__init__(f"Node '{node_id}': {message}")


class LLMError(ReadyAgentsError):
    """LLM provider, model, or API-key failure."""


class EgressDenied(ReadyAgentsError):
    """Sovereign mode refused a non-loopback, non-allowlisted connect."""

    def __init__(self, destination: str, *, node_id: str | None = None) -> None:
        self.destination = destination
        self.node_id = node_id
        where = f" at node '{node_id}'" if node_id else ""
        super().__init__(f"Sovereign mode denied egress to {destination}{where}")


class MCPError(ReadyAgentsError):
    """MCP client or server failure."""


class TemplateError(ReadyAgentsError):
    """Template interpolation failed (missing variable or bad path)."""


class ToolError(ReadyAgentsError):
    """Builtin or MCP tool invocation failed."""


class ConnectorCapError(ToolError):
    """Connector response, page count, or payload exceeded a declared cap."""


class ConnectorAuthError(ToolError):
    """Connector requested a secret that was not granted."""


class A2AError(ReadyAgentsError):
    """A2A card, task, or delegation failure. Not a second run store."""


class A2ACardError(A2AError):
    """Remote or local Agent Card is invalid, hostile, or oversized."""


class A2ATransitionError(A2AError):
    """Illegal A2A task state transition."""


class MemoryError(ReadyAgentsError):
    """Memory store, scope, or compaction failure. Not a second run store."""


class MemoryScopeError(MemoryError):
    """Templated scope escaped its declared pattern, kind, or safe token."""


class ApprovalRequired(ReadyAgentsError):
    """An approval node is waiting for an explicit operator decision."""

    def __init__(
        self,
        node_id: str,
        run_id: str,
        prompt: str,
        *,
        state: object | None = None,
        pause: dict | None = None,
    ) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.prompt = prompt
        self.state = state
        self.pause = pause
        super().__init__(
            f"Approval required at node '{node_id}' (run {run_id}). {prompt} "
            f"Resume with: readyagents resume {run_id} --approve {node_id} "
            f"(or --reject {node_id})"
        )


class BudgetExceeded(ReadyAgentsError):
    """An LLM call was blocked because the run is over its token or cost budget."""

    def __init__(
        self,
        kind: str,
        used: int,
        limit: int,
        *,
        reason: str | None = None,
    ) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        self.reason = reason
        super().__init__(f"Budget exceeded: {kind} used={used} limit={limit}")


class RunawayGuard(ReadyAgentsError):
    """A per-run loop/call/wall-clock guard fired. Distinct from BudgetExceeded."""

    def __init__(self, kind: str, used: int, limit: int) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"Runaway guard: {kind} used={used} limit={limit}")


class AuthorizationError(ReadyAgentsError):
    """An RBAC hook denied run, resume, approve, or reject."""

    def __init__(self, actor: str | None, action: str, resource: str) -> None:
        self.actor = actor
        self.action = action
        self.resource = resource
        who = actor if actor else "(anonymous)"
        super().__init__(f"Actor '{who}' is not allowed to {action} '{resource}'")


class IdentityError(ConfigError):
    """Identity verification or trust-anchor failure. Always fail closed."""


class TrustError(ConfigError):
    """Supply-chain verification failed. Always fail closed under enforcement."""

    def __init__(
        self,
        message: str,
        *,
        artifact: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.artifact = artifact
        self.reason = reason
        super().__init__(message)


class PolicyError(ConfigError):
    """A policy file is missing, malformed, or unreadable (fail closed)."""


class PolicyDenied(ReadyAgentsError):
    """The engine firewall denied a tool call."""

    def __init__(self, node_id: str, message: str, *, rule: str | None = None) -> None:
        self.node_id = node_id
        self.rule = rule
        super().__init__(f"Node '{node_id}': {message}")


class StructuredOutputError(NodeError):
    """An agent node's LLM output did not match its Pydantic/JSON schema."""


class CircuitOpen(LLMError):
    """A model is skipped because its circuit breaker is open."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"Circuit breaker open for model '{model}'")


class CancellationRequested(ReadyAgentsError):
    """Cooperative cancellation reached an engine safe point."""

    def __init__(
        self,
        message: str = "Run cancellation requested",
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.reason = reason
        super().__init__(message)
        self.run_id = run_id


class GovernorBackpressure(ReadyAgentsError):
    """Bounded concurrency queue is full, or acquire timed out. Not a retry storm."""


class GovernorShutdown(ReadyAgentsError):
    """The concurrency governor is draining; new runs are refused."""


class RunConflict(ReadyAgentsError):
    """Run cannot accept this mutation (wrong status, in-flight resume, idempotency mismatch)."""


class CassetteError(ConfigError):
    """Cassette is missing, corrupt, oversized, or cannot be written."""


class CassetteMiss(CassetteError):
    """Offline replay had no matching cassette entry. Never falls through to a live call."""

    def __init__(
        self,
        message: str,
        *,
        node_id: str | None = None,
        reason: str | None = None,
        nearest_key: str | None = None,
    ) -> None:
        self.node_id = node_id
        self.reason = reason or message
        self.nearest_key = nearest_key
        super().__init__(message)


class ForkError(WorkflowError):
    """Fork refused: node never ran, occurrence is ambiguous, or a parallel interior."""


class HttpAuthError(MCPError):
    """Missing or invalid HTTP bearer credentials."""


class HttpRequestError(MCPError):
    """Malformed HTTP extension request (bad JSON, unknown fields, bad id, limits)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


class UnsupportedProtocolVersionError(MCPError):
    """JSON-RPC ``-32022``: requested MCP protocol version is not honoured."""

    code = -32022

    def __init__(self, requested: str, supported: tuple[str, ...] | list[str]) -> None:
        self.requested = requested
        self.supported = tuple(supported)
        super().__init__("Unsupported protocol version")


class HeaderMismatchError(MCPError):
    """JSON-RPC ``-32020``: ``Mcp-Method`` / ``Mcp-Name`` disagree with the body."""

    code = -32020

    def __init__(self, message: str = "MCP header does not match the JSON-RPC body") -> None:
        super().__init__(message)


class TaskStateError(MCPError):
    """JSON-RPC ``-32023``: ``tasks/update`` is not valid in the current task state."""

    code = -32023

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class RunStoreError(ReadyAgentsError):
    """Run persistence backend failure."""


class RunStoreConflict(RunStoreError):
    """Optimistic concurrency failure (revision mismatch or missing row)."""


class SourceMapBoundError(WorkflowError):
    """YAML compose/index exceeded node-count or nesting-depth bounds."""
