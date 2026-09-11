"""Deterministic route selection. CLI ``models route --explain`` and the engine share this."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.cost.prices import PriceTable, load_price_table
from readyagents.errors import RoutingError
from readyagents.llm.capabilities import (
    LATENCY_RANK,
    QUALITY_RANK,
    CapabilityMatrix,
    assert_capable,
    is_hosted_ref,
    is_local_ref,
    load_capability_matrix,
    lookup_model,
)
from readyagents.routing.taint import effective_untrusted, resolve_routing_taint
from readyagents.workflow.schema import NodeSpec, RouteRule, RoutingSpec, WorkflowSpec


@dataclass
class RouteDecision:
    """Recorded choice: model, rule, strategy, fallback, taint. Dry-run safe."""

    model: str
    policy: bool
    reason: str
    strategy: str | None = None
    rule_id: str | None = None
    rule_index: int | None = None
    pin: str | None = None
    fallback: bool = False
    skipped: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    require: dict[str, Any] = field(default_factory=dict)
    taint: str = "trusted"
    local_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "policy": self.policy,
            "reason": self.reason,
            "strategy": self.strategy,
            "rule_id": self.rule_id,
            "rule_index": self.rule_index,
            "pin": self.pin,
            "fallback": self.fallback,
            "skipped": list(self.skipped),
            "candidates": list(self.candidates),
            "require": dict(self.require),
            "taint": self.taint,
            "local_only": self.local_only,
        }


def explain_route(
    workflow: WorkflowSpec,
    node: NodeSpec,
    *,
    state: Any = None,
    ctx: Any = None,
    primary: str | None = None,
    legacy_candidates: list[str] | None = None,
    tools: list[Any] | None = None,
    structured: bool = False,
    streaming: bool = False,
    matrix: CapabilityMatrix | None = None,
    prices: PriceTable | None = None,
) -> RouteDecision:
    """Same function the engine uses. Never calls a provider."""
    return select_route(
        workflow,
        node,
        state=state,
        ctx=ctx,
        primary=primary,
        legacy_candidates=legacy_candidates,
        tools=tools,
        structured=structured,
        streaming=streaming,
        matrix=matrix,
        prices=prices,
    )


def select_route(
    workflow: WorkflowSpec,
    node: NodeSpec,
    *,
    state: Any = None,
    ctx: Any = None,
    primary: str | None = None,
    legacy_candidates: list[str] | None = None,
    tools: list[Any] | None = None,
    structured: bool = False,
    streaming: bool = False,
    matrix: CapabilityMatrix | None = None,
    prices: PriceTable | None = None,
) -> RouteDecision:
    """First matching rule wins. No policy → legacy candidate list, unchanged."""
    legacy = list(legacy_candidates or [])
    default_model = (
        primary or getattr(node, "model", None) or getattr(workflow, "default_model", None)
    )
    if not legacy:
        if default_model:
            legacy = [default_model]
        fallback = list(getattr(node, "fallback_models", None) or [])
        fallback.extend(list(getattr(workflow, "fallback_models", None) or []))
        for item in fallback:
            ref = str(item).strip()
            if ref and ref not in legacy:
                legacy.append(ref)
    policy: RoutingSpec | None = getattr(workflow, "routing", None)
    taint = resolve_routing_taint(state, node)
    if policy is None or not policy.rules:
        model = legacy[0] if legacy else (default_model or "mock")
        return RouteDecision(
            model=model,
            policy=False,
            reason="no_routing_policy",
            candidates=list(legacy) if legacy else [model],
            taint=taint,
        )

    matrix = matrix or _matrix_for(policy, ctx)
    prices = prices or load_price_table()
    breaker = getattr(ctx, "circuit_breaker", None) if ctx is not None else None

    for index, rule in enumerate(policy.rules):
        if not _rule_matches(rule, node, taint):
            continue
        return _resolve_rule(
            rule,
            index=index,
            node=node,
            policy=policy,
            taint=taint,
            tools=tools,
            structured=structured,
            streaming=streaming,
            matrix=matrix,
            prices=prices,
            breaker=breaker,
            legacy=legacy,
            residency_locked=_residency_locked(policy, taint),
        )

    model = legacy[0] if legacy else (default_model or "mock")
    return RouteDecision(
        model=model,
        policy=False,
        reason="no_matching_rule",
        candidates=list(legacy) if legacy else [model],
        taint=taint,
    )


def _matrix_for(policy: RoutingSpec, ctx: Any) -> CapabilityMatrix:
    override = (policy.capability_matrix or "").strip() or None
    if override:
        return load_capability_matrix(override)
    extra = getattr(ctx, "capability_matrix", None) if ctx is not None else None
    if extra is not None:
        return extra
    return load_capability_matrix()


def _rule_matches(rule: RouteRule, node: NodeSpec, taint: str) -> bool:
    match = rule.match
    if match is None:
        return True
    if match.node and match.node != node.id:
        return False
    if match.node_tag:
        tags = [str(item) for item in (node.tags or [])]
        if match.node_tag not in tags:
            return False
    if match.role and match.role != (node.role or ""):
        return False
    if match.taint:
        want = match.taint
        effective = "untrusted" if effective_untrusted(taint) else "trusted"
        if want != effective:
            return False
    return True


def _resolve_rule(
    rule: RouteRule,
    *,
    index: int,
    node: NodeSpec,
    policy: RoutingSpec,
    taint: str,
    tools: list[Any] | None,
    structured: bool,
    streaming: bool,
    matrix: CapabilityMatrix,
    prices: PriceTable,
    breaker: Any,
    legacy: list[str],
    residency_locked: bool = False,
) -> RouteDecision:
    strategy = rule.strategy or ("pin" if rule.pin else None)
    require = _merged_require(rule, tools=tools, structured=structured, streaming=streaming)
    local_only = strategy == "local_only" or bool(require.get("local"))
    taint_rule = bool(rule.match and rule.match.taint == "untrusted")
    if residency_locked or (effective_untrusted(taint) and (local_only or taint_rule)):
        local_only = True
        require = {**require, "local": True}

    skipped: list[str] = []
    if strategy == "pin" or rule.pin:
        ordered = [str(rule.pin).strip()]
        reason = f"pin {ordered[0]}"
    else:
        pool = [str(item).strip() for item in (rule.pool or policy.pool or []) if str(item).strip()]
        if not pool:
            pool = list(matrix.catalog_refs())
            for extra in legacy:
                if extra not in pool:
                    pool.append(extra)
        ordered, reason = _rank_pool(
            pool,
            strategy=strategy or "cheapest_capable",
            prices=prices,
            matrix=matrix,
        )

    capable: list[str] = []
    for ref in ordered:
        if breaker is not None and not breaker.allow(ref):
            skipped.append(ref)
            continue
        if local_only and is_hosted_ref(ref, matrix=matrix):
            skipped.append(ref)
            continue
        caps = lookup_model(ref, matrix=matrix)
        if caps is None or not caps.supports(require):
            skipped.append(ref)
            continue
        if local_only and not is_local_ref(ref, matrix=matrix):
            skipped.append(ref)
            continue
        capable.append(ref)

    if not capable:
        raise RoutingError(
            "Unsatisfiable routing constraint: no capable model under the "
            f"declared rule (index={index}, strategy={strategy}, taint={taint}, "
            f"skipped={skipped[:8]})",
            rule_index=index,
            strategy=strategy,
            taint=taint,
        )

    chosen = capable[0]
    if strategy == "pin":
        assert_capable(chosen, require, matrix=matrix)
        if local_only and is_hosted_ref(chosen, matrix=matrix):
            raise RoutingError(
                f"local-only rule refuses hosted pin '{chosen}' (taint={taint})",
                rule_index=index,
                strategy=strategy,
                taint=taint,
            )

    return RouteDecision(
        model=chosen,
        policy=True,
        reason=reason,
        strategy=strategy,
        rule_id=rule.id,
        rule_index=index,
        pin=rule.pin,
        fallback=False,
        skipped=skipped,
        candidates=capable,
        require=require,
        taint=taint,
        local_only=local_only,
    )


def _residency_locked(policy: RoutingSpec, taint: str) -> bool:
    """Any local_only / taint:untrusted rule locks hosted routing for untrusted data."""
    if not effective_untrusted(taint):
        return False
    for rule in policy.rules:
        if (rule.strategy or "") == "local_only":
            return True
        if rule.match is not None and rule.match.taint == "untrusted":
            return True
    return False


def _merged_require(
    rule: RouteRule,
    *,
    tools: list[Any] | None,
    structured: bool,
    streaming: bool,
) -> dict[str, Any]:
    data = dict(rule.require.as_dict() if rule.require is not None else {})
    if tools:
        data["tool_calling"] = True
    if structured:
        data["structured_output"] = True
    if streaming and data.get("streaming"):
        data["streaming"] = True
    return data


def _rank_pool(
    pool: list[str],
    *,
    strategy: str,
    prices: PriceTable,
    matrix: CapabilityMatrix,
) -> tuple[list[str], str]:
    unique: list[str] = []
    for item in pool:
        if item and item not in unique:
            unique.append(item)
    if strategy == "fastest":
        ranked = sorted(unique, key=lambda ref: (_latency_rank(ref, matrix), unique.index(ref)))
        return ranked, "fastest by declared latency_class"
    if strategy == "highest_quality":
        ranked = sorted(unique, key=lambda ref: (_quality_rank(ref, matrix), unique.index(ref)))
        return ranked, "highest_quality by declared quality_class"
    if strategy == "local_only":
        return unique, "local_only (hosted providers excluded)"
    ranked = sorted(unique, key=lambda ref: (_price_rank(ref, prices), unique.index(ref)))
    return ranked, "cheapest_capable by price table"


def _latency_rank(ref: str, matrix: CapabilityMatrix) -> int:
    caps = lookup_model(ref, matrix=matrix)
    if caps is None:
        return 99
    return LATENCY_RANK.get(caps.latency_class, 99)


def _quality_rank(ref: str, matrix: CapabilityMatrix) -> int:
    caps = lookup_model(ref, matrix=matrix)
    if caps is None:
        return 99
    return QUALITY_RANK.get(caps.quality_class, 99)


def _price_rank(ref: str, prices: PriceTable) -> float:
    quote = prices.quote(ref)
    if not quote.priced or quote.rate is None:
        return 1e18
    return float(quote.rate.input) + float(quote.rate.output)
