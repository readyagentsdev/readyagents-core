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
    IdentityError,
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
delegations_app = typer.Typer(help="Time-bounded approval delegations.", no_args_is_help=True)
policy_app = typer.Typer(help="Validate and explain firewall policy files.", no_args_is_help=True)
audit_app = typer.Typer(
    help="Inspect the append-only hash-chained audit trail.", no_args_is_help=True
)
identity_app = typer.Typer(
    help="Verify approver assertions and inspect workload identity.", no_args_is_help=True
)
identity_trust_app = typer.Typer(help="Manage local trust-anchor issuers.", no_args_is_help=True)
trust_app = typer.Typer(
    help="Manage the local publisher keyring for signed artifacts.",
    no_args_is_help=True,
)
app.add_typer(mcp_app, name="mcp")
app.add_typer(runs_app, name="runs")
app.add_typer(approvals_app, name="approvals")
app.add_typer(delegations_app, name="delegations")
app.add_typer(policy_app, name="policy")
app.add_typer(audit_app, name="audit")
app.add_typer(identity_app, name="identity")
app.add_typer(trust_app, name="trust")
identity_app.add_typer(identity_trust_app, name="trust")
connectors_app = typer.Typer(
    help="List, show, and test installed connectors (small catalog by design).",
    no_args_is_help=True,
)
app.add_typer(connectors_app, name="connectors")
a2a_app = typer.Typer(
    help="Serve a workflow as an A2A agent, print its card, or probe a remote card.",
    no_args_is_help=True,
)
app.add_typer(a2a_app, name="a2a")
memory_app = typer.Typer(
    help="Inspect, search, forget, and export the local memory store.",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")
models_app = typer.Typer(
    help="Catalog and dry-explain model routing. Never calls a provider.",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")

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


@app.command("attest")
def attest_cmd(
    run_id: str = typer.Argument(..., help="Persisted run id (or unique prefix)."),
    out: Path | None = typer.Option(None, "--out", help="Write JSON to this path."),
    as_json: bool = typer.Option(False, "--json"),
    sign: bool = typer.Option(False, "--sign", help="Detached Ed25519 beside the JSON."),
    key: Path | None = typer.Option(None, "--key", help="Ed25519 private key for --sign."),
) -> None:
    """Write a data-residency attestation. Technical evidence, not legal compliance."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.sovereign.attest import build_attestation, dump_attestation, sign_attestation

    try:
        settings = get_settings()
        store = open_run_store(settings)
        try:
            state = store.get(run_id, allow_prefix=True).state
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        payload = build_attestation(state, mcp_names=list(state.metadata.get("mcp_servers") or []))
        text = dump_attestation(payload)
        dest = out
        if dest is not None:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        if sign:
            if key is None:
                raise ConfigError("--sign requires --key")
            sig = sign_attestation(payload, key=key)
            sig_path = (dest or Path(f"attest-{state.run_id}.json")).with_suffix(".json.sig")
            sig_path.write_text(json.dumps(sig, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            payload = dict(payload)
            payload["signature_path"] = str(sig_path)
        if as_json:
            extra = dict(payload)
            if dest is not None:
                extra["path"] = str(dest)
            _print_json(_json_envelope("attest", ok=True, **extra))
            return
        if dest is not None:
            console.print(f"wrote {dest}")
        else:
            console.print(text, markup=False, end="")
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("attest", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


@app.command("bundle")
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
        if estimate:
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
        if resume:
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
    except ReadyAgentsError as extra:
        if stream_flag and as_json:
            payload = {
                "event": "run.finished",
                "status": "paused" if type(extra).__name__ == "ApprovalRequired" else "failed",
                "error": type(extra).__name__,
            }
            rid = getattr(extra, "run_id", None)
            if rid:
                payload["run_id"] = rid
            typer.echo(json.dumps(payload) + "\n", nl=False)
            if isinstance(extra, ApprovalRequired):
                raise typer.Exit(code=2) from extra
            raise typer.Exit(code=1) from extra
        _emit_run_exception(extra, as_json=as_json, persist=persist, command="run")
    if stream_flag and as_json:
        if state.status != "succeeded":
            raise typer.Exit(code=2 if state.status == "paused" else 1)
        return
    _emit_run(state, as_json=as_json, command="run")


@app.command("batch")
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
) -> None:
    """Resume a paused or failed run from the last successful node."""
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
) -> None:
    """Inject an external approval decision into a paused run, then resume.

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
        )
    except ReadyAgentsError as exc:
        _emit_run_exception(exc, as_json=as_json, persist=persist, command="decide")
    _emit_run(state, as_json=as_json, command="decide")


@a2a_app.command("serve")
def a2a_serve_cmd(
    path: Path = _WORKFLOW_ARG,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Loopback by default.",
        envvar="READYAGENTS_A2A_HOST",
    ),
    port: int = typer.Option(
        8770,
        "--port",
        min=1,
        max=65535,
        help="Bind port.",
        envvar="READYAGENTS_A2A_PORT",
    ),
    allow_public_bind: bool = typer.Option(
        False,
        "--allow-public-bind",
        help="Allow a non-loopback bind. Prints a warning. You own the exposure.",
    ),
    token_env: str = typer.Option(
        "READYAGENTS_A2A_TOKEN",
        "--token-env",
        help="Env var holding the bearer token. There is no token-value CLI flag.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print bind + card JSON, then serve."),
) -> None:
    """Expose one workflow as an A2A agent (loopback). Foreground; stops when it stops."""
    from readyagents.a2a.card import load_workflow_card
    from readyagents.a2a.server import serve_a2a

    try:
        card = load_workflow_card(path, url=f"http://{host}:{int(port)}")
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a serve",
                    ok=True,
                    host=host,
                    port=int(port),
                    url=card.get("url"),
                    card=card,
                )
            )
        serve_a2a(
            path,
            host=host,
            port=port,
            allow_public_bind=allow_public_bind,
            token_env=token_env,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a serve",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


@a2a_app.command("card")
def a2a_card_cmd(
    path: Path = _WORKFLOW_ARG,
    url: str = typer.Option(
        "http://127.0.0.1:8770",
        "--url",
        help="Canonical agent URL written into the card.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the card JSON to FILE."),
    as_json: bool = typer.Option(False, "--json", help="Print the card as a JSON envelope."),
) -> None:
    """Generate a deterministic Agent Card from a workflow. No network."""
    from readyagents.a2a.card import load_workflow_card

    try:
        card = load_workflow_card(path, url=url)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a card",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if out is not None:
        out.write_text(
            json.dumps(card, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if as_json:
        _print_json(_json_envelope("a2a card", ok=True, card=card))
        return
    if out is not None:
        console.print(f"wrote {out}")
        return
    _print_json(card)


@a2a_app.command("probe")
def a2a_probe_cmd(
    url: str = typer.Argument(..., help="Remote A2A agent origin or card URL."),
    as_json: bool = typer.Option(False, "--json", help="Print the probe envelope as JSON."),
) -> None:
    """Fetch and validate a remote Agent Card. Read-only. Never prints secret values."""
    import os

    from readyagents.a2a.card import card_digest, card_signature_status
    from readyagents.a2a.client import fetch_agent_card

    token = (os.environ.get("READYAGENTS_A2A_TOKEN") or "").strip() or None
    card_secret = (os.environ.get("READYAGENTS_A2A_CARD_SECRET") or "").strip() or None
    try:
        card = fetch_agent_card(url, token=token)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "a2a probe",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    url=url,
                )
            )
        else:
            err_console.print(f"[red]{type(extra).__name__}[/red]: {extra}")
        raise typer.Exit(code=1) from extra
    schemes = card.get("securitySchemes")
    redacted_schemes: dict[str, Any] = {}
    if isinstance(schemes, dict):
        for name, spec in schemes.items():
            if isinstance(spec, dict):
                redacted_schemes[str(name)] = {
                    "type": spec.get("type"),
                    "scheme": spec.get("scheme"),
                }
            else:
                redacted_schemes[str(name)] = {"type": None}
    extra = card.get("readyagents") if isinstance(card.get("readyagents"), dict) else {}
    caps = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
    report = {
        "name": card.get("name"),
        "url": card.get("url"),
        "version": card.get("version"),
        "protocolVersion": card.get("protocolVersion"),
        "capabilities": caps,
        "securitySchemes": redacted_schemes,
        "digest": card_digest(card),
        "signatureStatus": card_signature_status(card, secret=card_secret),
        "approvalCapable": extra.get("approvalCapable") if isinstance(extra, dict) else None,
        "streaming": bool(caps.get("streaming")) if isinstance(caps, dict) else False,
    }
    if as_json:
        _print_json(_json_envelope("a2a probe", ok=True, **report))
        return
    console.print(f"name: {report['name']}")
    console.print(f"url: {report['url']}")
    console.print(f"protocolVersion: {report['protocolVersion']}")
    console.print(f"approvalCapable: {report['approvalCapable']}")
    console.print(f"streaming: {report['streaming']}")
    console.print(f"digest: {report['digest']}")
    console.print(f"signatureStatus: {report['signatureStatus']}")


@memory_app.command("list")
def memory_list_cmd(
    scope: str | None = typer.Option(None, "--scope", help="Limit to one explicit scope."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List local memory records. Offline. No secret values beyond stored text."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        records = store.list(scope=scope)
        rows = [rec.as_dict() for rec in records]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("memory list", ok=True, records=rows, count=len(rows)))
        return
    if not rows:
        console.print("(no memory records)")
        return
    table = Table(title="Memory")
    table.add_column("id")
    table.add_column("scope")
    table.add_column("created")
    for row in rows:
        table.add_row(str(row["id"])[:12], str(row["scope"]), str(row.get("created_at") or ""))
    console.print(table)


@memory_app.command("show")
def memory_show_cmd(
    record_id: str = typer.Argument(..., help="Memory record id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one local memory record."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store

    settings = get_settings()
    store = open_memory_store(settings.home_path(), backend=settings.memory_store)
    try:
        rec = store.get(record_id).as_dict()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    if as_json:
        _print_json(_json_envelope("memory show", ok=True, record=rec))
        return
    _print_json(rec)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(..., help="Keyword query."),
    scope: str = typer.Option(..., "--scope", help="Explicit scope to search."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """BM25 search one local scope. Offline."""
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope

    settings = get_settings()
    try:
        validate_scope(scope)
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        hits = store.search(scope, query)
        rows = [{"score": hit.score, **hit.record.as_dict()} for hit in hits]
        store.close()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory search", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory search", ok=True, hits=rows, count=len(rows)))
        return
    if not rows:
        console.print("(no hits)")
        return
    for row in rows:
        console.print(f"{row['id'][:12]}  {row['scope']}  {row.get('text', '')[:80]}")


@memory_app.command("forget")
def memory_forget_cmd(
    scope: str | None = typer.Option(None, "--scope", help="Scope to erase."),
    subject: str | None = typer.Option(None, "--subject", help="Subject token to sweep."),
    record_id: str | None = typer.Option(None, "--id", help="Single record id."),
    yes: bool = typer.Option(False, "--yes", help="Required. Forgetting is irreversible."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Erase records, index entries, and vectors. Audited. Requires --yes."""
    from readyagents.audit import make_auditor
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import parse_scope, validate_scope

    if not yes:
        extra = ConfigError("memory forget requires --yes")
        if as_json:
            _print_json(
                _json_envelope("memory forget", ok=False, error="ConfigError", message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if not scope and not subject and not record_id:
        extra = ConfigError("memory forget requires --scope, --subject, or --id")
        if as_json:
            _print_json(
                _json_envelope("memory forget", ok=False, error="ConfigError", message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    settings = get_settings()
    try:
        if scope:
            validate_scope(scope)
        if subject:
            parse_scope(f"subject:{subject}")
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        removed = store.forget(scope=scope, record_id=record_id, subject=subject)
        store.close()
        auditor = make_auditor(settings.audit_dir())
        auditor(
            "memory_forget",
            scope=scope,
            subject=subject,
            record_id=record_id,
            removed=removed,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory forget", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory forget", ok=True, removed=removed))
        return
    console.print(f"removed {removed}")


@memory_app.command("export")
def memory_export_cmd(
    out: Path = typer.Option(..., "--out", help="Workspace-relative export path."),
    scope: str | None = typer.Option(None, "--scope", help="Limit to one scope."),
    yes: bool = typer.Option(False, "--yes", help="Confirm writing a sensitive file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write records as JSON. Confined, warned, audited. Vectors omitted."""
    from readyagents.audit import make_auditor
    from readyagents.config import get_settings
    from readyagents.memory.protocol import open_memory_store
    from readyagents.memory.scope import validate_scope
    from readyagents.workflow.runner import confine_under

    settings = get_settings()
    try:
        if scope:
            validate_scope(scope)
        dest = confine_under(out, settings.workspace_path(), what="memory export")
        if dest.exists() and not yes:
            raise ConfigError("memory export refuses to overwrite without --yes")
        if not yes:
            err_console.print(
                f"Warning: writing a sensitive memory export to {dest}. Pass --yes to confirm."
            )
            raise ConfigError("memory export requires --yes")
        store = open_memory_store(settings.home_path(), backend=settings.memory_store)
        records = [rec.as_dict() for rec in store.list(scope=scope)]
        store.close()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps({"records": records}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        auditor = make_auditor(settings.audit_dir())
        auditor("memory_export", path=str(dest), scope=scope, count=len(records))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "memory export", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("memory export", ok=True, path=str(dest), count=len(records)))
        return
    console.print(f"wrote {dest} ({len(records)} records)")


@connectors_app.command("list")
def connectors_list_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """List installed connectors (schemas, auth names, destinations). No secret values."""
    from readyagents.connectors.catalog import list_payload, redact_catalog

    try:
        payload = redact_catalog(list_payload())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors list", ok=True, **payload))
        return
    table = Table(title="Connectors")
    table.add_column("Name")
    table.add_column("Side effects")
    table.add_column("Auth")
    table.add_column("Destinations")
    for row in payload["connectors"]:
        table.add_row(
            str(row["name"]),
            str(row["side_effects"]),
            str(row.get("auth") or "none"),
            ", ".join(row.get("destinations") or ()) or "—",
        )
    console.print(table)


@connectors_app.command("show")
def connectors_show_cmd(
    name: str = typer.Argument(..., help="Connector name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one connector's declaration. Secret names only, never values."""
    from readyagents.connectors.catalog import redact_catalog, show_payload

    try:
        payload = redact_catalog(show_payload(name))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors show", ok=True, **payload))
        return
    console.print(f"[bold]{payload['name']}[/bold] {payload.get('version')}")
    console.print(payload.get("description") or "")
    console.print(f"destinations: {', '.join(payload.get('destinations') or ())}")
    auth = payload.get("auth")
    kind = auth.get("kind") if isinstance(auth, dict) else auth
    console.print(f"auth: {kind}")
    console.print(f"idempotent: {payload.get('idempotent')} key={payload.get('idempotency_key')}")
    console.print(
        f"side_effects: {payload.get('side_effects')} determinism: {payload.get('determinism')}"
    )


@connectors_app.command("test")
def connectors_test_cmd(
    name: str = typer.Argument(..., help="Connector name."),
    fixtures: Path | None = typer.Option(None, "--fixtures", help="Offline fixture directory."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the conformance harness offline. Exit 0 if the connector passes."""
    from readyagents.config import get_settings
    from readyagents.connectors.catalog import test_payload

    try:
        payload = test_payload(name, fixtures=fixtures, workspace=get_settings().workspace_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "connectors test", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        err_console.print(f"[red]{extra}[/red]")
        raise typer.Exit(code=1) from extra
    if as_json:
        _print_json(_json_envelope("connectors test", ok=payload["ok"], **payload))
    else:
        if payload["ok"]:
            console.print(f"[green]{name} passed conformance[/green]")
        else:
            err_console.print(f"[red]{name} failed conformance[/red]")
            for row in payload["failures"]:
                err_console.print(f"  {row['check']}: {row['message']}")
    if not payload["ok"]:
        raise typer.Exit(code=1)


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
            pack_specs=collect_pack_specs(pack),
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
            pack_specs=collect_pack_specs(pack),
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
    from readyagents.policy import Redactor
    from readyagents.replay.diff import diff_runs
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        left = store.get(run_a, allow_prefix=True).state
        right = store.get(run_b, allow_prefix=True).state
        # Diff output is a terminal/CI artifact: always apply default secret
        # patterns, not only when READYAGENTS_REDACT is on.
        redactor = Redactor(
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
    override_retention: bool = typer.Option(
        False,
        "--override-retention",
        help="Delete records inside the retention window. This override is audited.",
    ),
) -> None:
    """Delete old succeeded/failed/cancelled runs. Paused runs are kept unless forced."""
    from readyagents.audit import audit_dir_for, make_auditor
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store

    settings = get_settings()
    if not yes:
        console.print("Pass --yes to garbage-collect matching run files.")
        raise typer.Exit(code=1)
    store = open_run_store(settings)
    try:
        try:
            deleted = store.gc(
                statuses=status,
                include_paused=include_paused,
                keep=keep,
                min_age_seconds=None if override_retention else settings.retention_seconds(),
                override_retention=override_retention,
            )
        except ReadyAgentsError as extra:
            _fail(extra)
            return
    finally:
        store.close()
    if override_retention:
        make_auditor(audit_dir_for(settings.home_path()))(
            "gc_override",
            run_id="gc",
            deleted=len(deleted),
            actor=settings.actor,
        )
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


@approvals_app.command("list")
def approvals_list_cmd(
    role: str | None = typer.Option(None, "--role", help="Filter to this approver role."),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    expiring_within: str | None = typer.Option(
        None, "--expiring-within", help="Duration like 1h; omit to list all pending."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List paused approval gates this caller may see. Unauthorized looks empty."""
    from readyagents.approvals.queue import list_approvals
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.run_store.base import RunQuery

    try:
        settings = get_settings()
        store = open_run_store(settings)
        try:
            found = [item.state for item in store.list(RunQuery(status="paused", limit=256))]
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        rows = list_approvals(
            found,
            actor=actor,
            role=role,
            expiring_within=expiring_within,
            home=settings.home_path(),
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "approvals list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("approvals list", ok=True, approvals=rows))
        return
    if not rows:
        console.print("no pending approvals")
        return
    for row in rows:
        console.print(
            f"{row['run_id']} node={row['node_id']} "
            f"votes={row['approvals_received']}/{row['approvals_required']} "
            f"expires={row['expires_at'] or '-'} status={row['status']}"
        )


@app.command("delegate")
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


@delegations_app.command("list")
def delegations_list_cmd(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List local delegations."""
    from readyagents.approvals.delegate import load_delegations
    from readyagents.config import get_settings

    try:
        rows = [item.as_dict() for item in load_delegations(home=get_settings().home_path())]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "delegations list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("delegations list", ok=True, delegations=rows))
        return
    if not rows:
        console.print("no delegations")
        return
    for row in rows:
        flag = " revoked" if row.get("revoked") else ""
        console.print(
            f"{row['id']} {row['from']} -> {row['to']} until={row['until']} "
            f"scope={row.get('scope') or '-'}{flag}"
        )


@delegations_app.command("revoke")
def delegations_revoke_cmd(
    delegation_id: str = typer.Argument(..., help="Delegation id."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Revoke a delegation. Later decisions from the delegate are refused."""
    from readyagents.approvals.delegate import revoke_delegation
    from readyagents.config import get_settings

    try:
        settings = get_settings()
        entry = revoke_delegation(delegation_id, home=settings.home_path())
        from readyagents.audit import audit_dir_for, make_auditor

        make_auditor(audit_dir_for(settings.home_path()))(
            "delegation_revoked",
            run_id="delegation",
            actor=entry.from_actor,
            delegated_from=entry.from_actor,
            delegated_to=entry.to_actor,
            delegation_id=entry.id,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "delegations revoke",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("delegations revoke", ok=True, **entry.as_dict()))
        return
    console.print(f"revoked {entry.id}")


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


@audit_app.command("verify")
def audit_verify_cmd(
    file: Path | None = typer.Option(None, "--file", help="One JSONL audit file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Walk the hash chain. Exit 0 if no break; unchained ranges are reported, not failed."""
    from readyagents.audit import verify_audit_dir, verify_audit_file
    from readyagents.config import get_settings

    try:
        if file is not None:
            reports = [verify_audit_file(file)]
        else:
            reports = verify_audit_dir(get_settings().audit_dir())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "audit verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    ok = all(item.ok for item in reports)
    payload = {
        "files": [item.as_dict() for item in reports],
        "total": sum(item.total for item in reports),
        "chained": sum(item.chained for item in reports),
        "unchained": sum(item.unchained for item in reports),
        "first_break": next(
            (item.first_break for item in reports if item.first_break is not None), None
        ),
        "first_break_reason": next(
            (item.first_break_reason for item in reports if item.first_break_reason), None
        ),
    }
    if as_json:
        if file is not None and reports:
            body = reports[0].as_dict()
            _print_json(_json_envelope("audit verify", **body))
        else:
            _print_json(_json_envelope("audit verify", ok=ok, **payload))
    else:
        status = "ok" if ok else "BREAK"
        console.print(
            f"audit verify {status}: files={len(reports)} chained={payload['chained']} "
            f"unchained={payload['unchained']}"
        )
        if payload["first_break_reason"]:
            err_console.print(f"[red]{payload['first_break_reason']}[/red]")
    if not ok:
        raise typer.Exit(code=1)


@app.command("evidence")
def evidence_cmd(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    out: Path | None = typer.Option(None, "--out", help="Output directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing pack."),
    sign: bool = typer.Option(False, "--sign", help="Detached HMAC of manifest.json."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write a local evidence pack. Evidence, not compliance or certification."""
    from readyagents.compliance.evidence import write_evidence_pack
    from readyagents.config import get_settings
    from readyagents.policy import Redactor
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        try:
            state = store.get(run_id, allow_prefix=True).state
        except ReadyAgentsError as extra:
            if as_json:
                _print_json(
                    _json_envelope(
                        "evidence",
                        ok=False,
                        error=type(extra).__name__,
                        message=str(extra),
                    )
                )
                raise typer.Exit(code=1) from extra
            _fail(extra)
            return
    finally:
        store.close()
    dest = out or Path(f"evidence-{state.run_id}")
    try:
        resolved = confine_under(dest, settings.workspace_path(), what="evidence pack")
        source = state.metadata.get("source")
        workflow = None
        workflow_text = ""
        if source:
            src_path = Path(str(source))
            if src_path.is_file():
                workflow_text = src_path.read_text(encoding="utf-8")
                workflow = load_workflow(src_path)
        redactor = Redactor(
            patterns=settings.redact_pattern_list(),
            literals=settings.redact_literal_list(),
        )
        secret = settings.decision_secret if sign else None
        if sign and not secret:
            raise ConfigError("READYAGENTS_DECISION_SECRET is required for --sign")
        pack = write_evidence_pack(
            resolved,
            state=state,
            workflow=workflow,
            workflow_text=workflow_text,
            audit_dir=settings.audit_dir(),
            redactor=redactor,
            force=force,
            sign_secret=secret,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "evidence",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    err_console.print(
        "[yellow]Warning:[/yellow] evidence packs may contain recorded model prompts and outputs."
    )
    if as_json:
        _print_json(_json_envelope("evidence", ok=True, run_id=state.run_id, path=str(pack)))
        return
    console.print(f"Wrote evidence pack {pack}")


@app.command("spend")
def spend_cmd(
    since: str | None = typer.Option(None, "--since", help="Include entries on/after YYYY-MM-DD."),
    by: str = typer.Option(
        "day",
        "--by",
        help="Aggregate by day, workflow, model, actor, or label.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """Aggregate the local spend ledger. No network."""
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


@models_app.command("list")
def models_list_cmd(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """List the shipped capability catalog. No provider calls, no API keys."""
    from readyagents.cost.prices import load_price_table
    from readyagents.llm.capabilities import load_capability_matrix

    try:
        matrix = load_capability_matrix()
        prices = load_price_table()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("models", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    rows = []
    for ref, caps in matrix.models.items():
        quote = prices.quote(ref)
        rows.append(
            {
                "model": ref,
                **caps.as_dict(),
                "priced": quote.priced,
            }
        )
    if as_json:
        _print_json(
            _json_envelope(
                "models",
                ok=True,
                stale=matrix.stale,
                updated_at=matrix.updated_at,
                models=rows,
            )
        )
        return
    table = Table(title="Model catalog")
    table.add_column("model")
    table.add_column("local")
    table.add_column("tools")
    table.add_column("structured")
    table.add_column("latency")
    table.add_column("quality")
    for row in rows:
        table.add_row(
            str(row["model"]),
            "yes" if row["local"] else "no",
            "yes" if row["tool_calling"] else "no",
            "yes" if row["structured_output"] else "no",
            str(row["latency_class"]),
            str(row["quality_class"]),
        )
    console.print(table)
    if matrix.stale:
        err_console.print(
            f"[yellow]Warning:[/yellow] capability matrix is older than "
            f"{matrix.warn_after_days} days (updated_at={matrix.updated_at})."
        )


@models_app.command("show")
def models_show_cmd(
    model: str = typer.Argument(..., help="provider:model ref."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
) -> None:
    """Show one model's declared capabilities and price quote. No provider call."""
    from readyagents.cost.prices import load_price_table
    from readyagents.llm.capabilities import load_capability_matrix, lookup_model

    try:
        matrix = load_capability_matrix()
        caps = lookup_model(model, matrix=matrix)
        quote = load_price_table().quote(model)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("models", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = {
        "model": model,
        "found": caps is not None,
        "capabilities": caps.as_dict() if caps is not None else None,
        "priced": quote.priced,
        "price_match": quote.match,
        "unpriced_reason": quote.unpriced_reason,
    }
    if as_json:
        _print_json(_json_envelope("models", ok=True, **payload))
        return
    if caps is None:
        console.print(f"model {model}: not in capability matrix")
        return
    console.print(f"model {model}")
    console.print(f"  match={caps.match} local={caps.local}")
    console.print(
        f"  context_window={caps.context_window} tool_calling={caps.tool_calling} "
        f"structured_output={caps.structured_output} media={caps.media} streaming={caps.streaming}"
    )
    console.print(f"  latency_class={caps.latency_class} quality_class={caps.quality_class}")
    if quote.priced and quote.rate is not None:
        console.print(f"  price input={quote.rate.input} output={quote.rate.output} /MTok")
    else:
        console.print(f"  unpriced ({quote.unpriced_reason})")


@models_app.command("route")
def models_route_cmd(
    workflow: Path = _WORKFLOW_ARG,
    node_id: str = typer.Option(..., "--node", help="Node id to explain."),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="Print the matching rule, strategy, and candidate chain. No execution.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text."),
) -> None:
    """Dry-explain which model a node would use. Never calls a provider."""
    from readyagents.routing.select import explain_route
    from readyagents.workflow.runner import load_workflow

    try:
        spec = load_workflow(workflow)
        node = spec.node_map().get(node_id)
        if node is None:
            raise ConfigError(f"Node '{node_id}' is not in workflow '{spec.name}'")
        from readyagents.llm.resilience import model_candidates

        primary = node.model or spec.default_model
        legacy = model_candidates(primary, node.fallback_models, spec.fallback_models)
        decision = explain_route(
            spec,
            node,
            primary=primary,
            legacy_candidates=legacy,
            tools=["_"] if node.tools else None,
            structured=bool(node.output_schema),
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("models", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = decision.as_dict()
    payload["node"] = node_id
    payload["workflow"] = spec.name
    payload["explain"] = bool(explain)
    if as_json:
        _print_json(_json_envelope("models", ok=True, **payload))
        return
    console.print(f"node {node_id} -> {decision.model}")
    console.print(f"  reason={decision.reason}")
    if decision.strategy:
        console.print(f"  strategy={decision.strategy} rule_index={decision.rule_index}")
    if decision.pin:
        console.print(f"  pin={decision.pin}")
    console.print(f"  taint={decision.taint} local_only={decision.local_only}")
    if decision.candidates:
        console.print(f"  candidates={', '.join(decision.candidates)}")
    if decision.skipped:
        console.print(f"  skipped={', '.join(decision.skipped)}")
    if explain:
        console.print("  explain=true")


@app.command("studio")
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


@app.command("graph")
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


@identity_app.command("verify")
def identity_verify_cmd(
    token_file: Path = typer.Option(..., "--token-file", help="JWT/OIDC token file."),
    trust_anchors: Path | None = typer.Option(
        None,
        "--trust-anchors",
        help="Trust-anchor YAML.",
        envvar="READYAGENTS_TRUST_ANCHORS",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify an assertion against local trust anchors. Does not resume a run."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path
    from readyagents.identity.decide import load_token_file
    from readyagents.identity.verify import verify_token

    try:
        settings = get_settings()
        resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
        if resolved is None:
            raise IdentityError("no trust-anchor file configured")
        anchors = load_trust_anchors(resolved)
        actor = verify_token(load_token_file(token_file), anchors, base=resolved.parent)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = actor.as_dict()
    if as_json:
        _print_json(_json_envelope("identity verify", ok=True, **payload))
        return
    console.print(f"ok subject={actor.subject} issuer={actor.issuer} actor={actor.actor}")


@identity_app.command("whoami")
def identity_whoami_cmd(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print the configured workload identity fingerprint. Never the private key."""
    from readyagents.identity.workload import whoami

    try:
        info = whoami()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity whoami",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("identity whoami", ok=True, **info))
        return
    if not info.get("configured"):
        console.print("workload identity is not configured")
        return
    console.print(
        f"subject={info['subject']} key_id={info['key_id']} fingerprint={info['fingerprint']}"
    )


@identity_trust_app.command("list")
def identity_trust_list(
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List configured issuers."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path

    try:
        settings = get_settings()
        resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
        if resolved is None:
            rows: list[dict[str, Any]] = []
        else:
            anchors = load_trust_anchors(resolved)
            rows = [
                {"issuer": item.issuer, "audience": item.audiences(), "jwks_file": item.jwks_file}
                for item in anchors.issuers
            ]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity trust list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("identity trust list", ok=True, issuers=rows))
        return
    if not rows:
        console.print("no trust anchors")
        return
    for row in rows:
        console.print(f"{row['issuer']} aud={row['audience']} jwks={row['jwks_file']}")


@identity_trust_app.command("add")
def identity_trust_add(
    issuer: str = typer.Option(..., "--issuer"),
    jwks_file: Path = typer.Option(..., "--jwks-file"),
    audience: str = typer.Option("readyagents", "--audience"),
    actor_claim: str = typer.Option("sub", "--actor-claim"),
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
) -> None:
    """Append an issuer to the local trust-anchor file. Fail closed on malformed files."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import (
        IssuerAnchor,
        TrustAnchors,
        load_trust_anchors,
        resolve_trust_path,
    )

    settings = get_settings()
    dest = Path(trust_anchors) if trust_anchors is not None else settings.home_path() / "trust.yaml"
    existing = resolve_trust_path(
        explicit=dest if dest.is_file() else None, home=settings.home_path()
    )
    try:
        if existing is not None and existing.is_file():
            anchors = load_trust_anchors(existing)
        else:
            anchors = TrustAnchors()
        anchors.issuers.append(
            IssuerAnchor(
                issuer=issuer,
                audience=audience,
                jwks_file=str(jwks_file),
                actor_claim=actor_claim,
            )
        )
        import yaml

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.safe_dump(anchors.model_dump(exclude={"source"}), sort_keys=False),
            encoding="utf-8",
        )
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    console.print(f"added issuer {issuer} -> {dest}")


@identity_trust_app.command("remove")
def identity_trust_remove(
    issuer: str = typer.Option(..., "--issuer"),
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
) -> None:
    """Remove an issuer from the local trust-anchor file."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path

    settings = get_settings()
    resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
    if resolved is None:
        raise typer.Exit(code=1)
    try:
        anchors = load_trust_anchors(resolved)
        anchors.issuers = [item for item in anchors.issuers if item.issuer != issuer]
        if not anchors.issuers:
            raise IdentityError("refusing to write a trust-anchor file with no issuers")
        import yaml

        resolved.write_text(
            yaml.safe_dump(anchors.model_dump(exclude={"source"}), sort_keys=False),
            encoding="utf-8",
        )
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    console.print(f"removed issuer {issuer}")


@app.command("sign")
def sign_cmd(
    path: Path = typer.Argument(..., help="Workflow or pack path."),
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


@app.command("verify")
def verify_cmd(
    path: Path = typer.Argument(..., help="Workflow or pack path."),
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


@trust_app.command("add")
def trust_add_cmd(
    pubkey: Path = typer.Argument(..., help="Ed25519 public key (PEM, raw, hex, or base64)."),
    name: str = typer.Option(..., "--name", help="Human-meaningful publisher name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Trust a publisher public key. Fail closed on a malformed keyring."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import add_key

    try:
        entry = add_key(pubkey, name=name, home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust add",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("trust add", ok=True, **entry.as_dict()))
        return
    console.print(f"trusted {entry.name} key_id={entry.key_id}")


@trust_app.command("list")
def trust_list_cmd(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List trusted publisher keys."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import load_keyring

    try:
        ring = load_keyring(home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    rows = [item.as_dict() for item in ring.keys]
    if as_json:
        _print_json(_json_envelope("trust list", ok=True, keys=rows))
        return
    if not rows:
        console.print("no trusted publishers")
        return
    for row in rows:
        console.print(f"{row['key_id']} name={row['name']} added_at={row['added_at']}")


@trust_app.command("remove")
def trust_remove_cmd(
    key_id: str = typer.Argument(..., help="Key id from `readyagents trust list`."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove a trusted publisher key. Fail closed on a malformed keyring."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import remove_key

    try:
        entry = remove_key(key_id, home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust remove",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("trust remove", ok=True, **entry.as_dict()))
        return
    console.print(f"removed {entry.name} key_id={entry.key_id}")


@app.command("lock")
def lock_cmd(
    path: Path = _WORKFLOW_ARG,
    out: Path | None = typer.Option(
        None, "--out", help="Lockfile path (default: readyagents.lock beside the workflow)."
    ),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write readyagents.lock pinning workflow, include, pack, and MCP surface digests."""
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


@app.command("sbom")
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
    from readyagents.approvals.queue import fire_lazy_expiry

    state = fire_lazy_expiry(state, settings=get_settings())
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
    if getattr(state, "provenance", None):
        console.print(Panel(escape(_preview(state.provenance, limit=2000)), title="Provenance"))


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
    savings = state.usage.get("cache_savings_micros")
    if savings:
        parts.append(f"cache_savings_usd={savings / 1_000_000:.6f}")
    console.print("usage: " + " ".join(parts))
    spend = state.metadata.get("spend") if isinstance(state.metadata, dict) else None
    if isinstance(spend, dict) and spend.get("unpriced"):
        models = ", ".join(str(m) for m in spend.get("unpriced_models") or []) or "unknown"
        console.print(f"unpriced models (not $0): {models}")


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
