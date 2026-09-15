"""CLI group: identity (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import (
    IdentityError,
    ReadyAgentsError,
)

identity_app = typer.Typer(
    help="Verify approver assertions and inspect workload identity.", no_args_is_help=True
)
identity_trust_app = typer.Typer(help="Manage local trust-anchor issuers.", no_args_is_help=True)
trust_app = typer.Typer(
    help="Manage the local publisher keyring for signed artifacts.",
    no_args_is_help=True,
)

identity_app.add_typer(identity_trust_app, name="trust")


@identity_app.command("verify")
def identity_verify_cmd(
    token_file: Path = typer.Option(..., "--token-file", help="JWT/OIDC token file."),
    trust_anchors: Path | None = typer.Option(
        None,
        "--trust-anchors",
        help="Trust-anchor YAML.",
        envvar="READYAGENTS_TRUST_ANCHORS",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Verify an assertion against local trust anchors. Does not resume a run."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path
    from readyagents.identity.decide import load_token_file
    from readyagents.identity.verify import verify_token

    try:
        settings = get_settings()
        resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
        if resolved is None:
            raise IdentityError("no trust-anchor file configured")
        anchors = load_trust_anchors(resolved)
        actor = verify_token(load_token_file(token_file), anchors, base=resolved.parent)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = actor.as_dict()
    if as_json:
        _print_json(_json_envelope("identity verify", ok=True, **payload))
        return
    console.print(f"ok subject={actor.subject} issuer={actor.issuer} actor={actor.actor}")


@identity_app.command("whoami")
def identity_whoami_cmd(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Print the configured workload identity fingerprint. Never the private key."""
    from readyagents.identity.workload import whoami

    try:
        info = whoami()
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity whoami",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("identity whoami", ok=True, **info))
        return
    if not info.get("configured"):
        console.print("workload identity is not configured")
        return
    console.print(
        f"subject={info['subject']} key_id={info['key_id']} fingerprint={info['fingerprint']}"
    )


@identity_trust_app.command("list")
def identity_trust_list(
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List configured issuers."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path

    try:
        settings = get_settings()
        resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
        if resolved is None:
            rows: list[dict[str, Any]] = []
        else:
            anchors = load_trust_anchors(resolved)
            rows = [
                {"issuer": item.issuer, "audience": item.audiences(), "jwks_file": item.jwks_file}
                for item in anchors.issuers
            ]
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "identity trust list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("identity trust list", ok=True, issuers=rows))
        return
    if not rows:
        console.print("no trust anchors")
        return
    for row in rows:
        console.print(f"{row['issuer']} aud={row['audience']} jwks={row['jwks_file']}")


@identity_trust_app.command("add")
def identity_trust_add(
    issuer: str = typer.Option(..., "--issuer"),
    jwks_file: Path = typer.Option(..., "--jwks-file"),
    audience: str = typer.Option("readyagents", "--audience"),
    actor_claim: str = typer.Option("sub", "--actor-claim"),
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
) -> None:
    """Append an issuer to the local trust-anchor file. Fail closed on malformed files."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import (
        IssuerAnchor,
        TrustAnchors,
        load_trust_anchors,
        resolve_trust_path,
    )

    settings = get_settings()
    dest = Path(trust_anchors) if trust_anchors is not None else settings.home_path() / "trust.yaml"
    existing = resolve_trust_path(
        explicit=dest if dest.is_file() else None, home=settings.home_path()
    )
    try:
        if existing is not None and existing.is_file():
            anchors = load_trust_anchors(existing)
        else:
            anchors = TrustAnchors()
        anchors.issuers.append(
            IssuerAnchor(
                issuer=issuer,
                audience=audience,
                jwks_file=str(jwks_file),
                actor_claim=actor_claim,
            )
        )
        import yaml

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.safe_dump(anchors.model_dump(exclude={"source"}), sort_keys=False),
            encoding="utf-8",
        )
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    console.print(f"added issuer {issuer} -> {dest}")


@identity_trust_app.command("remove")
def identity_trust_remove(
    issuer: str = typer.Option(..., "--issuer"),
    trust_anchors: Path | None = typer.Option(
        None, "--trust-anchors", envvar="READYAGENTS_TRUST_ANCHORS"
    ),
) -> None:
    """Remove an issuer from the local trust-anchor file."""
    from readyagents.config import get_settings
    from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path

    settings = get_settings()
    resolved = resolve_trust_path(explicit=trust_anchors, home=settings.home_path())
    if resolved is None:
        raise typer.Exit(code=1)
    try:
        anchors = load_trust_anchors(resolved)
        anchors.issuers = [item for item in anchors.issuers if item.issuer != issuer]
        if not anchors.issuers:
            raise IdentityError("refusing to write a trust-anchor file with no issuers")
        import yaml

        resolved.write_text(
            yaml.safe_dump(anchors.model_dump(exclude={"source"}), sort_keys=False),
            encoding="utf-8",
        )
    except ReadyAgentsError as extra:
        _fail(extra)
        return
    console.print(f"removed issuer {issuer}")


@trust_app.command("add")
def trust_add_cmd(
    pubkey: Path = typer.Argument(..., help="Ed25519 public key (PEM, raw, hex, or base64)."),
    name: str = typer.Option(..., "--name", help="Human-meaningful publisher name."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Trust a publisher public key. Fail closed on a malformed keyring."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import add_key

    try:
        entry = add_key(pubkey, name=name, home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust add",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("trust add", ok=True, **entry.as_dict()))
        return
    console.print(f"trusted {entry.name} key_id={entry.key_id}")


@trust_app.command("list")
def trust_list_cmd(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List trusted publisher keys."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import load_keyring

    try:
        ring = load_keyring(home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust list",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    rows = [item.as_dict() for item in ring.keys]
    if as_json:
        _print_json(_json_envelope("trust list", ok=True, keys=rows))
        return
    if not rows:
        console.print("no trusted publishers")
        return
    for row in rows:
        console.print(f"{row['key_id']} name={row['name']} added_at={row['added_at']}")


@trust_app.command("remove")
def trust_remove_cmd(
    key_id: str = typer.Argument(..., help="Key id from `readyagents trust list`."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Remove a trusted publisher key. Fail closed on a malformed keyring."""
    from readyagents.config import get_settings
    from readyagents.trust.keyring import remove_key

    try:
        entry = remove_key(key_id, home=get_settings().home_path())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "trust remove",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        _print_json(_json_envelope("trust remove", ok=True, **entry.as_dict()))
        return
    console.print(f"removed {entry.name} key_id={entry.key_id}")
