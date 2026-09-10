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
from readyagents.errors import (
    ApprovalRequired,
    ConfigError,
    SourceMapBoundError,
    TrustError,
    WorkflowError,
)
from readyagents.llm.base import LLMProvider
from readyagents.llm.cache import LLMCache
from readyagents.llm.resilience import CircuitBreaker, usd_to_micros
from readyagents.logging import configure_logging, get_logger
from readyagents.notify import post_json
from readyagents.packs.loader import (
    collect_pack_authorizers,
    collect_pack_nodes,
    collect_pack_observers,
    collect_pack_seals,
    collect_pack_secrets,
    collect_pack_tools,
    confine_pack_path,
    discover_packs,
    load_pack_file,
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


def load_workflow(
    path: Path | str,
    *,
    display_path: str | None = None,
    source: str | None = None,
) -> WorkflowSpec:
    file = Path(path)
    shown = display_path if display_path is not None else str(path)
    if source is None:
        if not file.is_file():
            raise ConfigError(f"Workflow file not found: {file}")
        text = file.read_text(encoding="utf-8")
    else:
        text = source
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        if file.suffix.lower() in {".json"}:
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        from readyagents.workflow.source_map import locate_errors

        problems = locate_errors(exc, path, source=text, display_path=shown)
        raise WorkflowError(f"Could not parse {file}: {exc}", problems=problems) from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"Workflow {file} must be a mapping")
    try:
        return WorkflowSpec.model_validate(data)
    except ValidationError as exc:
        from readyagents.workflow.source_map import LocatedError, locate_errors

        try:
            problems = locate_errors(exc, path, source=text, display_path=shown)
        except SourceMapBoundError as bound:
            problems = [LocatedError(loc=(), message=str(bound), position=None)]
            raise WorkflowError(_format_validation(file, exc), problems=problems) from bound
        raise WorkflowError(_format_validation(file, exc), problems=problems) from exc


def _format_validation(path: Path, exc: ValidationError) -> str:
    lines = [f"Invalid workflow {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  - {loc}: {err.get('msg')}")
    return "\n".join(lines)


