"""CLI group: core (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from readyagents import __version__
from readyagents.cli._common import (
    _PACK_HELP,
    _WORKFLOW_ARG,
    _emit_run,
    _emit_run_exception,
    _fail,
    _feedback_payload,
    _json_envelope,
    _print_json,
    _problems_fields,
    console,
    err_console,
)
from readyagents.errors import ConfigError, ReadyAgentsError
from readyagents.examples import list_examples, materialize_example
from readyagents.packs.loader import collect_pack_specs
from readyagents.scaffold import (
    TEMPLATES,
    create_project,
)
from readyagents.testing.eval import (
    load_eval_suite,
    run_eval,
)
from readyagents.workflow.runner import (
    load_workflow,
    resume_run,
)
from readyagents.workflow.state import (
    build_decisions,
    load_decision_file,
    parse_input_pairs,
)

_ENV_TEMPLATE = """# ReadyAgents BYOK — fill in your keys. Never commit real keys.

READYAGENTS_DEFAULT_MODEL=openai:gpt-4o-mini
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
# READYAGENTS_ALLOW_HTTP=0
"""


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


def version() -> None:
    """Print the version."""
    console.print(__version__)


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
    list_examples_flag: bool = typer.Option(
        False,
        "--list-examples",
        help="List shipped example workflows and exit.",
    ),
    from_example: str | None = typer.Option(
        None,
        "--from-example",
        help="Copy a shipped example instead of a template (cannot combine with --template).",
    ),
) -> None:
    """Scaffold a starter workflow."""
    if list_examples_flag:
        for example in list_examples():
            console.print(example)
        return
    if from_example is not None:
        if template != "pipeline":
            _fail(ConfigError("Cannot combine --from-example with --template."))
            return
        target = dest if dest is not None else Path(name)
        try:
            written = materialize_example(from_example, target)
        except ReadyAgentsError as exc:
            _fail(exc)
            return
        console.print(f"[green]Created {target.resolve()}[/green]  from-example={from_example}")
        for path in written:
            console.print(f"  {path.name}")
        console.print(f"Run: [cyan]readyagents run {target / written[0].name}[/cyan]")
        return
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


def register_version(app: typer.Typer) -> None:
    app.command(rich_help_panel="Core")(version)


def register_init(app: typer.Typer) -> None:
    app.command("init", rich_help_panel="Core")(init_cmd)


def register_new(app: typer.Typer) -> None:
    app.command("new", rich_help_panel="Core")(new_cmd)


def register_validate(app: typer.Typer) -> None:
    app.command(rich_help_panel="Core")(validate)


def register_doctor(app: typer.Typer) -> None:
    app.command("doctor", rich_help_panel="Core")(doctor_cmd)


def register_eval(app: typer.Typer) -> None:
    app.command("eval", rich_help_panel="Core")(eval_cmd)


def register_resume(app: typer.Typer) -> None:
    app.command("resume", rich_help_panel="Core")(resume_cmd)


def register_decide(app: typer.Typer) -> None:
    app.command("decide", rich_help_panel="Core")(decide_cmd)


def register_spend(app: typer.Typer) -> None:
    app.command("spend", rich_help_panel="Core")(spend_cmd)
