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
from readyagents.cli.connectors import connectors_app
from readyagents.cli.delegations import delegations_app
from readyagents.cli.env import env_app
from readyagents.cli.health import health_app
from readyagents.cli.identity import identity_app, trust_app
from readyagents.cli.knowledge import knowledge_app
from readyagents.cli.mcp import mcp_app
from readyagents.cli.memory import memory_app
from readyagents.cli.models import models_app
from readyagents.cli.policy import policy_app
from readyagents.cli.prompts import prompts_app
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
package_app = typer.Typer(
    help="Build, install, and catalog workflow packages. No hosted registry.",
    no_args_is_help=True,
)
app.add_typer(package_app, name="package", rich_help_panel="Extras: Packaging and distribution")
app.add_typer(models_app, name="models", rich_help_panel="Extras: Model and prompt")
app.add_typer(distill_app, name="distill", rich_help_panel="Extras: Model and prompt")
app.add_typer(health_app, name="health", rich_help_panel="Extras: Operations")
bench_app = typer.Typer(
    help="Offline-by-default benchmark suite. Not a model-quality claim.",
    no_args_is_help=True,
)
app.add_typer(bench_app, name="bench", rich_help_panel="Extras: Model and prompt")
app.add_typer(prompts_app, name="prompts", rich_help_panel="Extras: Model and prompt")
feedback_app = typer.Typer(
    help="Export consented corrections. Production data in a portable file. No hosted dataset.",
    no_args_is_help=True,
)
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


@app.command(rich_help_panel="Core")
def version() -> None:
    """Print the version."""
    console.print(__version__)


@app.command("init", rich_help_panel="Core")
def init_cmd(
    dest: Path = typer.Option(Path(".env"), "--dest", help="Path to write the env file."),
) -> None:
    """Create a local .env."""
    example = Path(".env.example")
    if dest.exists():
        console.print(f"[yellow]{dest} already exists[/yellow] — left unchanged.")
        _print_next_steps()
        return
    if example.is_file():
        dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        dest.write_text(_ENV_TEMPLATE, encoding="utf-8")
    console.print(f"[green]Wrote {dest}[/green] — then the keyless smoke:")
    _print_next_steps()


def _print_next_steps() -> None:
    console.print(
        Panel(
            "[bold]Next steps[/bold]\n"
            "1. Diagnose (no keys):  [cyan]readyagents doctor[/cyan]\n"
            "2. Scaffold:  [cyan]readyagents new my-flow[/cyan]\n"
            "3. Smoke the scaffold:  [cyan]readyagents run my-flow/workflow.yaml[/cyan]\n"
            "4. Edit `.env` and set OPENAI_API_KEY and/or ANTHROPIC_API_KEY\n"
            "The PyPI wheel does not ship `examples/`. From a clone, "
            "`readyagents run examples/calc_pipeline.yaml` is the same keyless graph.\n"
            "See docs/first-ten-minutes.md",
            title="ReadyAgents",
        )
    )


@app.command("new", rich_help_panel="Core")
def new_cmd(
    name: str = typer.Argument("starter", help="Project / workflow name."),
    dest: Path | None = typer.Option(
        None,
        "--dest",
        help="Directory to write (defaults to ./<name>).",
    ),
    template: str = typer.Option(
        "pipeline",
        "--template",
        "-t",
        help=f"Starter kind: {', '.join(TEMPLATES)}.",
    ),
) -> None:
    """Scaffold a starter workflow."""
    target = dest if dest is not None else Path(name)
    try:
        written = create_project(target, name=name, template=template)
    except ReadyAgentsError as exc:
        _fail(exc)
        return
    console.print(f"[green]Created {target.resolve()}[/green]  template={template}")
    for path in written:
        console.print(f"  {path.name}")
    wf = target / "workflow.yaml"
    if template in {"basic", "pipeline", "foreach"}:
        console.print(f"Run: [cyan]readyagents run {wf}[/cyan]")
    elif template == "agent-tools":
        console.print(f"Run: [cyan]readyagents run {wf} --dry-run[/cyan]")
    elif template == "research":
        console.print(f"Run: [cyan]readyagents run {wf} --approve publish[/cyan]")
    else:
        console.print(f"Run: [cyan]readyagents run {wf} --approve gate[/cyan]")


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


