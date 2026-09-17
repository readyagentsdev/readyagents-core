"""CLI group: _common (split from cli.py; see H-05)."""

import json
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from readyagents.errors import (
    ApprovalRequired,
    CassetteMiss,
    ConverseRequired,
    EnvRefused,
    ReadyAgentsError,
    WaitingRequired,
)
from readyagents.workflow.state import (
    RunState,
)

console = Console()

err_console = Console(stderr=True)

_WORKFLOW_ARG = typer.Argument(
    ...,
    help="Workflow YAML or JSON file.",
)

_PACK_HELP = (
    "Local pack .py to load (repeatable). Confined to the workspace. Env: READYAGENTS_PACK."
)


def _print_json(payload: object) -> None:
    """Write JSON to stdout without Rich markup (values may contain `[...]`)."""
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _feedback_payload(
    nodes: list[str],
    edit: str | None,
    rating: int | None,
    label: str | None,
) -> dict[str, Any]:
    if not edit and rating is None and not label:
        return {}
    payload: dict[str, Any] = {}
    for node in nodes:
        row: dict[str, Any] = {}
        if edit is not None:
            row["edit"] = edit
        if rating is not None:
            row["rating"] = rating
        if label:
            row["label"] = label
        payload[node] = row
    return payload


def _json_envelope(command: str, *, ok: bool, **fields: Any) -> dict[str, Any]:
    """Additive JSON envelope: existing keys stay, ok/command always win."""
    payload = dict(fields)
    payload["ok"] = ok
    payload["command"] = command
    return payload


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
    if isinstance(exc, WaitingRequired):
        state = _state_from_exc(exc)
        if as_json:
            payload: dict[str, Any] = {
                "error": type(exc).__name__,
                "message": str(exc),
                "run_id": exc.run_id,
                "node_id": exc.node_id,
                "status": "waiting",
            }
            if state is not None:
                payload["run"] = state.to_record()
            _print_json(_json_envelope(command, ok=False, **payload))
        else:
            err_console.print(f"[yellow]waiting[/yellow] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    if isinstance(exc, (ApprovalRequired, ConverseRequired)):
        state = _state_from_exc(exc)
        if as_json:
            payload: dict[str, Any] = {
                "error": type(exc).__name__,
                "message": str(exc),
                "run_id": exc.run_id,
                "node_id": exc.node_id,
                "prompt": getattr(exc, "prompt", None) or getattr(exc, "say", None),
                "status": "paused",
            }
            if isinstance(exc, ConverseRequired):
                payload["say"] = exc.say
                payload["mode"] = exc.mode
            if state is not None:
                payload["run"] = state.to_record()
            _print_json(_json_envelope(command, ok=False, **payload))
        else:
            if isinstance(exc, ConverseRequired):
                err_console.print(f"[yellow]converse[/yellow] {escape(exc.say)}")
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
        err_console.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
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
    err_console.print(f"[yellow]{type(exc).__name__}:[/yellow] {escape(str(exc))}")
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


def _emit_env_error(command: str, extra: EnvRefused, *, as_json: bool) -> NoReturn:
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


def _fail(exc: BaseException) -> NoReturn:
    err_console.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
    problems = getattr(exc, "problems", None)
    if problems:
        from readyagents.workflow.source_map import render_located_problems

        located = render_located_problems(list(problems))
        if located:
            err_console.print(located, markup=False)
    raise typer.Exit(code=1) from exc
