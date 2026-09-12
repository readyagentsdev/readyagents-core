"""Typer app for `readyagents registry`. Inventory, not a hosted directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from readyagents.errors import AuthorizationError, RegistryRefused

registry_app = typer.Typer(
    help=(
        "Agent inventory derived from declared roots. Not a hosted registry, "
        "not an Article 49 filing, not a compliance claim."
    ),
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _print_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _envelope(command: str, *, ok: bool, **fields: Any) -> dict[str, Any]:
    payload = dict(fields)
    payload["ok"] = ok
    payload["command"] = command
    return payload


def _fail(exc: BaseException) -> NoReturn:
    err_console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
    raise typer.Exit(code=1) from exc


def _emit_error(command: str, extra: BaseException, *, as_json: bool) -> NoReturn:
    if as_json:
        body: dict[str, Any] = {
            "error": type(extra).__name__,
            "message": str(extra),
        }
        reason = getattr(extra, "reason", None)
        if reason is not None:
            body["reason"] = reason
        _print_json(_envelope(command, ok=False, **body))
        raise typer.Exit(code=1) from extra
    _fail(extra)


def _authorizer():
    from readyagents.packs.loader import collect_pack_authorizers
    from readyagents.policy import resolve_authorizer

    return resolve_authorizer(collect_pack_authorizers())


@registry_app.command("scan")
def registry_scan_cmd(
    root: list[str] | None = typer.Option(
        None,
        "--root",
        help="Declared discovery root under the workspace (repeatable). Default: config roots.",
    ),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    unredact: bool = typer.Option(False, "--unredact", help="Show endpoints. RBAC-checked."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Discover workflows, packages, releases, and packs in declared roots only."""
    from readyagents.config import get_settings
    from readyagents.registry.scan import scan

    try:
        settings = get_settings()
        auth = _authorizer()
        if unredact:
            auth.check(actor, "registry.unredact", "registry")
        entries = scan(
            settings=settings,
            roots=list(root) if root else None,
            authorizer=auth,
            actor=actor,
        )
        rows = [item.as_dict(redact=not unredact) for item in entries]
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry scan", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry scan", ok=True, agents=rows, count=len(rows)))
        return
    if not rows:
        console.print("no agents")
        return
    for row in rows:
        console.print(
            f"{row['agent_id']} {row['derived'].get('name')} {row['derived'].get('kind')}"
        )