@app.command(rich_help_panel="Core")
def validate(
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the workflow summary as JSON on stdout (no tables).",
    ),
) -> None:
    """Schema-validate a workflow."""
    try:
        workflow = load_workflow(path)
    except ReadyAgentsError as exc:
        if as_json:
            _print_json(
                _json_envelope(
                    "validate",
                    ok=False,
                    error=type(exc).__name__,
                    message=str(exc),
                    **_problems_fields(exc),
                )
            )
            raise typer.Exit(code=1) from exc
        _fail(exc)
    nodes = [
        {
            "id": node.id,
            "type": str(node.type),
            "next": node.next,
            "then": node.then,
            "else": node.else_,
        }
        for node in workflow.nodes
    ]
    if as_json:
        _print_json(
            _json_envelope(
                "validate",
                ok=True,
                name=workflow.name,
                start=workflow.start,
                node_count=len(workflow.nodes),
                nodes=nodes,
            )
        )
        return
    table = Table(title=f"Valid: {workflow.name}")
    table.add_column("Node")
    table.add_column("Type")
    table.add_column("Next")
    for node in workflow.nodes:
        table.add_row(node.id, str(node.type), _node_routing(node))
    console.print(table)
    console.print(f"[green]OK[/green] — {len(workflow.nodes)} node(s), start={workflow.start}")


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


@app.command("doctor", rich_help_panel="Core")
def doctor_cmd(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the diagnostic envelope as JSON (no tables).",
    ),
) -> None:
    """Report platform, extras, workspace."""
    from readyagents.doctor import format_doctor, run_doctor

    report = run_doctor()
    if as_json:
        _print_json(report)
    else:
        console.print(format_doctor(report), markup=False)
    if not report.get("ok"):
        raise typer.Exit(code=1)


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


@app.command("eval", rich_help_panel="Core")
def eval_cmd(
    path: Path = typer.Argument(
        ...,
        help="Eval suite YAML or JSON file.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the eval report as JSON on stdout (no tables).",
    ),
) -> None:
    """Score a keyless fixture suite."""
    try:
        cases = load_eval_suite(path)
        report = run_eval(cases)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "eval",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if as_json:
        _print_json(
            _json_envelope(
                "eval",
                ok=report.ok,
                passed=report.passed,
                failed=report.failed,
                results=[
                    {"name": row.name, "passed": row.passed, "reason": row.reason}
                    for row in report.results
                ],
            )
        )
    else:
        for row in report.results:
            if row.passed:
                console.print(f"[green]PASS[/green] {escape(row.name)}")
            else:
                console.print(f"[red]FAIL[/red] {escape(row.name)}: {escape(row.reason)}")
        console.print(f"passed={report.passed} failed={report.failed}")
    if not report.ok:
        raise typer.Exit(code=1)


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


