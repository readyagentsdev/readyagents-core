"""Run-scoped spend meter: consult before each model call, share under a lock."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from readyagents.cost.prices import PriceTable, load_price_table
from readyagents.errors import BudgetExceeded, RunawayGuard


class SharedBudget:
    """Cross-run USD cap for ``readyagents batch``. Per-row meters keep their own stats."""

    def __init__(self, max_spend_micros: int) -> None:
        self.max_spend_micros = max(0, int(max_spend_micros))
        self.cost_micros = 0
        self._reserved = 0
        self._lock = threading.Lock()

    def remaining_micros(self) -> int:
        with self._lock:
            return max(0, self.max_spend_micros - self.cost_micros - self._reserved)

    def reserve(self, estimated_cost: int | None, *, model: str = "") -> None:
        del model
        with self._lock:
            if estimated_cost is None:
                raise BudgetExceeded(
                    "cost_micros",
                    self.cost_micros,
                    self.max_spend_micros,
                    reason="unpriced",
                )
            cost = max(0, int(estimated_cost))
            in_flight = self.cost_micros + self._reserved
            projected = in_flight + cost
            if in_flight >= self.max_spend_micros or projected > self.max_spend_micros:
                raise BudgetExceeded(
                    "cost_micros",
                    projected,
                    self.max_spend_micros,
                    reason="before_call",
                )
            self._reserved += cost

    def commit(self, actual_cost: int, reserved_cost: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - max(0, int(reserved_cost)))
            self.cost_micros += max(0, int(actual_cost))

    def release(self, reserved_cost: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - max(0, int(reserved_cost)))


class SpendMeter:
    """Accumulated usage + optional hard caps. Caps are inert when unset.

    Parallel branches share one instance. ``consult_before_call`` is the cap:
    it runs under the lock *before* ``complete`` so a ScriptedLLM that would
    exceed is never invoked. An unpriced model plus ``max_spend_micros`` fails
    closed rather than treating the call as free.
    """

    def __init__(
        self,
        *,
        max_spend_micros: int | None = None,
        max_tokens: int | None = None,
        max_model_calls: int | None = None,
        max_tool_rounds: int | None = None,
        max_wall_seconds: float | None = None,
        table: PriceTable | None = None,
        started_at: str | None = None,
        clock: Any | None = None,
        shared_budget: SharedBudget | None = None,
    ) -> None:
        self.max_spend_micros = max_spend_micros
        self.max_tokens = max_tokens
        self.max_model_calls = max_model_calls
        self.max_tool_rounds = max_tool_rounds
        self.max_wall_seconds = max_wall_seconds
        self.table = table
        self.started_at = started_at
        self.shared_budget = shared_budget
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cost_micros = 0
        self.unpriced = False
        self.unpriced_models: list[str] = []
        self.model_calls = 0
        self.tool_rounds = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_savings_micros = 0
        self.by_model: dict[str, dict[str, int]] = {}
        self._reserved_tokens = 0
        self._reserved_cost = 0
        self._started_mono = self._clock()

    @property
    def has_spend_cap(self) -> bool:
        return self.max_spend_micros is not None or self.max_tokens is not None

    @property
    def has_runaway_cap(self) -> bool:
        return (
            self.max_model_calls is not None
            or self.max_tool_rounds is not None
            or self.max_wall_seconds is not None
        )

    def consult_before_call(
        self,
        model: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Raise ``BudgetExceeded`` / ``RunawayGuard`` *before* invoking complete."""
        table = self.table or load_price_table()
        quote = table.quote(model)
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        estimated_tokens = prompt + completion
        estimated_cost = quote.cost_micros(prompt, completion)
        with self._lock:
            self._check_wall_locked()
            if self.max_model_calls is not None and self.model_calls >= self.max_model_calls:
                raise RunawayGuard("model_calls", self.model_calls, self.max_model_calls)
            if self.max_tokens is not None:
                in_flight = self.total_tokens + self._reserved_tokens
                projected = in_flight + estimated_tokens
                if in_flight >= self.max_tokens or projected > self.max_tokens:
                    raise BudgetExceeded(
                        "tokens",
                        projected,
                        self.max_tokens,
                        reason="before_call",
                    )
                self._reserved_tokens += estimated_tokens
            if self.max_spend_micros is not None:
                if not quote.priced or estimated_cost is None:
                    self.unpriced = True
                    if model not in self.unpriced_models:
                        self.unpriced_models.append(model)
                    raise BudgetExceeded(
                        "cost_micros",
                        self.cost_micros,
                        self.max_spend_micros,
                        reason="unpriced",
                    )
                in_flight_cost = self.cost_micros + self._reserved_cost
                projected_cost = in_flight_cost + estimated_cost
                if (
                    in_flight_cost >= self.max_spend_micros
                    or projected_cost > self.max_spend_micros
                ):
                    raise BudgetExceeded(
                        "cost_micros",
                        projected_cost,
                        self.max_spend_micros,
                        reason="before_call",
                    )
                self._reserved_cost += estimated_cost
            self.model_calls += 1
            if self.shared_budget is not None:
                shared_cost = estimated_cost if quote.priced else None
                try:
                    self.shared_budget.reserve(shared_cost, model=model)
                except BudgetExceeded:
                    if self.max_tokens is not None:
                        self._reserved_tokens = max(0, self._reserved_tokens - estimated_tokens)
                    if self.max_spend_micros is not None and estimated_cost:
                        self._reserved_cost = max(0, self._reserved_cost - estimated_cost)
                    self.model_calls = max(0, self.model_calls - 1)
                    raise

    def release_reservation(self, *, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        tokens = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        table = self.table or load_price_table()
        reserved_cost = 0
        with self._lock:
            self._reserved_tokens = max(0, self._reserved_tokens - tokens)
            # Best-effort: drop matching reserved USD when we know the table.
            prompt = max(0, int(prompt_tokens))
            completion = max(0, int(completion_tokens))
            quote = table.quote("")
            priced = quote.cost_micros(prompt, completion)
            if priced:
                reserved_cost = priced
                self._reserved_cost = max(0, self._reserved_cost - priced)
            if self._reserved_tokens == 0:
                self._reserved_cost = 0
        if self.shared_budget is not None and reserved_cost:
            self.shared_budget.release(reserved_cost)

    def record_usage(
        self,
        model: str,
        usage: Mapping[str, Any],
        *,
        reserved_prompt: int = 0,
        reserved_completion: int = 0,
    ) -> dict[str, int]:
        """Fold provider usage into the meter. Returns priced extras for the run."""
        table = self.table or load_price_table()
        quote = table.quote(model)
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        total = _as_int(usage.get("total_tokens")) or (prompt + completion)
        priced = quote.cost_micros(prompt, completion)
        extras: dict[str, int] = {}
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += total
            reserved = max(0, int(reserved_prompt)) + max(0, int(reserved_completion))
            self._reserved_tokens = max(0, self._reserved_tokens - reserved)
            reserved_cost = quote.cost_micros(reserved_prompt, reserved_completion)
            if reserved_cost:
                self._reserved_cost = max(0, self._reserved_cost - reserved_cost)
            if self._reserved_tokens == 0:
                self._reserved_cost = 0
            if quote.priced and priced is not None:
                self.cost_micros += priced
                extras["priced_cost_micros"] = priced
            else:
                self.unpriced = True
                if model not in self.unpriced_models:
                    self.unpriced_models.append(model)
            if self.shared_budget is not None:
                self.shared_budget.commit(priced or 0, reserved_cost or 0)
            bucket = self.by_model.setdefault(
                model or "unknown",
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_micros": 0},
            )
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["total_tokens"] += total
            bucket["cost_micros"] += priced or 0
        return extras

    def record_cache_hit(self, model: str, usage: Mapping[str, Any]) -> int:
        table = self.table or load_price_table()
        quote = table.quote(model)
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        priced = quote.cost_micros(prompt, completion) or 0
        with self._lock:
            self.cache_hits += 1
            self.cache_savings_micros += priced
        return priced

    def record_cache_miss(self) -> None:
        with self._lock:
            self.cache_misses += 1

    def record_tool_round(self) -> None:
        with self._lock:
            if self.max_tool_rounds is not None and self.tool_rounds >= self.max_tool_rounds:
                raise RunawayGuard("tool_rounds", self.tool_rounds, self.max_tool_rounds)
            self.tool_rounds += 1

    def consult_tool_round(self) -> None:
        with self._lock:
            self._check_wall_locked()
            if self.max_tool_rounds is not None and self.tool_rounds >= self.max_tool_rounds:
                raise RunawayGuard("tool_rounds", self.tool_rounds, self.max_tool_rounds)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "cost_micros": self.cost_micros,
                "unpriced": self.unpriced,
                "unpriced_models": list(self.unpriced_models),
                "model_calls": self.model_calls,
                "tool_rounds": self.tool_rounds,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "cache_savings_micros": self.cache_savings_micros,
                "by_model": {k: dict(v) for k, v in self.by_model.items()},
                "max_spend_micros": self.max_spend_micros,
                "max_tokens": self.max_tokens,
                "max_model_calls": self.max_model_calls,
                "max_tool_rounds": self.max_tool_rounds,
                "max_wall_seconds": self.max_wall_seconds,
                "started_at": self.started_at,
            }

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any] | None,
        *,
        table: PriceTable | None = None,
        clock: Any | None = None,
    ) -> SpendMeter:
        row = dict(data or {})
        meter = cls(
            max_spend_micros=_opt_int(row.get("max_spend_micros")),
            max_tokens=_opt_int(row.get("max_tokens")),
            max_model_calls=_opt_int(row.get("max_model_calls")),
            max_tool_rounds=_opt_int(row.get("max_tool_rounds")),
            max_wall_seconds=_opt_float(row.get("max_wall_seconds")),
            table=table,
            started_at=str(row["started_at"]) if row.get("started_at") else None,
            clock=clock,
        )
        meter.prompt_tokens = _as_int(row.get("prompt_tokens"))
        meter.completion_tokens = _as_int(row.get("completion_tokens"))
        meter.total_tokens = _as_int(row.get("total_tokens"))
        meter.cost_micros = _as_int(row.get("cost_micros"))
        meter.unpriced = bool(row.get("unpriced"))
        meter.unpriced_models = [str(m) for m in list(row.get("unpriced_models") or [])]
        meter.model_calls = _as_int(row.get("model_calls"))
        meter.tool_rounds = _as_int(row.get("tool_rounds"))
        meter.cache_hits = _as_int(row.get("cache_hits"))
        meter.cache_misses = _as_int(row.get("cache_misses"))
        meter.cache_savings_micros = _as_int(row.get("cache_savings_micros"))
        raw_models = row.get("by_model") or {}
        if isinstance(raw_models, dict):
            meter.by_model = {
                str(k): {
                    "prompt_tokens": _as_int(v.get("prompt_tokens")),
                    "completion_tokens": _as_int(v.get("completion_tokens")),
                    "total_tokens": _as_int(v.get("total_tokens")),
                    "cost_micros": _as_int(v.get("cost_micros")),
                }
                for k, v in raw_models.items()
                if isinstance(v, dict)
            }
        return meter

    def cache_usage_delta(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            if self.cache_hits:
                out["cache_hits"] = self.cache_hits
            if self.cache_misses:
                out["cache_misses"] = self.cache_misses
            if self.cache_savings_micros:
                out["cache_savings_micros"] = self.cache_savings_micros
            return out

    def _check_wall_locked(self) -> None:
        if self.max_wall_seconds is None:
            return
        elapsed = _elapsed_seconds(self.started_at, self._clock, self._started_mono)
        if elapsed >= float(self.max_wall_seconds):
            raise RunawayGuard("wall_seconds", int(elapsed), int(self.max_wall_seconds))


def _elapsed_seconds(started_at: str | None, clock: Any, started_mono: float) -> float:
    if started_at:
        try:
            text = started_at.replace("Z", "+00:00")
            began = datetime.fromisoformat(text)
            if began.tzinfo is None:
                began = began.replace(tzinfo=UTC)
            return max(0.0, (datetime.now(UTC) - began).total_seconds())
        except ValueError:
            pass
    return max(0.0, float(clock() - started_mono))


def _as_int(raw: Any) -> int:
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _opt_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _opt_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