def confine_under(raw: str | Path, root: Path, *, what: str) -> Path:
    """Resolve `raw` and refuse anything outside `root` (symlink-aware)."""
    from readyagents.errors import PathError
    from readyagents.paths import resolve_within

    try:
        return resolve_within(raw, root, what=what)
    except PathError as extra:
        raise ConfigError(str(extra)) from extra


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
    record: bool | None = None,
    offline: bool = False,
    cassette_path: Path | str | None = None,
    policy: Path | str | None = None,
    max_spend: float | None = None,
    max_tokens_cap: int | None = None,
    labels: Mapping[str, str] | None = None,
    override_budget: bool = False,
    max_model_calls: int | None = None,
    max_run_tool_rounds: int | None = None,
    max_wall_seconds: float | None = None,
    verified_actor: Any | None = None,
    credentials: Path | str | None = None,
    require_signed: bool = False,
    frozen: bool = False,
    pack_specs: Sequence[str] | None = None,
) -> RunState:
    settings = settings or get_settings()
    source_file = Path(path)
    if not source_file.is_file():
        raise ConfigError(f"Workflow file not found: {source_file}")
    workflow_text = source_file.read_text(encoding="utf-8")
    if workflow_text.startswith("\ufeff"):
        workflow_text = workflow_text[1:]
    workflow = load_workflow(source_file, source=workflow_text)
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

    from readyagents.firewall.policy_file import load_resolved
    from readyagents.trust.enforce import (
        FILE_LOCK_KINDS,
        LOCK_GATE_NODE,
        ArtifactStatus,
        TrustReport,
        apply_lock,
        evaluate_workflow,
        lock_gate_pending,
        resolve_enforcement,
    )

    stored_policy = None
    stored_meta = resume_state.metadata if resume_state is not None else None
    if resume_state is not None:
        raw_stored = resume_state.metadata.get("policy")
        if isinstance(raw_stored, str) and raw_stored.strip():
            stored_policy = raw_stored.strip()
    loaded_policy = load_resolved(
        explicit=policy, workflow_dir=source_path.parent, stored=stored_policy
    )
    required, lock_frozen, on_mismatch = resolve_enforcement(
        require_signed=require_signed,
        frozen=frozen,
        policy=loaded_policy,
        stored=stored_meta,
    )
    pack_root = settings.workspace_path()
    keyring = None
    if required:
        from readyagents.trust.keyring import load_keyring

        keyring = load_keyring(home=settings.home_path())
    try:
        trust_report = evaluate_workflow(
            source_path,
            workspace=pack_root,
            pack_specs=pack_specs,
            require_signed=required,
            frozen=lock_frozen,
            on_lock_mismatch=on_mismatch,
            keyring=keyring,
            run_id=resume_state.run_id if resume_state is not None else run_id,
            check_lock=False,
            source_text=workflow_text,
        )
    except TrustError:
        if required or lock_frozen:
            raise
        trust_report = TrustReport()

    apply_lock(
        trust_report,
        source_path,
        frozen=lock_frozen,
        on_lock_mismatch=on_mismatch,
        kinds=FILE_LOCK_KINDS,
    )
    pending_lock_gate = lock_gate_pending(
        trust_report, on_lock_mismatch=on_mismatch, decisions=merged_decisions
    )

    tools = default_registry(allow_http=allow_http, workspace=workspace)
    packs = list(discover_packs())
    if extra_packs:
        packs.extend(list(extra_packs))
    if not pending_lock_gate:
        for spec in pack_specs or ():
            pack_path = confine_pack_path(spec, pack_root)
            packs.append(
                load_pack_file(
                    spec,
                    root=pack_root,
                    require_signed=False,
                    source=trust_report.pack_buffers.get(str(pack_path)),
                )
            )
    tools.merge(collect_pack_tools(packs))
    if extra_tools:
        tools.merge(extra_tools)
    tool_seals = collect_pack_seals(packs, extra_tools=extra_tools)

    pack_secrets = list(collect_pack_secrets(packs))
    if secrets is not None:
        from readyagents.secrets import as_backends

        pack_secrets = as_backends(secrets) + pack_secrets
    pack_authorizers = list(collect_pack_authorizers(packs))
    if authorizer is not None:
        pack_authorizers = [authorizer, *pack_authorizers]
    resolved_authorizer = resolve_authorizer(pack_authorizers)
    resolved_actor = actor if actor is not None else settings.actor
    if verified_actor is not None and getattr(verified_actor, "actor", None):
        resolved_actor = verified_actor.actor
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
    env_guard = None
    if workflow.mcp_servers and not dry_run and not pending_lock_gate:
        from readyagents.mcp.client import MCPClient

        mcp = MCPClient(workflow.mcp_servers, workspace)
        tools.merge(mcp.tools())

    pin_digests: dict[str, str] = {}
    mcp_descriptions: dict[str, str] = {}
    if mcp is not None:
        from readyagents.firewall.mcp_pin import grouped_snapshots
        from readyagents.trust.digest import KIND_MCP, prefixed

        for server, snap in grouped_snapshots(mcp.tools()).items():
            pin_digests[server] = snap.digest
            for tname, desc in snap.tools:
                mcp_descriptions[tname] = desc
            trust_report.artifacts.append(
                ArtifactStatus(
                    kind=KIND_MCP,
                    name=server,
                    digest=prefixed(snap.digest),
                    signature="n/a",
                )
            )
    if not pending_lock_gate:
        apply_lock(
            trust_report,
            source_path,
            frozen=lock_frozen,
            on_lock_mismatch=on_mismatch,
        )

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

    if pending_lock_gate:
        _raise_lock_gate(
            workflow,
            merged,
            source_path=source_path,
            trust_report=trust_report,
            persist=persist,
            save=_save if persist else None,
            auditor=auditor,
            actor=resolved_actor,
            run_id=resume_state.run_id if resume_state is not None else run_id,
            workspace=workspace,
            allow_http=allow_http,
            dry_run=dry_run,
            loaded_policy=loaded_policy,
        )

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
    want_record = bool(settings.record if record is None else record)
    if offline:
        want_record = False
    cassette = None
    from readyagents.replay.record import known_secret_values

    secret_values = known_secret_values(settings, pack_secrets or None)
    if want_record or offline:
        from readyagents.replay.cassette import Cassette

        if offline:
            path = Path(cassette_path) if cassette_path else None
            if path is None:
                raise ConfigError(
                    "Offline replay requires a cassette. Record one with --record "
                    "or READYAGENTS_RECORD=1."
                )
            cassette = Cassette.load(
                path,
                max_entry_bytes=settings.cassette_max_entry_bytes,
                max_bytes=settings.cassette_max_bytes,
            )
            if llm is None:
                from readyagents.replay.offline import CassetteProvider

                llm = CassetteProvider(cassette)
        else:
            cassette = Cassette.new(
                run_id="",
                workflow=workflow.name,
                max_entry_bytes=settings.cassette_max_entry_bytes,
                max_bytes=settings.cassette_max_bytes,
            )
        cassette.tool_seals = dict(tool_seals)
    fallback = list(workflow.fallback_models or []) + settings.fallback_model_list()
    pause_url = workflow.on_pause_url or settings.pause_notify_url
    from readyagents.cost.meter import SpendMeter
    from readyagents.cost.prices import load_price_table

    price_table = load_price_table(settings.prices_path())
    stored_spend = None
    stored_labels: dict[str, str] = {}
    if resume_state is not None and isinstance(resume_state.metadata, dict):
        raw_spend = resume_state.metadata.get("spend")
        if isinstance(raw_spend, dict):
            stored_spend = raw_spend
        raw_labels = resume_state.metadata.get("labels")
        if isinstance(raw_labels, dict):
            stored_labels = {str(k): str(v) for k, v in raw_labels.items()}
    resolved_labels = dict(stored_labels)
    if labels:
        resolved_labels.update({str(k): str(v) for k, v in dict(labels).items()})
    runaway = workflow.runaway
    meter_max_spend = usd_to_micros(max_spend) if max_spend is not None else None
    if meter_max_spend is None and stored_spend is not None:
        raw_cap = stored_spend.get("max_spend_micros")
        meter_max_spend = int(raw_cap) if raw_cap is not None else None
    meter_max_tokens = max_tokens_cap
    if meter_max_tokens is None and stored_spend is not None:
        raw_tok = stored_spend.get("max_tokens")
        meter_max_tokens = int(raw_tok) if raw_tok is not None else None
    meter_max_calls = max_model_calls
    if meter_max_calls is None and runaway is not None:
        meter_max_calls = runaway.max_model_calls
    if meter_max_calls is None and stored_spend is not None:
        raw_calls = stored_spend.get("max_model_calls")
        meter_max_calls = int(raw_calls) if raw_calls is not None else None
    meter_max_rounds = max_run_tool_rounds
    if meter_max_rounds is None and runaway is not None:
        meter_max_rounds = runaway.max_tool_rounds
    if meter_max_rounds is None and stored_spend is not None:
        raw_rounds = stored_spend.get("max_tool_rounds")
        meter_max_rounds = int(raw_rounds) if raw_rounds is not None else None
    meter_max_wall = max_wall_seconds
    if meter_max_wall is None and runaway is not None:
        meter_max_wall = runaway.max_wall_seconds
    if meter_max_wall is None and stored_spend is not None:
        raw_wall = stored_spend.get("max_wall_seconds")
        meter_max_wall = float(raw_wall) if raw_wall is not None else None
    if stored_spend is not None:
        spend_meter = SpendMeter.from_snapshot(stored_spend, table=price_table)
        spend_meter.table = price_table
        if max_spend is not None:
            spend_meter.max_spend_micros = meter_max_spend
        if max_tokens_cap is not None:
            spend_meter.max_tokens = meter_max_tokens
        if max_model_calls is not None:
            spend_meter.max_model_calls = meter_max_calls
        if max_run_tool_rounds is not None:
            spend_meter.max_tool_rounds = meter_max_rounds
        if max_wall_seconds is not None:
            spend_meter.max_wall_seconds = meter_max_wall
    else:
        spend_meter = SpendMeter(
            max_spend_micros=meter_max_spend,
            max_tokens=meter_max_tokens,
            max_model_calls=meter_max_calls,
            max_tool_rounds=meter_max_rounds,
            max_wall_seconds=meter_max_wall,
            table=price_table,
        )
    if resume_state is None:
        _refuse_to_start(
            workflow,
            merged,
            path=source_path,
            meter=spend_meter,
            override=override_budget,
            auditor=auditor,
            actor=resolved_actor,
            default_model=workflow.default_model or settings.default_model,
        )

    def _pause(exc: Any, state: RunState) -> None:
        if on_pause is not None:
            on_pause(exc, state)
        if pause_url:
            payload = build_pause_event(exc, state)
            try:
                post_json(pause_url, payload)
            except Exception as notify_exc:  # noqa: BLE001
                log.warning("pause webhook failed: %s", notify_exc)

    cred_policy = _load_credentials(credentials, source_path.parent)
    if cred_policy is not None:
        from readyagents.credentials.broker import RunEnvGuard

        env_guard = RunEnvGuard(cred_policy.managed_names())
        env_guard.install()
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
        cassette=cassette,
        offline=offline,
        recording=want_record,
        cassette_secrets=secret_values,
        policy=loaded_policy,
        pin_digests=pin_digests,
        mcp_descriptions=mcp_descriptions,
        pin_home=settings.home_path(),
        observers=collect_pack_observers(packs),
        spend_meter=spend_meter,
        labels=resolved_labels,
        verified_actor=verified_actor,
        credential_policy=cred_policy,
        credential_env=env_guard.saved if env_guard is not None else None,
        include_buffers=trust_report.include_buffers,
        require_signed=required,
        frozen=lock_frozen,
    )
    metadata = {
        "source": str(source_path),
        "allow_http": allow_http,
        "dry_run": dry_run,
        "workspace": str(workspace),
        "actor": resolved_actor,
    }
    if verified_actor is not None:
        metadata["identity"] = (
            verified_actor.as_dict() if hasattr(verified_actor, "as_dict") else dict(verified_actor)
        )
    if resolved_labels:
        metadata["labels"] = dict(resolved_labels)
    if loaded_policy is not None and loaded_policy.source:
        metadata["policy"] = loaded_policy.source
    if offline:
        metadata["replay"] = True
    if want_record:
        metadata["recorded"] = True
    metadata["supply_chain"] = trust_report.as_dict()
    if (
        resume_state is not None
        and resume_state.pending_node == LOCK_GATE_NODE
        and str(merged_decisions.get(LOCK_GATE_NODE) or "").strip().lower() in _APPROVE
    ):
        resume_state.pending_node = None
        resume_state.pending = None
        resume_state.status = "running"
    try:
        state = run_workflow(
            workflow,
            merged,
            ctx,
            metadata=metadata,
            state=resume_state if resume_state is not None else initial_state,
            run_id=run_id,
        )
        if cassette is not None:
            state.metadata["determinism"] = cassette.report.as_dict()
        _write_cassette(
            cassette,
            want_record,
            settings,
            ctx,
            state=state,
            persist_fn=_save if persist else None,
        )
        if not want_record and any(str(n.type) == "agent" for n in workflow.nodes):
            from readyagents.replay.record import first_run_hint

            hint = first_run_hint(recording=False, has_agent=True)
            if hint:
                log.info("%s", hint)
        _write_ledger(settings, ctx, state, persist=persist)
        return state
    except Exception as exc:
        run_state = getattr(exc, "state", None)
        _write_cassette(
            cassette,
            want_record,
            settings,
            ctx,
            state=run_state if isinstance(run_state, RunState) else None,
            persist_fn=_save if persist else None,
        )
        if isinstance(run_state, RunState):
            _write_ledger(settings, ctx, run_state, persist=persist)
        raise
    finally:
        from readyagents.observability import shutdown_observers

        ctx_obj = locals().get("ctx")
        if ctx_obj is not None:
            shutdown_observers(getattr(ctx_obj, "observers", None), redactor=redactor)
        if mcp is not None:
            mcp.close()
        if owned_store and store is not None:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        if env_guard is not None:
            env_guard.restore()


