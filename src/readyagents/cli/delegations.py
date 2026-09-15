"""CLI group: delegations (split from cli.py; see H-05)."""

from __future__ import annotations

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

delegations_app = typer.Typer(help="Time-bounded approval delegations.", no_args_is_help=True)


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
