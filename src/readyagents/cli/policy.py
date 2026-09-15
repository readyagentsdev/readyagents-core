"""CLI group: policy (split from cli.py; see H-05)."""

from pathlib import Path

import typer
from rich.table import Table

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import (
    ReadyAgentsError,
)
from readyagents.workflow.runner import (
    load_workflow,
)

policy_app = typer.Typer(help="Validate and explain firewall policy.", no_args_is_help=True)


@policy_app.command("check")
def policy_check(
    path: Path = typer.Argument(..., help="Policy YAML file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Validate a firewall policy file (fail closed on errors)."""
    from readyagents.firewall.explain import check_policy

    try:
        policy = check_policy(path)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "policy check",
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
                "policy check",
                ok=True,
                default=policy.default,
                source=policy.source,
            )
        )
        return
    console.print(f"ok: {policy.source} (default={policy.default})")


@policy_app.command("explain")
def policy_explain(
    workflow: Path = _WORKFLOW_ARG,
    policy: Path | None = typer.Option(None, "--policy"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show which tools each node may call and why."""
    from readyagents.firewall.explain import explain_workflow
    from readyagents.firewall.policy_file import load_resolved

    try:
        spec = load_workflow(workflow)
        loaded = load_resolved(explicit=policy, workflow_dir=workflow.parent)
        rows = explain_workflow(spec, loaded)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "policy explain",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("policy explain", ok=True, nodes=rows))
        return
    if not rows:
        console.print("No tool nodes.")
        return
    table = Table(title="Policy explain")
    table.add_column("node")
    table.add_column("tool")
    table.add_column("action")
    table.add_column("rule")
    table.add_column("reason")
    for row in rows:
        table.add_row(row["node"], row["tool"], row["action"], row["rule"], row["reason"])
    console.print(table)