def _raise_lock_gate(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any],
    *,
    source_path: Path,
    trust_report: Any,
    persist: bool,
    save: Any,
    auditor: Any,
    actor: str | None,
    run_id: str | None,
    workspace: Path,
    allow_http: bool,
    dry_run: bool,
    loaded_policy: Any,
) -> None:
    from readyagents.trust.enforce import LOCK_GATE_NODE

    summary = ", ".join(item["artifact"] for item in trust_report.lock_mismatches)
    prompt = f"Lockfile mismatch: {summary}. Re-approve after reviewing artifact drift."
    metadata: dict[str, Any] = {
        "source": str(source_path),
        "allow_http": allow_http,
        "dry_run": dry_run,
        "workspace": str(workspace),
        "actor": actor,
        "supply_chain": trust_report.as_dict(),
    }
    if loaded_policy is not None and getattr(loaded_policy, "source", None):
        metadata["policy"] = loaded_policy.source
    state = RunState.start(workflow.name, inputs, metadata=metadata, run_id=run_id)
    state.pending_node = LOCK_GATE_NODE
    state.pending = {
        "node_id": LOCK_GATE_NODE,
        "type": "approval",
        "prompt": prompt,
        "resume": f"readyagents resume {state.run_id} --approve {LOCK_GATE_NODE}",
        "decide": (f"readyagents decide {state.run_id} --node {LOCK_GATE_NODE} --decision approve"),
    }
    state.finish("paused")
    if persist and save is not None:
        save(state)
    if auditor is not None:
        auditor("paused", run_id=state.run_id, node_id=LOCK_GATE_NODE, actor=actor)
    raise ApprovalRequired(LOCK_GATE_NODE, state.run_id, prompt, state=state)


