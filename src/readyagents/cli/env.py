"""CLI group: env (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _emit_env_error,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import (
    EnvRefused,
    ReadyAgentsError,
)

env_app = typer.Typer(
    help="Declared environments, pinned releases, status/history/diff. Not a hosted deploy.",
    no_args_is_help=True,
)


@env_app.command("status")
def env_status_cmd(
    env: str | None = typer.Option(None, "--env", help="Named environment. Default: all declared."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show what is deployed where. Configuration plus a pin — not a cluster."""
    from readyagents.config import get_settings
    from readyagents.env.schema import load_env_file
    from readyagents.env.store import EnvStore

    try:
        settings = get_settings()
        loaded = load_env_file(settings=settings)
        store = EnvStore(settings)
        declared = list(loaded.environments) if loaded else []
        names = [env] if env else store.list_names(declared)
        rows = [store.status(name) for name in names]
    except EnvRefused as extra:
        _emit_env_error("env status", extra, as_json=as_json)
    if as_json:
        _print_json(_json_envelope("env status", ok=True, environments=rows))
        return
    if not rows:
        console.print("no environments")
        return
    for row in rows:
        cur = (row.get("current") or {}).get("digest") or "(none)"
        console.print(f"{row['environment']} current={cur}")


@env_app.command("history")
def env_history_cmd(
    env: str = typer.Option(..., "--env", help="Named environment."),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Audited deploy / promote / rollback / shadow events for one environment."""
    from readyagents.config import get_settings
    from readyagents.env.store import EnvStore

    try:
        events = EnvStore(get_settings()).history(env, limit=limit)
    except EnvRefused as extra:
        _emit_env_error("env history", extra, as_json=as_json)
    if as_json:
        _print_json(_json_envelope("env history", ok=True, environment=env, events=events))
        return
    if not events:
        console.print("(empty)")
        return
    for row in events:
        digest = row.get("digest") or row.get("release")
        console.print(f"{row.get('ts')} {row.get('event')} {digest}")


@env_app.command("diff")
def env_diff_cmd(
    env: str = typer.Option(..., "--env", help="Named environment."),
    source: str = typer.Option(
        "previous",
        "--from",
        help="Pointer: current, previous, or candidate.",
    ),
    target: str = typer.Option(
        "current",
        "--to",
        help="Pointer: current, previous, or candidate.",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Complete pin diff between two releases in an environment."""
    from readyagents.config import get_settings
    from readyagents.env.release import diff_releases
    from readyagents.env.store import EnvStore

    try:
        settings = get_settings()
        store = EnvStore(settings)
        left = store.pointer_named(env, source)
        right = store.pointer_named(env, target)
        payload = diff_releases(left, right, settings=settings)
        payload["environment"] = env
    except EnvRefused as extra:
        _emit_env_error("env diff", extra, as_json=as_json)
    if as_json:
        _print_json(_json_envelope("env diff", ok=True, **payload))
        return
    console.print(f"{env} {payload.get('from')} -> {payload.get('to')}")
    pins = payload.get("pins") or {}
    if isinstance(pins, dict):
        for key, value in sorted(pins.items()):
            if isinstance(value, dict):
                console.print(f"  {key}: {value.get('from')} -> {value.get('to')}")


@env_app.command("deploy")
def env_deploy_cmd(
    path: Path = _WORKFLOW_ARG,
    env: str = typer.Option(..., "--env", help="Named environment to pin this working copy to."),
    candidate: bool = typer.Option(
        False, "--candidate", help="Set as canary/shadow candidate instead of current."
    ),
    sign_key: Path | None = typer.Option(None, "--sign-key", help="Ed25519 PEM for the manifest."),
    actor: str | None = typer.Option(None, "--actor", help="Actor id.", envvar="READYAGENTS_ACTOR"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Pin a content-addressed release to an environment. Not a hosted deploy."""
    from readyagents.config import get_settings
    from readyagents.env.release import deploy
    from readyagents.env.run import resolve_environment

    try:
        settings = get_settings()
        _file, spec = resolve_environment(env, settings=settings)
        pointer = deploy(
            path,
            env,
            spec=spec,
            settings=settings,
            actor=actor,
            sign_key=sign_key,
            as_candidate=candidate,
        )
    except EnvRefused as extra:
        _emit_env_error("env deploy", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "env deploy", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    if as_json:
        _print_json(_json_envelope("env deploy", ok=True, environment=env, **pointer))
        return
    console.print(f"{env} {pointer.get('digest')} {'candidate' if candidate else 'current'}")
