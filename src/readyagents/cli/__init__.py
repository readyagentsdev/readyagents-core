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
from readyagents.cli.authoring import (
    register_graph,
    register_import,
    register_schema,
    register_simulate,
    register_studio,
)
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
from readyagents.cli.governance import (
    register_delegate,
    register_lock,
    register_sbom,
    register_sign,
    register_verify,
)
from readyagents.cli.health import health_app
from readyagents.cli.identity import identity_app, trust_app
from readyagents.cli.knowledge import knowledge_app
from readyagents.cli.mcp import mcp_app
from readyagents.cli.memory import memory_app
from readyagents.cli.models import models_app
from readyagents.cli.operations import (
    register_batch,
    register_event,
    register_promote,
    register_rollback,
    register_wake,
)
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


register_import(app)


register_validate(app)


register_schema(app)


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


register_promote(app)


register_rollback(app)


register_simulate(app)


register_run(app)


register_batch(app)


register_resume(app)


register_wake(app)


register_event(app)


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


register_delegate(app)


register_evidence(app)


register_spend(app)


register_studio(app)


register_graph(app)


register_sign(app)


register_verify(app)


register_lock(app)


register_sbom(app)


def _load_extra_packs(pack_flags: list[str]) -> list[Any]:
    """Load --pack / READYAGENTS_PACK modules confined to the workspace."""
    from readyagents.config import get_settings

    specs = collect_pack_specs(pack_flags)
    if not specs:
        return []
    return load_local_packs(specs, root=get_settings().workspace_path())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