def _load_credentials(explicit: Path | str | None, workflow_dir: Path) -> Any:
    from readyagents.credentials.policy import load_credentials_policy, resolve_credentials_path

    path = resolve_credentials_path(explicit=explicit, workflow_dir=workflow_dir)
    if path is None:
        return None
    return load_credentials_policy(path)


_TERMINAL_LEDGER = frozenset({"succeeded", "failed", "paused", "cancelled"})


def _refuse_to_start(
    workflow: WorkflowSpec,
    inputs: Mapping[str, Any],
    *,
    path: Path,
    meter: Any,
    override: bool,
    auditor: Any,
    actor: str | None,
    default_model: str | None = None,
) -> None:
    from readyagents.cost.estimate import estimate_workflow
    from readyagents.errors import BudgetExceeded

    if not meter.has_spend_cap:
        return
    estimate = estimate_workflow(
        workflow,
        inputs,
        default_model=default_model or workflow.default_model,
        workflow_dir=path.parent,
        table=meter.table,
    )
    over_tokens = meter.max_tokens is not None and estimate.ceiling_tokens >= meter.max_tokens
    over_spend = False
    if meter.max_spend_micros is not None:
        if estimate.unpriced:
            over_spend = True
        elif (estimate.ceiling_micros or 0) >= meter.max_spend_micros:
            over_spend = True
    if not over_tokens and not over_spend:
        return
    payload = {
        "workflow": workflow.name,
        "floor_tokens": estimate.floor_tokens,
        "ceiling_tokens": estimate.ceiling_tokens,
        "floor_micros": estimate.floor_micros,
        "ceiling_micros": estimate.ceiling_micros,
        "unpriced": estimate.unpriced,
        "max_tokens": meter.max_tokens,
        "max_spend_micros": meter.max_spend_micros,
        "actor": actor,
    }
    if override:
        if auditor is not None:
            auditor("budget_override", **payload)
        return
    if auditor is not None:
        auditor("budget_refuse", **payload)
    if over_tokens:
        raise BudgetExceeded(
            "tokens",
            estimate.ceiling_tokens,
            int(meter.max_tokens or 0),
            reason="refuse_to_start",
        )
    used = estimate.ceiling_micros if estimate.ceiling_micros is not None else 0
    raise BudgetExceeded(
        "cost_micros",
        int(used),
        int(meter.max_spend_micros or 0),
        reason="refuse_to_start",
    )


