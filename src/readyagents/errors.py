"""Typed errors for ReadyAgents Core."""

from __future__ import annotations


class ReadyAgentsError(Exception):
    """Base error for all ReadyAgents failures."""

    run_id: str | None = None
    state: object | None = None


class ConfigError(ReadyAgentsError):
    """Invalid configuration, missing settings, or unreadable files."""


class WorkflowError(ReadyAgentsError):
    """Invalid workflow definition or graph execution problem."""


class NodeError(ReadyAgentsError):
    """A single node failed after retries / timeout."""

    def __init__(self, node_id: str, message: str, *, cause: BaseException | None = None) -> None:
        self.node_id = node_id
        self.cause = cause
        super().__init__(f"Node '{node_id}': {message}")


class LLMError(ReadyAgentsError):
    """LLM provider, model, or API-key failure."""


class MCPError(ReadyAgentsError):
    """MCP client or server failure."""


class TemplateError(ReadyAgentsError):
    """Template interpolation failed (missing variable or bad path)."""


class ToolError(ReadyAgentsError):
    """Builtin or MCP tool invocation failed."""


class ApprovalRequired(ReadyAgentsError):
    """An approval node is waiting for an explicit operator decision."""

    def __init__(
        self,
        node_id: str,
        run_id: str,
        prompt: str,
        *,
        state: object | None = None,
    ) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.prompt = prompt
        self.state = state
        super().__init__(
            f"Approval required at node '{node_id}' (run {run_id}). {prompt} "
            f"Resume with: readyagents resume {run_id} --approve {node_id} "
            f"(or --reject {node_id})"
        )


class BudgetExceeded(ReadyAgentsError):
    """An LLM call was blocked because the run is over its token or cost budget."""

    def __init__(self, kind: str, used: int, limit: int) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"Budget exceeded: {kind} used={used} limit={limit}")


class AuthorizationError(ReadyAgentsError):
    """An RBAC hook denied run, resume, approve, or reject."""

    def __init__(self, actor: str | None, action: str, resource: str) -> None:
        self.actor = actor
        self.action = action
        self.resource = resource
        who = actor if actor else "(anonymous)"
        super().__init__(f"Actor '{who}' is not allowed to {action} '{resource}'")


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


class RunConflict(ReadyAgentsError):
    """Run cannot accept this mutation (wrong status, in-flight resume, idempotency mismatch)."""


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
