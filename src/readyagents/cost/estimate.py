"""Preflight spend estimate. Walks the same routing the engine uses; executes nothing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents.cost.prices import PriceTable, load_price_table
from readyagents.cost.tokens import HEURISTIC_BAND, MEASURED_BAND, count_tokens
from readyagents.errors import ConfigError, NodeError, TemplateError, WorkflowError
from readyagents.workflow.schema import NodeSpec, NodeType, WorkflowSpec
from readyagents.workflow.state import RunState

FLOOR_COMPLETION = 16
CEILING_COMPLETION = 512
_MAX_INCLUDE_DEPTH = 8
_DEFAULT_MAX_FOREACH = 32
_HARD_MAX_FOREACH = 100
_DEFAULT_MAX_TOOL_ROUNDS = 8
_HARD_MAX_TOOL_ROUNDS = 20
_MAX_STEPS = 500


@dataclass
class EstimateResult:
    floor_tokens: int = 0
    ceiling_tokens: int = 0
    floor_micros: int | None = 0
    ceiling_micros: int | None = 0
    unpriced: bool = False
    unpriced_models: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    measured: bool = False
    stale: bool = False
    table_updated_at: str = ""
    table_source: str = ""
    nodes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimate": True,
            "floor_tokens": self.floor_tokens,
            "ceiling_tokens": self.ceiling_tokens,
            "floor_cost_micros": self.floor_micros,
            "ceiling_cost_micros": self.ceiling_micros,
            "floor_cost_usd": None if self.floor_micros is None else self.floor_micros / 1_000_000,
            "ceiling_cost_usd": None
            if self.ceiling_micros is None
            else self.ceiling_micros / 1_000_000,
            "unpriced": self.unpriced,
            "unpriced_models": list(self.unpriced_models),
            "assumptions": list(self.assumptions),
            "measured": self.measured,
            "stale": self.stale,
            "table_updated_at": self.table_updated_at,
            "table_source": self.table_source,
            "nodes": list(self.nodes),
            "invoice_authoritative": True,
        }


def estimate_workflow_file(
    path: Path | str,
    *,
    inputs: Mapping[str, Any] | None = None,
    default_model: str | None = None,
    table: PriceTable | None = None,
) -> EstimateResult:
    from readyagents.workflow.runner import load_workflow, merge_inputs

    file = Path(path)
    workflow = load_workflow(file)
    merged = merge_inputs(workflow, inputs)
    if default_model is None:
        from readyagents.config import get_settings

        default_model = workflow.default_model or get_settings().default_model
    return estimate_workflow(
        workflow,
        merged,
        default_model=default_model or workflow.default_model,
        workflow_dir=file.parent,
        table=table,
    )


def estimate_workflow(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any] | None = None,
    *,
    default_model: str | None = None,
    workflow_dir: Path | None = None,
    table: PriceTable | None = None,
    include_depth: int = 0,
) -> EstimateResult:
    table = table or load_price_table()
    merged = dict(inputs or {})
    model = default_model or workflow.default_model or ""
    directory = Path(workflow_dir) if workflow_dir else Path.cwd()
    floor_calls: list[dict[str, Any]] = []
    ceiling_calls: list[dict[str, Any]] = []
    measured_any = False
    _walk(
        workflow,
        merged,
        default_model=model,
        workflow_dir=directory,
        assume="floor",
        into=floor_calls,
        include_depth=include_depth,
        measured={"flag": False},
    )
    _walk(
        workflow,
        merged,
        default_model=model,
        workflow_dir=directory,
        assume="ceiling",
        into=ceiling_calls,
        include_depth=include_depth,
        measured={"flag": False},
    )
    result = EstimateResult(
        table_updated_at=table.updated_at,
        table_source=table.source,
        stale=table.stale,
        nodes=_node_summaries(floor_calls, ceiling_calls),
    )
    _fold_calls(result, floor_calls, table, bound="floor")
    _fold_calls(result, ceiling_calls, table, bound="ceiling")
    measured_any = any(bool(c.get("measured")) for c in floor_calls + ceiling_calls)
    result.measured = measured_any
    result.assumptions = _assumptions(result, measured=measured_any)
    return result


def _walk(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any],
    *,
    default_model: str,
    workflow_dir: Path,
    assume: str,
    into: list[dict[str, Any]],
    include_depth: int,
    measured: dict[str, bool],
) -> None:
    from readyagents.workflow.engine import _next_node

    nodes = workflow.node_map()
    state = RunState.start(
        workflow.name,
        dict(inputs),
        metadata={"allow_http": bool(workflow.allow_http), "estimate": True},
    )
    current = workflow.start or workflow.nodes[0].id
    seen: set[str] = set()
    steps = 0
    while current:
        steps += 1
        if steps > _MAX_STEPS:
            break
        if current in seen:
            break
        if current not in nodes:
            break
        node = nodes[current]
        seen.add(current)
        _emit_node(
            node,
            state,
            default_model=default_model,
            workflow_dir=workflow_dir,
            assume=assume,
            into=into,
            include_depth=include_depth,
            measured=measured,
        )
        _seed_routing_output(node, state, assume)
        current = _next_node(workflow, node, state)


def _emit_node(
    node: NodeSpec,
    state: RunState,
    *,
    default_model: str,
    workflow_dir: Path,
    assume: str,
    into: list[dict[str, Any]],
    include_depth: int,
    measured: dict[str, bool],
) -> None:
    kind = str(node.type)
    if kind == NodeType.agent.value:
        into.extend(
            _agent_calls(
                node,
                state,
                default_model=default_model,
                assume=assume,
                measured=measured,
            )
        )
        return
    if kind == NodeType.parallel.value:
        for branch in node.branches or []:
            _emit_node(
                branch,
                state,
                default_model=default_model,
                workflow_dir=workflow_dir,
                assume=assume,
                into=into,
                include_depth=include_depth,
                measured=measured,
            )
        return
    if kind == NodeType.foreach.value:
        body = node.body
        if body is None:
            return
        count = _foreach_count(node, state, assume)
        nested: list[dict[str, Any]] = []
        _emit_node(
            body,
            state,
            default_model=default_model,
            workflow_dir=workflow_dir,
            assume=assume,
            into=nested,
            include_depth=include_depth,
            measured=measured,
        )
        for _ in range(count):
            into.extend(dict(row) for row in nested)
        return
    if kind == NodeType.include.value:
        if include_depth >= _MAX_INCLUDE_DEPTH:
            return
        child = _load_include(node, workflow_dir, state.mapping())
        if child is None:
            return
        spec, child_dir, child_inputs = child
        _walk(
            spec,
            child_inputs,
            default_model=default_model or spec.default_model or "",
            workflow_dir=child_dir,
            assume=assume,
            into=into,
            include_depth=include_depth + 1,
            measured=measured,
        )
        return


def _agent_calls(
    node: NodeSpec,
    state: RunState,
    *,
    default_model: str,
    assume: str,
    measured: dict[str, bool],
) -> list[dict[str, Any]]:
    ns = state.mapping()
    prompt = _safe_interpolate(node.prompt or "", ns)
    system = _safe_interpolate(node.system or "", ns) if node.system else ""
    prompt_tokens, prompt_measured = count_tokens(prompt)
    sys_tokens, sys_measured = count_tokens(system) if system else (0, False)
    if prompt_measured or sys_measured:
        measured["flag"] = True
    prompt_total = prompt_tokens + sys_tokens
    completion = FLOOR_COMPLETION if assume == "floor" else CEILING_COMPLETION
    model = node.model or default_model or ""
    retries = 1
    if assume == "ceiling" and node.retry is not None:
        retries = max(1, int(node.retry.max_attempts))
    candidates = 1
    if assume == "ceiling":
        seen: list[str] = []
        for ref in (model, *list(node.fallback_models or [])):
            if ref and ref not in seen:
                seen.append(ref)
        candidates = max(1, len(seen) or 1)
    tool_factor = 1
    if node.tools:
        cap = node.max_tool_rounds if node.max_tool_rounds is not None else _DEFAULT_MAX_TOOL_ROUNDS
        cap = max(1, min(int(cap), _HARD_MAX_TOOL_ROUNDS))
        tool_factor = 1 if assume == "floor" else (1 + cap)
    multiplier = retries * candidates * tool_factor if assume == "ceiling" else 1
    row = {
        "node_id": node.id,
        "model": model,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion,
        "multiplier": multiplier,
        "measured": prompt_measured or sys_measured,
    }
    return [row]


def _foreach_count(node: NodeSpec, state: RunState, assume: str) -> int:
    cap = node.max_items if node.max_items is not None else _DEFAULT_MAX_FOREACH
    cap = max(1, min(int(cap), _HARD_MAX_FOREACH))
    items = _try_foreach_items(node, state)
    if items is not None:
        return max(0, min(len(items), cap))
    return 1 if assume == "floor" else cap


def _try_foreach_items(node: NodeSpec, state: RunState) -> list[Any] | None:
    from readyagents.workflow.templates import interpolate, lookup

    ns = state.mapping()
    raw: Any
    try:
        raw = lookup(ns, node.items or "")
    except TemplateError:
        try:
            text = interpolate(node.items or "", ns)
        except (TemplateError, WorkflowError):
            return None
        raw = text
    if isinstance(raw, list):
        return raw
    return None


def _load_include(
    node: NodeSpec, workflow_dir: Path, ns: Mapping[str, Any]
) -> tuple[WorkflowSpec, Path, dict[str, Any]] | None:
    from readyagents.paths import resolve_within
    from readyagents.workflow.runner import load_workflow, merge_inputs
    from readyagents.workflow.templates import interpolate_value

    raw_path = (node.path or "").strip()
    if not raw_path:
        return None
    try:
        candidate = resolve_within(raw_path, workflow_dir, what="included workflow")
    except Exception:  # noqa: BLE001
        return None
    if not candidate.is_file():
        return None
    try:
        spec = load_workflow(candidate)
    except (ConfigError, WorkflowError):
        return None
    raw_inputs = interpolate_value(node.call_inputs, ns)
    if not isinstance(raw_inputs, dict):
        raw_inputs = dict(node.call_inputs or {})
    try:
        merged = merge_inputs(spec, raw_inputs)
    except WorkflowError:
        merged = dict(spec.input_defaults())
        merged.update(raw_inputs)
    return spec, candidate.parent, merged


def _seed_routing_output(node: NodeSpec, state: RunState, assume: str) -> None:
    kind = str(node.type)
    if kind == NodeType.condition.value:
        matched = _eval_condition(node.when or "", state)
        if matched is None:
            matched = assume == "floor"
        nxt = node.then if matched else node.else_
        state.node_outputs[node.id] = {"matched": matched, "next": nxt}
        if node.output_key:
            state.output_keys[node.output_key] = state.node_outputs[node.id]
        return
    if kind == NodeType.approval.value:
        approved = assume == "floor"
        nxt = (node.then or node.next) if approved else node.else_
        if assume == "ceiling" and node.else_ and node.then:
            nxt = node.then
        state.node_outputs[node.id] = {"approved": approved, "next": nxt}
        return
    if kind in {NodeType.transform.value, NodeType.tool.value}:
        state.node_outputs[node.id] = ""
        return


def _eval_condition(expr: str, state: RunState) -> bool | None:
    from readyagents.workflow.conditions import evaluate_condition

    try:
        return bool(evaluate_condition(expr, state.mapping()))
    except (WorkflowError, TemplateError, NodeError):
        return None


def _safe_interpolate(template: str, ns: Mapping[str, Any]) -> str:
    from readyagents.workflow.templates import interpolate

    try:
        return interpolate(template, ns)
    except (TemplateError, WorkflowError):
        return template


def _fold_calls(
    result: EstimateResult,
    calls: list[dict[str, Any]],
    table: PriceTable,
    *,
    bound: str,
) -> None:
    tokens = 0
    micros = 0
    unpriced = False
    for row in calls:
        n = max(1, int(row.get("multiplier") or 1))
        prompt = int(row.get("prompt_tokens") or 0)
        completion = int(row.get("completion_tokens") or 0)
        tokens += (prompt + completion) * n
        quote = table.quote(str(row.get("model") or ""))
        if not quote.priced:
            unpriced = True
            model = str(row.get("model") or "")
            if model and model not in result.unpriced_models:
                result.unpriced_models.append(model)
            continue
        priced = quote.cost_micros(prompt, completion) or 0
        micros += priced * n
    if bound == "floor":
        result.floor_tokens = tokens
        result.floor_micros = None if unpriced and micros == 0 and tokens else micros
        if unpriced:
            result.unpriced = True
            if result.floor_micros is not None and unpriced:
                # Priced calls still contribute; unpriced is a flag, not a zero.
                pass
    else:
        result.ceiling_tokens = tokens
        result.ceiling_micros = None if unpriced and micros == 0 and tokens else micros
        if unpriced:
            result.unpriced = True
    if unpriced:
        result.unpriced = True
        if bound == "floor" and result.floor_micros == 0 and result.unpriced_models:
            result.floor_micros = None
        if bound == "ceiling" and result.ceiling_micros == 0 and result.unpriced_models:
            result.ceiling_micros = None


def _node_summaries(
    floor_calls: list[dict[str, Any]], ceiling_calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: list[str] = []
    for row in floor_calls + ceiling_calls:
        nid = str(row.get("node_id") or "")
        if nid and nid not in seen:
            seen.append(nid)
    out: list[dict[str, Any]] = []
    for nid in seen:
        floors = [r for r in floor_calls if r.get("node_id") == nid]
        ceils = [r for r in ceiling_calls if r.get("node_id") == nid]
        model = (floors or ceils)[0].get("model") if (floors or ceils) else ""
        out.append(
            {
                "node_id": nid,
                "model": model,
                "floor_calls": sum(int(r.get("multiplier") or 1) for r in floors),
                "ceiling_calls": sum(int(r.get("multiplier") or 1) for r in ceils),
            }
        )
    return out


def _assumptions(result: EstimateResult, *, measured: bool) -> list[str]:
    band = MEASURED_BAND if measured else HEURISTIC_BAND
    kind = "tiktoken cl100k_base (measured)" if measured else "heuristic chars/4 (estimated)"
    rows = [
        f"tokenizer: {kind}; documented error band ±{int(band * 100)}%",
        f"floor completion tokens per call: {FLOOR_COMPLETION} (no retries, no tool rounds)",
        f"ceiling completion tokens per call: {CEILING_COMPLETION} "
        "(max retries × candidates × (1+max_tool_rounds when tools are declared))",
        "foreach: known list length when inputs resolve; otherwise floor 1 item, "
        "ceiling max_items (default 32)",
        "condition/approval: evaluated from inputs when possible; otherwise floor takes "
        "then, ceiling still walks one engine path via _next_node",
        "include nodes expand the child workflow (same loader as execution)",
        "the provider invoice is authoritative; this range is informational",
    ]
    if result.unpriced:
        models = ", ".join(result.unpriced_models) or "(unnamed)"
        rows.append(
            f"unpriced models (not $0): {models}. Caps that need a dollar amount fail closed."
        )
    if result.stale:
        rows.append(
            f"price table updated_at={result.table_updated_at} is older than warn_after_days; "
            "override with READYAGENTS_PRICES=PATH"
        )
    return rows
