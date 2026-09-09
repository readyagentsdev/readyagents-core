"""Load a workflow file, execute it, persist the run record."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from readyagents.audit import audit_dir_for, make_auditor
from readyagents.config import Settings, get_settings
from readyagents.errors import ConfigError, WorkflowError
from readyagents.llm.base import LLMProvider
from readyagents.llm.cache import LLMCache
from readyagents.llm.resilience import CircuitBreaker, usd_to_micros
from readyagents.logging import configure_logging, get_logger
from readyagents.notify import post_json
from readyagents.packs.loader import (
    collect_pack_authorizers,
    collect_pack_nodes,
    collect_pack_secrets,
    collect_pack_tools,
    discover_packs,
)
from readyagents.policy import redactor_from_settings, resolve_authorizer
from readyagents.tools import ToolRegistry, default_registry
from readyagents.workflow.cancellation import CancellationToken
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.pause import build_pause_event
from readyagents.workflow.schema import WorkflowSpec, validate_required_inputs
from readyagents.workflow.state import RunState, load_decision_file, persist_run

log = get_logger("runner")

_APPROVE = {"approve", "approved", "yes", "true", "accept", "ok"}


def load_workflow(path: Path | str) -> WorkflowSpec:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Workflow file not found: {file}")
    text = file.read_text(encoding="utf-8")
    try:
        if file.suffix.lower() in {".json"}:
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorkflowError(f"Could not parse {file}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"Workflow {file} must be a mapping")
    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as exc:
        raise WorkflowError(_format_validation(file, exc)) from exc


def _format_validation(path: Path, exc: ValidationError) -> str:
    lines = [f"Invalid workflow {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  - {loc}: {err.get('msg')}")
    return "\n".join(lines)


def confine_under(raw: str | Path, root: Path, *, what: str) -> Path:
    """Resolve `raw` and refuse anything outside `root` (symlink-aware)."""
    root = Path(root).resolve()
    text = str(raw).strip()
    if not text or "\x00" in text:
        raise ConfigError(f"{what} must be a path under {root}")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ConfigError(
            f"{what} is outside the workspace: {raw} "
            f"(resolved to {resolved}, must stay under {root})"
        )
    return resolved


def merge_inputs(workflow: WorkflowSpec, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(workflow.input_defaults())
    if overrides:
        merged.update(overrides)
    validate_required_inputs(workflow, merged)
    return merged


def run_workflow_file(
    path: Path | str,
    *,
    inputs: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    persist: bool = True,
    extra_tools: ToolRegistry | None = None,
    extra_packs: Sequence[Any] | None = None,
    decisions: Mapping[str, str] | None = None,
    resume_state: RunState | None = None,
    actor: str | None = None,
    authorizer: Any | None = None,
    secrets: Any | None = None,
    decision_file: Path | str | None = None,
    on_pause: Any | None = None,
    no_cache: bool = False,
    run_id: str | None = None,
    initial_state: RunState | None = None,
    cancellation: CancellationToken | None = None,
    store: Any | None = None,
) -> RunState:
    settings = settings or get_settings()
    workflow = load_workflow(path)
    if initial_state is not None and resume_state is not None:
        raise WorkflowError("initial_state and resume_state are mutually exclusive")
    merged_decisions: dict[str, str] = {}
    if decision_file:
        merged_decisions.update(load_decision_file(decision_file))
    if decisions:
        merged_decisions.update(
            {str(k): str(v).strip().lower() for k, v in dict(decisions).items()}
        )
    if resume_state is not None:
        merged = dict(resume_state.inputs)
        if inputs:
            merged.update(inputs)
        validate_required_inputs(workflow, merged)
    elif initial_state is not None:
        overrides = dict(initial_state.inputs)
        if inputs:
            overrides.update(inputs)
        merged = merge_inputs(workflow, overrides)
    else:
        merged = merge_inputs(workflow, inputs)

    source_path = Path(path).resolve()
    workflow_dir = source_path.parent
    if settings.workspace is not None:
        root = settings.workspace_path()
    else:
        root = workflow_dir
    declared = (workflow.workspace or "").strip()
    workspace = confine_under(declared, root, what="workspace") if declared else root
    allow_http = bool(workflow.allow_http or settings.allow_http)

    tools = default_registry(allow_http=allow_http, workspace=workspace)
    packs = list(discover_packs())
    if extra_packs:
        packs.extend(list(extra_packs))
    tools.merge(collect_pack_tools(packs))
    if extra_tools:
        tools.merge(extra_tools)

    pack_secrets = list(collect_pack_secrets(packs))
    if secrets is not None:
        from readyagents.secrets import as_backends

        pack_secrets = as_backends(secrets) + pack_secrets
    pack_authorizers = list(collect_pack_authorizers(packs))
    if authorizer is not None:
        pack_authorizers = [authorizer, *pack_authorizers]
    resolved_authorizer = resolve_authorizer(pack_authorizers)
    resolved_actor = actor if actor is not None else settings.actor
    action = "resume" if resume_state is not None else "run"
    resource = resume_state.run_id if resume_state is not None else workflow.name
    resolved_authorizer.check(resolved_actor, action, resource)
    for node_id, decision in merged_decisions.items():
        gate = "approve" if str(decision).strip().lower() in _APPROVE else "reject"
        resolved_authorizer.check(resolved_actor, gate, node_id)

    redact_on = bool(settings.redact if workflow.redact is None else workflow.redact)
    redactor = redactor_from_settings(
        enabled=redact_on,
        patterns=settings.redact_pattern_list(),
        literals=settings.redact_literal_list(),
    )
    if redactor is not None:
        configure_logging(settings.log_level, fmt=settings.log_format, redactor=redactor)

    mcp = None
    if workflow.mcp_servers and not dry_run:
        from readyagents.mcp.client import MCPClient

        mcp = MCPClient(workflow.mcp_servers, workspace)
        tools.merge(mcp.tools())

    runs_dir = settings.runs_dir()
    auditor = None
    if persist:
        auditor = make_auditor(audit_dir_for(settings.home_path()), redactor=redactor)

    owned_store = False
    if persist and store is None:
        try:
            from readyagents.run_store import open_run_store

            store = open_run_store(settings)
            owned_store = True
        except ImportError:
            store = None

    def _save(state: RunState) -> None:
        if store is not None:
            store.save(state, redactor=redactor)
        else:
            persist_run(state, runs_dir, redactor=redactor)

    budget = workflow.budget
    if budget and budget.max_tokens is not None:
        budget_tokens = budget.max_tokens
    else:
        budget_tokens = settings.max_tokens
    budget_cost = (
        usd_to_micros(budget.max_cost_usd)
        if budget and budget.max_cost_usd is not None
        else usd_to_micros(settings.max_cost_usd)
    )
    circuit_spec = workflow.circuit
    breaker = CircuitBreaker(
        failure_threshold=(
            circuit_spec.failure_threshold if circuit_spec else settings.circuit_failure_threshold
        ),
        cooldown_seconds=(
            circuit_spec.cooldown_seconds if circuit_spec else settings.circuit_cooldown_seconds
        ),
    )
    cache_enabled = bool(settings.llm_cache if workflow.cache_llm is None else workflow.cache_llm)
    if no_cache:
        cache_enabled = False
    llm_cache = LLMCache(settings.cache_dir()) if cache_enabled else None
    fallback = list(workflow.fallback_models or []) + settings.fallback_model_list()
    pause_url = workflow.on_pause_url or settings.pause_notify_url

    def _pause(exc: Any, state: RunState) -> None:
        if on_pause is not None:
            on_pause(exc, state)
        if pause_url:
            payload = build_pause_event(exc, state)
            try:
                post_json(pause_url, payload)
            except Exception as notify_exc:  # noqa: BLE001
                log.warning("pause webhook failed: %s", notify_exc)

    ctx = ExecutionContext(
        workflow,
        tools,
        dry_run=dry_run,
        llm=llm,
        default_model=workflow.default_model or settings.default_model,
        extra_handlers=collect_pack_nodes(packs),
        decisions=merged_decisions,
        on_persist=_save if persist else None,
        workflow_dir=source_path.parent,
        circuit_breaker=breaker,
        llm_cache=llm_cache,
        budget_tokens=budget_tokens,
        budget_cost_micros=budget_cost,
        secrets=pack_secrets or None,
        authorizer=resolved_authorizer,
        actor=resolved_actor,
        redactor=redactor,
        auditor=auditor,
        on_pause=_pause if (on_pause is not None or pause_url) else None,
        fallback_models=fallback,
        cache_llm=cache_enabled,
        usage_state=resume_state,
        cancellation=cancellation,
    )
    metadata = {
        "source": str(source_path),
        "allow_http": allow_http,
        "dry_run": dry_run,
        "workspace": str(workspace),
        "actor": resolved_actor,
    }
    try:
        state = run_workflow(
            workflow,
            merged,
            ctx,
            metadata=metadata,
            state=resume_state if resume_state is not None else initial_state,
            run_id=run_id,
        )
    finally:
        if mcp is not None:
            mcp.close()
        if owned_store and store is not None:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
    return state


def resume_run(
    run_id: str,
    *,
    settings: Settings | None = None,
    path: Path | str | None = None,
    inputs: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    persist: bool = True,
    extra_tools: ToolRegistry | None = None,
    extra_packs: Sequence[Any] | None = None,
    decisions: Mapping[str, str] | None = None,
    llm: LLMProvider | None = None,
    actor: str | None = None,
    authorizer: Any | None = None,
    secrets: Any | None = None,
    decision_file: Path | str | None = None,
    on_pause: Any | None = None,
    no_cache: bool = False,
    cancellation: CancellationToken | None = None,
    store: Any | None = None,
) -> RunState:
    settings = settings or get_settings()
    owned_store = False
    if store is None:
        from readyagents.run_store import open_run_store

        store = open_run_store(settings)
        owned_store = True
    try:
        state = store.get(run_id, allow_prefix=True).state
        source = path or state.metadata.get("source")
        if not source:
            raise ConfigError(
                f"Run {state.run_id} has no stored workflow path. Pass --workflow PATH."
            )
        return run_workflow_file(
            source,
            inputs=inputs,
            dry_run=dry_run,
            settings=settings,
            llm=llm,
            persist=persist,
            extra_tools=extra_tools,
            extra_packs=extra_packs,
            decisions=decisions,
            resume_state=state,
            actor=actor,
            authorizer=authorizer,
            secrets=secrets,
            decision_file=decision_file,
            on_pause=on_pause,
            no_cache=no_cache,
            cancellation=cancellation,
            store=store,
        )
    finally:
        if owned_store:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()


def replay_run(
    run_id: str,
    *,
    settings: Settings | None = None,
    persist: bool = True,
    dry_run: bool = False,
    extra_tools: ToolRegistry | None = None,
    extra_packs: Sequence[Any] | None = None,
    decisions: Mapping[str, str] | None = None,
    llm: LLMProvider | None = None,
    actor: str | None = None,
    authorizer: Any | None = None,
    secrets: Any | None = None,
    decision_file: Path | str | None = None,
    no_cache: bool = False,
) -> RunState:
    """Start a new run with the stored workflow path and inputs."""
    settings = settings or get_settings()
    from readyagents.run_store import open_run_store

    store = open_run_store(settings)
    try:
        previous = store.get(run_id, allow_prefix=True).state
        source = previous.metadata.get("source")
        if not source:
            raise ConfigError(
                f"Run {previous.run_id} has no stored workflow path. Cannot replay."
            )
        return run_workflow_file(
            source,
            inputs=previous.inputs,
            dry_run=dry_run,
            settings=settings,
            llm=llm,
            persist=persist,
            extra_tools=extra_tools,
            extra_packs=extra_packs,
            decisions=decisions,
            actor=actor,
            authorizer=authorizer,
            secrets=secrets,
            decision_file=decision_file,
            no_cache=no_cache,
            store=store,
        )
    finally:
        store.close()
