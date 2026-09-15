"""CLI group: sessions (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _emit_run_exception,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

sessions_app = typer.Typer(
    help="List, show, close, replay, and freeze conversational sessions. Turns are runs.",
    no_args_is_help=True,
)


@sessions_app.command("start")
def sessions_start_cmd(
    path: Path = _WORKFLOW_ARG,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Start a session. Parks at the first converse node."""
    from readyagents.sessions.service import start_session

    try:
        session = start_session(path)
    except ReadyAgentsError as extra:
        _emit_run_exception(extra, as_json=as_json, persist=True, command="sessions start")
        return
    if as_json:
        _print_json(
            _json_envelope(
                "sessions start",
                ok=True,
                session_id=session.session_id,
                status=session.status,
                pending_run_id=session.pending_run_id,
            )
        )
        return
    console.print(f"{session.session_id} {session.status}")


@sessions_app.command("list")
def sessions_list_cmd(
    as_json: bool = typer.Option(False, "--json"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List durable sessions."""
    from readyagents.sessions.service import list_sessions

    rows = [
        {
            "session_id": item.session_id,
            "status": item.status,
            "workflow": item.workflow,
            "turns": len(item.turns),
        }
        for item in list_sessions(limit=limit)
    ]
    if as_json:
        _print_json(_json_envelope("sessions list", ok=True, sessions=rows))
        return
    for row in rows:
        console.print(f"{row['session_id']} {row['status']} turns={row['turns']}")


@sessions_app.command("show")
def sessions_show_cmd(
    session_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one session."""
    from readyagents.errors import SessionRefused
    from readyagents.sessions.service import show_session

    try:
        session = show_session(session_id)
    except SessionRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sessions show", ok=False, error="SessionRefused", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sessions show", ok=True, session=session.as_dict()))
        return
    console.print(f"{session.session_id} {session.status} turns={len(session.turns)}")


@sessions_app.command("close")
def sessions_close_cmd(
    session_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Close a session and clear session memory unless promotion was declared."""
    from readyagents.errors import SessionRefused
    from readyagents.sessions.service import close_session

    try:
        session = close_session(session_id)
    except SessionRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sessions close", ok=False, error="SessionRefused", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sessions close", ok=True, status=session.status))
        return
    console.print(f"closed {session.session_id}")


@sessions_app.command("reply")
def sessions_reply_cmd(
    session_id: str,
    text: str = typer.Option(..., "--text", help="User or human-agent reply."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Resume a parked converse node with a reply."""
    from readyagents.errors import SessionRefused
    from readyagents.sessions.service import reply_session

    try:
        session = reply_session(session_id, text)
    except SessionRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sessions reply", ok=False, error="SessionRefused", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        _emit_run_exception(extra, as_json=as_json, persist=True, command="sessions reply")
        return
    if as_json:
        _print_json(
            _json_envelope(
                "sessions reply",
                ok=True,
                session_id=session.session_id,
                status=session.status,
            )
        )
        return
    console.print(f"{session.session_id} {session.status}")


@sessions_app.command("replay")
def sessions_replay_cmd(
    session_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Replay a session's turns from recorded runs/cassettes (offline)."""
    from readyagents.errors import SessionRefused
    from readyagents.sessions.service import replay_session

    try:
        report = replay_session(session_id)
    except SessionRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sessions replay", ok=False, error="SessionRefused", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sessions replay", ok=True, **report))
        return
    console.print(f"replay {report['session_id']} turns={len(report.get('turns') or [])}")


@sessions_app.command("freeze")
def sessions_freeze_cmd(
    session_id: str,
    out: Path = typer.Option(..., "--out", help="Directory for the multi-turn fixture."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Freeze a session into a multi-turn eval fixture."""
    from readyagents.errors import SessionRefused
    from readyagents.sessions.service import freeze_session

    try:
        dest = freeze_session(session_id, out_dir=out)
    except SessionRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sessions freeze", ok=False, error="SessionRefused", message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sessions freeze", ok=True, path=str(dest)))
        return
    console.print(f"froze {session_id} -> {dest}")
