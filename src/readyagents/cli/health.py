"""CLI group: health (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

health_app = typer.Typer(
    help="Cluster failures by fingerprint over the run store. No daemon, no telemetry.",
    no_args_is_help=False,
)


@health_app.callback(invoke_without_command=True)
def health_cmd(
    ctx: typer.Context,
    workflow: str | None = typer.Option(None, "--workflow", help="Filter by workflow name."),
    window: int = typer.Option(50, "--window", help="Per-node score window (capped)."),
    limit: int = typer.Option(64, "--limit", help="Max runs to scan (capped)."),
    cursor: str | None = typer.Option(None, "--cursor", help="Pagination cursor (run id)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Cluster failures by fingerprint and rank by impact. Read-only over the run store."""
    if ctx.invoked_subcommand:
        return
    from readyagents.config import get_settings
    from readyagents.health.explain import discover_fixtures
    from readyagents.health.layout import HARD_MAX_RUNS, HARD_MAX_WINDOW
    from readyagents.health.query import query_health
    from readyagents.policy import Redactor
    from readyagents.replay.record import known_secret_values
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        fixtures = discover_fixtures(settings.workspace_path(), cap=HARD_MAX_RUNS)
        report = query_health(
            store,
            workflow=workflow,
            window=min(window, HARD_MAX_WINDOW),
            limit=min(limit, HARD_MAX_RUNS),
            cursor=cursor,
            secrets=known_secret_values(settings),
            redactor=Redactor(literals=known_secret_values(settings)),
            fixtures=fixtures,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("health", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    body = report.as_dict()
    if as_json:
        _print_json(_json_envelope("health", ok=True, **body))
        return
    clusters = report.clusters
    if not clusters:
        console.print(f"no failure clusters (scanned={report.scanned})")
    for row in clusters:
        hint = ""
        if row.fixture:
            hint = f" fixture={row.fixture}"
        elif row.run_ids:
            hint = f" freeze: readyagents runs freeze {row.run_ids[0]}"
        console.print(
            f"{row.fingerprint.id}  class={row.fingerprint.klass}  "
            f"node={row.node_id}  count={row.count}  cost_micros={row.cost_micros}{hint}"
        )
    for node in report.nodes:
        flag = " flaky" if node.flaky else (" broken" if node.broken else "")
        console.print(
            f"node {node.node_id}  success={node.success_rate}  "
            f"score={node.score}  window={node.window}{flag}"
        )


@health_app.command("explain")
def health_explain_cmd(
    fingerprint: str = typer.Argument(..., help="Fingerprint id from readyagents health."),
    out: Path = typer.Option(..., "--out", help="Directory to write the root-cause bundle."),
    yes: bool = typer.Option(False, "--yes", help="Required. The bundle is diagnostic data."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing bundle directory."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write a confined, redacted, audited root-cause bundle for one fingerprint."""
    from readyagents.config import get_settings
    from readyagents.health.explain import write_explain_bundle
    from readyagents.policy import Redactor
    from readyagents.replay.record import known_secret_values
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        dest = write_explain_bundle(
            fingerprint,
            store,
            out_dir=out,
            workspace=settings.workspace_path(),
            settings=settings,
            secrets=known_secret_values(settings),
            redactor=Redactor(literals=known_secret_values(settings)),
            force=force,
            confirm=yes,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "health explain",
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
    if as_json:
        _print_json(_json_envelope("health explain", ok=True, path=str(dest)))
        return
    console.print(f"wrote {dest}")
