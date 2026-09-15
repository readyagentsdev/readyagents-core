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
from readyagents.cli.models import models_app, register_optimize
from readyagents.cli.operations import (
    register_batch,
    register_event,
    register_promote,
    register_rollback,
    register_wake,
)
from readyagents.cli.package import package_app
from readyagents.cli.packaging import register_bundle, register_packs
from readyagents.cli.policy import policy_app
from readyagents.cli.prompts import prompts_app
from readyagents.cli.run import register_run
from readyagents.cli.runs import runs_app
from readyagents.cli.serve import serve_app
from readyagents.cli.sessions import sessions_app
from readyagents.cli.skills import register_agents_md, skills_app
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


register_bundle(app)


register_eval(app)


register_optimize(app)


register_promote(app)


register_rollback(app)


register_simulate(app)


register_run(app)


register_batch(app)


register_resume(app)


register_wake(app)


register_event(app)


register_agents_md(app)


register_decide(app)


register_packs(app)


register_delegate(app)


register_evidence(app)


register_spend(app)


register_studio(app)


register_graph(app)


register_sign(app)


register_verify(app)


register_lock(app)


register_sbom(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
