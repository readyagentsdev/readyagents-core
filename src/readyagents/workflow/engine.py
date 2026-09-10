"""Execute a workflow graph with retries, timeouts, and branching."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from readyagents.errors import (
    ApprovalRequired,
    AuthorizationError,
    BudgetExceeded,
    CancellationRequested,
    CassetteMiss,
    CircuitOpen,
    PolicyDenied,
    ReadyAgentsError,
    RunawayGuard,
    WorkflowError,
)
from readyagents.logging import get_logger, log_event
from readyagents.workflow.nodes import (
    ExecutionContext,
    evaluate_condition,
    execute_node_with_policy,
)
from readyagents.workflow.schema import NodeSpec, NodeType, WorkflowSpec
from readyagents.workflow.state import RunState, utc_now

log = get_logger("engine")

_MAX_STEPS = 500
_TERMINAL_STATUSES = frozenset({"cancelled", "succeeded", "failed", "paused"})


def run_workflow(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any],
    ctx: ExecutionContext,
    *,
    metadata: Mapping[str, Any] | None = None,
    state: RunState | None = None,
    run_id: str | None = None,
) -> RunState:
    nodes = workflow.node_map()
    if state is None:
        state = RunState.start(workflow.name, inputs, metadata=metadata, run_id=run_id)
        current = workflow.start or workflow.nodes[0].id
        seen: set[str] = set()
    elif _is_fresh_start(state):
        current = workflow.start or workflow.nodes[0].id
        seen = set()
        state.status = "running"
        state.finished_at = None
        if inputs:
            state.inputs.update(dict(inputs))
        if metadata:
            state.metadata.update(dict(metadata))
    else:
        current, seen = _resume_cursor(workflow, state)
        if inputs:
            state.inputs.update(dict(inputs))
        if metadata:
            state.metadata.update(dict(metadata))

    if ctx.usage_state is None:
        ctx.usage_state = state
    from readyagents.firewall.taint import seed_input_provenance

    seed_input_provenance(state)
    _arm_cancellation_persist(ctx, state)
    _persist(ctx, state)
    log_event(
        log,
        "run_start",
        "run %s status=%s",
        state.run_id,
        state.status,
        run_id=state.run_id,
        node_id="-",
        status=state.status,
    )
    if ctx.auditor is not None:
        supply = None
        if isinstance(state.metadata, dict):
            supply = state.metadata.get("supply_chain")
        if supply:
            ctx.auditor(
                "run_started",
                run_id=state.run_id,
                workflow=workflow.name,
                actor=ctx.actor,
                supply_chain=supply,
            )
        else:
            ctx.auditor("run_started", run_id=state.run_id, workflow=workflow.name, actor=ctx.actor)
    _observe(
        ctx,
        "run.started",
        state,
        status=state.status,
    )

    steps = 0
    try:
        _raise_if_cancelled(ctx, state)
        while current:
            steps += 1
            if steps > _MAX_STEPS:
                raise WorkflowError(f"Workflow exceeded {_MAX_STEPS} steps (possible cycle)")
            if current in seen:
                raise WorkflowError(f"Cycle detected at node '{current}'")
            if current not in nodes:
                raise WorkflowError(f"Unknown node '{current}'")
            node = nodes[current]
            seen.add(current)
            log_event(
                log,
                "node_start",
                "node %s (%s)",
                node.id,
                node.type,
                run_id=state.run_id,
                node_id=node.id,
            )
            _raise_if_cancelled(ctx, state)
            _observe(
                ctx,
                "node.started",
                state,
                node_id=node.id,
                node_type=str(node.type),
            )
            try:
                _execute_with_policy(node, state, ctx)
            except CancellationRequested:
                raise
            except ApprovalRequired:
                raise
            except CassetteMiss:
                raise
            except PolicyDenied:
                raise
            except ReadyAgentsError:
                _raise_if_cancelled(ctx, state)
                raise
            _raise_if_cancelled(ctx, state)
            state.pending_node = None
            state.pending = None
            _persist(ctx, state)
            if ctx.auditor is not None:
                ctx.auditor(
                    "node_ok",
                    run_id=state.run_id,
                    node_id=node.id,
                    node_type=str(node.type),
                    actor=ctx.actor,
                )
            last = state.results[-1] if state.results else None
            _observe(
                ctx,
                "node.finished",
                state,
                node_id=node.id,
                node_type=str(node.type),
                status="ok",
                usage=dict(last.usage) if last is not None else {},
                duration_ms=_duration_ms(last.started_at, last.finished_at) if last else None,
            )
            current = _next_node(workflow, node, state)
        _raise_if_cancelled(ctx, state)
        state.pending_node = None
        state.pending = None
        state.finish("succeeded")
        _persist(ctx, state)
        if ctx.auditor is not None:
            ctx.auditor("run_finished", run_id=state.run_id, status="succeeded", actor=ctx.actor)
        _observe(ctx, "run.finished", state, status="succeeded", usage=dict(state.usage))
    except KeyboardInterrupt:
        state.pending_node = current
        state.pending = {
            "node_id": current,
            "type": str(nodes[current].type) if current in nodes else "?",
            "error": "cancelled",
        }
        state.finish("cancelled")
        _persist(ctx, state)
        raise
    except CancellationRequested as exc:
        _finalize_cancelled(ctx, state, current, nodes)
        exc.state = state
        if not exc.run_id:
            exc.run_id = state.run_id
        raise
    except ApprovalRequired as exc:
        state.pending_node = current
        paused = nodes.get(current) if current else None
        state.pending = {
            "node_id": exc.node_id,
            "type": "approval",
            "prompt": exc.prompt,
            "then": getattr(paused, "then", None),
            "else": getattr(paused, "else_", None),
            "resume": f"readyagents resume {state.run_id} --approve {exc.node_id}",
            "decide": (
                f"readyagents decide {state.run_id} --node {exc.node_id} --decision approve"
            ),
        }
        extra = getattr(exc, "pause", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key in {"node_id", "type"}:
                    continue
                state.pending[key] = value
        state.finish("paused")
        _persist(ctx, state)
        exc.state = state
        if ctx.auditor is not None:
            ctx.auditor(
                "paused",
                run_id=state.run_id,
                node_id=exc.node_id,
                actor=ctx.actor,
            )
        _notify_pause(ctx, exc, state)
        _observe(
            ctx,
            "run.paused",
            state,
            node_id=exc.node_id,
            node_type="approval",
            status="paused",
        )
        raise
    except ReadyAgentsError as exc:
        state.take_node_usage()
        state.pending_node = current
        state.record_error(
            current or "?",
            str(nodes[current].type) if current in nodes else "?",
            str(exc),
        )
        state.pending = {
            "node_id": current,
            "type": str(nodes[current].type) if current in nodes else "?",
            "error": str(exc),
        }
        state.finish("failed")
        _persist(ctx, state)
        exc.state = state
        if not exc.run_id:
            exc.run_id = state.run_id
        if ctx.auditor is not None:
            ctx.auditor(
                "run_finished",
                run_id=state.run_id,
                status="failed",
                node_id=current,
                actor=ctx.actor,
            )
        _observe(ctx, "run.finished", state, status="failed", node_id=current)
        raise
    return state


def _resume_cursor(workflow: WorkflowSpec, state: RunState) -> tuple[str | None, set[str]]:
    if state.status == "succeeded":
        raise WorkflowError(
            f"Run {state.run_id} already succeeded. "
            f"Use 'readyagents runs replay {state.run_id}' to start a new run."
        )
    completed = {r.node_id for r in state.results if r.status == "ok"}
    current = state.pending_node
    if not current:
        for result in reversed(state.results):
            if result.status == "error":
                current = result.node_id
                break
        if not current and state.results:
            last_ok = next((r for r in reversed(state.results) if r.status == "ok"), None)
            if last_ok and last_ok.node_id in workflow.node_map():
                current = _next_node(workflow, workflow.node_map()[last_ok.node_id], state)
    if current:
        state.results = [
            r for r in state.results if not (r.node_id == current and r.status == "error")
        ]
        if current not in completed:
            state.node_outputs.pop(current, None)
    preserved = None
    if (
        isinstance(state.pending, dict)
        and state.pending.get("type") == "approval"
        and current
        and state.pending.get("node_id") == current
    ):
        preserved = dict(state.pending)
    state.pending_node = None
    state.pending = preserved
    state.status = "running"
    state.finished_at = None
    return current, completed


def _is_fresh_start(state: RunState) -> bool:
    return state.status in {"queued", "running"} and not state.results and not state.pending_node


def _raise_if_cancelled(ctx: ExecutionContext, state: RunState) -> None:
    if ctx.cancellation is None:
        return
    ctx.cancellation.raise_if_requested(run_id=state.run_id)


def _arm_cancellation_persist(ctx: ExecutionContext, state: RunState) -> None:
    token = ctx.cancellation
    if token is None:
        return
    token.clear_listeners()
    token.add_listener(lambda: _persist(ctx, state))


def _finalize_cancelled(
    ctx: ExecutionContext,
    state: RunState,
    current: str | None,
    nodes: Mapping[str, NodeSpec],
) -> None:
    persist = False
    with ctx._persist_lock:
        if state.status != "cancelled":
            state.pending_node = current
            state.pending = {
                "node_id": current,
                "type": str(nodes[current].type) if current in nodes else "?",
                "error": "cancelled",
            }
            state.finish("cancelled")
            persist = True
    if not persist:
        return
    # Persist cancelled directly so a concurrent listener cannot rewrite it to
    # cancel_requested (retry backoff is a safe point, not an in-flight body).
    if ctx.on_persist is not None:
        ctx.on_persist(state)
    if ctx.auditor is not None:
        ctx.auditor("run_finished", run_id=state.run_id, status="cancelled", actor=ctx.actor)
    _observe(ctx, "run.finished", state, status="cancelled")


def _observe(
    ctx: ExecutionContext,
    name: str,
    state: RunState,
    *,
    node_id: str | None = None,
    node_type: str | None = None,
    status: str | None = None,
    usage: dict[str, int] | None = None,
    duration_ms: int | None = None,
) -> None:
    try:
        if getattr(ctx, "include_depth", 0):
            return
        observers = getattr(ctx, "observers", None)
        if not observers:
            return
        from readyagents.observability import emit_event, make_event

        emit_event(
            observers,
            make_event(
                name,
                run_id=state.run_id,
                workflow=state.workflow_name,
                node_id=node_id,
                node_type=node_type,
                status=status,
                duration_ms=duration_ms,
                usage=usage,
                attributes={"model": ctx.default_model} if ctx.default_model else None,
            ),
            redactor=ctx.redactor,
        )
    except Exception:  # noqa: BLE001
        return


def _duration_ms(started: str, finished: str) -> int | None:
    if not started or not finished:
        return None
    try:
        from datetime import datetime

        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds() * 1000))
    except ValueError:
        return None


def _persist(ctx: ExecutionContext, state: RunState) -> None:
    meter = getattr(ctx, "spend_meter", None)
    if meter is not None:
        if meter.started_at is None:
            meter.started_at = state.started_at
        state.metadata["spend"] = meter.snapshot()
        delta = meter.cache_usage_delta()
        if delta:
            for key, value in delta.items():
                state.usage[key] = int(value)
    token = ctx.cancellation
    with ctx._persist_lock:
        if token is not None and token.is_requested() and state.status not in _TERMINAL_STATUSES:
            if ctx.node_body_in_flight():
                state.status = "cancel_requested"
            else:
                if not state.pending:
                    state.pending = {
                        "node_id": state.pending_node,
                        "type": "?",
                        "error": "cancelled",
                    }
                state.finish("cancelled")
    if ctx.on_persist is None:
        return
    ctx.on_persist(state)


def _notify_pause(ctx: ExecutionContext, exc: ApprovalRequired, state: RunState) -> None:
    if ctx.on_pause is None:
        return
    try:
        ctx.on_pause(exc, state)
    except Exception as notify_exc:  # noqa: BLE001
        log.warning(
            "pause notify failed: %s",
            notify_exc,
            extra={"run_id": state.run_id, "node_id": exc.node_id, "event": "pause_notify_error"},
        )


def _execute_with_policy(node: NodeSpec, state: RunState, ctx: ExecutionContext) -> None:
    started = utc_now()
    try:
        output, attempt = execute_node_with_policy(node, state, ctx)
    except (BudgetExceeded, AuthorizationError, CircuitOpen, PolicyDenied, RunawayGuard):
        state.take_node_usage()
        raise
    usage = state.take_node_usage()
    rounds = list(ctx.last_tool_rounds or [])
    ctx.last_tool_rounds = []
    state.record(
        node.id,
        output,
        node_type=str(node.type),
        output_key=node.output_key,
        attempts=attempt,
        started_at=started,
        finished_at=utc_now(),
        usage=usage,
        tool_rounds=rounds,
    )
    from readyagents.firewall.taint import note_node_output

    note_node_output(state, node, output)


def _uses_explicit_routing(workflow: WorkflowSpec) -> bool:
    if workflow.edges:
        return True
    return any(n.next or n.then or n.else_ for n in workflow.nodes)


def _next_node(workflow: WorkflowSpec, node: NodeSpec, state: RunState) -> str | None:
    if str(node.type) in {NodeType.condition.value, NodeType.approval.value}:
        output = state.node_outputs.get(node.id) or {}
        nxt = output.get("next") if isinstance(output, dict) else None
        return nxt

    edges = [e for e in workflow.edges if e.from_ == node.id]
    if edges:
        default = None
        ns = state.mapping()
        for edge in edges:
            if edge.when is None:
                default = edge.to
                continue
            if evaluate_condition(edge.when, ns):
                return edge.to
        return default

    if node.next:
        return node.next

    # List order only for purely sequential workflows (no next/edges/branches).
    if _uses_explicit_routing(workflow):
        return None

    ids = [n.id for n in workflow.nodes]
    idx = ids.index(node.id)
    if idx + 1 < len(ids):
        return ids[idx + 1]
    return None
