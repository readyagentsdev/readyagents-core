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
from readyagents.errors import (
    ApprovalRequired,
    CassetteMiss,
    ConfigError,
    MCPError,
    ReadyAgentsError,
)
from readyagents.logging import configure_logging
from readyagents.packs.loader import collect_pack_specs, discover_packs, load_local_packs
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
mcp_app = typer.Typer(help="Run ReadyAgents as an MCP server.", no_args_is_help=True)
runs_app = typer.Typer(help="Inspect persisted workflow runs.", no_args_is_help=True)
approvals_app = typer.Typer(
    help="Foreground localhost approval UI (not a hosted dashboard).",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")
app.add_typer(runs_app, name="runs")
app.add_typer(approvals_app, name="approvals")

console = Console()
err_console = Console(stderr=True)

_WORKFLOW_ARG = typer.Argument(
    ...,
    help="Workflow YAML or JSON file.",
)
_PACK_HELP = (
    "Local pack .py to load (repeatable). Confined to the workspace. Env: READYAGENTS_PACK."
)


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


@app.command()
def version() -> None:
    """Print the ReadyAgents version."""
    console.print(__version__)


@app.command("init")
def init_cmd(
    dest: Path = typer.Option(Path(".env"), "--dest", help="Path to write the env file."),
) -> None:
    """Create a local `.env` from `.env.example` if it does not exist."""
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
            "1. Smoke test (no keys):  "
            "[cyan]readyagents run examples/calc_pipeline.yaml[/cyan]\n"
            "2. Scaffold:  [cyan]readyagents new my-flow[/cyan]\n"
            "3. Edit `.env` and set OPENAI_API_KEY and/or ANTHROPIC_API_KEY\n"
            "4. With keys:  [cyan]readyagents run examples/research_brief.yaml "
            "--input topic=your-topic[/cyan]\n"
            "See docs/getting-started.md",
            title="ReadyAgents",
        )
    )


@app.command("new")
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
    """Write a starter workflow, README, and `.env.example`."""
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


@app.command()
def validate(
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the workflow summary as JSON on stdout (no tables).",
    ),
) -> None:
    """Schema-validate a workflow file without executing it."""
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


@app.command("schema")
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


@app.command("doctor")
def doctor_cmd(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the diagnostic envelope as JSON (no tables).",
    ),
) -> None:
    """Report platform, extras, workspace, permissions, loopback, and run-store. Read-only."""
    from readyagents.doctor import format_doctor, run_doctor

    report = run_doctor()
    if as_json:
        _print_json(report)
    else:
        console.print(format_doctor(report), markup=False)
    if not report.get("ok"):
        raise typer.Exit(code=1)


@app.command("eval")
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
    """Score fixture workflows from a suite file (no network, no API keys)."""
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


@app.command()
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
) -> None:
    """Execute a workflow."""
    if log_level or log_format:
        configure_logging(log_level or "INFO", **({"fmt": log_format} if log_format else {}))
    persist = not no_persist
    try:
        parsed = parse_input_pairs(inputs)
        decisions = build_decisions(approve, reject)
        extra_packs = _load_extra_packs(pack)
        if resume:
            state = resume_run(
                resume,
                path=path,
                inputs=parsed or None,
                dry_run=dry_run,
                persist=persist,
                extra_packs=extra_packs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
            )
        else:
            state = run_workflow_file(
                path,
                inputs=parsed,
                dry_run=dry_run,
                persist=persist,
                extra_packs=extra_packs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                record=record,
            )
    except KeyboardInterrupt:
        if as_json:
            _print_json(_json_envelope("run", ok=False, error="cancelled", status="cancelled"))
        else:
            err_console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1) from None
    except ReadyAgentsError as extra:
        _emit_run_exception(extra, as_json=as_json, persist=persist, command="run")
    _emit_run(state, as_json=as_json, command="run")


