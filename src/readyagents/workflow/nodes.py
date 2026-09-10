"""Execute individual node types."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from readyagents.errors import (
    A2AError,
    ApprovalRequired,
    AuthorizationError,
    BudgetExceeded,
    CancellationRequested,
    CassetteMiss,
    CircuitOpen,
    EgressDenied,
    GateExpired,
    LLMError,
    MemoryError,
    NodeError,
    PolicyDenied,
    ReadyAgentsError,
    RunawayGuard,
    TemplateError,
    ToolError,
    TrustError,
    WorkflowError,
)
from readyagents.llm.base import CompletionResult, LLMProvider, Message, ToolCall
from readyagents.llm.cache import LLMCache
from readyagents.llm.registry import get_provider
from readyagents.llm.resilience import (
    check_budget,
    model_candidates,
    model_id_for,
    normalize_usage,
    raise_exhausted,
    usage_nonzero,
)
from readyagents.llm.tool_calls import spec_from_tool
from readyagents.logging import get_logger, log_event
from readyagents.tools import ToolRegistry
from readyagents.workflow.cancellation import CancellationToken, cancellable_sleep
from readyagents.workflow.schema import NodeSpec, NodeType, WorkflowSpec
from readyagents.workflow.state import RunState
from readyagents.workflow.structured import validate_structured_output
from readyagents.workflow.templates import interpolate, interpolate_value, lookup, resolve_path

log = get_logger("nodes")

_APPROVE_VALUES = {"approve", "approved", "yes", "true", "accept", "ok"}
_REJECT_VALUES = {"reject", "rejected", "deny", "denied", "no", "false"}
_MAX_INCLUDE_DEPTH = 8
_MAX_PARALLEL = 8
_DEFAULT_MAX_FOREACH = 32
_HARD_MAX_FOREACH = 100
_FOREACH_META = "_foreach"
_INCLUDE_META = "_include"
_PARALLEL_META = "_parallel"
# Dry-run still walks the graph; these tools must not hit the network or disk.
_DRY_RUN_STUB_TOOLS = frozenset({"http_get", "write_file"})
_DEFAULT_MAX_TOOL_ROUNDS = 8
_HARD_MAX_TOOL_ROUNDS = 20


class ExecutionContext:
    def __init__(
        self,
        workflow: WorkflowSpec,
        tools: ToolRegistry,
        *,
        dry_run: bool = False,
        llm: LLMProvider | None = None,
        default_model: str | None = None,
        extra_handlers: Mapping[str, Any] | None = None,
        decisions: Mapping[str, str] | None = None,
        on_persist: Callable[[RunState], None] | None = None,
        workflow_dir: Path | None = None,
        include_depth: int = 0,
        circuit_breaker: Any | None = None,
        llm_cache: LLMCache | None = None,
        budget_tokens: int | None = None,
        budget_cost_micros: int | None = None,
        secrets: Any | None = None,
        authorizer: Any | None = None,
        actor: str | None = None,
        redactor: Any | None = None,
        auditor: Callable[..., None] | None = None,
        on_pause: Callable[..., None] | None = None,
        fallback_models: list[str] | None = None,
        cache_llm: bool = False,
        usage_state: RunState | None = None,
        cancellation: CancellationToken | None = None,
        cassette: Any | None = None,
        offline: bool = False,
        recording: bool = False,
        cassette_secrets: list[str] | None = None,
        policy: Any | None = None,
        pin_digests: dict[str, str] | None = None,
        mcp_descriptions: dict[str, str] | None = None,
        pin_home: Path | None = None,
        observers: list[Any] | None = None,
        spend_meter: Any | None = None,
        labels: Mapping[str, str] | None = None,
        verified_actor: Any | None = None,
        credential_policy: Any | None = None,
        credential_env: Mapping[str, str | None] | None = None,
        include_buffers: Mapping[str, str] | None = None,
        require_signed: bool = False,
        frozen: bool = False,
        vote_reasons: Mapping[str, str] | None = None,
        vote_signature_status: str = "unsigned",
    ) -> None:
        self.workflow = workflow
        self.tools = tools
        self.dry_run = dry_run
        self.llm = llm
        self.default_model = default_model or workflow.default_model
        self.extra_handlers = dict(extra_handlers or {})
        self.decisions = {str(k): str(v).strip().lower() for k, v in dict(decisions or {}).items()}
        self.on_persist = on_persist
        self.workflow_dir = Path(workflow_dir) if workflow_dir else Path.cwd()
        self.include_depth = include_depth
        self.circuit_breaker = circuit_breaker
        self.llm_cache = llm_cache
        self.budget_tokens = budget_tokens
        self.budget_cost_micros = budget_cost_micros
        self.secrets = secrets
        self.authorizer = authorizer
        self.actor = actor
        self.redactor = redactor
        self.auditor = auditor
        self.on_pause = on_pause
        self.fallback_models = list(fallback_models or [])
        self.cache_llm = cache_llm
        self.usage_state = usage_state
        self.cancellation = cancellation
        self.cassette = cassette
        self.offline = offline
        self.recording = recording
        self.cassette_secrets = list(cassette_secrets or [])
        self.policy = policy
        self.pin_digests = dict(pin_digests or {})
        self.mcp_descriptions = dict(mcp_descriptions or {})
        self.pin_home = Path(pin_home) if pin_home else None
        self.observers = list(observers or [])
        self.spend_meter = spend_meter
        self.labels = {str(k): str(v) for k, v in dict(labels or {}).items()}
        self.verified_actor = verified_actor
        self.credential_policy = credential_policy
        self.credential_env = dict(credential_env) if credential_env is not None else None
        self.include_buffers = {str(k): str(v) for k, v in dict(include_buffers or {}).items()}
        self.require_signed = bool(require_signed)
        self.frozen = bool(frozen)
        self.vote_reasons = {str(k): str(v) for k, v in dict(vote_reasons or {}).items()}
        self.vote_signature_status = str(vote_signature_status or "unsigned")
        self.last_credential_kind: str | None = None
        self.last_tool_rounds: list[dict[str, Any]] = []
        self._persist_lock = threading.RLock()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def enter_node_body(self) -> None:
        """Mark a node body (not retry backoff) as executing."""
        with self._in_flight_lock:
            self._in_flight += 1

    def leave_node_body(self) -> None:
        with self._in_flight_lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    def node_body_in_flight(self) -> bool:
        """True while a node body is executing; False during retry backoff."""
        with self._in_flight_lock:
            return self._in_flight > 0

    def decision_for(self, node_id: str) -> str | None:
        value = self.decisions.get(node_id)
        if (
            value
            and self.usage_state is not None
            and isinstance(getattr(self.usage_state, "metadata", None), dict)
            and self.usage_state.metadata.get("mcp_consume_decisions")
        ):
            self.decisions.pop(node_id, None)
        return value if value else None

    def child(
        self,
        workflow: WorkflowSpec,
        *,
        workflow_dir: Path,
        include_depth: int,
        on_persist: Callable[[RunState], None] | None = None,
    ) -> ExecutionContext:
        return ExecutionContext(
            workflow,
            self.tools,
            dry_run=self.dry_run,
            llm=self.llm,
            default_model=self.default_model or workflow.default_model,
            extra_handlers=self.extra_handlers,
            decisions=self.decisions,
            on_persist=on_persist,
            workflow_dir=workflow_dir,
            include_depth=include_depth,
            circuit_breaker=self.circuit_breaker,
            llm_cache=self.llm_cache,
            budget_tokens=self.budget_tokens,
            budget_cost_micros=self.budget_cost_micros,
            secrets=self.secrets,
            authorizer=self.authorizer,
            actor=self.actor,
            redactor=self.redactor,
            auditor=self.auditor,
            on_pause=self.on_pause,
            fallback_models=self.fallback_models,
            cache_llm=self.cache_llm,
            usage_state=self.usage_state,
            cancellation=self.cancellation,
            cassette=self.cassette,
            offline=self.offline,
            recording=self.recording,
            cassette_secrets=self.cassette_secrets,
            policy=self.policy,
            pin_digests=self.pin_digests,
            mcp_descriptions=self.mcp_descriptions,
            pin_home=self.pin_home,
            observers=self.observers,
            spend_meter=self.spend_meter,
            labels=self.labels,
            verified_actor=self.verified_actor,
            credential_policy=self.credential_policy,
            credential_env=self.credential_env,
            include_buffers=self.include_buffers,
            require_signed=self.require_signed,
            frozen=self.frozen,
            vote_reasons=self.vote_reasons,
            vote_signature_status=self.vote_signature_status,
        )


def _maybe_node_gate(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> None:
    policy = getattr(ctx, "policy", None)
    if policy is None:
        return
    rule = (policy.nodes or {}).get(node.id)
    if rule is None or not rule.require_approval:
        return
    raw = ctx.decision_for(node.id)
    if raw is None:
        from readyagents.errors import ApprovalRequired

        raise ApprovalRequired(
            node.id,
            state.run_id,
            f"Policy requires approval before node '{node.id}' "
            f"(rule nodes.{node.id}.require_approval)",
            state=state,
        )
    if raw in _APPROVE_VALUES:
        return
    raise PolicyDenied(
        node.id,
        f"Policy requires approval before node '{node.id}' was not granted "
        f"(rule nodes.{node.id}.require_approval)",
        rule=f"nodes.{node.id}.require_approval",
    )


def execute_node(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    from readyagents.sovereign.egress import reset_node, set_node

    token = set_node(node.id)
    try:
        return _execute_node_body(node, state, ctx)
    finally:
        reset_node(token)


def _execute_node_body(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    _maybe_node_gate(node, state, ctx)
    kind = node.type if isinstance(node.type, str) else str(node.type)
    handler = ctx.extra_handlers.get(kind)
    if handler is not None:
        return handler.execute(node, state, ctx)

    if kind == NodeType.agent.value:
        return _run_agent(node, state, ctx)
    if kind == NodeType.tool.value:
        return _run_tool(node, state, ctx)
    if kind == NodeType.transform.value:
        return _run_transform(node, state, ctx)
    if kind == NodeType.condition.value:
        return _run_condition(node, state, ctx)
    if kind == NodeType.approval.value:
        return _run_approval(node, state, ctx)
    if kind == NodeType.parallel.value:
        return _run_parallel(node, state, ctx)
    if kind == NodeType.include.value:
        return _run_include(node, state, ctx)
    if kind == NodeType.foreach.value:
        return _run_foreach(node, state, ctx)
    if kind == NodeType.a2a.value:
        from readyagents.a2a.node import run_a2a_node

        return run_a2a_node(node, state, ctx)
    if kind == NodeType.memory.value:
        from readyagents.memory.node import run_memory_node

        return run_memory_node(node, state, ctx)
    known = ", ".join(t.value for t in NodeType)
    raise WorkflowError(
        f"Unsupported node type '{node.type}' on node '{node.id}'. "
        f"Known types: {known}. Packs may register extra types via readyagents.packs."
    )


def _run_agent(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    ns = state.mapping()
    prompt = interpolate(node.prompt or "", ns)
    system = interpolate(node.system, ns) if node.system else None
    ctx.last_tool_rounds = []
    allowlist = list(node.tools or [])
    tool_specs = _resolve_agent_tool_specs(node, ctx, allowlist) if allowlist else None
    if ctx.dry_run:
        preview = prompt if not system else f"[system]\n{system}\n[user]\n{prompt}"
        tools_line = f" tools={','.join(allowlist)}" if allowlist else ""
        estimated = _estimate_tokens(prompt, system or "")
        _account_usage(state, ctx, {"estimated_tokens": estimated})
        return f"[dry-run]{tools_line}\n{preview}\n[estimated_tokens={estimated}]"
    messages: list[Message] = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=prompt))
    result = _complete_agent(node, state, ctx, messages, tools=tool_specs)
    if tool_specs:
        result = _agent_tool_loop(node, state, ctx, messages, result, allowlist, tool_specs)
    if node.output_schema:
        return validate_structured_output(result.text, node.output_schema, node_id=node.id)
    return result.text


def _resolve_agent_tool_specs(
    node: NodeSpec, ctx: ExecutionContext, allowlist: list[str]
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in allowlist:
        try:
            tool = ctx.tools.get(name)
        except ToolError:
            missing.append(name)
            continue
        specs.append(spec_from_tool(tool))
    if missing:
        known = ", ".join(ctx.tools.names()) or "(none)"
        raise NodeError(
            node.id,
            f"unknown tool(s): {', '.join(missing)}. Available: {known}",
        )
    return specs


def _tool_round_cap(node: NodeSpec) -> int:
    raw = node.max_tool_rounds if node.max_tool_rounds is not None else _DEFAULT_MAX_TOOL_ROUNDS
    return max(1, min(int(raw), _HARD_MAX_TOOL_ROUNDS))


def _tool_result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


_TRACE_LIMIT = 400


def _truncate_trace(value: Any, limit: int = _TRACE_LIMIT) -> str:
    text = value if isinstance(value, str) else _tool_result_content(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _invoke_agent_tool(
    node: NodeSpec, state: RunState, ctx: ExecutionContext, call: ToolCall
) -> Any:
    name = call.name
    args = call.arguments if isinstance(call.arguments, dict) else {}
    if ctx.dry_run and name in _DRY_RUN_STUB_TOOLS:
        return f"[dry-run] {name} {args}"
    from readyagents.firewall.taint import prompt_tainted as agent_prompt_tainted
    from readyagents.replay.record import dispatch_tool

    result = dispatch_tool(
        cassette=ctx.cassette,
        offline=ctx.offline,
        recording=ctx.recording,
        node_id=node.id,
        name=name,
        arguments=args,
        runner=lambda: ctx.tools.get(name).run(**args),
        redactor=ctx.redactor,
        secrets=ctx.cassette_secrets,
        ctx=ctx,
        state=state,
        raw_arguments=args,
        prompt_tainted=agent_prompt_tainted(state, node.prompt or "", node.system),
    )
    return _maybe_quarantine(ctx, name, result)


def _maybe_quarantine(ctx: ExecutionContext, name: str, result: Any) -> Any:
    policy = getattr(ctx, "policy", None)
    if policy is None:
        return result
    _rule_id, rule = policy.tool_rule(name)
    if rule is None or not rule.quarantine:
        return result
    if not isinstance(result, str):
        return result
    from readyagents.firewall.enforce import quarantine_text

    return quarantine_text(result)


def _agent_tool_loop(
    node: NodeSpec,
    state: RunState,
    ctx: ExecutionContext,
    messages: list[Message],
    result: CompletionResult,
    allowlist: list[str],
    tool_specs: list[dict[str, Any]],
) -> CompletionResult:
    allowed = set(allowlist)
    cap = _tool_round_cap(node)
    rounds = 0
    while result.tool_calls:
        rounds += 1
        if rounds > cap:
            raise NodeError(node.id, f"tool-use exceeded max_tool_rounds={cap}")
        meter = getattr(ctx, "spend_meter", None)
        if meter is not None:
            meter.consult_tool_round()
            meter.record_tool_round()
        for call in result.tool_calls:
            if call.name not in allowed:
                raise NodeError(
                    node.id,
                    f"model requested tool '{call.name}' which is not on the allowlist",
                )
        messages.append(
            Message(
                role="assistant",
                content=result.text or "",
                tool_calls=list(result.tool_calls),
            )
        )
        for call in result.tool_calls:
            log_event(
                log,
                "agent_tool_call",
                "agent tool %s",
                call.name,
                run_id=state.run_id,
                node_id=node.id,
                tool=call.name,
            )
            try:
                output = _invoke_agent_tool(node, state, ctx, call)
            except ToolError as exc:
                err_text = str(exc)
                ctx.last_tool_rounds.append(
                    {
                        "name": call.name,
                        "status": "error",
                        "error": _truncate_trace(err_text),
                    }
                )
                messages.append(
                    Message(
                        role="tool",
                        content=_tool_result_content({"error": err_text}),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                continue
            ctx.last_tool_rounds.append(
                {
                    "name": call.name,
                    "status": "ok",
                    "output": _truncate_trace(output),
                }
            )
            messages.append(
                Message(
                    role="tool",
                    content=_tool_result_content(output),
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
        result = _complete_agent(node, state, ctx, messages, tools=tool_specs)
    return result


def _account_usage(state: RunState, ctx: ExecutionContext, usage: Mapping[str, Any]) -> None:
    cleaned = {str(k): v for k, v in dict(usage).items()}
    state.note_node_usage(cleaned, rollup=True)
    sink = ctx.usage_state
    if sink is not None and sink is not state:
        sink.add_usage(**cleaned)


def _complete_agent(
    node: NodeSpec,
    state: RunState,
    ctx: ExecutionContext,
    messages: list[Message],
    tools: list[dict[str, Any]] | None = None,
) -> CompletionResult:
    from readyagents.firewall.secrets_scan import scan_messages

    scan_messages(messages, ctx.cassette_secrets, node_id=node.id)
    explicit = bool(node.model)
    primary = node.model or ctx.default_model
    candidates = model_candidates(primary, node.fallback_models, ctx.fallback_models)
    if not candidates:
        candidates = [primary or "mock"]
    use_cache = bool(ctx.cache_llm if node.cache is None else node.cache)
    last_error: BaseException | None = None
    skipped: list[str] = []
    tried: list[str] = []
    for ref in candidates:
        breaker = ctx.circuit_breaker
        if breaker is not None and not breaker.allow(ref):
            log_event(
                log,
                "circuit_open",
                "circuit open, skip %s",
                ref,
                run_id=state.run_id,
                node_id=node.id,
                model=ref,
            )
            skipped.append(ref)
            continue
        cache_hit = None
        if use_cache and ctx.llm_cache is not None:
            cache_key = ctx.llm_cache.key(ref, messages, tools=tools)
            cache_hit = ctx.llm_cache.get(cache_key)
            if cache_hit is not None:
                extras: dict[str, int] = {"cache_hits": 1}
                meter = getattr(ctx, "spend_meter", None)
                if meter is not None:
                    saved = meter.record_cache_hit(ref, cache_hit.usage or {})
                    if saved:
                        extras["cache_savings_micros"] = saved
                _account_usage(state, ctx, extras)
                log_event(
                    log,
                    "cache_hit",
                    "llm cache hit model=%s",
                    ref,
                    run_id=state.run_id,
                    node_id=node.id,
                    model=ref,
                )
                return cache_hit
            meter = getattr(ctx, "spend_meter", None)
            if meter is not None:
                meter.record_cache_miss()
                _account_usage(state, ctx, {"cache_misses": 1})
        check_budget(
            (ctx.usage_state or state).usage,
            max_tokens=ctx.budget_tokens,
            max_cost_micros=ctx.budget_cost_micros,
        )
        meter = getattr(ctx, "spend_meter", None)
        hint = _prompt_token_hint(messages)
        if meter is not None:
            meter.consult_before_call(ref, prompt_tokens=hint)
        try:
            if ctx.offline:
                from readyagents.replay.offline import CassetteProvider

                if ctx.cassette is None:
                    get_provider(ref, offline=True)
                provider = CassetteProvider(ctx.cassette, node_id=node.id)
                model_id = model_id_for(ref)
            elif ctx.llm is not None:
                provider = ctx.llm
                model_id = model_id_for(ref)
                if ctx.recording and ctx.cassette is not None:
                    from readyagents.replay.record import RecordingProvider

                    if not isinstance(provider, RecordingProvider):
                        provider = RecordingProvider(
                            ctx.cassette,
                            provider,
                            node_id=node.id,
                            redactor=ctx.redactor,
                            secrets=ctx.cassette_secrets,
                        )
            else:
                provider, model_id = get_provider(
                    ref,
                    implicit=not explicit,
                    secrets=ctx.secrets,
                )
                if ctx.recording and ctx.cassette is not None:
                    from readyagents.replay.record import RecordingProvider

                    provider = RecordingProvider(
                        ctx.cassette,
                        provider,
                        node_id=node.id,
                        redactor=ctx.redactor,
                        secrets=ctx.cassette_secrets,
                    )
            tried.append(ref)
            result = provider.complete(messages, model=model_id, tools=tools)
        except (BudgetExceeded, RunawayGuard):
            raise
        except LLMError as exc:
            last_error = exc
            if meter is not None:
                meter.release_reservation(prompt_tokens=hint)
            if breaker is not None:
                breaker.record_failure(ref)
            log_event(
                log,
                "llm_error",
                "model %s failed: %s",
                ref,
                exc,
                run_id=state.run_id,
                node_id=node.id,
                model=ref,
            )
            continue
        if breaker is not None:
            breaker.record_success(ref)
        usage = normalize_usage(result.usage, model=result.model or model_id_for(ref))
        if meter is not None:
            meter.record_usage(
                result.model or ref,
                usage,
                reserved_prompt=hint,
            )
        if usage_nonzero(usage):
            _account_usage(state, ctx, usage)
        if use_cache and ctx.llm_cache is not None:
            ctx.llm_cache.put(ctx.llm_cache.key(ref, messages, tools=tools), result)
        if tried and tried[0] != ref:
            log_event(
                log,
                "llm_fallback",
                "fell back to %s",
                ref,
                run_id=state.run_id,
                node_id=node.id,
                model=ref,
            )
        return result
    if skipped and not tried:
        raise CircuitOpen(skipped[0])
    raise_exhausted(tried, skipped, last_error)
    raise LLMError("No LLM model was available")  # pragma: no cover


def _run_tool(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    name = node.tool or ""
    args = interpolate_value(node.arguments, state.mapping())
    if not isinstance(args, dict):
        raise NodeError(node.id, "tool arguments must be a mapping")
    if ctx.dry_run and name in _DRY_RUN_STUB_TOOLS:
        return f"[dry-run] {name} {args}"
    from readyagents.replay.record import dispatch_tool

    result = dispatch_tool(
        cassette=ctx.cassette,
        offline=ctx.offline,
        recording=ctx.recording,
        node_id=node.id,
        name=name,
        arguments=args,
        runner=lambda: ctx.tools.get(name).run(**args),
        redactor=ctx.redactor,
        secrets=ctx.cassette_secrets,
        ctx=ctx,
        state=state,
        raw_arguments=node.arguments,
    )
    return _maybe_quarantine(ctx, name, result)


def _run_transform(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    if ctx.cassette is not None:
        ctx.cassette.report.note(node.id, "recomputed")
    ns = state.mapping()
    value: Any
    if node.template is not None:
        value = interpolate(node.template, ns)
    elif node.source:
        value = lookup(ns, node.source)
    else:
        if not state.results:
            raise TemplateError("transform has no source and no prior node output")
        value = state.node_outputs[state.results[-1].node_id]

    if node.parse_json:
        if isinstance(value, str):
            if ctx.dry_run and value.lstrip().startswith("[dry-run]"):
                value = {"dry_run": True}
            else:
                value = _parse_json_lenient(value)
    if node.json_path:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ToolError(f"transform json_path: value is not JSON: {exc}") from exc
        value = resolve_path(value, node.json_path)
    return value


def _parse_json_lenient(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                pass
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise ToolError("transform parse_json: could not parse JSON from text") from None


def _run_condition(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> dict[str, Any]:
    matched = evaluate_condition(node.when or "", state.mapping())
    nxt = node.then if matched else node.else_
    return {"matched": matched, "next": nxt}


def _run_approval(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> dict[str, Any]:
    from readyagents.approvals.gate import (
        Vote,
        actor_is_eligible,
        apply_escalation,
        clock_now,
        eligible_labels,
        enterprise_fields_set,
        evaluate_gate,
        expires_at_for,
        format_clock,
        legacy_pause,
        normalize_actor,
        parse_clock,
        pause_from_pending,
    )

    ns = state.mapping()
    prompt = interpolate(node.prompt or f"Approve node '{node.id}'?", ns)
    pending = state.pending if isinstance(state.pending, dict) else {}
    use_enterprise = enterprise_fields_set(node) or (
        isinstance(pending, dict)
        and pending.get("node_id") == node.id
        and not legacy_pause(pending)
    )
    if not use_enterprise:
        return _run_approval_classic(node, state, ctx, prompt)

    now = clock_now()
    pause = pause_from_pending(pending if pending.get("node_id") == node.id else {}, node=node)
    if not pause.paused_at:
        pause.paused_at = format_clock(now)
    if not pause.expires_at:
        paused_at = parse_clock(pause.paused_at) or now
        pause.expires_at = expires_at_for(node, paused_at=paused_at)
    if not pause.eligible_actors:
        pause.eligible_actors = eligible_labels(list(node.approver_roles or []))
        if pause.escalated_to:
            pause.eligible_actors = eligible_labels(pause.escalated_to)
    if not pause.on_expire and node.on_expire:
        pause.on_expire = node.on_expire
    if node.deny_actor and not pause.deny_actor:
        pause.deny_actor = list(node.deny_actor)
    initiator = ""
    if isinstance(state.metadata, dict):
        initiator = str(state.metadata.get("actor") or "")
    if initiator and normalize_actor(initiator) not in {
        normalize_actor(item) for item in pause.deny_actor
    }:
        if any(normalize_actor(item) in {"$initiator", "initiator"} for item in pause.deny_actor):
            pause.deny_actor = [
                initiator if normalize_actor(item) in {"$initiator", "initiator"} else item
                for item in pause.deny_actor
            ]

    just_escalated = False
    outcome = evaluate_gate(pause, now)
    if outcome.status == "escalated":
        targets = list(node.escalate_to or pause.approver_roles)
        apply_escalation(pause, now=now, targets=targets)
        _persist_pause(state, ctx, pause)
        if ctx.auditor is not None:
            ctx.auditor(
                "gate_escalated",
                run_id=state.run_id,
                node_id=node.id,
                elapsed_seconds=outcome.elapsed_seconds,
                escalated_to=list(pause.escalated_to),
                clock_source=pause.clock_source,
                actor=ctx.actor,
            )
        _notify_enterprise(node, state, ctx, prompt, pause, first=False)
        just_escalated = True
        outcome = evaluate_gate(pause, now, ignore_expiry=True)
    if outcome.status == "expired":
        _record_expiry(ctx, state, node, pause, outcome)
        raise GateExpired(
            f"Approval '{node.id}' expired after {outcome.elapsed_seconds}s (on_expire=fail)"
        )
    if outcome.status == "rejected" and outcome.reason == "expired":
        _record_expiry(ctx, state, node, pause, outcome)
        return _approval_output(node, state, ctx, prompt, approved=False, pause=pause)
    if outcome.status == "approved":
        return _approval_output(node, state, ctx, prompt, approved=True, pause=pause)
    if outcome.status == "rejected":
        return _approval_output(node, state, ctx, prompt, approved=False, pause=pause)

    raw = ctx.decision_for(node.id)
    if raw is None:
        first = not pause.approvals_received
        _persist_pause(state, ctx, pause)
        if first:
            _notify_enterprise(node, state, ctx, prompt, pause, first=True)
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause.as_dict())

    if raw in _APPROVE_VALUES:
        action = "approve"
    elif raw in _REJECT_VALUES:
        action = "reject"
    else:
        raise NodeError(node.id, f"unknown decision '{raw}' (use approve or reject)")

    if ctx.authorizer is not None:
        try:
            ctx.authorizer.check(ctx.actor, action, node.id)
        except AuthorizationError:
            if ctx.auditor is not None:
                ctx.auditor(
                    "decision_refused",
                    run_id=state.run_id,
                    node_id=node.id,
                    actor=ctx.actor,
                    reason="unauthorized",
                )
            raise ApprovalRequired(
                node.id, state.run_id, prompt, state=state, pause=pause.as_dict()
            ) from None

    reason = (ctx.vote_reasons or {}).get(node.id)
    if pause.require_reason and not (reason and str(reason).strip()):
        if ctx.auditor is not None:
            ctx.auditor(
                "decision_refused",
                run_id=state.run_id,
                node_id=node.id,
                actor=ctx.actor,
                reason="missing_reason",
            )
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause.as_dict())

    verified = getattr(ctx, "verified_actor", None)
    roles = list(getattr(verified, "roles", ()) or ())
    fn = getattr(ctx.authorizer, "roles_for", None)
    if callable(fn):
        roles = list(roles) + [str(item) for item in list(fn(ctx.actor) or [])]
    delegated_from = None
    delegated_role = None
    try:
        from readyagents.approvals.delegate import find_delegation
        from readyagents.config import get_settings

        home = getattr(ctx, "pin_home", None) or get_settings().home_path()
        grant = find_delegation(
            actor=ctx.actor,
            roles=list(pause.approver_roles or []) + list(roles),
            home=home,
            now=now,
        )
    except Exception:  # noqa: BLE001
        grant = None
    if grant is not None:
        delegated_from = grant.from_actor
        delegated_role = grant.scope
        if grant.scope:
            roles = list(roles) + [grant.scope]
        else:
            roles = list(roles) + list(pause.approver_roles)

    if not actor_is_eligible(ctx.actor, pause, roles=roles, delegated_role=delegated_role):
        if ctx.auditor is not None:
            ctx.auditor(
                "decision_refused",
                run_id=state.run_id,
                node_id=node.id,
                actor=ctx.actor,
                reason="ineligible",
            )
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause.as_dict())

    existing = next(
        (
            vote
            for vote in pause.approvals_received
            if vote.actor_norm() == normalize_actor(ctx.actor)
        ),
        None,
    )
    if existing is not None:
        if existing.decision != action:
            if ctx.auditor is not None:
                ctx.auditor(
                    "decision_refused",
                    run_id=state.run_id,
                    node_id=node.id,
                    actor=ctx.actor,
                    reason="duplicate_actor",
                )
            raise ApprovalRequired(
                node.id, state.run_id, prompt, state=state, pause=pause.as_dict()
            )
    else:
        rec = pause.recommendation
        rec_norm = rec.strip().lower() if rec else ""
        override = False
        if rec_norm and rec_norm not in {"none", action}:
            if rec_norm in _APPROVE_VALUES and action == "reject":
                override = True
            elif rec_norm in _REJECT_VALUES and action == "approve":
                override = True
            elif rec_norm != action:
                override = True
        declared = {normalize_actor(item) for item in pause.approver_roles}
        matched_role = next(
            (item for item in roles if normalize_actor(item) in declared),
            None,
        )
        if matched_role is None and delegated_role and normalize_actor(delegated_role) in declared:
            matched_role = delegated_role
        vote = Vote(
            actor=str(ctx.actor or ""),
            decision=action,
            at=format_clock(now),
            role=matched_role or (roles[0] if roles else None),
            reason=str(reason).strip() if reason else None,
            signature_status=str(getattr(ctx, "vote_signature_status", None) or "unsigned"),
            delegated_from=delegated_from,
            override=override,
        )
        pause.approvals_received.append(vote)
        _persist_pause(state, ctx, pause)
        if ctx.auditor is not None:
            ctx.auditor(
                "decision",
                run_id=state.run_id,
                node_id=node.id,
                decision=action,
                actor=ctx.actor,
                delegated_from=delegated_from,
                override=override,
                signature_status=vote.signature_status,
                reason=vote.reason,
            )

    outcome = evaluate_gate(pause, now, ignore_expiry=just_escalated)
    if outcome.status == "pending":
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause.as_dict())
    if outcome.status == "approved":
        return _approval_output(node, state, ctx, prompt, approved=True, pause=pause)
    if outcome.status in {"rejected", "expired"}:
        if outcome.reason == "expired":
            _record_expiry(ctx, state, node, pause, outcome)
            if outcome.status == "expired":
                raise GateExpired(
                    f"Approval '{node.id}' expired after "
                    f"{outcome.elapsed_seconds}s (on_expire=fail)"
                )
        return _approval_output(node, state, ctx, prompt, approved=False, pause=pause)
    if outcome.status == "escalated":
        apply_escalation(pause, now=now, targets=list(node.escalate_to or []))
        _persist_pause(state, ctx, pause)
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state, pause=pause.as_dict())
    return _approval_output(node, state, ctx, prompt, approved=False, pause=pause)


def _run_approval_classic(
    node: NodeSpec, state: RunState, ctx: ExecutionContext, prompt: str
) -> dict[str, Any]:
    raw = ctx.decision_for(node.id)
    if raw is None:
        raise ApprovalRequired(node.id, state.run_id, prompt, state=state)
    if raw in _APPROVE_VALUES:
        approved = True
    elif raw in _REJECT_VALUES:
        approved = False
    else:
        raise NodeError(
            node.id,
            f"unknown decision '{raw}' (use approve or reject)",
        )
    action = "approve" if approved else "reject"
    if ctx.authorizer is not None:
        ctx.authorizer.check(ctx.actor, action, node.id)
    if ctx.auditor is not None:
        verified = getattr(ctx, "verified_actor", None)
        extra: dict[str, Any] = {}
        if verified is not None:
            extra["subject"] = getattr(verified, "subject", None)
            extra["issuer"] = getattr(verified, "issuer", None)
            extra["method"] = getattr(verified, "method", "none")
            extra["identified"] = bool(getattr(verified, "identified", lambda: False)())
            extra["roles"] = list(getattr(verified, "roles", ()) or ())
        ctx.auditor(
            "decision",
            run_id=state.run_id,
            node_id=node.id,
            decision=action,
            actor=ctx.actor,
            **extra,
        )
    return _approval_output(node, state, ctx, prompt, approved=approved, pause=None)


def _persist_pause(state: RunState, ctx: ExecutionContext, pause: Any) -> None:
    payload = pause.as_dict() if hasattr(pause, "as_dict") else dict(pause)
    pending = dict(state.pending or {}) if isinstance(state.pending, dict) else {}
    pending.update(payload)
    pending["type"] = "approval"
    pending["node_id"] = state.pending_node or pending.get("node_id")
    state.pending = pending
    if ctx.on_persist is not None:
        ctx.on_persist(state)


def _record_expiry(
    ctx: ExecutionContext, state: RunState, node: NodeSpec, pause: Any, outcome: Any
) -> None:
    pause.expired = True
    _persist_pause(state, ctx, pause)
    if ctx.auditor is not None:
        ctx.auditor(
            "gate_expired",
            run_id=state.run_id,
            node_id=node.id,
            elapsed_seconds=getattr(outcome, "elapsed_seconds", None),
            on_expire=pause.on_expire,
            clock_source=pause.clock_source,
            actor=ctx.actor,
        )


def _notify_enterprise(
    node: NodeSpec,
    state: RunState,
    ctx: ExecutionContext,
    prompt: str,
    pause: Any,
    *,
    first: bool,
) -> None:
    channels = list(getattr(node, "notify", None) or [])
    if not channels:
        return
    from readyagents.approvals.channels import notify_channels

    workspace = None
    if isinstance(state.metadata, dict) and state.metadata.get("workspace"):
        from pathlib import Path

        workspace = Path(str(state.metadata["workspace"]))
    notify_channels(
        channels,
        run_id=state.run_id,
        node_id=node.id,
        prompt=prompt,
        eligible=list(pause.eligible_actors),
        expires_at=pause.expires_at,
        workspace=workspace,
        redactor=ctx.redactor,
    )


def _approval_output(
    node: NodeSpec,
    state: RunState,
    ctx: ExecutionContext,
    prompt: str,
    *,
    approved: bool,
    pause: Any,
) -> dict[str, Any]:
    action = "approve" if approved else "reject"
    if approved:
        nxt = node.then or node.next
    else:
        nxt = node.else_
    output: dict[str, Any] = {
        "approved": approved,
        "decision": action,
        "prompt": prompt,
        "next": nxt,
        "actor": ctx.actor,
    }
    if pause is not None:
        output["approvals_received"] = [vote.as_dict() for vote in pause.approvals_received]
        output["approvals_required"] = pause.approvals_required
        if any(vote.override for vote in pause.approvals_received):
            output["override"] = True
    verified = getattr(ctx, "verified_actor", None)
    if verified is not None:
        output["identified"] = bool(getattr(verified, "identified", lambda: False)())
        output["subject"] = getattr(verified, "subject", None)
        output["issuer"] = getattr(verified, "issuer", None)
        output["method"] = getattr(verified, "method", "none")
    return output


def _estimate_tokens(*texts: str) -> int:
    total = sum(len(t or "") for t in texts)
    return max(1, total // 4)


def _prompt_token_hint(messages: list[Message]) -> int:
    from readyagents.cost.tokens import count_tokens

    total = 0
    for message in messages:
        n, _measured = count_tokens(message.content or "")
        total += n
    return total


@contextmanager
def _track_node_body(ctx: ExecutionContext) -> Iterator[None]:
    ctx.enter_node_body()
    try:
        yield
    finally:
        ctx.leave_node_body()


def execute_node_with_policy(
    node: NodeSpec, state: RunState, ctx: ExecutionContext
) -> tuple[Any, int]:
    """Run a node with timeout_seconds / retry. Does not record the result."""
    retry = node.retry
    attempts = retry.max_attempts if retry else 1
    backoff = retry.backoff_seconds if retry else 1.0
    multiplier = retry.backoff_multiplier if retry else 2.0
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        if ctx.cancellation is not None:
            ctx.cancellation.raise_if_requested(run_id=state.run_id)
        try:
            with _track_node_body(ctx):
                return _call_with_timeout(node, state, ctx), attempt
        except CancellationRequested:
            raise
        except ApprovalRequired:
            raise
        except GateExpired:
            raise
        except EgressDenied:
            raise
        except CassetteMiss:
            raise
        except PolicyDenied:
            raise
        except A2AError:
            raise
        except MemoryError:
            raise
        except (BudgetExceeded, AuthorizationError, CircuitOpen, RunawayGuard):
            raise
        except ReadyAgentsError as exc:
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        log.warning(
            "Node %s attempt %s/%s failed: %s",
            node.id,
            attempt,
            attempts,
            last_error,
            extra={"run_id": state.run_id, "node_id": node.id},
        )
        if attempt >= attempts:
            break
        # Retry backoff is a cooperative safe point, not an in-flight node body.
        cancellable_sleep(backoff * (multiplier ** (attempt - 1)), ctx.cancellation)

    if isinstance(last_error, NodeError) and last_error.node_id == node.id:
        raise last_error
    message = str(last_error) if last_error else "unknown error"
    raise NodeError(node.id, message, cause=last_error) from last_error


def _call_with_timeout(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    if not node.timeout_seconds:
        return execute_node(node, state, ctx)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = execute_node(node, state, ctx)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(
        target=_worker,
        name=f"readyagents-node-{node.id}",
        daemon=True,
    )
    thread.start()
    thread.join(node.timeout_seconds)
    if thread.is_alive():
        raise NodeError(node.id, f"timed out after {node.timeout_seconds}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _foreach_cap(node: NodeSpec) -> int:
    raw = node.max_items if node.max_items is not None else _DEFAULT_MAX_FOREACH
    return max(1, min(int(raw), _HARD_MAX_FOREACH))


def _foreach_items(node: NodeSpec, state: RunState) -> list[Any]:
    ns = state.mapping()
    raw: Any
    try:
        raw = lookup(ns, node.items or "")
    except TemplateError:
        text = interpolate(node.items or "", ns)
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = text
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                pass
    if not isinstance(raw, list):
        raise NodeError(node.id, f"foreach items must be a list (got {type(raw).__name__})")
    return raw


def _run_foreach(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> list[Any]:
    body = node.body
    if body is None:
        raise NodeError(node.id, "foreach nodes require 'body'")
    items = _foreach_items(node, state)
    cap = _foreach_cap(node)
    if len(items) > cap:
        raise NodeError(node.id, f"foreach exceeded max_items={cap}")
    bucket = state.metadata.setdefault(_FOREACH_META, {})
    if not isinstance(bucket, dict):
        bucket = {}
        state.metadata[_FOREACH_META] = bucket
    prior = bucket.get(node.id) if isinstance(bucket.get(node.id), list) else []
    outputs: list[Any] = []
    for row in prior:
        if isinstance(row, dict) and row.get("status") == "ok":
            outputs.append(row.get("output"))
    start = len(outputs)
    from readyagents.firewall.taint import seed_foreach_item_provenance

    for index, item in enumerate(items):
        if index < start:
            continue
        child = RunState.start(
            state.workflow_name,
            {**state.inputs, "item": item, "index": index},
            metadata=state.metadata,
            run_id=state.run_id,
        )
        child.node_outputs = dict(state.node_outputs)
        child.output_keys = dict(state.output_keys)
        child.node_outputs["item"] = item
        child.output_keys["item"] = item
        child.node_outputs["index"] = index
        child.output_keys["index"] = index
        seed_foreach_item_provenance(state, child, items_expr=node.items or "", node_id=node.id)
        item_ctx = ctx
        prev_rounds = item_ctx.last_tool_rounds
        item_ctx.last_tool_rounds = []
        try:
            output, _attempt = execute_node_with_policy(body, child, item_ctx)
        except ApprovalRequired:
            bucket[node.id] = [
                {"index": i, "status": "ok", "output": outputs[i]} for i in range(len(outputs))
            ]
            if ctx.on_persist is not None:
                ctx.on_persist(state)
            raise
        except CancellationRequested:
            bucket[node.id] = [
                {"index": i, "status": "ok", "output": outputs[i]} for i in range(len(outputs))
            ]
            if ctx.on_persist is not None:
                ctx.on_persist(state)
            raise
        except Exception:
            bucket[node.id] = [
                {"index": i, "status": "ok", "output": outputs[i]} for i in range(len(outputs))
            ]
            if ctx.on_persist is not None:
                ctx.on_persist(state)
            raise
        finally:
            item_ctx.last_tool_rounds = prev_rounds
        outputs.append(output)
        bucket[node.id] = [
            {"index": i, "status": "ok", "output": outputs[i]} for i in range(len(outputs))
        ]
        if ctx.on_persist is not None:
            ctx.on_persist(state)
    return outputs


def _meta_bucket(state: RunState, key: str) -> dict[str, Any]:
    bucket = state.metadata.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        state.metadata[key] = bucket
    return bucket


def _include_child_state(state: RunState, node_id: str) -> RunState | None:
    bucket = state.metadata.get(_INCLUDE_META)
    if not isinstance(bucket, dict):
        return None
    entry = bucket.get(node_id)
    if not isinstance(entry, dict):
        return None
    record = entry.get("run")
    if not isinstance(record, Mapping):
        return None
    child = RunState.from_record(record)
    if child.status == "succeeded":
        return None
    return child


def _persist_include_child(
    parent: RunState, node_id: str, child: RunState, ctx: ExecutionContext
) -> None:
    _meta_bucket(parent, _INCLUDE_META)[node_id] = {"run": child.to_record()}
    if ctx.on_persist is not None:
        ctx.on_persist(parent)


def _clear_include_child(state: RunState, node_id: str) -> None:
    bucket = state.metadata.get(_INCLUDE_META)
    if not isinstance(bucket, dict):
        return
    bucket.pop(node_id, None)
    if not bucket:
        state.metadata.pop(_INCLUDE_META, None)


def _parallel_prior(state: RunState, node_id: str) -> dict[str, Any]:
    bucket = state.metadata.get(_PARALLEL_META)
    if not isinstance(bucket, dict):
        return {}
    prior = bucket.get(node_id)
    return dict(prior) if isinstance(prior, dict) else {}


def _persist_parallel_ok(
    state: RunState, node_id: str, collected: Mapping[str, Any], ctx: ExecutionContext
) -> None:
    _meta_bucket(state, _PARALLEL_META)[node_id] = dict(collected)
    if ctx.on_persist is not None:
        ctx.on_persist(state)


def _run_parallel(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> dict[str, Any]:
    branches = list(node.branches or [])
    if not branches:
        raise NodeError(node.id, "parallel nodes require 'branches'")
    prior = _parallel_prior(state, node.id)
    collected: dict[str, Any] = {}
    remaining: list[NodeSpec] = []
    for branch in branches:
        if branch.id in prior:
            collected[branch.id] = prior[branch.id]
        else:
            remaining.append(branch)

    def _one(branch: NodeSpec) -> tuple[str, Any]:
        try:
            output, _attempt = execute_node_with_policy(branch, state, ctx)
            return branch.id, output
        except ApprovalRequired:
            raise
        except CancellationRequested:
            raise
        except (
            BudgetExceeded,
            AuthorizationError,
            CircuitOpen,
            RunawayGuard,
            CassetteMiss,
            PolicyDenied,
        ):
            raise
        except NodeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NodeError(
                branch.id,
                f"parallel branch '{branch.id}' failed: {exc}",
                cause=exc if isinstance(exc, BaseException) else None,
            ) from exc

    first_error: BaseException | None = None
    if remaining:
        workers = max(1, min(_MAX_PARALLEL, len(remaining)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, branch) for branch in remaining]
            for fut in as_completed(futures):
                try:
                    branch_id, output = fut.result()
                except Exception as exc:  # noqa: BLE001
                    if first_error is None:
                        first_error = exc
                    continue
                collected[branch_id] = output
    if first_error is not None:
        _persist_parallel_ok(state, node.id, collected, ctx)
        raise first_error
    _persist_parallel_ok(state, node.id, collected, ctx)
    ordered = {branch.id: collected[branch.id] for branch in branches}
    # Branch execute_node calls accumulate on state._last_node_usage (thread-safe).
    # The engine's take_node_usage then stores the merged total on this node.
    return ordered


def _run_include(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> Any:
    if ctx.include_depth >= _MAX_INCLUDE_DEPTH:
        raise WorkflowError(
            f"include depth exceeded ({_MAX_INCLUDE_DEPTH}). Check for cycles in sub-workflows."
        )
    raw_path = interpolate(node.path or "", state.mapping())
    if not raw_path:
        raise NodeError(node.id, "include nodes require 'path'")
    candidate = _confine_include_path(raw_path, ctx.workflow_dir, node.id)
    source = (ctx.include_buffers or {}).get(str(candidate))
    if source is None:
        try:
            source = (ctx.include_buffers or {}).get(str(candidate.resolve()))
        except OSError:
            source = None
    enforce = bool(getattr(ctx, "require_signed", False) or getattr(ctx, "frozen", False))
    if source is None and not candidate.is_file():
        raise NodeError(
            node.id,
            f"included workflow not found: {candidate} "
            f"(resolved from path '{raw_path}' relative to {ctx.workflow_dir})",
        )
    if enforce and source is None:
        raise TrustError(
            f"included workflow not in the digested graph: {candidate}",
            artifact=str(candidate),
            reason="unresolved_include",
        )

    from readyagents.workflow.engine import run_workflow
    from readyagents.workflow.runner import load_workflow, merge_inputs

    try:
        spec = load_workflow(candidate, display_path=raw_path, source=source)
    except WorkflowError as exc:
        parent_source = None
        meta = getattr(state, "metadata", None)
        if isinstance(meta, dict):
            parent_source = meta.get("source")
        if parent_source:
            from readyagents.workflow.source_map import locate_node_field, with_include_site

            site = locate_node_field(parent_source, node.id, "path")
            problems = with_include_site(list(getattr(exc, "problems", []) or []), site)
            raise WorkflowError(str(exc), problems=problems) from exc
        raise
    nested_in = interpolate_value(node.call_inputs, state.mapping())
    if nested_in is None:
        nested_in = {}
    if not isinstance(nested_in, dict):
        raise NodeError(node.id, "include inputs must be a mapping")
    merged = merge_inputs(spec, nested_in)

    def _persist_child(child_state: RunState) -> None:
        _persist_include_child(state, node.id, child_state, ctx)

    nested_ctx = ctx.child(
        spec,
        workflow_dir=candidate.parent,
        include_depth=ctx.include_depth + 1,
        on_persist=_persist_child,
    )
    nested_state = _include_child_state(state, node.id)
    try:
        nested = run_workflow(
            spec,
            merged,
            nested_ctx,
            metadata={"source": str(candidate), "included_by": node.id},
            state=nested_state,
        )
    except ApprovalRequired as exc:
        # Parent run is what was persisted. Keep the child's node id so
        # `resume --approve <child-id>` works; rewrite run_id to the parent.
        raise ApprovalRequired(
            exc.node_id,
            state.run_id,
            exc.prompt,
            state=state,
        ) from exc
    if nested.status == "paused":
        raise ApprovalRequired(
            nested.pending_node or node.id,
            state.run_id,
            f"Nested workflow '{spec.name}' is waiting for approval.",
            state=state,
        )
    if nested.status != "succeeded":
        raise NodeError(node.id, f"included workflow '{spec.name}' {nested.status}")
    if any(
        isinstance(row, dict) and row.get("trust") == "untrusted"
        for row in (nested.provenance or {}).values()
    ):
        flags = state.metadata.setdefault("_child_untrusted", {})
        flags[node.id] = True
    _clear_include_child(state, node.id)
    # Nested agents already add_usage onto ctx.usage_state (the parent run).
    # Record nested totals on this include node without rolling them up again.
    already_on_parent = ctx.usage_state is state
    state.note_node_usage(nested.usage, rollup=not already_on_parent)
    return nested.output_keys or nested.node_outputs


def _confine_include_path(raw_path: str, workflow_dir: Path, node_id: str) -> Path:
    """Resolve an include path and refuse anything outside the parent workflow dir."""
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    try:
        return resolve_within(raw_path, workflow_dir, what="included workflow")
    except PathError as extra:
        raise NodeError(node_id, str(extra)) from extra


def evaluate_condition(expr: str, mapping: Mapping[str, Any]) -> bool:
    """Evaluate a small boolean of comparisons / truthy paths. No `eval()`."""
    from readyagents.workflow.conditions import evaluate_condition as eval_bool

    return eval_bool(expr, mapping)
