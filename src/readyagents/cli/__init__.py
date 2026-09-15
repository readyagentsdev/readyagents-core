"""Typer CLI for ReadyAgents Core."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from readyagents import __version__
from readyagents.config import DEFAULT_MCP_TOKEN_ENV
from readyagents.distill.cli import adapters_app, distill_app
from readyagents.errors import (
    ApprovalRequired,
    CassetteMiss,
    ConfigError,
    ConverseRequired,
    EnvRefused,
    IdentityError,
    ImportRefused,
    MCPError,
    ReadyAgentsError,
    RegistryRefused,
    WaitingRequired,
)
from readyagents.logging import configure_logging
from readyagents.packs.loader import collect_pack_specs, discover_packs, load_local_packs
from readyagents.registry.cli import registry_app
from readyagents.scaffold import TEMPLATES, create_project
from readyagents.testing.eval import load_eval_suite, run_eval
from readyagents.workflow.runner import (
    confine_under,
    load_workflow,
    replay_run,
    resume_run,
    run_workflow_file,
)
from readyagents.workflow.state import (
    RunState,
    build_decisions,
    load_decision_file,
    parse_input_pairs,
)

app = typer.Typer(
    name="readyagents",
    help="ReadyAgents Core — Agent Workflow engine + MCP Toolkit (BYOK).",
    no_args_is_help=True,
    add_completion=False,
)
from readyagents.cli._common import (
    _PACK_HELP,
    _WORKFLOW_ARG,
    _emit_env_error,
    _emit_run,
    _emit_run_exception,
    _fail,
    _feedback_payload,
    _json_envelope,
    _preview,
    _print_json,
    _print_paused,
    _print_run,
    _print_usage,
    _problems_fields,
    _state_from_exc,
    console,
    err_console,
)
from readyagents.cli.a2a import a2a_app
from readyagents.cli.approvals import approvals_app
from readyagents.cli.audit import audit_app, register_attest, register_evidence
from readyagents.cli.bench import bench_app
from readyagents.cli.connectors import connectors_app
from readyagents.cli.core import (
    register_decide,
    register_doctor,
    register_eval,
    register_init,
    register_new,
    register_resume,
    register_spend,
    register_validate,
    register_version,
)
from readyagents.cli.delegations import delegations_app
from readyagents.cli.env import env_app
from readyagents.cli.feedback import feedback_app
from readyagents.cli.health import health_app
from readyagents.cli.identity import identity_app, trust_app
from readyagents.cli.knowledge import knowledge_app
from readyagents.cli.mcp import mcp_app
from readyagents.cli.memory import memory_app
from readyagents.cli.models import models_app
from readyagents.cli.package import package_app
from readyagents.cli.policy import policy_app
from readyagents.cli.prompts import prompts_app
from readyagents.cli.run import register_run
from readyagents.cli.runs import runs_app
from readyagents.cli.serve import serve_app
from readyagents.cli.sessions import sessions_app
from readyagents.cli.skills import skills_app
from readyagents.cli.table import table_app
from readyagents.cli.triggers import triggers_app

app.add_typer(mcp_app, name="mcp", rich_help_panel="Core")
app.add_typer(runs_app, name="runs", rich_help_panel="Core")
app.add_typer(approvals_app, name="approvals", rich_help_panel="Core")
app.add_typer(delegations_app, name="delegations", rich_help_panel="Extras: Governance and audit")
app.add_typer(policy_app, name="policy", rich_help_panel="Core")
app.add_typer(audit_app, name="audit", rich_help_panel="Extras: Governance and audit")
app.add_typer(identity_app, name="identity", rich_help_panel="Extras: Governance and audit")
app.add_typer(trust_app, name="trust", rich_help_panel="Extras: Governance and audit")
app.add_typer(connectors_app, name="connectors", rich_help_panel="Extras: Connectivity")
app.add_typer(a2a_app, name="a2a", rich_help_panel="Extras: Connectivity")
app.add_typer(memory_app, name="memory", rich_help_panel="Extras: Data and knowledge")
app.add_typer(knowledge_app, name="knowledge", rich_help_panel="Extras: Data and knowledge")
app.add_typer(table_app, name="table", rich_help_panel="Extras: Data and knowledge")
app.add_typer(triggers_app, name="triggers", rich_help_panel="Extras: Operations")
app.add_typer(skills_app, name="skills", rich_help_panel="Extras: Agent capability")
app.add_typer(package_app, name="package", rich_help_panel="Extras: Packaging and distribution")
app.add_typer(models_app, name="models", rich_help_panel="Extras: Model and prompt")
app.add_typer(distill_app, name="distill", rich_help_panel="Extras: Model and prompt")
app.add_typer(health_app, name="health", rich_help_panel="Extras: Operations")
app.add_typer(bench_app, name="bench", rich_help_panel="Extras: Model and prompt")
app.add_typer(prompts_app, name="prompts", rich_help_panel="Extras: Model and prompt")
app.add_typer(feedback_app, name="feedback", rich_help_panel="Extras: Model and prompt")
app.add_typer(sessions_app, name="sessions", rich_help_panel="Extras: Agent capability")
app.add_typer(env_app, name="env", rich_help_panel="Extras: Operations")
app.add_typer(registry_app, name="registry", rich_help_panel="Extras: Operations")
app.add_typer(serve_app, name="serve", rich_help_panel="Extras: Packaging and distribution")


def _version_flag(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=_version_flag,
        is_eager=True,
        help="Print version and exit.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="DEBUG, INFO, WARNING, or ERROR.",
        envvar="READYAGENTS_LOG_LEVEL",
    ),
    log_format: str = typer.Option(
        "text",
        "--log-format",
        help="text or json (machine-parseable events with run/node).",
        envvar="READYAGENTS_LOG_FORMAT",
    ),
) -> None:
    configure_logging(log_level, fmt=log_format)


register_version(app)


register_init(app)


register_new(app)


@app.command("import", rich_help_panel="Extras: Authoring")
def import_cmd(
    source: str | None = typer.Argument(
        None, help="n8n, langgraph, crewai, or trigger (Zapier-shaped JSON)."
    ),
    path: Path | None = typer.Argument(None, help="Operator-exported workflow file."),
    explain: str | None = typer.Option(
        None,
        "--explain",
        help="Print the mapping table for SOURCE and write nothing.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Directory for workflow.yaml and report."),
    report: Path | None = typer.Option(None, "--report", help="Fidelity report path."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing workflow.yaml."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import an exported workflow. Structural translation only — not equivalence."""
    from readyagents.importers import explain_source, import_workflow

    try:
        if explain:
            table = explain_source(explain)
            rows = [
                {
                    "kind": item.kind,
                    "target": item.target,
                    "fidelity": item.fidelity,
                    "reason": item.reason,
                    "nearest": item.nearest,
                }
                for item in (*table.entries, table.default)
            ]
            if as_json:
                _print_json(
                    _json_envelope(
                        "import explain",
                        ok=True,
                        source=table.source,
                        schema_versions=list(table.schema_versions),
                        entries=rows,
                    )
                )
                return
            console.print(f"{table.source} mapping table (structural translation only)")
            for row in rows:
                console.print(f"  {row['kind']} -> {row['target'] or 'stub'} ({row['fidelity']})")
            return
        if not source or not path:
            raise ImportRefused(
                "usage: readyagents import SOURCE PATH --out DIR",
                reason="usage",
            )
        if out is None:
            out = Path("imported") / source
        result = import_workflow(source, path, out=out, report_path=report, force=force)
    except ImportRefused as extra:
        _emit_import_error("import", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("import", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    body = result.report.as_dict()
    if as_json:
        _print_json(
            _json_envelope(
                "import",
                ok=True,
                **body,
                workflow=str(result.workflow_path),
                report=str(result.report_path),
                graph=str(result.graph_path),
                dry_run_ok=result.dry_run_ok,
            )
        )
        return
    console.print(f"wrote {result.workflow_path}")
    console.print(f"report {result.report_path} coverage={result.report.coverage}%")
    console.print("structural translation only — test before use")


register_validate(app)


@app.command("schema", rich_help_panel="Extras: Authoring")
def schema_cmd(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the schema UTF-8 file. Refuses overwrite without --force.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite --output if it exists."),
    as_json: bool = typer.Option(False, "--json", help="Print the standard JSON envelope."),
    check: Path | None = typer.Option(
        None,
        "--check",
        help="Exit 0 if PATH matches the generated schema byte-for-byte.",
    ),
) -> None:
    """Emit the workflow JSON Schema (no network, no workflow execution)."""
    from readyagents.workflow.jsonschema import workflow_json_schema, workflow_json_schema_text

    try:
        text = workflow_json_schema_text()
        schema = workflow_json_schema()
        if check is not None and output is not None:
            raise ConfigError("Use --check or --output, not both.")
        if check is not None:
            _check_schema_file(check, text)
            if as_json:
                _print_json(_json_envelope("schema", ok=True, check=str(check), match=True))
            else:
                console.print(f"[green]schema matches[/green] {check}")
            return
        if output is not None:
            _write_schema_file(output, text, force=force)
            if as_json:
                _print_json(_json_envelope("schema", ok=True, path=str(output), schema=schema))
            else:
                console.print(f"[green]Wrote {output}[/green]")
            return
        if as_json:
            _print_json(_json_envelope("schema", ok=True, schema=schema))
            return
        typer.echo(text, nl=False)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "schema",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


register_doctor(app)


register_attest(app)


@app.command("bundle", rich_help_panel="Extras: Packaging and distribution")
def bundle_cmd(
    out: Path = typer.Option(..., "--out", help="Directory to write wheels and manifest.json."),
    python: str | None = typer.Option(
        None, "--python", help="Target Python X.Y (one per invocation)."
    ),
    platform: str | None = typer.Option(
        None, "--platform", help="Target platform tag (one per invocation)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Collect wheels for offline `pip install --no-index --find-links`."""
    from readyagents.sovereign.bundle import write_bundle

    try:
        payload = write_bundle(out, python=python, platform=platform)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("bundle", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("bundle", ok=True, path=str(out), **payload))
        return
    console.print(f"wrote {len(payload.get('files') or [])} files under {out}")


register_eval(app)


@app.command("optimize", rich_help_panel="Extras: Model and prompt")
def optimize_cmd(
    path: Path = _WORKFLOW_ARG,
    eval_suite: Path = typer.Option(..., "--eval", help="Eval suite YAML used as the scorer."),
    node: str | None = typer.Option(None, "--node", help="Prompt-bearing node id."),
    max_iterations: int = typer.Option(8, "--max-iterations"),
    max_spend: float | None = typer.Option(None, "--max-spend", help="Generation spend cap (USD)."),
    max_wall_seconds: float | None = typer.Option(None, "--max-wall-seconds"),
    min_improvement: float = typer.Option(0.05, "--min-improvement"),
    n_candidates: int = typer.Option(3, "--candidates"),
    hold_out: Path | None = typer.Option(
        None, "--hold-out", help="Held-out eval suite (mandatory)."
    ),
    frozen: Path | None = typer.Option(
        None, "--frozen", help="Frozen fixture suite that must not regress."
    ),
    require_approval: bool = typer.Option(False, "--require-approval"),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Provider for candidate generation and candidate scoring (not baseline replay).",
    ),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Reflective prompt optimization. Scoring is eval; generation is the only spend."""
    from readyagents.config import get_settings
    from readyagents.errors import OptimizeRefused, OptimizeStopped
    from readyagents.optimize.loop import optimize_workflow

    settings = get_settings()
    try:
        report = optimize_workflow(
            path,
            eval_suite,
            node=node,
            max_iterations=max_iterations,
            max_spend=max_spend,
            max_wall_seconds=max_wall_seconds,
            min_improvement=min_improvement,
            candidates=n_candidates,
            hold_out=hold_out,
            frozen=frozen,
            require_approval=require_approval,
            model=model,
            settings=settings,
            resume=resume,
        )
    except OptimizeRefused as extra:
        payload = extra.report.as_dict() if getattr(extra, "report", None) is not None else {}
        payload.pop("ok", None)
        if as_json:
            _print_json(
                _json_envelope(
                    "optimize",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                    **payload,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "optimize",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    ok = bool(body.pop("ok", True))
    if as_json:
        _print_json(_json_envelope("optimize", ok=ok, **body))
        return
    console.print(
        f"optimize stop={report.stop_reason} promoted={report.promoted} "
        f"delta={report.delta} spend_usd={report.spend_usd} "
        f"held_out={report.held_out.get('score')}"
    )
    if isinstance(report.stop, OptimizeStopped) and not report.promoted:
        return


@app.command("promote", rich_help_panel="Extras: Operations")
def promote_cmd(
    path: Path = _WORKFLOW_ARG,
    source: str = typer.Option(..., "--from", help="Source environment."),
    target: str = typer.Option(..., "--to", help="Target environment."),
    actor: str | None = typer.Option(None, "--actor", help="Actor id.", envvar="READYAGENTS_ACTOR"),
    approve: list[str] = typer.Option(
        [], "--approve", help="Approve the promotion gate (repeatable node id)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Copy a source pin onto a target after declared gates. Never auto-promotes."""
    from readyagents.config import get_settings
    from readyagents.env.promote import promote
    from readyagents.env.run import resolve_environment

    try:
        settings = get_settings()
        _file, spec = resolve_environment(target, settings=settings)
        pointer = promote(
            path,
            source=source,
            target=target,
            spec=spec,
            settings=settings,
            actor=actor,
            decisions=build_decisions(approve, []),
        )
    except ApprovalRequired as extra:
        _emit_run_exception(extra, as_json=as_json, persist=False, command="promote")
    except RegistryRefused as extra:
        _emit_registry_error("promote", extra, as_json=as_json)
    except EnvRefused as extra:
        _emit_env_error("promote", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("promote", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if as_json:
        _print_json(_json_envelope("promote", ok=True, environment=target, **pointer))
        return
    console.print(f"{source} -> {target} {pointer.get('digest')}")


@app.command("rollback", rich_help_panel="Extras: Operations")
def rollback_cmd(
    env: str = typer.Option(..., "--env", help="Named environment."),
    reason: str = typer.Option("manual", "--reason", help="Recorded rollback reason."),
    actor: str | None = typer.Option(None, "--actor", help="Actor id.", envvar="READYAGENTS_ACTOR"),
    approve: list[str] = typer.Option(
        [], "--approve", help="Approve rollback when the env requires the same roles as promote."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Restore the previous release atomically. Never rolls forward automatically."""
    from readyagents.config import get_settings
    from readyagents.env.promote import rollback_env
    from readyagents.env.run import resolve_environment

    try:
        settings = get_settings()
        _file, spec = resolve_environment(env, settings=settings)
        pointer = rollback_env(
            env,
            spec=spec,
            settings=settings,
            actor=actor,
            decisions=build_decisions(approve, []),
            reason=reason,
        )
    except ApprovalRequired as extra:
        _emit_run_exception(extra, as_json=as_json, persist=False, command="rollback")
    except EnvRefused as extra:
        _emit_env_error("rollback", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("rollback", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if as_json:
        _print_json(_json_envelope("rollback", ok=True, environment=env, **pointer))
        return
    console.print(f"{env} rolled back to {pointer.get('digest')} ({reason})")


@app.command("simulate", rich_help_panel="Extras: Authoring")
def simulate_cmd(
    path: Path = _WORKFLOW_ARG,
    seed: int = typer.Option(42, "--seed", help="Deterministic generator seed."),
    cases: int = typer.Option(64, "--cases", help="Maximum generated cases."),
    deterministic_only: bool = typer.Option(
        False, "--deterministic-only", help="Do not call a model even if --model is set."
    ),
    model: str | None = typer.Option(None, "--model", help="Opt-in model-assisted personas."),
    personas: str | None = typer.Option(
        None, "--personas", help="Comma-separated persona names (with --model)."
    ),
    max_spend: float | None = typer.Option(None, "--max-spend", help="Spend cap for --model."),
    fail_on: str | None = typer.Option(
        None, "--fail-on", help="CI mode: new-failure blocks unseen failure classes."
    ),
    out: Path | None = typer.Option(None, "--out", help="Directory for frozen fixtures."),
    live: bool = typer.Option(
        False,
        "--live-side-effects",
        help="Allow real write_file/http_get. Requires a permitting policy.",
    ),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file for live side effects."),
    sovereign: bool = typer.Option(False, "--sovereign"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Generate declaration-driven cases, score with eval, freeze distinct failures."""
    from readyagents.config import get_settings
    from readyagents.errors import SimulateRefused
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.simulate.run import simulate_workflow

    settings = get_settings()
    loaded = load_resolved(
        explicit=policy, workflow_dir=path.parent if path.is_file() else None, stored=None
    )
    persona_list = [p.strip() for p in (personas or "").split(",") if p.strip()]
    use_model = None if deterministic_only else model
    sov = sovereign or bool(settings.sovereign)
    llm = None
    if use_model and not sov:
        from readyagents.llm.registry import get_provider

        llm, _ = get_provider(use_model, settings=settings)
    try:
        report = simulate_workflow(
            path,
            seed=seed,
            cap=cases,
            out_dir=out,
            fail_on_new=fail_on == "new-failure",
            live_side_effects=live,
            policy=loaded,
            settings=settings,
            llm=llm,
            model=use_model,
            personas=persona_list or None,
            max_spend=max_spend,
            sovereign=sov,
        )
    except SimulateRefused as extra:
        payload = extra.report.as_dict() if getattr(extra, "report", None) is not None else {}
        if as_json:
            _print_json(
                _json_envelope(
                    "simulate",
                    ok=False,
                    error="SimulateRefused",
                    message=str(extra),
                    reason=extra.reason,
                    **payload,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "simulate",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    if as_json:
        _print_json(_json_envelope("simulate", ok=True, **body))
        return
    console.print(
        f"cases={report.cases} passed={report.passed} failed={report.failed} "
        f"clusters={len(report.clusters)} dry_run={report.dry_run}"
    )
    cov = report.coverage
    console.print(
        f"coverage reached={len(cov.get('reached') or [])} "
        f"unreached={len(cov.get('unreached') or [])} declared={cov.get('declared')}"
    )
    if report.new_failures:
        raise typer.Exit(code=1)


register_run(app)


@app.command("batch", rich_help_panel="Extras: Operations")
def batch_cmd(
    path: Path = _WORKFLOW_ARG,
    input_file: Path = typer.Option(
        ...,
        "--input-file",
        help="JSONL (one object per line, or a JSON array) or CSV of per-row inputs.",
    ),
    concurrency: int = typer.Option(
        8,
        "--concurrency",
        min=1,
        help="Max in-flight rows. Capped by READYAGENTS_MAX_CONCURRENCY (default 4096).",
    ),
    per_workflow_limit: int | None = typer.Option(
        None,
        "--per-workflow-limit",
        min=1,
        help="Max in-flight rows for this workflow. Env: READYAGENTS_PER_WORKFLOW_CONCURRENCY.",
        envvar="READYAGENTS_PER_WORKFLOW_CONCURRENCY",
    ),
    per_provider_limit: int | None = typer.Option(
        None,
        "--per-provider-limit",
        min=1,
        help="Max in-flight rows per provider. Env: READYAGENTS_PER_PROVIDER_CONCURRENCY.",
        envvar="READYAGENTS_PER_PROVIDER_CONCURRENCY",
    ),
    provider_rate: float | None = typer.Option(
        None,
        "--provider-rate",
        help="Token-bucket rate per provider (tokens/sec). Env: READYAGENTS_PROVIDER_RATE.",
        envvar="READYAGENTS_PROVIDER_RATE",
    ),
    continue_on_error: bool = typer.Option(
        True,
        "--continue-on-error/--no-continue-on-error",
        help="Record a failed row and keep going (default). A failed row never aborts the others.",
    ),
    max_spend: float | None = typer.Option(
        None,
        "--max-spend",
        help="Hard USD cap across all rows, consulted before each model call.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write per-row JSONL results (sorted by index). Workspace-confined.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the batch summary as JSON on stdout (no tables).",
    ),
    no_persist: bool = typer.Option(False, "--no-persist", help="Do not write run records."),
    run_store: str = typer.Option(
        "sqlite",
        "--run-store",
        help="json or sqlite. Batch defaults to sqlite (WAL). JSON remains the run default.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Actor id for RBAC hooks (env: READYAGENTS_ACTOR).",
        envvar="READYAGENTS_ACTOR",
    ),
    policy: Path | None = typer.Option(
        None,
        "--policy",
        help="Firewall policy file (env: READYAGENTS_POLICY).",
        envvar="READYAGENTS_POLICY",
    ),
) -> None:
    """Run one workflow over many input rows. Foreground; ends when the file is done."""
    persist = not no_persist
    backend = (run_store or "sqlite").strip().lower()
    if backend not in {"json", "sqlite"}:
        _fail(ConfigError("Invalid --run-store. Expected json or sqlite."))
        return
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.workflow.batch import run_batch
    from readyagents.workflow.governor import get_governor

    settings = get_settings()
    root = settings.workspace_path()
    dest = confine_under(out, root, what="batch results") if out is not None else None
    store = None
    owned_store = False
    governor = get_governor()
    governor.configure(
        per_workflow_limit=per_workflow_limit,
        per_provider_limit=per_provider_limit,
        provider_rate=provider_rate,
    )
    pack_specs = collect_pack_specs(pack)

    def _progress(row: Any, done: int, total: int) -> None:
        extra = f" run_id={row.run_id}" if row.run_id else ""
        err_console.print(f"batch {done}/{total} row={row.index} {row.status}{extra}")

    try:
        if persist:
            store = open_run_store(settings, backend=backend)
            owned_store = True
        report = run_batch(
            path,
            input_file,
            concurrency=concurrency,
            continue_on_error=continue_on_error,
            max_spend=max_spend,
            out=dest,
            governor=governor,
            persist=persist,
            store=store,
            settings=settings,
            on_progress=_progress,
            run_kwargs={
                "dry_run": dry_run,
                "pack_specs": pack_specs,
                "actor": actor,
                "policy": policy,
            },
        )
    except KeyboardInterrupt:
        governor.request_shutdown()
        if as_json:
            _print_json(_json_envelope("batch", ok=False, error="cancelled", status="cancelled"))
        else:
            err_console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1) from None
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "batch",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        if owned_store and store is not None:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
    _emit_batch(report, as_json=as_json)


register_resume(app)


@app.command("wake", rich_help_panel="Extras: Operations")
def wake_cmd(
    run_id: str | None = typer.Argument(None, help="Waiting run id, or omit with --all."),
    all_runs: bool = typer.Option(False, "--all", help="Evaluate every waiting run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Evaluate wait conditions. Lazy. Core starts no timer or daemon."""
    from readyagents.config import get_settings
    from readyagents.wait.wake import wake_all, wake_one

    settings = get_settings()
    try:
        if all_runs or run_id is None:
            report = wake_all(settings=settings)
        else:
            report = wake_one(run_id, settings=settings)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("wake", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("wake", ok=True, **report))
        return
    console.print(str(report))


@app.command("event", rich_help_panel="Extras: Operations")
def event_cmd(
    name: str = typer.Argument(..., help="Event name."),
    payload: Path | None = typer.Option(None, "--payload", help="JSON payload file."),
    signature: str | None = typer.Option(None, "--signature", help="HMAC hex of the event body."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Inject a signed event. Unsigned events are refused and do not wake."""
    import os

    from readyagents.audit import audit_dir_for, make_auditor
    from readyagents.config import get_settings
    from readyagents.wait.events import accept_event

    settings = get_settings()
    body: dict[str, Any] = {}
    if payload is not None:
        raw = payload.read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise typer.Exit(code=1)
        body = loaded
    secret = (os.environ.get("READYAGENTS_EVENT_SECRET") or "").strip() or None
    auditor = make_auditor(audit_dir_for(settings.home_path()))
    try:
        report = accept_event(
            settings.home_path(),
            name=name,
            payload=body,
            secret=secret,
            signature=signature,
            actor=settings.actor,
            auditor=auditor,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("event", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("event", ok=True, **report))
        return
    console.print(f"event {name} id={report.get('event_id')} idempotent={report.get('idempotent')}")


@app.command("agents-md", rich_help_panel="Extras: Agent capability")
def agents_md_cmd(
    dest: Path | None = typer.Option(None, "--out", help="Write AGENTS.md here."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Emit project context: how to run, validate, and test workflows here."""
    from readyagents.skills.agents_md import generate_agents_md

    text = generate_agents_md(dest=dest)
    if as_json:
        _print_json(_json_envelope("agents-md", ok=True, text=text))
        return
    if dest is None:
        console.print(text)


register_decide(app)


@app.command("packs", rich_help_panel="Extras: Packaging and distribution")
def packs_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
) -> None:
    """List installed ReadyAgents packs (entry point group readyagents.packs)."""
    try:
        found = list(discover_packs())
        found.extend(_load_extra_packs(pack))
    except ReadyAgentsError as exc:
        if as_json:
            _print_json(
                _json_envelope(
                    "packs",
                    ok=False,
                    error=type(exc).__name__,
                    message=str(exc),
                )
            )
            raise typer.Exit(code=1) from exc
        _fail(exc)
        return
    if as_json:
        _print_json(
            _json_envelope(
                "packs",
                ok=True,
                packs=[{"name": pack.name, "version": pack.version} for pack in found],
            )
        )
        return
    if not found:
        console.print("No packs installed. Core runs without any packs.")
        console.print("readyagents packs --pack examples/packs/connector_pack.py")
        return
    table = Table(title="Installed packs")
    table.add_column("Name")
    table.add_column("Version")
    for pack in found:
        table.add_row(pack.name, pack.version)
    console.print(table)


@app.command("delegate", rich_help_panel="Extras: Governance and audit")
def delegate_cmd(
    from_actor: str = typer.Option(..., "--from", help="Delegator actor id."),
    to_actor: str = typer.Option(..., "--to", help="Delegate actor id."),
    until: str = typer.Option(..., "--until", help="RFC 3339 / ISO-8601 timestamp."),
    scope: str | None = typer.Option(None, "--scope", help="Optional role scope."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Grant a time-bounded, single-hop, revocable delegation. Checked at decision time."""
    from readyagents.approvals.delegate import add_delegation
    from readyagents.config import get_settings

    try:
        settings = get_settings()
        entry = add_delegation(
            from_actor=from_actor,
            to_actor=to_actor,
            until=until,
            scope=scope,
            home=settings.home_path(),
        )
        from readyagents.audit import audit_dir_for, make_auditor

        make_auditor(audit_dir_for(settings.home_path()))(
            "delegation_granted",
            run_id="delegation",
            actor=from_actor,
            delegated_from=from_actor,
            delegated_to=to_actor,
            until=entry.until,
            scope=entry.scope,
            delegation_id=entry.id,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "delegate",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("delegate", ok=True, **entry.as_dict()))
        return
    console.print(
        f"delegated {entry.from_actor} -> {entry.to_actor} until={entry.until} id={entry.id}"
    )


register_evidence(app)


register_spend(app)


@app.command("studio", rich_help_panel="Extras: Authoring")
def studio_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Non-loopback binds are refused.",
    ),
    port: int = typer.Option(
        8790,
        "--port",
        min=1,
        max=65535,
        help="Bind port (default 8790).",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open",
        help="Open the loopback URL in a browser. The bootstrap token stays on stderr.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Disable every write path server-side (save, fork, freeze, decide).",
    ),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
) -> None:
    """Foreground localhost workflow studio. Stops when this process stops."""
    try:
        from readyagents.studio.server import serve_studio

        serve_studio(
            host=host,
            port=port,
            open_browser=open_browser,
            read_only=read_only,
            actor=actor,
        )
    except ReadyAgentsError as exc:
        _fail(exc)


@app.command("graph", rich_help_panel="Extras: Authoring")
def graph_cmd(
    path: Path = _WORKFLOW_ARG,
    direction: str = typer.Option("LR", "--direction", help="LR or TD."),
    output: Path | None = typer.Option(None, "--output", "--out", help="Write Mermaid to a file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Deterministic Mermaid routing diagram. Executes nothing."""
    from readyagents.compliance.graph import render_mermaid
    from readyagents.config import get_settings

    try:
        spec = load_workflow(path)
        mermaid = render_mermaid(spec, direction=direction)
        if output is not None:
            dest = confine_under(output, get_settings().workspace_path(), what="graph output")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(mermaid, encoding="utf-8")
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "graph",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload: dict[str, Any] = {"mermaid": mermaid, "workflow": spec.name}
        if output is not None:
            payload["path"] = str(output)
        _print_json(_json_envelope("graph", ok=True, **payload))
        return
    console.print(mermaid, end="")


@app.command("sign", rich_help_panel="Extras: Governance and audit")
def sign_cmd(
    path: Path = typer.Argument(..., help="Workflow, pack, or SKILL.md path."),
    key: Path = typer.Option(..., "--key", help="Operator-supplied Ed25519 private key."),
    out: Path | None = typer.Option(None, "--out", help="Signature path (default: PATH.sig)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write a detached Ed25519 signature beside the artifact. Private keys are not stored."""
    from readyagents.trust.sign import sign_artifact

    try:
        payload = sign_artifact(path, key=key, out=out)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sign",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    artifact=getattr(extra, "artifact", str(path)),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sign", ok=True, **payload, path=str(path)))
        return
    console.print(
        f"signed {path} kind={payload['kind']} "
        f"digest={payload['digest']} key_id={payload['key_id']}"
    )


@app.command("verify", rich_help_panel="Extras: Governance and audit")
def verify_cmd(
    path: Path = typer.Argument(..., help="Workflow, pack, or SKILL.md path."),
    as_json: bool = typer.Option(False, "--json"),
    sig: Path | None = typer.Option(None, "--sig", help="Detached signature path."),
) -> None:
    """Verify a detached signature against the local publisher keyring."""
    from readyagents.trust.sign import verify_artifact

    try:
        payload = verify_artifact(path, sig_path=sig)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    artifact=getattr(extra, "artifact", str(path)),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        body = {k: v for k, v in payload.items() if k != "ok"}
        _print_json(_json_envelope("verify", ok=True, **body))
        return
    console.print(
        f"ok kind={payload['kind']} digest={payload['digest']} key_id={payload['key_id']}"
    )


@app.command("lock", rich_help_panel="Extras: Governance and audit")
def lock_cmd(
    path: Path = _WORKFLOW_ARG,
    out: Path | None = typer.Option(
        None, "--out", help="Lockfile path (default: readyagents.lock beside the workflow)."
    ),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write readyagents.lock pinning workflow, include, pack, MCP, and skill digests."""
    from readyagents.config import get_settings
    from readyagents.trust.lock import (
        build_lockfile,
        default_lock_path,
        snapshot_mcp_surfaces,
        write_lockfile,
    )
    from readyagents.workflow.runner import confine_under, load_workflow

    try:
        settings = get_settings()
        spec = load_workflow(path)
        source = path.resolve()
        pack_root = settings.workspace_path()
        root = pack_root if settings.workspace is not None else source.parent
        declared = (spec.workspace or "").strip()
        workspace = confine_under(declared, root, what="workspace") if declared else root
        surfaces = snapshot_mcp_surfaces(spec, workspace) if spec.mcp_servers else {}
        lock = build_lockfile(
            source,
            pack_specs=collect_pack_specs(pack),
            workspace=pack_root,
            mcp_surfaces=surfaces,
            skill_home=settings.home_path(),
        )
        dest = out if out is not None else default_lock_path(source)
        write_lockfile(lock, dest)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "lock",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("lock", ok=True, path=str(dest), **lock.as_dict()))
        return
    console.print(f"wrote {dest} artifacts={len(lock.artifacts)}")


@app.command("sbom", rich_help_panel="Extras: Governance and audit")
def sbom_cmd(
    path: Path = _WORKFLOW_ARG,
    out: Path | None = typer.Option(None, "--out", help="Write the SBOM JSON to this path."),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Emit a deterministic CycloneDX-shaped inventory. No network, no secrets."""
    from readyagents.config import get_settings
    from readyagents.trust.sbom import build_sbom, dumps_sbom, write_sbom

    try:
        settings = get_settings()
        source = path.resolve()
        pack_root = settings.workspace_path()
        bom = build_sbom(source, pack_specs=collect_pack_specs(pack), workspace=pack_root)
        written = write_sbom(bom, out) if out is not None else None
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sbom",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload = dict(bom)
        if written is not None:
            payload["path"] = str(written)
        _print_json(_json_envelope("sbom", ok=True, **payload))
        return
    text = dumps_sbom(bom)
    if written is not None:
        console.print(f"wrote {written}")
        return
    console.print(text, end="")


def _load_extra_packs(pack_flags: list[str]) -> list[Any]:
    """Load --pack / READYAGENTS_PACK modules confined to the workspace."""
    from readyagents.config import get_settings

    specs = collect_pack_specs(pack_flags)
    if not specs:
        return []
    return load_local_packs(specs, root=get_settings().workspace_path())


def _emit_batch(report: Any, *, as_json: bool) -> None:
    payload = report.as_dict()
    ok = report.failed == 0 and report.cancelled == 0 and report.skipped == 0
    if as_json:
        _print_json(_json_envelope("batch", ok=ok and report.paused == 0, **payload))
    else:
        table = Table(title=f"Batch {report.workflow}")
        table.add_column("index")
        table.add_column("status")
        table.add_column("run_id")
        table.add_column("error", overflow="fold")
        for row in report.results:
            table.add_row(
                str(row.index),
                row.status,
                row.run_id or "",
                escape(row.error or ""),
            )
        console.print(table)
        console.print(
            f"succeeded={report.succeeded} failed={report.failed} "
            f"paused={report.paused} skipped={report.skipped} "
            f"cancelled={report.cancelled} total={report.total}"
        )
        if report.out:
            console.print(f"results: {report.out}")
    if report.paused and report.failed == 0 and report.cancelled == 0:
        raise typer.Exit(code=2)
    if not ok:
        raise typer.Exit(code=1)


def _write_schema_file(dest: Path, text: str, *, force: bool) -> Path:
    from readyagents.config import get_settings

    if dest.exists() and dest.is_dir():
        raise ConfigError(f"Refusing to write schema to directory: {dest}")
    root = get_settings().workspace_path()
    resolved = confine_under(dest, root, what="schema output")
    if resolved.is_dir():
        raise ConfigError(f"Refusing to write schema to directory: {dest}")
    if (resolved.exists() or dest.is_symlink()) and not force:
        raise ConfigError(f"Refusing to overwrite existing file: {dest}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    return resolved


def _check_schema_file(path: Path, generated: str) -> None:
    if path.is_dir():
        raise ConfigError(f"schema --check path is a directory: {path}")
    if not path.is_file():
        raise ConfigError(f"schema file not found: {path}")
    existing = path.read_text(encoding="utf-8")
    if existing == generated:
        return
    import difflib

    diff = difflib.unified_diff(
        existing.splitlines(),
        generated.splitlines(),
        fromfile=str(path),
        tofile="generated",
        lineterm="",
    )
    preview = "\n".join(list(diff)[:80])
    raise ConfigError(f"schema drift: {path} does not match generated output\n{preview}")


def _emit_import_error(command: str, extra: ImportRefused, *, as_json: bool) -> NoReturn:
    if as_json:
        _print_json(
            _json_envelope(
                command,
                ok=False,
                error=type(extra).__name__,
                message=str(extra),
                reason=extra.reason,
            )
        )
        raise typer.Exit(code=1) from extra
    _fail(extra)


def _emit_registry_error(command: str, extra: RegistryRefused, *, as_json: bool) -> NoReturn:
    if as_json:
        _print_json(
            _json_envelope(
                command,
                ok=False,
                error=type(extra).__name__,
                message=str(extra),
                reason=extra.reason,
            )
        )
        raise typer.Exit(code=1) from extra
    _fail(extra)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