def _write_ledger(
    settings: Settings, ctx: ExecutionContext, state: RunState, *, persist: bool
) -> None:
    if not persist:
        return
    if state.status not in _TERMINAL_LEDGER:
        return
    from readyagents.cost.ledger import append_spend, spend_entry_from_state

    meter = getattr(ctx, "spend_meter", None)
    if meter is not None:
        state.metadata["spend"] = meter.snapshot()
    labels = getattr(ctx, "labels", None)
    if labels:
        state.metadata["labels"] = dict(labels)
    entry = spend_entry_from_state(state, labels=labels)
    append_spend(settings.ledger_dir(), entry, redactor=ctx.redactor)


def _write_cassette(
    cassette: Any,
    want_record: bool,
    settings: Settings,
    ctx: ExecutionContext,
    *,
    state: RunState | None = None,
    persist_fn: Any = None,
) -> None:
    if not want_record or cassette is None:
        return
    run_id = state.run_id if state is not None else (cassette.run_id or "unassigned")
    cassette.run_id = run_id
    dest = settings.cassettes_dir() / f"{run_id}.json"
    cassette.save(dest, root=settings.home_path())
    if state is not None:
        state.metadata["cassette"] = str(dest)
        state.metadata["recorded"] = True
        if cassette.report is not None:
            state.metadata["determinism"] = cassette.report.as_dict()
        if persist_fn is not None:
            persist_fn(state)


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
    policy: Path | str | None = None,
    max_spend: float | None = None,
    max_tokens_cap: int | None = None,
    labels: Mapping[str, str] | None = None,
    override_budget: bool = False,
    max_model_calls: int | None = None,
    max_run_tool_rounds: int | None = None,
    max_wall_seconds: float | None = None,
    verified_actor: Any | None = None,
    credentials: Path | str | None = None,
    require_signed: bool = False,
    frozen: bool = False,
    pack_specs: Sequence[str] | None = None,
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
            policy=policy,
            max_spend=max_spend,
            max_tokens_cap=max_tokens_cap,
            labels=labels,
            override_budget=override_budget,
            max_model_calls=max_model_calls,
            max_run_tool_rounds=max_run_tool_rounds,
            max_wall_seconds=max_wall_seconds,
            verified_actor=verified_actor,
            credentials=credentials,
            require_signed=require_signed,
            frozen=frozen,
            pack_specs=pack_specs,
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
    offline: bool = False,
    pack_specs: Sequence[str] | None = None,
    require_signed: bool = False,
    frozen: bool = False,
) -> RunState:
    """Start a new run with the stored workflow path and inputs."""
    settings = settings or get_settings()
    from readyagents.run_store import open_run_store

    store = open_run_store(settings)
    try:
        previous = store.get(run_id, allow_prefix=True).state
        source = previous.metadata.get("source")
        if not source:
            raise ConfigError(f"Run {previous.run_id} has no stored workflow path. Cannot replay.")
        cassette_path = previous.metadata.get("cassette")
        if offline and not cassette_path:
            fallback = settings.cassettes_dir() / f"{previous.run_id}.json"
            cassette_path = str(fallback) if fallback.is_file() else None
        state = run_workflow_file(
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
            offline=offline,
            cassette_path=cassette_path,
            record=False,
            pack_specs=pack_specs,
            require_signed=require_signed,
            frozen=frozen,
        )
        state.metadata["replayed_from"] = previous.run_id
        if offline:
            state.metadata["replay"] = True
        if persist:
            store.save(state)
        return state
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