@feedback_app.command("export")
def feedback_export_cmd(
    fmt: str = typer.Option("eval", "--format", help="eval (default), sft, or dpo."),
    out: Path = typer.Option(..., "--out", help="Destination file under the workspace."),
    scope: str | None = typer.Option(None, "--scope", help="Recorded consent scope to include."),
    node: str | None = typer.Option(None, "--node"),
    since: str | None = typer.Option(None, "--since"),
    min_rating: int | None = typer.Option(None, "--min-rating"),
    yes: bool = typer.Option(False, "--yes", help="Acknowledge the production-data warning."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Export consented corrections. Unconsented runs are excluded from every format."""
    from readyagents.audit import audit_dir_for, make_auditor
    from readyagents.config import get_settings
    from readyagents.errors import FeedbackRefused
    from readyagents.feedback.export import export_feedback
    from readyagents.policy import Redactor
    from readyagents.replay.record import known_secret_values

    settings = get_settings()
    console.print("Warning: an export is production data in a portable file.")
    if not yes:
        typer.confirm("Write the export?", abort=True)
    try:
        report = export_feedback(
            settings=settings,
            dest=out,
            fmt=fmt,
            scope=scope,
            node=node,
            since=since,
            min_rating=min_rating,
            yes=yes,
            secrets=known_secret_values(settings),
            redactor=Redactor(literals=known_secret_values(settings)),
            auditor=make_auditor(audit_dir_for(settings.home_path())),
        )
    except FeedbackRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback export",
                    ok=False,
                    error="FeedbackRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback export",
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
        _print_json(_json_envelope("feedback export", ok=ok, **body))
        return
    console.print(
        f"export format={report.format} written={report.written} excluded={report.excluded}"
    )


@feedback_app.command("stats")
def feedback_stats_cmd(
    by: str = typer.Option("node", "--by", help="node, model, label, or week."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Correction rates with sample sizes. Does not imply statistical significance."""
    from readyagents.config import get_settings
    from readyagents.errors import FeedbackRefused
    from readyagents.feedback.stats import feedback_stats

    try:
        report = feedback_stats(settings=get_settings(), by=by)
    except FeedbackRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback stats",
                    ok=False,
                    error="FeedbackRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback stats", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    ok = bool(body.pop("ok", True))
    if as_json:
        _print_json(_json_envelope("feedback stats", ok=ok, **body))
        return
    console.print(f"stats by={report.by} n={report.sample_size} (no significance claim)")
    for row in report.rows:
        console.print(
            f"{row.get(report.by)} n={row['n']} rate={row['correction_rate']} significance=None"
        )


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


@app.command(rich_help_panel="Core")
def run(
    path: Path = _WORKFLOW_ARG,
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="Input as KEY=VALUE (repeatable).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Walk the graph without calling an LLM, http_get, or write_file.",
    ),
    no_persist: bool = typer.Option(False, "--no-persist", help="Do not write a run record."),
    approve: list[str] = typer.Option(
        [],
        "--approve",
        help="Approve an approval node by id (repeatable).",
    ),
    reject: list[str] = typer.Option(
        [],
        "--reject",
        help="Reject an approval node by id (repeatable).",
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="Resume this run id instead of starting a new run.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the run record as JSON on stdout (no tables).",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="DEBUG, INFO, WARNING, or ERROR (same as the root flag).",
    ),
    log_format: str | None = typer.Option(
        None,
        "--log-format",
        help="text or json (same as the root flag).",
    ),
    decision_file: Path | None = typer.Option(
        None,
        "--decision-file",
        help="JSON file injecting approval decisions (not only --approve flags).",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Actor id for RBAC hooks (env: READYAGENTS_ACTOR).",
        envvar="READYAGENTS_ACTOR",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip the local LLM response cache for this run.",
    ),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    record: bool = typer.Option(
        False,
        "--record",
        help="Write a content-addressed cassette (prompts and completions). Opt-in.",
        envvar="READYAGENTS_RECORD",
    ),
    policy: Path | None = typer.Option(
        None,
        "--policy",
        help="Firewall policy file (env: READYAGENTS_POLICY).",
        envvar="READYAGENTS_POLICY",
    ),
    estimate: bool = typer.Option(
        False,
        "--estimate",
        help="Print a spend range and exit. No node execution, no network.",
    ),
    max_spend: float | None = typer.Option(
        None,
        "--max-spend",
        help="Hard USD cap consulted before each model call.",
    ),
    max_tokens: int | None = typer.Option(
        None,
        "--max-tokens",
        help="Hard token cap consulted before each model call.",
    ),
    label: list[str] = typer.Option(
        [],
        "--label",
        help="Attribution label KEY=VALUE (repeatable). Stored on the run and ledger.",
    ),
    override_budget: bool = typer.Option(
        False,
        "--override-budget",
        help="Start even when the preflight estimate exceeds a cap (audited).",
    ),
    max_model_calls: int | None = typer.Option(
        None,
        "--max-model-calls",
        help="Runaway guard: maximum LLM complete() attempts.",
    ),
    max_run_tool_rounds: int | None = typer.Option(
        None,
        "--max-run-tool-rounds",
        help="Runaway guard: maximum agent tool rounds across the run.",
    ),
    max_wall_seconds: float | None = typer.Option(
        None,
        "--max-wall-seconds",
        help="Runaway guard: maximum wall-clock seconds.",
    ),
    require_signed: bool = typer.Option(
        False,
        "--require-signed",
        help="Refuse unsigned or untrusted workflow and pack artifacts.",
    ),
    frozen: bool = typer.Option(
        False,
        "--frozen",
        help="Refuse to run when readyagents.lock digests do not match.",
    ),
    sovereign: bool = typer.Option(
        False,
        "--sovereign",
        help="Refuse non-loopback egress at the socket boundary (env: READYAGENTS_SOVEREIGN).",
        envvar="READYAGENTS_SOVEREIGN",
    ),
    sovereign_allow: list[str] = typer.Option(
        [],
        "--sovereign-allow",
        help="Private endpoint allowlisted under --sovereign (repeatable).",
    ),
    stream_flag: bool = typer.Option(
        False,
        "--stream",
        help="Emit incremental run events (tokens, partials, node start/finish).",
    ),
    edit: str | None = typer.Option(None, "--edit", help="Edited output for a feedback gate."),
    rating: int | None = typer.Option(None, "--rating", help="Declared rating on a feedback gate."),
    feedback_label: str | None = typer.Option(
        None, "--feedback-label", help="Declared taxonomy label on a feedback gate."
    ),
    env: str | None = typer.Option(
        None,
        "--env",
        help="Run the pinned release for this declared environment, not the working copy.",
    ),
) -> None:
    """Execute a workflow."""
    if log_level or log_format:
        configure_logging(log_level or "INFO", **({"fmt": log_format} if log_format else {}))
    persist = not no_persist
    try:
        parsed = parse_input_pairs(inputs)
        decisions = build_decisions(approve, reject)
        pack_specs = collect_pack_specs(pack)
        from readyagents.cost.ledger import parse_labels

        labels = parse_labels(label) if label else None
        feedback = _feedback_payload(list(decisions), edit, rating, feedback_label)
        if env and resume:
            raise EnvRefused(
                "resume a paused run with readyagents resume; --env starts from the pin",
                reason="resume",
            )
        if estimate:
            if env:
                from readyagents.env.run import pinned_workflow_for

                path = pinned_workflow_for(env)
            _emit_estimate(path, inputs=parsed, as_json=as_json)
            return
        session = None
        if stream_flag:
            from readyagents.workflow.stream import StreamSession

            def _write_stream(line: str) -> None:
                if as_json:
                    typer.echo(line, nl=False)
                else:
                    err_console.print(line.rstrip("\n"), markup=False)

            session = StreamSession(ndjson=as_json, write=_write_stream)
        if env:
            from readyagents.env.run import run_in_environment

            state = run_in_environment(
                path,
                env,
                inputs=parsed,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                record=record,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
        elif resume:
            state = resume_run(
                resume,
                path=path,
                inputs=parsed or None,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
        else:
            state = run_workflow_file(
                path,
                inputs=parsed,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                record=record,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
    except KeyboardInterrupt:
        if stream_flag and as_json:
            typer.echo(
                json.dumps({"event": "run.cancelled", "status": "cancelled"}) + "\n",
                nl=False,
            )
            raise typer.Exit(code=1) from None
        if as_json:
            _print_json(_json_envelope("run", ok=False, error="cancelled", status="cancelled"))
        else:
            err_console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1) from None
    except EnvRefused as extra:
        _emit_env_error("run", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if stream_flag and as_json:
            payload = {
                "event": "run.finished",
                "status": (
                    "paused"
                    if type(extra).__name__ in {"ApprovalRequired", "ConverseRequired"}
                    else "failed"
                ),
                "error": type(extra).__name__,
            }
            rid = getattr(extra, "run_id", None)
            if rid:
                payload["run_id"] = rid
            typer.echo(json.dumps(payload) + "\n", nl=False)
            if isinstance(extra, (ApprovalRequired, WaitingRequired, ConverseRequired)):
                raise typer.Exit(code=2) from extra
            raise typer.Exit(code=1) from extra
        _emit_run_exception(extra, as_json=as_json, persist=persist, command="run")
    if stream_flag and as_json:
        if state.status != "succeeded":
            raise typer.Exit(code=2 if state.status in {"paused", "waiting"} else 1)
        return
    _emit_run(state, as_json=as_json, command="run")


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


@app.command("resume", rich_help_panel="Core")
def resume_cmd(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    workflow: Path | None = typer.Option(
        None,
        "--workflow",
        help="Workflow file (defaults to the path stored on the run).",
    ),
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="Override stored inputs as KEY=VALUE (repeatable).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_persist: bool = typer.Option(False, "--no-persist"),
    approve: list[str] = typer.Option([], "--approve"),
    reject: list[str] = typer.Option([], "--reject"),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the run record as JSON on stdout (no tables).",
    ),
    decision_file: Path | None = typer.Option(
        None,
        "--decision-file",
        help="JSON file injecting approval decisions.",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        envvar="READYAGENTS_ACTOR",
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    policy: Path | None = typer.Option(
        None,
        "--policy",
        help="Firewall policy file (env: READYAGENTS_POLICY).",
        envvar="READYAGENTS_POLICY",
    ),
    max_spend: float | None = typer.Option(None, "--max-spend"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    label: list[str] = typer.Option([], "--label"),
    override_budget: bool = typer.Option(False, "--override-budget"),
    max_model_calls: int | None = typer.Option(None, "--max-model-calls"),
    max_run_tool_rounds: int | None = typer.Option(None, "--max-run-tool-rounds"),
    max_wall_seconds: float | None = typer.Option(None, "--max-wall-seconds"),
    require_signed: bool = typer.Option(
        False,
        "--require-signed",
        help="Refuse unsigned or untrusted workflow and pack artifacts.",
    ),
    frozen: bool = typer.Option(
        False,
        "--frozen",
        help="Refuse to run when readyagents.lock digests do not match.",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Reason captured with the vote."),
    edit: str | None = typer.Option(None, "--edit", help="Edited output for a feedback gate."),
    rating: int | None = typer.Option(None, "--rating", help="Declared rating on a feedback gate."),
    feedback_label: str | None = typer.Option(
        None, "--feedback-label", help="Declared taxonomy label on a feedback gate."
    ),
) -> None:
    """Resume a paused or failed run."""
    persist = not no_persist
    try:
        parsed = parse_input_pairs(inputs)
        from readyagents.cost.ledger import parse_labels

        labels = parse_labels(label) if label else None
        decisions = build_decisions(approve, reject)
        reason_nodes = dict(decisions)
        if decision_file is not None and reason:
            reason_nodes.update(load_decision_file(decision_file))
        state = resume_run(
            run_id,
            path=workflow,
            inputs=parsed or None,
            dry_run=dry_run,
            persist=persist,
            pack_specs=collect_pack_specs(pack),
            decisions=decisions,
            decision_file=decision_file,
            actor=actor,
            no_cache=no_cache,
            policy=policy,
            max_spend=max_spend,
            max_tokens_cap=max_tokens,
            labels=labels,
            override_budget=override_budget,
            max_model_calls=max_model_calls,
            max_run_tool_rounds=max_run_tool_rounds,
            max_wall_seconds=max_wall_seconds,
            require_signed=require_signed,
            frozen=frozen,
            vote_reasons={key: reason for key in reason_nodes} if reason and reason_nodes else None,
            feedback=_feedback_payload(list(decisions), edit, rating, feedback_label),
        )
    except KeyboardInterrupt:
        if as_json:
            _print_json(_json_envelope("resume", ok=False, error="cancelled", status="cancelled"))
        else:
            err_console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1) from None
    except ReadyAgentsError as extra:
        _emit_run_exception(extra, as_json=as_json, persist=persist, command="resume")
    _emit_run(state, as_json=as_json, command="resume")


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


@package_app.command("build")
def package_build(
    source: Path = typer.Argument(..., help="Directory containing readyagents.pkg.yaml."),
    out: Path | None = typer.Option(
        None, "--out", help="Archive path (default: NAME-VERSION.rapkg)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Build a deterministic .rapkg archive. Secret values are refused."""
    from readyagents.package.archive import build_package, digest_archive
    from readyagents.package.manifest import load_manifest

    try:
        dest = build_package(source, out=out)
        manifest = load_manifest(source)
        digest = digest_archive(dest)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package build",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(
            _json_envelope(
                "package build",
                ok=True,
                path=str(dest),
                name=manifest.name,
                version=manifest.version,
                digest=digest,
            )
        )
        return
    console.print(f"built {dest} digest={digest}")


@package_app.command("install")
def package_install(
    source: str = typer.Argument(..., help="Package archive path or URL."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Write after review. Default: show review and refuse."
    ),
    require_signature: bool = typer.Option(False, "--require-signature"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify, review capabilities, then install. Nothing executes. Default is no write."""
    from readyagents.config import get_settings
    from readyagents.errors import PackageNeedsConfirm
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.package.install import install_package
    from readyagents.trust.keyring import load_keyring

    settings = get_settings()
    policy = load_resolved(explicit=None, workflow_dir=settings.workspace_path(), stored=None)
    try:
        row = install_package(
            source,
            home=settings.home_path(),
            confirm=confirm,
            policy=policy,
            keyring=load_keyring(home=settings.home_path()),
            require_signature=require_signature,
        )
    except PackageNeedsConfirm as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package install",
                    ok=False,
                    error="PackageNeedsConfirm",
                    message=str(extra),
                    review=extra.review,
                )
            )
            raise typer.Exit(code=1) from extra
        _print_review(extra.review)
        console.print(str(extra))
        raise typer.Exit(code=1) from extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package install",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    review=getattr(extra, "review", None),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package install", ok=True, **row))
        return
    console.print(f"installed {row.get('name')}@{row.get('version')} digest={row.get('digest')}")


@package_app.command("list")
def package_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List installed packages: name, version, digest, signature."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import list_records

    rows = list_records(get_settings().home_path())
    if as_json:
        _print_json(_json_envelope("package list", ok=True, packages=rows))
        return
    if not rows:
        console.print("No packages installed.")
        return
    for row in rows:
        console.print(
            f"name: {row.get('name')}  version: {row.get('version')}  "
            f"digest: {row.get('digest')}  sig: {row.get('signature_status')}"
        )


@package_app.command("show")
def package_show(
    name: str = typer.Argument(..., help="Installed package name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one installed package record."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import get_record

    try:
        row = get_record(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package show", ok=True, package=row))
        return
    console.print(json.dumps(row, indent=2, ensure_ascii=False))


@package_app.command("remove")
def package_remove(
    name: str = typer.Argument(..., help="Installed package name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove an installed package from the local catalog."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import remove_name

    try:
        remove_name(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package remove", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package remove", ok=True, name=name))
        return
    console.print(f"removed {name}")


@package_app.command("upgrade")
def package_upgrade(
    name: str = typer.Argument(..., help="Installed package name."),
    source: str = typer.Argument(..., help="Replacement archive path or URL."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Write after review. Default: show review and refuse."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Upgrade a package. Overlay is preserved. Permission widening needs --confirm."""
    from readyagents.config import get_settings
    from readyagents.errors import PackageNeedsConfirm
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.package.catalog import upgrade_package
    from readyagents.trust.keyring import load_keyring

    settings = get_settings()
    policy = load_resolved(explicit=None, workflow_dir=settings.workspace_path(), stored=None)
    try:
        row = upgrade_package(
            name,
            source,
            home=settings.home_path(),
            confirm=confirm,
            policy=policy,
            keyring=load_keyring(home=settings.home_path()),
        )
    except PackageNeedsConfirm as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package upgrade",
                    ok=False,
                    error="PackageNeedsConfirm",
                    message=str(extra),
                    review=extra.review,
                )
            )
            raise typer.Exit(code=1) from extra
        _print_review(extra.review)
        console.print(str(extra))
        raise typer.Exit(code=1) from extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package upgrade",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    review=getattr(extra, "review", None),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package upgrade", ok=True, **row))
        return
    console.print(f"upgraded {row.get('name')}@{row.get('version')} digest={row.get('digest')}")


@package_app.command("index")
def package_index(
    path: Path = typer.Argument(..., help="Static JSON index (readyagents.index.json)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify a signed static package index. Unsigned indexes are refused."""
    from readyagents.config import get_settings
    from readyagents.package.index import verify_index
    from readyagents.trust.keyring import load_keyring

    try:
        payload = verify_index(path, keyring=load_keyring(home=get_settings().home_path()))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package index", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package index", ok=True, **payload))
        return
    count = len(payload.get("packages") or [])
    console.print(f"ok packages={count} digest={payload.get('digest')}")


def _print_review(review: dict | None) -> None:
    if not review:
        return
    console.print(
        "review "
        f"name={review.get('name')} version={review.get('version')} "
        f"sig={review.get('signature')} digest={review.get('digest')}"
    )
    console.print(f"tools: {review.get('tools')}")
    console.print(f"hosts: {review.get('hosts')}")
    console.print(f"secrets: {review.get('secrets')}")
    console.print(f"budget: {review.get('budget')}")
    console.print(f"approvals: {review.get('approvals')}")
    diff = review.get("policy_diff") or {}
    if review.get("constrained") or diff.get("extra_tools") or diff.get("extra_hosts"):
        extra_tools = diff.get("extra_tools")
        extra_hosts = diff.get("extra_hosts")
        console.print(f"constrained extra_tools={extra_tools} extra_hosts={extra_hosts}")
    upgrade = review.get("upgrade")
    if upgrade:
        widened = upgrade.get("widened")
        new_tools = upgrade.get("new_tools")
        console.print(f"upgrade widened={widened} new_tools={new_tools}")


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


@app.command("decide", rich_help_panel="Core")
def decide_cmd(
    run_id: str = typer.Argument(..., help="Paused run id (or unique prefix)."),
    decision_file: Path | None = typer.Option(
        None,
        "--file",
        "--decision-file",
        help='JSON payload: {"node": "approve"} or {"node_id", "decision"}.',
    ),
    node: str | None = typer.Option(None, "--node", help="Approval node id."),
    decision: str | None = typer.Option(
        None,
        "--decision",
        help="approve or reject (requires --node).",
    ),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="OIDC/JWT assertion to identify the approver (verified against local trust anchors).",
    ),
    trust_anchors: Path | None = typer.Option(
        None,
        "--trust-anchors",
        help="Trust-anchor YAML (env: READYAGENTS_TRUST_ANCHORS).",
        envvar="READYAGENTS_TRUST_ANCHORS",
    ),
    as_json: bool = typer.Option(False, "--json"),
    no_persist: bool = typer.Option(False, "--no-persist"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    reason: str | None = typer.Option(None, "--reason", help="Reason captured with the vote."),
    edit: str | None = typer.Option(None, "--edit", help="Edited output for a feedback gate."),
    rating: int | None = typer.Option(None, "--rating", help="Declared rating on a feedback gate."),
    feedback_label: str | None = typer.Option(
        None, "--feedback-label", help="Declared taxonomy label on a feedback gate."
    ),
) -> None:
    """Inject an approval decision.

    This is the core side of a webhook/pack: no always-on HTTP listener.
    A pack can receive the webhook and call this (or write --file).
    ``--actor NAME`` remains the default unconfigured path. ``--token-file``
    identifies the approver; it is separate from HMAC *signing* of the body.
    """
    from readyagents.config import get_settings
    from readyagents.errors import ConfigError

    persist = not no_persist
    try:
        decisions: dict[str, str] = {}
        if decision_file is not None:
            decisions.update(load_decision_file(decision_file))
        if node:
            if not decision:
                raise ConfigError("--decision is required with --node")
            decisions[node] = decision.strip().lower()
        if not decisions:
            raise ConfigError("Pass --file or --node plus --decision")
        verified = None
        resolved_actor = actor
        if token_file is not None:
            from readyagents.identity.decide import identify_approver, load_token_file
            from readyagents.run_store import open_run_store

            settings = get_settings()
            token = load_token_file(token_file)
            store = open_run_store(settings)
            try:
                paused = store.get(run_id, allow_prefix=True).state
            finally:
                closer = getattr(store, "close", None)
                if callable(closer):
                    closer()
            node_id = next(iter(decisions))
            verified = identify_approver(
                token,
                home=settings.home_path(),
                run_id=paused.run_id,
                node_id=node_id,
                decision=str(decisions[node_id]),
                trust_path=trust_anchors,
            )
            resolved_actor = verified.actor
        state = resume_run(
            run_id,
            persist=persist,
            pack_specs=collect_pack_specs(pack),
            decisions=decisions,
            actor=resolved_actor,
            verified_actor=verified,
            vote_reasons={key: reason for key in decisions} if reason else None,
            vote_signature_status="identified" if verified is not None else "unsigned",
            feedback=_feedback_payload(list(decisions), edit, rating, feedback_label),
        )
    except ReadyAgentsError as exc:
        _emit_run_exception(exc, as_json=as_json, persist=persist, command="decide")
    _emit_run(state, as_json=as_json, command="decide")


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


@app.command("spend", rich_help_panel="Core")
def spend_cmd(
    since: str | None = typer.Option(None, "--since", help="Include entries on/after YYYY-MM-DD."),
    by: str = typer.Option(
        "day",
        "--by",
        help="Aggregate by day, workflow, model, actor, or label.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """Aggregate the local spend ledger."""
    from readyagents.config import get_settings
    from readyagents.cost.ledger import query_spend

    try:
        agg = query_spend(get_settings().ledger_dir(), since=since, by=by)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "spend",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = agg.as_dict()
    if as_json:
        _print_json(_json_envelope("spend", ok=True, **payload))
        return
    total = payload["total"]
    if not payload["rows"]:
        console.print("spend: no ledger entries")
        return
    table = Table(title=f"Spend by {payload['by']}")
    table.add_column("key")
    table.add_column("runs")
    table.add_column("tokens")
    table.add_column("cost_usd")
    table.add_column("cache_savings_usd")
    for row in payload["rows"]:
        table.add_row(
            str(row["key"]),
            str(row["runs"]),
            str(row["total_tokens"]),
            f"{row['cost_usd']:.6f}",
            f"{row['cache_savings_micros'] / 1_000_000:.6f}",
        )
    console.print(table)
    console.print(
        f"total runs={total['runs']} tokens={total['total_tokens']} "
        f"cost_usd={total['cost_usd']:.6f} "
        f"cache_savings_usd={total['cache_savings_micros'] / 1_000_000:.6f}"
    )


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


def _node_routing(node: Any) -> str:
    """Compact then/else/next for the validate table (else was previously dropped)."""
    bits: list[str] = []
    if node.then:
        bits.append(f"then:{node.then}")
    if node.else_:
        bits.append(f"else:{node.else_}")
    if node.next:
        bits.append(node.next if not bits else f"next:{node.next}")
    return " ".join(bits)


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


def _emit_estimate(path: Path, *, inputs: dict[str, Any], as_json: bool) -> None:
    from readyagents.config import get_settings
    from readyagents.cost.estimate import estimate_workflow_file

    try:
        result = estimate_workflow_file(
            path,
            inputs=inputs or None,
            default_model=get_settings().default_model,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "run",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    estimate=True,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = result.as_dict()
    if as_json:
        _print_json(_json_envelope("run", ok=True, **payload))
        return
    floor_usd = payload["floor_cost_usd"]
    ceil_usd = payload["ceiling_cost_usd"]
    floor_s = "unpriced" if floor_usd is None else f"${floor_usd:.6f}"
    ceil_s = "unpriced" if ceil_usd is None else f"${ceil_usd:.6f}"
    console.print(
        f"estimate: {result.floor_tokens}–{result.ceiling_tokens} tokens  {floor_s}–{ceil_s}"
    )
    if result.unpriced:
        models = ", ".join(result.unpriced_models) or "unknown"
        console.print(f"unpriced models (not $0): {models}")
    console.print("assumptions:")
    for item in result.assumptions:
        console.print(f"  - {item}")
    raise typer.Exit(code=0)


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


@bench_app.command("run")
def bench_run_cmd(
    suite: Path | None = typer.Option(None, "--suite", help="Suite YAML (default examples/bench)."),
    offline: bool = typer.Option(
        False, "--offline", help="Cassette replay (default). Engine timing, not provider latency."
    ),
    live: bool = typer.Option(
        False, "--live", help="Opt-in live end-to-end. Metered. Off by default."
    ),
    allow_ci_live: bool = typer.Option(
        False, "--allow-ci-live", help="Required with --live when CI=true."
    ),
    scenarios: str | None = typer.Option(
        None, "--scenarios", help="Comma-separated scenario names."
    ),
    max_spend: float | None = typer.Option(None, "--max-spend", help="Live spend cap in USD."),
    model: str | None = typer.Option(None, "--model", help="Optional model label for comparison."),
    out: Path | None = typer.Option(None, "--out", help="Write the JSON result document."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the scenario suite offline (default) or live. Zero cost offline."""
    from readyagents.bench.run import run_bench
    from readyagents.config import get_settings
    from readyagents.errors import BenchRefused

    names = [part.strip() for part in (scenarios or "").split(",") if part.strip()]
    try:
        if live and offline:
            raise BenchRefused("use --offline or --live, not both", reason="mode")
        report = run_bench(
            suite,
            live=live,
            allow_ci_live=allow_ci_live,
            settings=get_settings(),
            scenarios=names or None,
            max_spend=max_spend,
            model=model,
        )
    except BenchRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench run",
                    ok=False,
                    error="BenchRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench run", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    if out is not None:
        out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if as_json:
        _print_json(_json_envelope("bench run", ok=True, **body))
        return
    console.print(report.markdown())


@bench_app.command("compare")
def bench_compare_cmd(
    current: Path | None = typer.Argument(None, help="Current results JSON from bench run."),
    baseline: Path | None = typer.Option(None, "--baseline", help="Committed baseline JSON."),
    tolerance: float | None = typer.Option(
        None, "--tolerance", help="Wall-clock percent tolerance (optional override)."
    ),
    models: str | None = typer.Option(None, "--models", help="Comma-separated model refs."),
    workflows: str | None = typer.Option(
        None, "--workflows", help="Comma-separated workflow paths."
    ),
    scenario: str | None = typer.Option(None, "--scenario", help="Scenario name for --models."),
    suite: Path | None = typer.Option(None, "--suite"),
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="Shared KEY=VALUE for --workflows (repeatable).",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare a result document to a baseline, or models/workflows like-for-like."""
    from readyagents.bench.compare import (
        compare_models,
        compare_results,
        compare_workflows,
        load_baseline,
    )
    from readyagents.config import get_settings
    from readyagents.errors import BenchRefused

    try:
        if models:
            names = [part.strip() for part in models.split(",") if part.strip()]
            payload = compare_models(
                names,
                suite=suite,
                scenario=scenario,
                settings=get_settings(),
            )
            ok = bool(payload.pop("ok"))
            if as_json:
                _print_json(_json_envelope("bench compare", ok=ok, **payload))
            else:
                console.print(f"models {', '.join(names)} on identical inputs ok={ok}")
            if not ok:
                raise typer.Exit(code=1)
            return
        if workflows:
            paths = [part.strip() for part in workflows.split(",") if part.strip()]
            payload = compare_workflows(
                paths,
                inputs=parse_input_pairs(inputs),
                settings=get_settings(),
            )
            ok = bool(payload.pop("ok"))
            if as_json:
                _print_json(_json_envelope("bench compare", ok=ok, **payload))
            else:
                console.print(f"workflows {', '.join(paths)} on identical inputs ok={ok}")
            if not ok:
                raise typer.Exit(code=1)
            return
        if current is None or baseline is None:
            raise BenchRefused("bench compare needs results.json and --baseline", reason="args")
        data = json.loads(Path(current).read_text(encoding="utf-8"))
        base = load_baseline(baseline)
        extra_tol = {"wall_ms_pct": tolerance} if tolerance is not None else None
        report = compare_results(data, base, tolerance=extra_tol)
    except BenchRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench compare",
                    ok=False,
                    error="BenchRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench compare", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload = report.as_dict()
        payload.pop("ok", None)
        _print_json(_json_envelope("bench compare", ok=report.ok, **payload))
    else:
        if report.ok:
            console.print("compare ok")
        else:
            for line in report.regressions:
                console.print(f"[red]{line}[/red]")
    if not report.ok:
        raise typer.Exit(code=1)


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


_ENV_TEMPLATE = """# ReadyAgents BYOK — fill in your keys. Never commit real keys.

READYAGENTS_DEFAULT_MODEL=openai:gpt-4o-mini
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
# READYAGENTS_ALLOW_HTTP=0
"""


def main() -> None:
    app()


if __name__ == "__main__":
    main()
