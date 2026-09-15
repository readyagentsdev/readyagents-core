"""CLI group: models (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.distill.cli import adapters_app
from readyagents.errors import (
    ConfigError,
    ReadyAgentsError,
)

models_app = typer.Typer(
    help="Catalog and dry-explain model routing. Never calls a provider.",
    no_args_is_help=True,
)


models_app.add_typer(adapters_app, name="adapters")


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
