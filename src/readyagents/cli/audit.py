"""CLI group: audit (split from cli.py; see H-05)."""

import json
from pathlib import Path

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import (
    ConfigError,
    ReadyAgentsError,
)
from readyagents.workflow.runner import (
    confine_under,
    load_workflow,
)

audit_app = typer.Typer(
    help="Inspect the append-only hash-chained audit trail.", no_args_is_help=True
)


def attest_cmd(
    run_id: str = typer.Argument(..., help="Persisted run id (or unique prefix)."),
    out: Path | None = typer.Option(None, "--out", help="Write JSON to this path."),
    as_json: bool = typer.Option(False, "--json"),
    sign: bool = typer.Option(False, "--sign", help="Detached Ed25519 beside the JSON."),
    key: Path | None = typer.Option(None, "--key", help="Ed25519 private key for --sign."),
) -> None:
    """Write a data-residency attestation. Technical evidence, not legal compliance."""
    from readyagents.config import get_settings
    from readyagents.run_store import open_run_store
    from readyagents.sovereign.attest import build_attestation, dump_attestation, sign_attestation

    try:
        settings = get_settings()
        store = open_run_store(settings)
        try:
            state = store.get(run_id, allow_prefix=True).state
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                closer()
        payload = build_attestation(state, mcp_names=list(state.metadata.get("mcp_servers") or []))
        text = dump_attestation(payload)
        dest = out
        if dest is not None:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        if sign:
            if key is None:
                raise ConfigError("--sign requires --key")
            sig = sign_attestation(payload, key=key)
            sig_path = (dest or Path(f"attest-{state.run_id}.json")).with_suffix(".json.sig")
            sig_path.write_text(json.dumps(sig, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            payload = dict(payload)
            payload["signature_path"] = str(sig_path)
        if as_json:
            extra = dict(payload)
            if dest is not None:
                extra["path"] = str(dest)
            _print_json(_json_envelope("attest", ok=True, **extra))
            return
        if dest is not None:
            console.print(f"wrote {dest}")
        else:
            console.print(text, markup=False, end="")
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("attest", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


@audit_app.command("verify")
def audit_verify_cmd(
    file: Path | None = typer.Option(None, "--file", help="One JSONL audit file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Walk the hash chain. Exit 0 if no break; unchained ranges are reported, not failed."""
    from readyagents.audit import verify_audit_dir, verify_audit_file
    from readyagents.config import get_settings

    try:
        if file is not None:
            reports = [verify_audit_file(file)]
        else:
            reports = verify_audit_dir(get_settings().audit_dir())
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "audit verify",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    ok = all(item.ok for item in reports)
    payload = {
        "files": [item.as_dict() for item in reports],
        "total": sum(item.total for item in reports),
        "chained": sum(item.chained for item in reports),
        "unchained": sum(item.unchained for item in reports),
        "first_break": next(
            (item.first_break for item in reports if item.first_break is not None), None
        ),
        "first_break_reason": next(
            (item.first_break_reason for item in reports if item.first_break_reason), None
        ),
    }
    if as_json:
        if file is not None and reports:
            body = reports[0].as_dict()
            _print_json(_json_envelope("audit verify", **body))
        else:
            _print_json(_json_envelope("audit verify", ok=ok, **payload))
    else:
        status = "ok" if ok else "BREAK"
        console.print(
            f"audit verify {status}: files={len(reports)} chained={payload['chained']} "
            f"unchained={payload['unchained']}"
        )
        if payload["first_break_reason"]:
            err_console.print(f"[red]{payload['first_break_reason']}[/red]")
    if not ok:
        raise typer.Exit(code=1)


def evidence_cmd(
    run_id: str = typer.Argument(..., help="Run id (or unique prefix)."),
    out: Path | None = typer.Option(None, "--out", help="Output directory."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing pack."),
    sign: bool = typer.Option(False, "--sign", help="Detached HMAC of manifest.json."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Write a local evidence pack. Evidence, not compliance or certification."""
    from readyagents.compliance.evidence import write_evidence_pack
    from readyagents.config import get_settings
    from readyagents.policy import Redactor
    from readyagents.run_store import open_run_store

    settings = get_settings()
    store = open_run_store(settings)
    try:
        try:
            state = store.get(run_id, allow_prefix=True).state
        except ReadyAgentsError as extra:
            if as_json:
                _print_json(
                    _json_envelope(
                        "evidence",
                        ok=False,
                        error=type(extra).__name__,
                        message=str(extra),
                    )
                )
                raise typer.Exit(code=1) from extra
            _fail(extra)
            return
    finally:
        store.close()
    dest = out or Path(f"evidence-{state.run_id}")
    try:
        resolved = confine_under(dest, settings.workspace_path(), what="evidence pack")
        source = state.metadata.get("source")
        workflow = None
        workflow_text = ""
        if source:
            src_path = Path(str(source))
            if src_path.is_file():
                workflow_text = src_path.read_text(encoding="utf-8")
                workflow = load_workflow(src_path)
        redactor = Redactor(
            patterns=settings.redact_pattern_list(),
            literals=settings.redact_literal_list(),
        )
        secret = settings.decision_secret if sign else None
        if sign and not secret:
            raise ConfigError("READYAGENTS_DECISION_SECRET is required for --sign")
        pack = write_evidence_pack(
            resolved,
            state=state,
            workflow=workflow,
            workflow_text=workflow_text,
            audit_dir=settings.audit_dir(),
            redactor=redactor,
            force=force,
            sign_secret=secret,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "evidence",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    err_console.print(
        "[yellow]Warning:[/yellow] evidence packs may contain recorded model prompts and outputs."
    )
    if as_json:
        _print_json(_json_envelope("evidence", ok=True, run_id=state.run_id, path=str(pack)))
        return
    console.print(f"Wrote evidence pack {pack}")


def register_attest(app: typer.Typer) -> None:
    app.command("attest", rich_help_panel="Extras: Governance and audit")(attest_cmd)


def register_evidence(app: typer.Typer) -> None:
    app.command("evidence", rich_help_panel="Extras: Governance and audit")(evidence_cmd)
