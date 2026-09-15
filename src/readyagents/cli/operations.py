"""CLI group: operations (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import (
    Any,
    NoReturn,
)

import typer
from rich.markup import escape
from rich.table import Table

from readyagents.cli._common import (
    _PACK_HELP,
    _WORKFLOW_ARG,
    _emit_env_error,
    _emit_run_exception,
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import (
    ApprovalRequired,
    ConfigError,
    EnvRefused,
    ReadyAgentsError,
    RegistryRefused,
)
from readyagents.packs.loader import collect_pack_specs
from readyagents.workflow.runner import confine_under
from readyagents.workflow.state import build_decisions


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


def register_promote(app: typer.Typer) -> None:
    app.command("promote", rich_help_panel="Extras: Operations")(promote_cmd)


def register_rollback(app: typer.Typer) -> None:
    app.command("rollback", rich_help_panel="Extras: Operations")(rollback_cmd)


def register_batch(app: typer.Typer) -> None:
    app.command("batch", rich_help_panel="Extras: Operations")(batch_cmd)


def register_wake(app: typer.Typer) -> None:
    app.command("wake", rich_help_panel="Extras: Operations")(wake_cmd)


def register_event(app: typer.Typer) -> None:
    app.command("event", rich_help_panel="Extras: Operations")(event_cmd)
