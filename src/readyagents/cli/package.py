"""CLI group: package (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

package_app = typer.Typer(
    help="Build, install, and catalog workflow packages. No hosted registry.",
    no_args_is_help=True,
)


@package_app.command("build")
def package_build(
    source: Path = typer.Argument(..., help="Directory containing readyagents.pkg.yaml."),
    out: Path | None = typer.Option(
        None, "--out", help="Archive path (default: NAME-VERSION.rapkg)."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Build a deterministic .rapkg archive. Secret values are refused."""
    from readyagents.package.archive import build_package, digest_archive
    from readyagents.package.manifest import load_manifest

    try:
        dest = build_package(source, out=out)
        manifest = load_manifest(source)
        digest = digest_archive(dest)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package build",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(
            _json_envelope(
                "package build",
                ok=True,
                path=str(dest),
                name=manifest.name,
                version=manifest.version,
                digest=digest,
            )
        )
        return
    console.print(f"built {dest} digest={digest}")


@package_app.command("install")
def package_install(
    source: str = typer.Argument(..., help="Package archive path or URL."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Write after review. Default: show review and refuse."
    ),
    require_signature: bool = typer.Option(False, "--require-signature"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify, review capabilities, then install. Nothing executes. Default is no write."""
    from readyagents.config import get_settings
    from readyagents.errors import PackageNeedsConfirm
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.package.install import install_package
    from readyagents.trust.keyring import load_keyring

    settings = get_settings()
    policy = load_resolved(explicit=None, workflow_dir=settings.workspace_path(), stored=None)
    try:
        row = install_package(
            source,
            home=settings.home_path(),
            confirm=confirm,
            policy=policy,
            keyring=load_keyring(home=settings.home_path()),
            require_signature=require_signature,
        )
    except PackageNeedsConfirm as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package install",
                    ok=False,
                    error="PackageNeedsConfirm",
                    message=str(extra),
                    review=extra.review,
                )
            )
            raise typer.Exit(code=1) from extra
        _print_review(extra.review)
        console.print(str(extra))
        raise typer.Exit(code=1) from extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package install",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    review=getattr(extra, "review", None),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package install", ok=True, **row))
        return
    console.print(f"installed {row.get('name')}@{row.get('version')} digest={row.get('digest')}")


@package_app.command("list")
def package_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """List installed packages: name, version, digest, signature."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import list_records

    rows = list_records(get_settings().home_path())
    if as_json:
        _print_json(_json_envelope("package list", ok=True, packages=rows))
        return
    if not rows:
        console.print("No packages installed.")
        return
    for row in rows:
        console.print(
            f"name: {row.get('name')}  version: {row.get('version')}  "
            f"digest: {row.get('digest')}  sig: {row.get('signature_status')}"
        )


@package_app.command("show")
def package_show(
    name: str = typer.Argument(..., help="Installed package name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one installed package record."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import get_record

    try:
        row = get_record(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package show", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package show", ok=True, package=row))
        return
    console.print(json.dumps(row, indent=2, ensure_ascii=False))


@package_app.command("remove")
def package_remove(
    name: str = typer.Argument(..., help="Installed package name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove an installed package from the local catalog."""
    from readyagents.config import get_settings
    from readyagents.package.catalog import remove_name

    try:
        remove_name(get_settings().home_path(), name)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package remove", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package remove", ok=True, name=name))
        return
    console.print(f"removed {name}")


@package_app.command("upgrade")
def package_upgrade(
    name: str = typer.Argument(..., help="Installed package name."),
    source: str = typer.Argument(..., help="Replacement archive path or URL."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Write after review. Default: show review and refuse."
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Upgrade a package. Overlay is preserved. Permission widening needs --confirm."""
    from readyagents.config import get_settings
    from readyagents.errors import PackageNeedsConfirm
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.package.catalog import upgrade_package
    from readyagents.trust.keyring import load_keyring

    settings = get_settings()
    policy = load_resolved(explicit=None, workflow_dir=settings.workspace_path(), stored=None)
    try:
        row = upgrade_package(
            name,
            source,
            home=settings.home_path(),
            confirm=confirm,
            policy=policy,
            keyring=load_keyring(home=settings.home_path()),
        )
    except PackageNeedsConfirm as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package upgrade",
                    ok=False,
                    error="PackageNeedsConfirm",
                    message=str(extra),
                    review=extra.review,
                )
            )
            raise typer.Exit(code=1) from extra
        _print_review(extra.review)
        console.print(str(extra))
        raise typer.Exit(code=1) from extra
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package upgrade",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    review=getattr(extra, "review", None),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package upgrade", ok=True, **row))
        return
    console.print(f"upgraded {row.get('name')}@{row.get('version')} digest={row.get('digest')}")


@package_app.command("index")
def package_index(
    path: Path = typer.Argument(..., help="Static JSON index (readyagents.index.json)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify a signed static package index. Unsigned indexes are refused."""
    from readyagents.config import get_settings
    from readyagents.package.index import verify_index
    from readyagents.trust.keyring import load_keyring

    try:
        payload = verify_index(path, keyring=load_keyring(home=get_settings().home_path()))
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "package index", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("package index", ok=True, **payload))
        return
    count = len(payload.get("packages") or [])
    console.print(f"ok packages={count} digest={payload.get('digest')}")


def _print_review(review: dict | None) -> None:
    if not review:
        return
    console.print(
        "review "
        f"name={review.get('name')} version={review.get('version')} "
        f"sig={review.get('signature')} digest={review.get('digest')}"
    )
    console.print(f"tools: {review.get('tools')}")
    console.print(f"hosts: {review.get('hosts')}")
    console.print(f"secrets: {review.get('secrets')}")
    console.print(f"budget: {review.get('budget')}")
    console.print(f"approvals: {review.get('approvals')}")
    diff = review.get("policy_diff") or {}
    if review.get("constrained") or diff.get("extra_tools") or diff.get("extra_hosts"):
        extra_tools = diff.get("extra_tools")
        extra_hosts = diff.get("extra_hosts")
        console.print(f"constrained extra_tools={extra_tools} extra_hosts={extra_hosts}")
    upgrade = review.get("upgrade")
    if upgrade:
        widened = upgrade.get("widened")
        new_tools = upgrade.get("new_tools")
        console.print(f"upgrade widened={widened} new_tools={new_tools}")
