"""CLI group: governance (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _PACK_HELP,
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError
from readyagents.packs.loader import collect_pack_specs
from readyagents.workflow.runner import (
    confine_under,
    load_workflow,
)


def delegate_cmd(
    from_actor: str = typer.Option(..., "--from", help="Delegator actor id."),
    to_actor: str = typer.Option(..., "--to", help="Delegate actor id."),
    until: str = typer.Option(..., "--until", help="RFC 3339 / ISO-8601 timestamp."),
    scope: str | None = typer.Option(None, "--scope", help="Optional role scope."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Grant a time-bounded, single-hop, revocable delegation. Checked at decision time."""
    from readyagents.approvals.delegate import add_delegation
    from readyagents.config import get_settings

    try:
        settings = get_settings()
        entry = add_delegation(
            from_actor=from_actor,
            to_actor=to_actor,
            until=until,
            scope=scope,
            home=settings.home_path(),
        )
        from readyagents.audit import audit_dir_for, make_auditor

        make_auditor(audit_dir_for(settings.home_path()))(
            "delegation_granted",
            run_id="delegation",
            actor=from_actor,
            delegated_from=from_actor,
            delegated_to=to_actor,
            until=entry.until,
            scope=entry.scope,
            delegation_id=entry.id,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "delegate",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("delegate", ok=True, **entry.as_dict()))
        return
    console.print(
        f"delegated {entry.from_actor} -> {entry.to_actor} until={entry.until} id={entry.id}"
    )


def sign_cmd(
    path: Path = typer.Argument(..., help="Workflow, pack, or SKILL.md path."),
    key: Path = typer.Option(..., "--key", help="Operator-supplied Ed25519 private key."),
    out: Path | None = typer.Option(None, "--out", help="Signature path (default: PATH.sig)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write a detached Ed25519 signature beside the artifact. Private keys are not stored."""
    from readyagents.trust.sign import sign_artifact

    try:
        payload = sign_artifact(path, key=key, out=out)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sign",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    artifact=getattr(extra, "artifact", str(path)),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("sign", ok=True, **payload, path=str(path)))
        return
    console.print(
        f"signed {path} kind={payload['kind']} "
        f"digest={payload['digest']} key_id={payload['key_id']}"
    )


def verify_cmd(
    path: Path = typer.Argument(..., help="Workflow, pack, or SKILL.md path."),
    as_json: bool = typer.Option(False, "--json"),
    sig: Path | None = typer.Option(None, "--sig", help="Detached signature path."),
) -> None:
    """Verify a detached signature against the local publisher keyring."""
    from readyagents.trust.sign import verify_artifact

    try:
        payload = verify_artifact(path, sig_path=sig)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    artifact=getattr(extra, "artifact", str(path)),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        body = {k: v for k, v in payload.items() if k != "ok"}
        _print_json(_json_envelope("verify", ok=True, **body))
        return
    console.print(
        f"ok kind={payload['kind']} digest={payload['digest']} key_id={payload['key_id']}"
    )


def lock_cmd(
    path: Path = _WORKFLOW_ARG,
    out: Path | None = typer.Option(
        None, "--out", help="Lockfile path (default: readyagents.lock beside the workflow)."
    ),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write readyagents.lock pinning workflow, include, pack, MCP, and skill digests."""
    from readyagents.config import get_settings
    from readyagents.trust.lock import (
        build_lockfile,
        default_lock_path,
        snapshot_mcp_surfaces,
        write_lockfile,
    )

    try:
        settings = get_settings()
        spec = load_workflow(path)
        source = path.resolve()
        pack_root = settings.workspace_path()
        root = pack_root if settings.workspace is not None else source.parent
        declared = (spec.workspace or "").strip()
        workspace = confine_under(declared, root, what="workspace") if declared else root
        surfaces = snapshot_mcp_surfaces(spec, workspace) if spec.mcp_servers else {}
        lock = build_lockfile(
            source,
            pack_specs=collect_pack_specs(pack),
            workspace=pack_root,
            mcp_surfaces=surfaces,
            skill_home=settings.home_path(),
        )
        dest = out if out is not None else default_lock_path(source)
        write_lockfile(lock, dest)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "lock",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("lock", ok=True, path=str(dest), **lock.as_dict()))
        return
    console.print(f"wrote {dest} artifacts={len(lock.artifacts)}")


def sbom_cmd(
    path: Path = _WORKFLOW_ARG,
    out: Path | None = typer.Option(None, "--out", help="Write the SBOM JSON to this path."),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Emit a deterministic CycloneDX-shaped inventory. No network, no secrets."""
    from readyagents.config import get_settings
    from readyagents.trust.sbom import build_sbom, dumps_sbom, write_sbom

    try:
        settings = get_settings()
        source = path.resolve()
        pack_root = settings.workspace_path()
        bom = build_sbom(source, pack_specs=collect_pack_specs(pack), workspace=pack_root)
        written = write_sbom(bom, out) if out is not None else None
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "sbom",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload = dict(bom)
        if written is not None:
            payload["path"] = str(written)
        _print_json(_json_envelope("sbom", ok=True, **payload))
        return
    text = dumps_sbom(bom)
    if written is not None:
        console.print(f"wrote {written}")
        return
    console.print(text, end="")


def register_delegate(app: typer.Typer) -> None:
    app.command("delegate", rich_help_panel="Extras: Governance and audit")(delegate_cmd)


def register_sign(app: typer.Typer) -> None:
    app.command("sign", rich_help_panel="Extras: Governance and audit")(sign_cmd)


def register_verify(app: typer.Typer) -> None:
    app.command("verify", rich_help_panel="Extras: Governance and audit")(verify_cmd)


def register_lock(app: typer.Typer) -> None:
    app.command("lock", rich_help_panel="Extras: Governance and audit")(lock_cmd)


def register_sbom(app: typer.Typer) -> None:
    app.command("sbom", rich_help_panel="Extras: Governance and audit")(sbom_cmd)
