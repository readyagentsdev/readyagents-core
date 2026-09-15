"""CLI group: runs (split from cli.py; see H-05)."""

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.panel import Panel

from readyagents.cli._common import (
    _PACK_HELP,
    _emit_run,
    _emit_run_exception,
    _fail,
    _json_envelope,
    _preview,
    _print_json,
    _print_run,
    _print_usage,
    console,
    err_console,
)
from readyagents.errors import (
    ConfigError,
    ReadyAgentsError,
)
from readyagents.packs.loader import collect_pack_specs
from readyagents.workflow.runner import (
    replay_run,
)
from readyagents.workflow.state import (
    build_decisions,
    parse_input_pairs,
)

runs_app = typer.Typer(help="Inspect, replay, fork, diff, freeze runs.", no_args_is_help=True)


@runs_app.command("list")
def runs_list(
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    status: str | None = typer.Option(
        None, "--status", help="Filter: running, paused, failed, succeeded."
    ),
    waiting: bool = typer.Option(
        False, "--waiting", help="Only waiting runs; show condition and expiry."
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
    if waiting:
        found = [s for s in found if s.status == "waiting"]
    location = settings.runs_dir() if settings.run_store == "json" else settings.run_db_path()
    if as_json:
        payload = []
        for s in found:
            row = {
                "run_id": s.run_id,
                "workflow": s.workflow_name,
                "status": s.status,
                "started_at": s.started_at,
                "pending_node": s.pending_node,
                "nodes": [r.node_id for r in s.results],
            }
            if waiting or s.status == "waiting":
                pending = s.pending if isinstance(s.pending, dict) else {}
                row["waiting_for"] = pending.get("waiting_for")
                row["expires_at"] = pending.get("expires_at")
            payload.append(row)
        _print_json(payload)
        return
    if not found:
        console.print(f"No runs in {location}")
        return
    console.print(f"Runs in {location}")
    for state in found:
        nodes = ",".join(r.node_id for r in state.results) or "-"
        extra = ""
        if waiting or state.status == "waiting":
            pending = state.pending if isinstance(state.pending, dict) else {}
            extra = (
                f"  waiting_for: {pending.get('waiting_for')}  expires: {pending.get('expires_at')}"
            )
        console.print(
            f"run_id: {state.run_id}  workflow: {state.workflow_name}  "
            f"status: {state.status}  started: {state.started_at}  nodes: {nodes}{extra}"
        )


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    as_json: bool = typer.Option(False, "--json", help="Print the stored run record as JSON."),
) -> None:
    """Show a run record and its node timeline."""
    _show_run(run_id, as_json=as_json)


@runs_app.command("timeline")
def runs_timeline(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """What happened, what it is waiting for, spend so far, what it expects next."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.wait.timeline import timeline

    settings = get_settings()
    store = open_run_store(settings)
    try:
        state = store.get(run_id, allow_prefix=True).state
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "runs timeline", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    finally:
        store.close()
    payload = timeline(state)
    if as_json:
        _print_json(_json_envelope("runs timeline", ok=True, **payload))
        return
    console.print(f"run {payload['run_id']} status={payload['status']}")
    console.print(f"waiting_for={payload.get('waiting_for')} expires={payload.get('expires_at')}")
    console.print(f"expects: {payload.get('expects_next')}")
    console.print(f"spend: {payload.get('spend')}")


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
