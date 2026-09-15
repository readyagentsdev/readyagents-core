"""CLI group: skills (split from cli.py; see H-05)."""

from __future__ import annotations

import json
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

skills_app = typer.Typer(
    help="Install, list, export, and remove Agent Skills. No marketplace.",
    no_args_is_help=True,
)


@skills_app.command("add")
def skills_add(
    source: str = typer.Argument(..., help="Skill folder, zip, or URL."),
    require_signature: bool = typer.Option(False, "--require-signature"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Install a skill folder into the local catalog. Path-confined."""
    from readyagents.config import get_settings
    from readyagents.skills.install import add_skill

    try:
        row = add_skill(
            source, home=get_settings().home_path(), require_signature=require_signature
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "skills add", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("skills add", ok=True, skill=row))
        return
    console.print(f"installed {row.get('name')} digest={row.get('digest')}")


@skills_app.command("list")
def skills_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List installed skills: name, source, digest, tools, scripts, signature."""
    from readyagents.config import get_settings
    from readyagents.skills.catalog import list_records

    rows = list_records(get_settings().home_path())
    if as_json:
        _print_json(_json_envelope("skills list", ok=True, skills=rows))
        return
    if not rows:
        console.print("No skills installed.")
        return
    for row in rows:
        console.print(
            f"name: {row.get('name')}  digest: {row.get('digest')}  "
            f"sig: {row.get('signature_status')}  tools: {row.get('allowed_tools')}  "
            f"scripts: {row.get('scripts')}"
        )


@skills_app.command("show")
def skills_show(
    name: str = typer.Argument(..., help="Installed skill name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one installed skill record (frontmatter index, not the body)."""
    from readyagents.config import get_settings
    from readyagents.skills.catalog import get_record

    try:
        row = get_record(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "skills show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("skills show", ok=True, skill=row))
        return
    console.print(json.dumps(row, indent=2, ensure_ascii=False))


@skills_app.command("remove")
def skills_remove(
    name: str = typer.Argument(..., help="Installed skill name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove an installed skill from the local catalog."""
    from readyagents.config import get_settings
    from readyagents.skills.catalog import remove_name

    try:
        remove_name(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "skills remove", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("skills remove", ok=True, name=name))
        return
    console.print(f"removed {name}")


@skills_app.command("export")
def skills_export(
    path: Path = _WORKFLOW_ARG,
    dest: Path = typer.Option(..., "--out", help="Directory to write the skill folder into."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Emit an open-format skill folder for a workflow. No secrets or local paths."""
    from readyagents.skills.export import export_workflow

    try:
        folder = export_workflow(path, dest)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "skills export", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("skills export", ok=True, path=str(folder)))
        return
    console.print(f"exported {folder}")
