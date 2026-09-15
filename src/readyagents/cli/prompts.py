"""CLI group: prompts (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

prompts_app = typer.Typer(
    help="List, show, diff, and rollback versioned prompts. Never rewrites workflow YAML.",
    no_args_is_help=True,
)


@prompts_app.command("list")
def prompts_list_cmd(
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.errors import OptimizeRefused
    from readyagents.prompts.registry import list_prompts, register_literals

    try:
        register_literals(path)
        rows = list_prompts(path)
    except OptimizeRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts list",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts list", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("prompts list", ok=True, prompts=rows))
        return
    if not rows:
        console.print("no registered prompts")
        return
    for row in rows:
        console.print(
            f"{row['id']} node={row['node_id']} v{row['active_version']} "
            f"hash={row['content_hash'][:12]}"
        )


@prompts_app.command("show")
def prompts_show_cmd(
    path: Path = _WORKFLOW_ARG,
    prompt_id: str = typer.Option(..., "--id"),
    version: int | None = typer.Option(None, "--version"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.errors import OptimizeRefused
    from readyagents.prompts.registry import get_prompt, register_literals

    try:
        register_literals(path)
        row = get_prompt(path, prompt_id, version=version)
    except OptimizeRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts show",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = row.as_dict()
    if as_json:
        _print_json(_json_envelope("prompts show", ok=True, prompt=body))
        return
    console.print(f"{prompt_id}@{row.version} hash={row.content_hash}")
    console.print(row.text)


@prompts_app.command("history")
def prompts_history_cmd(
    path: Path = _WORKFLOW_ARG,
    prompt_id: str = typer.Option(..., "--id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.errors import OptimizeRefused
    from readyagents.prompts.registry import history, register_literals

    try:
        register_literals(path)
        rows = history(path, prompt_id)
    except OptimizeRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts history",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts history", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("prompts history", ok=True, versions=rows))
        return
    for row in rows:
        console.print(f"v{row['version']} {row['source']} hash={row['content_hash'][:12]}")


@prompts_app.command("diff")
def prompts_diff_cmd(
    path: Path = _WORKFLOW_ARG,
    prompt_id: str = typer.Option(..., "--id"),
    left: int | None = typer.Option(None, "--left"),
    right: int | None = typer.Option(None, "--right"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.errors import OptimizeRefused
    from readyagents.prompts.registry import diff_versions, register_literals

    try:
        register_literals(path)
        text = diff_versions(path, prompt_id, left=left, right=right)
    except OptimizeRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts diff",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts diff", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("prompts diff", ok=True, diff=text))
        return
    console.print(text or "(no diff)")


@prompts_app.command("rollback")
def prompts_rollback_cmd(
    path: Path = _WORKFLOW_ARG,
    prompt_id: str = typer.Option(..., "--id"),
    version: int | None = typer.Option(None, "--version"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.errors import OptimizeRefused
    from readyagents.prompts.registry import register_literals, rollback

    try:
        register_literals(path)
        row = rollback(path, prompt_id, version=version)
    except OptimizeRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts rollback",
                    ok=False,
                    error="OptimizeRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "prompts rollback",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = row.as_dict()
    if as_json:
        _print_json(_json_envelope("prompts rollback", ok=True, prompt=body))
        return
    console.print(f"rolled back {prompt_id} to v{row.version} hash={row.content_hash}")