@app.command("resume")
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
) -> None:
    """Resume a paused or failed run from the last successful node."""
    persist = not no_persist
    try:
        parsed = parse_input_pairs(inputs)
        state = resume_run(
            run_id,
            path=workflow,
            inputs=parsed or None,
            dry_run=dry_run,
            persist=persist,
            extra_packs=_load_extra_packs(pack),
            decisions=build_decisions(approve, reject),
            decision_file=decision_file,
            actor=actor,
            no_cache=no_cache,
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


@app.command("decide")
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
    as_json: bool = typer.Option(False, "--json"),
    no_persist: bool = typer.Option(False, "--no-persist"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
) -> None:
    """Inject an external approval decision into a paused run, then resume.

    This is the core side of a webhook/pack: no always-on HTTP listener.
    A pack can receive the webhook and call this (or write --file).
    """
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
        state = resume_run(
            run_id,
            persist=persist,
            extra_packs=_load_extra_packs(pack),
            decisions=decisions,
            actor=actor,
        )
    except ReadyAgentsError as exc:
        _emit_run_exception(exc, as_json=as_json, persist=persist, command="decide")
    _emit_run(state, as_json=as_json, command="decide")


@app.command("packs")
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


@runs_app.command("list")
def runs_list(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    status: str | None = typer.Option(
        None, "--status", help="Filter: running, paused, failed, succeeded."
    ),
    workflow: str | None = typer.Option(None, "--workflow", help="Filter by workflow name."),
    limit: int = typer.Option(0, "--limit", help="Max rows (0 = all)."),
) -> None:
    """List persisted runs (newest first)."""
    from readyagents.config import get_settings
    from readyagents.run_store import RunQuery, open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        found = [
            item.state
            for item in store.list(RunQuery(status=status, workflow=workflow, limit=limit))
        ]
    finally:
        store.close()
    location = settings.runs_dir() if settings.run_store == "json" else settings.run_db_path()
    if as_json:
        payload = [
            {
                "run_id": s.run_id,
                "workflow": s.workflow_name,
                "status": s.status,
                "started_at": s.started_at,
                "pending_node": s.pending_node,
                "nodes": [r.node_id for r in s.results],
            }
            for s in found
        ]
        _print_json(payload)
        return
    if not found:
        console.print(f"No runs in {location}")
        return
    console.print(f"Runs in {location}")
    for state in found:
        nodes = ",".join(r.node_id for r in state.results) or "-"
        console.print(
            f"run_id: {state.run_id}  workflow: {state.workflow_name}  "
            f"status: {state.status}  started: {state.started_at}  nodes: {nodes}"
        )


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    as_json: bool = typer.Option(False, "--json", help="Print the stored run record as JSON."),
) -> None:
    """Show a run record and its node timeline."""
    _show_run(run_id, as_json=as_json)


@runs_app.command("inspect")
def runs_inspect(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    as_json: bool = typer.Option(False, "--json", help="Print the stored run record as JSON."),
) -> None:
    """Alias for `runs show` — inspect stored state and the node timeline."""
    _show_run(run_id, as_json=as_json)


@runs_app.command("report")
def runs_report(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    dest: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="HTML file to write (default: <run_id>.html in cwd).",
    ),
) -> None:
    """Write a local HTML summary of a persisted run."""
    from readyagents.config import get_settings
    from readyagents.report import write_html_report
    from readyagents.run_store import open_run_store

    try:
        store = open_run_store(get_settings())
        try:
            state = store.get(run_id, allow_prefix=True).state
        finally:
            store.close()
        path = dest or Path(f"{state.run_id}.html")
        written = write_html_report(state, path)
    except ReadyAgentsError as exc:
        _fail(exc)
        return
    console.print(f"[green]Wrote {written}[/green]  open it in a browser.")