@registry_app.command("annotate")
def registry_annotate_cmd(
    agent_id: str = typer.Argument(..., help="Stable agent id (agt_…)."),
    owner: str | None = typer.Option(None, "--owner", help="Owner role, not a personal contact."),
    backup: str | None = typer.Option(None, "--backup", help="Backup owner role."),
    purpose: str | None = typer.Option(None, "--purpose"),
    tier: str | None = typer.Option(None, "--tier", help="high, medium, or low."),
    data_classes: str | None = typer.Option(None, "--data-classes", help="Comma-separated."),
    retention: str | None = typer.Option(None, "--retention"),
    review: str | None = typer.Option(None, "--review", help="Cadence such as 90d."),
    review_last: str | None = typer.Option(None, "--review-last", help="ISO date of last review."),
    decommission: str | None = typer.Option(None, "--decommission", help="ISO date."),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fill missing declared fields only. Never asks for derived facts."""
    from readyagents.config import get_settings
    from readyagents.registry.annotate import annotate
    from readyagents.registry.scan import get_entry

    updates: dict[str, Any] = {}
    if owner:
        updates["owner"] = owner
    if backup:
        updates["backup_owner"] = backup
    if purpose:
        updates["purpose"] = purpose
    if tier:
        updates["risk_tier"] = tier
    if data_classes:
        updates["data_classes"] = [part.strip() for part in data_classes.split(",") if part.strip()]
    if retention:
        updates["retention"] = retention
    if review or review_last:
        updates["review"] = {k: v for k, v in {"cadence": review, "last": review_last}.items() if v}
    if decommission:
        updates["decommission_after"] = decommission
    try:
        settings = get_settings()
        missing = annotate(
            agent_id,
            updates,
            settings=settings,
            authorizer=_authorizer(),
            actor=actor,
        )
        entry = get_entry(agent_id, settings=settings)
        payload = {
            "agent_id": agent_id,
            "missing": missing,
            "declared": entry.declared.model_dump(mode="python") if entry else {},
        }
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry annotate", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry annotate", ok=True, **payload))
        return
    if missing:
        console.print(f"missing: {', '.join(missing)}")
        return
    console.print(f"{agent_id} complete")


@registry_app.command("check")
def registry_check_cmd(
    enforce: bool = typer.Option(
        False,
        "--enforce",
        help="Fail with a distinct typed reason when a tier requirement is unmet.",
    ),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Report missing metadata, drift, overdue reviews, and unused candidates. Never deletes."""
    from readyagents.config import get_settings
    from readyagents.errors import (
        RegistryTierApproval,
        RegistryTierCadence,
        RegistryTierEvidence,
        RegistryTierSigned,
    )
    from readyagents.registry.check import check

    try:
        report = check(
            settings=get_settings(),
            authorizer=_authorizer(),
            actor=actor,
            enforce=enforce,
        )
    except (
        RegistryRefused,
        AuthorizationError,
        RegistryTierApproval,
        RegistryTierEvidence,
        RegistryTierSigned,
        RegistryTierCadence,
    ) as extra:
        _emit_error("registry check", extra, as_json=as_json)
    body = report.as_dict()
    if as_json:
        _print_json(_envelope("registry check", ok=True, **body))
        return
    console.print(
        f"missing={len(body['missing'])} drift={len(body['drift'])} "
        f"overdue={len(body['overdue'])} unused={len(body['unused'])} "
        f"violations={len(body['violations'])}"
    )


@registry_app.command("list")
def registry_list_cmd(
    owner: str | None = typer.Option(None, "--owner"),
    tier: str | None = typer.Option(None, "--tier"),
    model: str | None = typer.Option(None, "--model"),
    kind: str | None = typer.Option(None, "--kind"),
    stale: bool = typer.Option(False, "--stale", help="Only unused decommission candidates."),
    unredact: bool = typer.Option(False, "--unredact"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fleet view. Endpoints redacted unless --unredact is authorised."""
    from readyagents.config import get_settings
    from readyagents.registry.check import check
    from readyagents.registry.view import list_agents

    try:
        settings = get_settings()
        unused = {row["agent_id"] for row in check(settings=settings).unused}
        rows = list_agents(
            settings=settings,
            owner=owner,
            tier=tier,
            model=model,
            kind=kind,
            stale=True if stale else None,
            unused_ids=unused,
            redact=not unredact,
            authorizer=_authorizer(),
            actor=actor,
        )
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry list", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry list", ok=True, agents=rows, count=len(rows)))
        return
    if not rows:
        console.print("no agents")
        return
    for row in rows:
        console.print(f"{row['agent_id']} {row.get('name')} tier={row.get('risk_tier')}")


@registry_app.command("show")
def registry_show_cmd(
    agent_id: str = typer.Argument(...),
    unredact: bool = typer.Option(False, "--unredact"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one inventory entry. Derived facts are recomputed, not stored as truth."""
    from readyagents.config import get_settings
    from readyagents.registry.view import show_agent

    try:
        row = show_agent(
            agent_id,
            settings=get_settings(),
            redact=not unredact,
            authorizer=_authorizer(),
            actor=actor,
        )
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry show", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry show", ok=True, agent=row))
        return
    console.print(f"{row['agent_id']} {row['derived'].get('name')}")


@registry_app.command("stats")
def registry_stats_cmd(
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Portfolio summary for a governance meeting. Roles, not personal contacts."""
    from readyagents.config import get_settings
    from readyagents.registry.view import stats

    try:
        payload = stats(settings=get_settings(), authorizer=_authorizer(), actor=actor)
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry stats", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry stats", ok=True, **payload))
        return
    console.print(f"count={payload['count']} unused={payload['unused_candidates']}")


@registry_app.command("card")
def registry_card_cmd(
    agent_id: str = typer.Argument(...),
    unredact: bool = typer.Option(False, "--unredact"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Model-card-shaped document from evidence. Unknowns are named, never guessed."""
    from readyagents.config import get_settings
    from readyagents.registry.card import model_card

    try:
        card = model_card(
            agent_id,
            settings=get_settings(),
            authorizer=_authorizer(),
            actor=actor,
            redact=not unredact,
        )
    except (RegistryRefused, AuthorizationError) as extra:
        _emit_error("registry card", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("registry card", ok=True, card=card))
        return
    console.print(card.get("disclaimer", ""))
    console.print(f"{card['agent_id']} purpose={card.get('purpose')}")


@registry_app.command("export")
def registry_export_cmd(
    agent_id: str = typer.Argument(...),
    fmt: str = typer.Option(
        "annex-viii",
        "--format",
        help="Only annex-viii (draft). Not a legal filing.",
    ),
    out: Path = typer.Option(
        ..., "--out", help="Workspace-confined path. Filename must contain draft."
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm writing a reconnaissance document."),
    unredact: bool = typer.Option(False, "--unredact"),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write an Annex VIII-shaped draft. Filename, header, and docs label it a draft."""
    from readyagents.config import get_settings
    from readyagents.errors import ConfigError, PathError
    from readyagents.registry.export import write_annex_viii

    if str(fmt).strip().lower() not in {"annex-viii", "annex_viii", "annexviii"}:
        extra = RegistryRefused(f"unsupported export format {fmt!r}", reason="format")
        _emit_error("registry export", extra, as_json=as_json)
    try:
        if not yes:
            err_console.print(
                f"[yellow]Warning:[/yellow] registry export maps automation, data flows, "
                f"and owners. Pass --yes to write {out}."
            )
        dest = write_annex_viii(
            agent_id,
            out,
            settings=get_settings(),
            authorizer=_authorizer(),
            actor=actor,
            redact=not unredact,
            yes=yes,
        )
    except (RegistryRefused, AuthorizationError, PathError, ConfigError) as extra:
        _emit_error("registry export", extra, as_json=as_json)
    if as_json:
        _print_json(
            _envelope("registry export", ok=True, path=str(dest), format="annex-viii-draft")
        )
        return
    console.print(f"wrote draft {dest}")
