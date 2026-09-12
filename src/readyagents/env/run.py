"""Apply an environment to a run: pin, canary, shadow, rollback guards, provenance."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from readyagents.config import Settings, get_settings
from readyagents.env.release import verify_release, workflow_path_for
from readyagents.env.schema import EnvFile, EnvironmentSpec, load_env_file
from readyagents.env.store import EnvStore
from readyagents.errors import EnvRefused
from readyagents.run_store import JsonRunStore
from readyagents.run_store.base import RunQuery
from readyagents.workflow.runner import run_workflow_file
from readyagents.workflow.state import RunState


def resolve_environment(
    name: str,
    *,
    env_file: Path | str | None = None,
    settings: Settings | None = None,
) -> tuple[EnvFile, EnvironmentSpec]:
    loaded = load_env_file(env_file, settings=settings)
    if loaded is None:
        raise EnvRefused("no environment config (readyagents.env.yaml)", reason="missing")
    spec = loaded.environments.get(name)
    if spec is None:
        raise EnvRefused(f"unknown environment {name!r}", reason="unknown")
    return loaded, spec


def select_pointer(
    env: str,
    spec: EnvironmentSpec,
    *,
    run_id: str,
    store: EnvStore,
) -> tuple[dict[str, Any], str]:
    """Return (pointer, channel) where channel is current|canary."""
    current = store.current(env)
    if current is None:
        raise EnvRefused(f"environment {env!r} has no deployed release", reason="undeployed")
    candidate = store.candidate(env)
    percent = int(spec.canary.percent) if spec.canary else 0
    if candidate and percent > 0 and _canary_hit(run_id, percent):
        return candidate, "canary"
    return current, "current"


def _canary_hit(run_id: str, percent: int) -> bool:
    digest = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < int(percent)


def runs_dir_for(env: str, spec: EnvironmentSpec, store: EnvStore) -> Path:
    """Isolated run-store path. Relative store: under home. Default: environments/<env>/runs."""
    if spec.store:
        raw = Path(spec.store)
        if raw.is_absolute() or ".." in raw.parts:
            raise EnvRefused("environment store must be a relative path under home", reason="store")
        dest = (store.settings.home_path() / raw).resolve()
        home = store.settings.home_path().resolve()
        if dest != home and home not in dest.parents:
            raise EnvRefused("environment store escapes home", reason="store")
        dest.mkdir(parents=True, exist_ok=True)
        return dest
    return store.runs_dir(env)


def run_in_environment(
    workflow: Path | str,
    env: str,
    *,
    settings: Settings | None = None,
    env_file: Path | str | None = None,
    inputs: dict[str, Any] | None = None,
    persist: bool = True,
    run_id: str | None = None,
    actor: str | None = None,
    **kwargs: Any,
) -> RunState:
    settings = settings or get_settings()
    _file, spec = resolve_environment(env, env_file=env_file, settings=settings)
    store = EnvStore(settings)
    rid = run_id or uuid4().hex
    pointer, channel = select_pointer(env, spec, run_id=rid, store=store)
    require_signed = bool(kwargs.get("require_signed"))
    verify_release(pointer, settings=settings, require_signed=require_signed)
    pinned = workflow_path_for(pointer)
    policy = kwargs.pop("policy", None)
    folder = Path(str(pointer.get("path") or ""))
    if (folder / "policy.yaml").is_file():
        policy = folder / "policy.yaml"
    elif spec.policy:
        policy = _resolve_env_path(spec.policy, workflow, settings)
    env_store = JsonRunStore(runs_dir_for(env, spec, store))
    max_spend = kwargs.pop("max_spend", None)
    if spec.budget and spec.budget.max_cost_usd is not None:
        max_spend = float(spec.budget.max_cost_usd)
    kwargs.pop("store", None)
    kwargs.pop("run_id", None)
    routing = kwargs.pop("routing", None)
    if spec.routing:
        routing = _resolve_env_path(spec.routing, workflow, settings)
    state = run_workflow_file(
        pinned,
        inputs=inputs,
        settings=settings,
        persist=persist,
        policy=policy,
        routing=routing,
        max_spend=max_spend,
        store=env_store,
        run_id=rid,
        actor=actor,
        **kwargs,
    )
    state.metadata["environment"] = env
    state.metadata["release"] = pointer.get("digest")
    state.metadata["release_channel"] = channel
    state.metadata["secret_scope"] = spec.secrets
    state.metadata["env_store"] = str(env_store.runs_dir)
    if persist:
        env_store.save(state)
    shadow_cost = _maybe_shadow(
        spec, pointer, store, env, pinned, inputs, settings, persist, kwargs
    )
    if shadow_cost is not None:
        state.metadata["shadow_cost_micros"] = shadow_cost
        state.metadata["shadow_unused"] = True
        if persist:
            env_store.save(state)
    rolled = _maybe_rollback(spec, env, store, env_store, actor=actor)
    if rolled:
        state.metadata["rollback"] = rolled.get("rollback_reason")
        if persist:
            env_store.save(state)
    return state


def pinned_workflow_for(
    env: str,
    *,
    settings: Settings | None = None,
    env_file: Path | str | None = None,
    run_id: str | None = None,
) -> Path:
    settings = settings or get_settings()
    _file, spec = resolve_environment(env, env_file=env_file, settings=settings)
    store = EnvStore(settings)
    rid = run_id or "estimate"
    pointer, _channel = select_pointer(env, spec, run_id=rid, store=store)
    verify_release(pointer, settings=settings)
    return workflow_path_for(pointer)


def _resolve_env_path(raw: str, workflow: Path | str, settings: Settings) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise EnvRefused("environment paths must be relative", reason="path")
    candidates = [
        settings.workspace_path() / path,
        Path(workflow).resolve().parent / path,
    ]
    for item in candidates:
        if item.is_file():
            return item
    raise EnvRefused(f"environment file not found: {raw}", reason="missing")


def _maybe_shadow(
    spec: EnvironmentSpec,
    serving: dict[str, Any],
    store: EnvStore,
    env: str,
    workflow: Path,
    inputs: dict[str, Any] | None,
    settings: Settings,
    persist: bool,
    kwargs: dict[str, Any],
) -> int | None:
    del workflow
    if spec.shadow is None:
        return None
    candidate = store.candidate(env)
    if not candidate or candidate.get("digest") == serving.get("digest"):
        return None
    verify_release(candidate, settings=settings)
    pinned = workflow_path_for(candidate)
    shadow_store = JsonRunStore(store.env_dir(env) / "shadow-runs")
    max_spend = None
    if spec.shadow.budget and spec.shadow.budget.max_cost_usd is not None:
        max_spend = float(spec.shadow.budget.max_cost_usd)
    extra = dict(kwargs)
    extra.pop("store", None)
    extra.pop("max_spend", None)
    extra.pop("run_id", None)
    extra.pop("policy", None)
    policy = None
    folder = Path(str(candidate.get("path") or ""))
    if (folder / "policy.yaml").is_file():
        policy = folder / "policy.yaml"
    try:
        shadow = run_workflow_file(
            pinned,
            inputs=inputs,
            settings=settings,
            persist=persist,
            store=shadow_store,
            max_spend=max_spend,
            policy=policy,
            **extra,
        )
    except Exception as extra_err:  # noqa: BLE001
        store.append_history(env, {"event": "shadow_error", "error": str(extra_err)})
        return None
    cost = int((shadow.usage or {}).get("cost_micros") or 0)
    store.append_history(
        env,
        {
            "event": "shadow",
            "release": candidate.get("digest"),
            "shadow_cost_micros": cost,
            "unused": True,
        },
    )
    if persist:
        shadow.metadata["shadow"] = True
        shadow.metadata["unused"] = True
        shadow_store.save(shadow)
    return cost


def _maybe_rollback(
    spec: EnvironmentSpec,
    env: str,
    store: EnvStore,
    run_store: Any,
    *,
    actor: str | None,
) -> dict[str, Any] | None:
    guard = spec.rollback
    if guard is None or not guard.on:
        return None
    current = store.current(env)
    if current and current.get("rollback_reason"):
        # Already sitting on a revert. Leftover windowed failures must not
        # treat the abandoned pin as previous and roll forward.
        return None
    window = int(guard.on.get("window") or 50)
    rows = run_store.list(RunQuery(limit=window))
    total = len(rows)
    if total == 0:
        return None
    failed = sum(1 for item in rows if getattr(item.state, "status", "") == "failed")
    error_rate = failed / total
    threshold = guard.on.get("error_rate_above")
    if threshold is not None and error_rate > float(threshold):
        return store.rollback(
            env,
            actor=actor,
            reason=f"error_rate {error_rate:.3f} > {threshold}",
            retain_failed=False,
        )
    min_health = guard.on.get("health_below")
    if min_health is not None:
        ok = (total - failed) / total
        if ok < float(min_health):
            return store.rollback(
                env,
                actor=actor,
                reason=f"health {ok:.3f} < {min_health}",
                retain_failed=False,
            )
    cost_cap = guard.on.get("cost_per_run_above")
    if cost_cap is not None and rows:
        costs = [int((item.state.usage or {}).get("cost_micros") or 0) / 1_000_000 for item in rows]
        avg = sum(costs) / len(costs)
        if avg > float(cost_cap):
            return store.rollback(
                env,
                actor=actor,
                reason=f"cost_per_run {avg:.4f} > {cost_cap}",
                retain_failed=False,
            )
    latency_cap = guard.on.get("latency_ms_above")
    if latency_cap is not None and rows:
        samples = [_wall_ms(item.state) for item in rows]
        present = [item for item in samples if item is not None]
        if present:
            avg_ms = sum(present) / len(present)
            if avg_ms > float(latency_cap):
                return store.rollback(
                    env,
                    actor=actor,
                    reason=f"latency_ms {avg_ms:.1f} > {latency_cap}",
                    retain_failed=False,
                )
    return None


def _wall_ms(state: RunState) -> float | None:
    if not state.started_at or not state.finished_at:
        return None
    try:
        start = datetime.fromisoformat(str(state.started_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(state.finished_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - start).total_seconds() * 1000.0