@runs_app.command("replay")
def runs_replay(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_persist: bool = typer.Option(False, "--no-persist"),
    approve: list[str] = typer.Option([], "--approve"),
    reject: list[str] = typer.Option([], "--reject"),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the run record as JSON on stdout (no tables).",
    ),
    decision_file: Path | None = typer.Option(None, "--decision-file"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Replay from a cassette. No network, no API keys, no spend.",
    ),
) -> None:
    """Start a new run using the stored workflow path and inputs."""
    persist = not no_persist
    try:
        state = replay_run(
            run_id,
            dry_run=dry_run,
            persist=persist,
            extra_packs=_load_extra_packs(pack),
            decisions=build_decisions(approve, reject),
            decision_file=decision_file,
            actor=actor,
            no_cache=no_cache,
            offline=offline,
        )
    except ReadyAgentsError as exc:
        _emit_run_exception(exc, as_json=as_json, persist=persist, command="replay")
    extra_fields: dict[str, Any] = {}
    if offline:
        extra_fields["replayed_from"] = state.metadata.get("replayed_from")
        extra_fields["determinism"] = state.metadata.get("determinism") or {}
        extra_fields["replay"] = True
    _emit_run(state, as_json=as_json, command="replay", extra=extra_fields)


@runs_app.command("fork")
def runs_fork(
    run_id: str = typer.Argument(..., help="Parent run id (or unique prefix)."),
    from_node: str = typer.Option(..., "--from-node", help="Node id to fork after."),
    occurrence: int | None = typer.Option(None, "--occurrence", help="0-based occurrence."),
    sets: list[str] = typer.Option([], "--set", help="Override input KEY=VALUE (repeatable)."),
    offline: bool = typer.Option(False, "--offline"),
    as_json: bool = typer.Option(False, "--json"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
) -> None:
    """Branch a new run from a node checkpoint. Does not mutate the parent."""
    from readyagents.replay.fork import fork_run

    try:
        state = fork_run(
            run_id,
            from_node,
            occurrence=occurrence,
            overrides=parse_input_pairs(sets) if sets else None,
            extra_packs=_load_extra_packs(pack),
            actor=actor,
            offline=offline,
        )
    except ReadyAgentsError as extra:
        _emit_run_exception(extra, as_json=as_json, persist=True, command="fork")
    _emit_run(state, as_json=as_json, command="fork")


@runs_app.command("diff")
def runs_diff(
    run_a: str = typer.Argument(..., help="First run id."),
    run_b: str = typer.Argument(..., help="Second run id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two runs. Read-only."""
    from readyagents.config import get_settings
    from readyagents.policy import redactor_from_settings
    from readyagents.replay.diff import diff_runs
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        left = store.get(run_a, allow_prefix=True).state
        right = store.get(run_b, allow_prefix=True).state
        redactor = redactor_from_settings(
            enabled=bool(settings.redact),
            patterns=settings.redact_pattern_list(),
            literals=settings.redact_literal_list(),
        )
        report = diff_runs(left, right, redactor=redactor)
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    if as_json:
        _print_json(_json_envelope("diff", ok=True, **report))
        return
    first = report.get("first_divergence")
    if report.get("identical"):
        console.print("[green]identical[/green]")
        return
    console.print("first divergence:")
    console.print(escape(str(first)))
    if report.get("usage_delta"):
        console.print(f"usage delta: {report['usage_delta']}")


@runs_app.command("freeze")
def runs_freeze(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    out: Path = typer.Option(..., "--out", help="Directory to write the fixture into."),
    allow_unsealed: bool = typer.Option(False, "--allow-unsealed"),
    exact: bool = typer.Option(False, "--exact", help="Pin exact output equality."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Turn a recorded run into an offline eval fixture."""
    from readyagents.config import get_settings
    from readyagents.policy import redactor_from_settings
    from readyagents.replay.cassette import Cassette
    from readyagents.replay.freeze import FREEZE_WARNING, freeze_run
    from readyagents.replay.record import known_secret_values
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        state = store.get(run_id, allow_prefix=True).state
        cassette_path = state.metadata.get("cassette")
        if not cassette_path:
            fallback = settings.cassettes_dir() / f"{state.run_id}.json"
            cassette_path = str(fallback) if fallback.is_file() else None
        if not cassette_path:
            raise ConfigError(f"Run {state.run_id} has no cassette. Record one with --record.")
        cassette = Cassette.load(
            cassette_path,
            max_entry_bytes=settings.cassette_max_entry_bytes,
            max_bytes=settings.cassette_max_bytes,
        )
        redactor = redactor_from_settings(
            enabled=True,
            patterns=settings.redact_pattern_list(),
            literals=settings.redact_literal_list(),
        )
        dest = freeze_run(
            state,
            cassette,
            out_dir=out,
            workspace=settings.workspace_path(),
            redactor=redactor,
            secrets=known_secret_values(settings),
            allow_unsealed=allow_unsealed,
            exact=exact,
            settings=settings,
        )
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    finally:
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()
    err_console.print(f"[yellow]{FREEZE_WARNING}[/yellow]")
    if as_json:
        _print_json(
            _json_envelope(
                "freeze",
                ok=True,
                path=str(dest),
                run_id=state.run_id,
                warning=FREEZE_WARNING,
            )
        )
        return
    console.print(f"[green]Wrote {dest}[/green]")


@runs_app.command("delete")
def runs_delete(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt."),
) -> None:
    """Delete one persisted run."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        try:
            stored = store.get(run_id, allow_prefix=True)
            if not yes:
                console.print(
                    f"Delete run {stored.state.run_id} ({stored.state.status})? "
                    "Pass --yes to confirm."
                )
                raise typer.Exit(code=1)
            store.delete(run_id, allow_prefix=True)
        except ReadyAgentsError as extra:
            _fail(extra)
            return
    finally:
        store.close()
    console.print(f"[green]Deleted[/green] {run_id}")


@runs_app.command("migrate")
def runs_migrate(
    from_backend: str = typer.Option(
        "json",
        "--from",
        help="Source backend. v0.9 supports json only.",
    ),
    to_backend: str = typer.Option(
        "sqlite",
        "--to",
        help="Destination backend. v0.9 supports sqlite only.",
    ),
    source: Path | None = typer.Option(
        None,
        "--source",
        help="JSON runs directory (default: $READYAGENTS_HOME/runs).",
    ),
    database: Path | None = typer.Option(
        None,
        "--database",
        help="SQLite file (default: $READYAGENTS_HOME/runs.sqlite3).",
    ),
    on_conflict: str = typer.Option(
        "error",
        "--on-conflict",
        help="error (default) or skip-identical.",
    ),
    skip_invalid: bool = typer.Option(
        False,
        "--skip-invalid",
        help="Skip unreadable source JSON instead of aborting.",
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Re-read destination and compare canonical records (default: verify).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Plan and validate without writing the destination.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the migration envelope as JSON."),
) -> None:
    """Copy JSON run records into a local SQLite file. Never deletes source JSON."""
    from readyagents.config import get_settings
    from readyagents.run_store.migrate import migrate_json_to_sqlite

    from_backend = (from_backend or "").strip().lower()
    to_backend = (to_backend or "").strip().lower()
    policy = (on_conflict or "").strip().lower()
    try:
        if from_backend != "json" or to_backend != "sqlite":
            raise ConfigError("v0.9 migration supports --from json --to sqlite only.")
        if policy not in {"error", "skip-identical"}:
            raise ConfigError("--on-conflict must be error or skip-identical.")
        report = migrate_json_to_sqlite(
            source=source,
            database=database,
            settings=get_settings(),
            on_conflict=policy,  # type: ignore[arg-type]
            skip_invalid=skip_invalid,
            verify=verify,
            dry_run=dry_run,
        )
    except ReadyAgentsError as exc:
        if as_json:
            _print_json(
                _json_envelope(
                    "runs migrate",
                    ok=False,
                    error=type(exc).__name__,
                    message=str(exc),
                    scanned=0,
                    imported=0,
                    skipped=0,
                    conflicts=0,
                    invalid=0,
                    verified=False,
                )
            )
            raise typer.Exit(code=1) from exc
        _fail(exc)
        return
    if as_json:
        _print_json(report.as_envelope())
    else:
        status = "ok" if report.ok else "failed"
        console.print(
            f"runs migrate {status}: scanned={report.scanned} imported={report.imported} "
            f"skipped={report.skipped} conflicts={report.conflicts} invalid={report.invalid} "
            f"verified={report.verified} dry_run={report.dry_run}"
        )
        if not report.ok and report.message:
            err_console.print(f"[red]{report.error}:[/red] {report.message}")
    if not report.ok:
        raise typer.Exit(code=1)


@runs_app.command("gc")
def runs_gc_cmd(
    status: list[str] = typer.Option(
        ["succeeded", "failed", "cancelled"],
        "--status",
        help="Statuses to delete (repeatable).",
    ),
    keep: int = typer.Option(0, "--keep", help="Keep this many newest matching runs."),
    include_paused: bool = typer.Option(
        False,
        "--include-paused",
        help="Also delete paused runs (off by default).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt."),
) -> None:
    """Delete old succeeded/failed/cancelled runs. Paused runs are kept unless forced."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store

    settings = get_settings()
    if not yes:
        console.print("Pass --yes to garbage-collect matching run files.")
        raise typer.Exit(code=1)
    store = open_run_store(settings)
    try:
        try:
            deleted = store.gc(statuses=status, include_paused=include_paused, keep=keep)
        except ReadyAgentsError as extra:
            _fail(extra)
            return
    finally:
        store.close()
    console.print(f"[green]Deleted {len(deleted)} run(s)[/green]")
    for rid in deleted:
        console.print(f"  {rid}")


@approvals_app.command("serve")
def approvals_serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. v0.9 rejects non-loopback binds.",
    ),
    port: int = typer.Option(
        8766,
        "--port",
        min=1,
        max=65535,
        help="Bind port (default 8766).",
    ),
    token_env: str = typer.Option(
        "READYAGENTS_APPROVAL_UI_SECRET",
        "--token-env",
        help="Env var holding the UI HMAC secret. Never pass the secret as a flag.",
    ),
    session_ttl: int = typer.Option(
        1800,
        "--session-ttl",
        min=30,
        max=86400,
        help="Session cookie TTL in seconds (default 1800).",
    ),
    action_ttl: int = typer.Option(
        300,
        "--action-ttl",
        min=10,
        max=3600,
        help="One-use action token TTL in seconds (default 300).",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        envvar="READYAGENTS_ACTOR",
        help="Actor id for RBAC checks.",
    ),
    no_open: bool = typer.Option(
        True,
        "--no-open",
        help="Do not launch a browser (default). Print the bootstrap URL on stderr.",
    ),
) -> None:
    """Foreground localhost approval page. Stops when this process stops."""
    try:
        from readyagents.approvals.server import serve_approvals

        serve_approvals(
            host=host,
            port=port,
            token_env=token_env,
            session_ttl=float(session_ttl),
            action_ttl=float(action_ttl),
            actor=actor,
            no_open=no_open,
        )
    except ReadyAgentsError as exc:
        _fail(exc)


@mcp_app.command("serve")
def mcp_serve(
    ctx: typer.Context,
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="stdio (default) or streamable-http.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. HTTP only. v0.9 rejects non-loopback binds.",
        envvar="READYAGENTS_MCP_HTTP_HOST",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        min=1,
        max=65535,
        help="Bind port. HTTP only.",
        envvar="READYAGENTS_MCP_HTTP_PORT",
    ),
    auth: str = typer.Option(
        "token",
        "--auth",
        help="token (default) or none. none is loopback-only and warns.",
    ),
    token_env: str = typer.Option(
        DEFAULT_MCP_TOKEN_ENV,
        "--token-env",
        help="Env var holding the bearer token. HTTP only.",
    ),
    max_concurrent_runs: int = typer.Option(
        4,
        "--max-concurrent-runs",
        help="In-process executor cap. HTTP only.",
        envvar="READYAGENTS_MCP_MAX_CONCURRENT_RUNS",
        min=1,
    ),
    max_pending_runs: int = typer.Option(
        32,
        "--max-pending-runs",
        help="Pending-run queue cap. HTTP only.",
        envvar="READYAGENTS_MCP_MAX_PENDING_RUNS",
        min=1,
    ),
    approval_ui: bool = typer.Option(
        False,
        "--approval-ui",
        help="Mount the localhost approval UI on this HTTP process. HTTP only.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print protocol versions, extensions, and SDK pin as JSON, then serve.",
    ),
) -> None:
    """Expose builtin tools (and run_workflow) over MCP stdio or Streamable HTTP."""
    mode = (transport or "stdio").strip().lower()
    http_flags = (
        "--host",
        "--port",
        "--auth",
        "--token-env",
        "--max-concurrent-runs",
        "--max-pending-runs",
        "--approval-ui",
    )
    http_params = (
        "host",
        "port",
        "auth",
        "token_env",
        "max_concurrent_runs",
        "max_pending_runs",
        "approval_ui",
    )
    if mode == "stdio":
        from_argv = any(
            arg == flag or arg.startswith(f"{flag}=") for arg in sys.argv for flag in http_flags
        )
        from_cli = any(
            getattr(ctx.get_parameter_source(name), "name", None) == "COMMANDLINE"
            for name in http_params
        )
        passed = from_argv or from_cli
        if passed:
            _fail(
                MCPError(
                    "HTTP flags (--host, --port, --auth, --token-env, "
                    "--max-concurrent-runs, --max-pending-runs, --approval-ui) "
                    "are only valid with --transport streamable-http"
                )
            )
        if as_json:
            from readyagents.mcp.protocol import serve_json_envelope

            _print_json(serve_json_envelope(transport="stdio"))
        try:
            from readyagents.mcp.server import serve_stdio

            serve_stdio()
        except ReadyAgentsError as exc:
            _fail(exc)
        return
    if mode != "streamable-http":
        _fail(MCPError(f"Unknown --transport '{transport}'. Use stdio or streamable-http."))
    if as_json:
        from readyagents.mcp.protocol import serve_json_envelope

        _print_json(serve_json_envelope(transport="http"))
    try:
        from readyagents.mcp.http import serve_streamable_http

        serve_streamable_http(
            host=host,
            port=port,
            auth_mode=auth,
            token_env=token_env,
            max_concurrent_runs=max_concurrent_runs,
            max_pending_runs=max_pending_runs,
            approval_ui=approval_ui,
        )
    except ReadyAgentsError as err:
        _fail(err)


@mcp_app.command("probe")
def mcp_probe(
    url: str = typer.Argument(..., help="Remote MCP HTTP origin or /mcp URL."),
    as_json: bool = typer.Option(False, "--json", help="Print the probe envelope as JSON."),
) -> None:
    """Read-only diagnostic: call server/discover (then initialize). Never calls a tool."""
    from readyagents.mcp.probe import probe_server

    try:
        report = probe_server(url)
    except ReadyAgentsError as err:
        if as_json:
            _print_json(
                _json_envelope(
                    "mcp probe",
                    ok=False,
                    error=type(err).__name__,
                    message=str(err),
                    url=url,
                )
            )
        else:
            err_console.print(f"[red]{type(err).__name__}[/red]: {err}")
        raise typer.Exit(code=1) from err
    if as_json:
        _print_json(_json_envelope("mcp probe", ok=True, **report))
        return
    versions = ", ".join(report.get("protocol_versions") or []) or "(none)"
    extensions = ", ".join(report.get("extensions") or []) or "(none)"
    console.print(f"url: {report.get('url')}")
    console.print(f"protocol_versions: {versions}")
    console.print(f"extensions: {extensions}")
    console.print(f"negotiated: {report.get('negotiated')}")


def _show_run(run_id: str, *, as_json: bool = False) -> None:
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store

    store = open_run_store(get_settings())
    try:
        try:
            state = store.get(run_id, allow_prefix=True).state
        except ReadyAgentsError as exc:
            if as_json:
                _print_json(
                    _json_envelope(
                        "runs show",
                        ok=False,
                        error=type(exc).__name__,
                        message=str(exc),
                        run_id=run_id,
                    )
                )
                raise typer.Exit(code=1) from exc
            _fail(exc)
            return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("runs show", ok=True, **state.to_record()))
        return
    console.print(f"run_id: {state.run_id}")
    console.print(f"workflow: {state.workflow_name}")
    console.print(f"status: {state.status}")
    if state.pending_node:
        console.print(f"pending_node: {state.pending_node}")
    if state.pending:
        prompt = state.pending.get("prompt")
        if prompt:
            console.print(Panel(escape(str(prompt)), title="Pending prompt"))
        resume_hint = state.pending.get("resume")
        if resume_hint:
            console.print(f"Resume: [cyan]{escape(str(resume_hint))}[/cyan]")
    _print_usage(state)
    _print_run(state)
    if state.output_keys:
        console.print(Panel(escape(_preview(state.output_keys, limit=2000)), title="Outputs"))
    if state.inputs:
        console.print(Panel(escape(_preview(state.inputs, limit=2000)), title="Inputs"))


def _load_extra_packs(pack_flags: list[str]) -> list[Any]:
    """Load --pack / READYAGENTS_PACK modules confined to the workspace."""
    from readyagents.config import get_settings

    specs = collect_pack_specs(pack_flags)
    if not specs:
        return []
    return load_local_packs(specs, root=get_settings().workspace_path())


def _print_json(payload: object) -> None:
    """Write JSON to stdout without Rich markup (values may contain `[...]`)."""
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _json_envelope(command: str, *, ok: bool, **fields: Any) -> dict[str, Any]:
    """Additive JSON envelope: existing keys stay, ok/command always win."""
    payload = dict(fields)
    payload["ok"] = ok
    payload["command"] = command
    return payload


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


def _state_from_exc(exc: BaseException) -> RunState | None:
    state = getattr(exc, "state", None)
    return state if isinstance(state, RunState) else None


def _emit_run(
    state: RunState,
    *,
    as_json: bool,
    command: str = "run",
    extra: dict[str, Any] | None = None,
) -> None:
    if as_json:
        payload = dict(state.to_record())
        if extra:
            payload.update(extra)
        _print_json(
            _json_envelope(
                command,
                ok=state.status == "succeeded",
                **payload,
            )
        )
    else:
        _print_run(state)
        if state.status == "succeeded":
            console.print("[green]succeeded[/green]")
            console.print(f"run_id: {state.run_id}")
            _print_usage(state)
            if state.output_keys:
                console.print(
                    Panel(escape(_preview(state.output_keys, limit=2000)), title="Outputs")
                )
        else:
            console.print(f"[red]{state.status}[/red]")
            console.print(f"run_id: {state.run_id}")
    if state.status != "succeeded":
        raise typer.Exit(code=1)


def _emit_run_exception(
    exc: ReadyAgentsError, *, as_json: bool, persist: bool, command: str = "run"
) -> NoReturn:
    """Print a paused or failed run (JSON or tables) and exit. Never returns."""
    if isinstance(exc, ApprovalRequired):
        state = _state_from_exc(exc)
        if as_json:
            payload: dict[str, Any] = {
                "error": type(exc).__name__,
                "message": str(exc),
                "run_id": exc.run_id,
                "node_id": exc.node_id,
                "prompt": exc.prompt,
                "status": "paused",
            }
            if state is not None:
                payload["run"] = state.to_record()
            _print_json(_json_envelope(command, ok=False, **payload))
        else:
            _print_paused(exc)
        raise typer.Exit(code=2) from exc

    if isinstance(exc, CassetteMiss):
        if as_json:
            _print_json(
                _json_envelope(
                    command,
                    ok=False,
                    error="CassetteMiss",
                    message=str(exc),
                    node_id=exc.node_id,
                    reason=exc.reason,
                    nearest_key=exc.nearest_key,
                )
            )
        else:
            err_console.print(f"[red]CassetteMiss[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    state = _state_from_exc(exc)
    run_id = getattr(exc, "run_id", None) or (state.run_id if state is not None else None)
    if as_json:
        payload = {
            "error": type(exc).__name__,
            "message": str(exc),
            "run_id": run_id,
            "status": state.status if state is not None else "failed",
        }
        if state is not None:
            payload["run"] = state.to_record()
        payload.update(_problems_fields(exc))
        _print_json(_json_envelope(command, ok=False, **payload))
        raise typer.Exit(code=1) from exc

    if state is not None:
        _print_run(state)
        err_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        problems = getattr(exc, "problems", None)
        if problems:
            from readyagents.workflow.source_map import render_located_problems

            located = render_located_problems(list(problems))
            if located:
                err_console.print(located, markup=False)
        console.print(f"run_id: {state.run_id}  status: {state.status}")
        if persist:
            cmd = f"readyagents resume {state.run_id}"
            pending = state.pending_node
            if pending:
                console.print(
                    f"Resume: [cyan]{cmd}[/cyan]  (retry node [bold]{escape(pending)}[/bold])"
                )
            else:
                console.print(f"Resume: [cyan]{cmd}[/cyan]")
        raise typer.Exit(code=1) from exc

    _fail(exc)


def _print_usage(state: RunState) -> None:
    if not state.usage:
        return
    parts = [f"{k}={v}" for k, v in state.usage.items()]
    micros = state.usage.get("cost_micros")
    if micros:
        parts.append(f"cost_usd={micros / 1_000_000:.6f}")
    console.print("usage: " + " ".join(parts))


def _print_run(state: RunState) -> None:
    table = Table(title=f"Run {state.run_id} — {state.status}")
    table.add_column("Node")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Output", overflow="fold")
    for result in state.results:
        preview = result.error or _preview(result.output)
        if result.tool_rounds:
            names = ",".join(str(row.get("name") or "?") for row in result.tool_rounds)
            preview = f"{preview}  [tools:{names}]"
        table.add_row(result.node_id, result.type, result.status, escape(preview))
    console.print(table)


def _print_paused(exc: ApprovalRequired) -> None:
    if exc.state is not None and isinstance(exc.state, RunState):
        _print_run(exc.state)
    err_console.print(f"[yellow]{type(exc).__name__}:[/yellow] {exc}")
    if exc.prompt:
        console.print(Panel(escape(exc.prompt), title=f"Approval: {exc.node_id}"))
    console.print(f"Resume: [cyan]readyagents resume {exc.run_id} --approve {exc.node_id}[/cyan]")


def _preview(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _problems_fields(exc: BaseException) -> dict[str, Any]:
    problems = getattr(exc, "problems", None)
    if not problems:
        return {}
    from readyagents.workflow.source_map import problem_to_json

    return {"problems": [problem_to_json(item) for item in problems]}


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


def _fail(exc: BaseException) -> NoReturn:
    err_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
    problems = getattr(exc, "problems", None)
    if problems:
        from readyagents.workflow.source_map import render_located_problems

        located = render_located_problems(list(problems))
        if located:
            err_console.print(located, markup=False)
    raise typer.Exit(code=1) from exc


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
