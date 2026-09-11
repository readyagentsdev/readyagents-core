"""Per-route spend and token ceilings on top of the run-level cap."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from readyagents.cost.prices import load_price_table
from readyagents.errors import RouteBudgetExceeded
from readyagents.llm.resilience import usd_to_micros
from readyagents.workflow.schema import NodeSpec, RouteBudget


class RouteBudgetTracker:
    """Accumulates cost/tokens per budget key (node id, tag, or rule id)."""

    def __init__(self, budgets: Mapping[str, RouteBudget] | None = None) -> None:
        self.budgets = {str(k): v for k, v in dict(budgets or {}).items()}
        self.used: dict[str, dict[str, int]] = {}

    def keys_for(self, node: NodeSpec, *, rule_id: str | None = None) -> list[str]:
        found: list[str] = []
        for key in (node.id, *(node.tags or []), rule_id):
            if key and key in self.budgets and key not in found:
                found.append(key)
        return found

    def consult(
        self,
        node: NodeSpec,
        *,
        model: str,
        prompt_tokens: int = 0,
        rule_id: str | None = None,
    ) -> None:
        if not self.budgets:
            return
        table = load_price_table()
        quote = table.quote(model)
        prompt = max(0, int(prompt_tokens))
        estimated_cost = quote.cost_micros(prompt, 0)
        for key in self.keys_for(node, rule_id=rule_id):
            budget = self.budgets[key]
            used = self.used.setdefault(key, {"cost_micros": 0, "tokens": 0})
            token_limit = budget.max_tokens
            if token_limit is not None:
                projected = used["tokens"] + prompt
                if projected > int(token_limit):
                    raise RouteBudgetExceeded(
                        "route_tokens",
                        projected,
                        int(token_limit),
                        route=key,
                        reason="before_call",
                    )
            usd_limit = usd_to_micros(budget.max_cost_usd)
            if usd_limit is not None:
                if not quote.priced or estimated_cost is None:
                    raise RouteBudgetExceeded(
                        "route_cost_micros",
                        used["cost_micros"],
                        usd_limit,
                        route=key,
                        reason="unpriced",
                    )
                projected_cost = used["cost_micros"] + int(estimated_cost)
                if projected_cost > usd_limit:
                    raise RouteBudgetExceeded(
                        "route_cost_micros",
                        projected_cost,
                        usd_limit,
                        route=key,
                        reason="before_call",
                    )

    def record(
        self,
        node: NodeSpec,
        usage: Mapping[str, Any],
        *,
        rule_id: str | None = None,
    ) -> None:
        if not self.budgets:
            return
        tokens = int(usage.get("total_tokens") or 0)
        cost = int(usage.get("cost_micros") or 0)
        for key in self.keys_for(node, rule_id=rule_id):
            bucket = self.used.setdefault(key, {"cost_micros": 0, "tokens": 0})
            bucket["tokens"] += tokens
            bucket["cost_micros"] += cost
