"""CLI group: authoring (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    NoReturn,
)

import typer

from readyagents.cli._common import (
    _WORKFLOW_ARG,
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import (
    ConfigError,
    ImportRefused,
    ReadyAgentsError,
)
from readyagents.workflow.runner import (
    confine_under,
    load_workflow,
)


def _write_schema_file(dest: Path, text: str, *, force: bool) -> Path:
    from readyagents.config import get_settings

    if dest.exists() and dest.is_dir():
        raise ConfigError(f"Refusing to write schema to directory: {dest}")
    root = get_settings().workspace_path()
    resolved = confine_under(dest, root, what="schema output")
    if resolved.is_dir():
        raise ConfigError(f"Refusing to write schema to directory: {dest}")
    if (resolved.exists() or dest.is_symlink()) and not force:
        raise ConfigError(f"Refusing to overwrite existing file: {dest}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    return resolved


def _check_schema_file(path: Path, generated: str) -> None:
    if path.is_dir():
        raise ConfigError(f"schema --check path is a directory: {path}")
    if not path.is_file():
        raise ConfigError(f"schema file not found: {path}")
    existing = path.read_text(encoding="utf-8")
    if existing == generated:
        return
    import difflib

    diff = difflib.unified_diff(
        existing.splitlines(),
        generated.splitlines(),
        fromfile=str(path),
        tofile="generated",
        lineterm="",
    )
    preview = "\n".join(list(diff)[:80])
    raise ConfigError(f"schema drift: {path} does not match generated output\n{preview}")


def _emit_import_error(command: str, extra: ImportRefused, *, as_json: bool) -> NoReturn:
    if as_json:
        _print_json(
            _json_envelope(
                command,
                ok=False,
                error=type(extra).__name__,
                message=str(extra),
                reason=extra.reason,
            )
        )
        raise typer.Exit(code=1) from extra
    _fail(extra)


def import_cmd(
    source: str | None = typer.Argument(
        None, help="n8n, langgraph, crewai, or trigger (Zapier-shaped JSON)."
    ),
    path: Path | None = typer.Argument(None, help="Operator-exported workflow file."),
    explain: str | None = typer.Option(
        None,
        "--explain",
        help="Print the mapping table for SOURCE and write nothing.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Directory for workflow.yaml and report."),
    report: Path | None = typer.Option(None, "--report", help="Fidelity report path."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing workflow.yaml."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Import an exported workflow. Structural translation only — not equivalence."""
    from readyagents.importers import explain_source, import_workflow

    try:
        if explain:
            table = explain_source(explain)
            rows = [
                {
                    "kind": item.kind,
                    "target": item.target,
                    "fidelity": item.fidelity,
                    "reason": item.reason,
                    "nearest": item.nearest,
                }
                for item in (*table.entries, table.default)
            ]
            if as_json:
                _print_json(
                    _json_envelope(
                        "import explain",
                        ok=True,
                        source=table.source,
                        schema_versions=list(table.schema_versions),
                        entries=rows,
                    )
                )
                return
            console.print(f"{table.source} mapping table (structural translation only)")
            for row in rows:
                console.print(f"  {row['kind']} -> {row['target'] or 'stub'} ({row['fidelity']})")
            return
        if not source or not path:
            raise ImportRefused(
                "usage: readyagents import SOURCE PATH --out DIR",
                reason="usage",
            )
        if out is None:
            out = Path("imported") / source
        result = import_workflow(source, path, out=out, report_path=report, force=force)
    except ImportRefused as extra:
        _emit_import_error("import", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope("import", ok=False, error=type(extra).__name__, message=str(extra))
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
    body = result.report.as_dict()
    if as_json:
        _print_json(
            _json_envelope(
                "import",
                ok=True,
                **body,
                workflow=str(result.workflow_path),
                report=str(result.report_path),
                graph=str(result.graph_path),
                dry_run_ok=result.dry_run_ok,
            )
        )
        return
    console.print(f"wrote {result.workflow_path}")
    console.print(f"report {result.report_path} coverage={result.report.coverage}%")
    console.print("structural translation only — test before use")


def schema_cmd(
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the schema UTF-8 file. Refuses overwrite without --force.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite --output if it exists."),
    as_json: bool = typer.Option(False, "--json", help="Print the standard JSON envelope."),
    check: Path | None = typer.Option(
        None,
        "--check",
        help="Exit 0 if PATH matches the generated schema byte-for-byte.",
    ),
) -> None:
    """Emit the workflow JSON Schema (no network, no workflow execution)."""
    from readyagents.workflow.jsonschema import workflow_json_schema, workflow_json_schema_text

    try:
        text = workflow_json_schema_text()
        schema = workflow_json_schema()
        if check is not None and output is not None:
            raise ConfigError("Use --check or --output, not both.")
        if check is not None:
            _check_schema_file(check, text)
            if as_json:
                _print_json(_json_envelope("schema", ok=True, check=str(check), match=True))
            else:
                console.print(f"[green]schema matches[/green] {check}")
            return
        if output is not None:
            _write_schema_file(output, text, force=force)
            if as_json:
                _print_json(_json_envelope("schema", ok=True, path=str(output), schema=schema))
            else:
                console.print(f"[green]Wrote {output}[/green]")
            return
        if as_json:
            _print_json(_json_envelope("schema", ok=True, schema=schema))
            return
        typer.echo(text, nl=False)
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "schema",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)


def simulate_cmd(
    path: Path = _WORKFLOW_ARG,
    seed: int = typer.Option(42, "--seed", help="Deterministic generator seed."),
    cases: int = typer.Option(64, "--cases", help="Maximum generated cases."),
    deterministic_only: bool = typer.Option(
        False, "--deterministic-only", help="Do not call a model even if --model is set."
    ),
    model: str | None = typer.Option(None, "--model", help="Opt-in model-assisted personas."),
    personas: str | None = typer.Option(
        None, "--personas", help="Comma-separated persona names (with --model)."
    ),
    max_spend: float | None = typer.Option(None, "--max-spend", help="Spend cap for --model."),
    fail_on: str | None = typer.Option(
        None, "--fail-on", help="CI mode: new-failure blocks unseen failure classes."
    ),
    out: Path | None = typer.Option(None, "--out", help="Directory for frozen fixtures."),
    live: bool = typer.Option(
        False,
        "--live-side-effects",
        help="Allow real write_file/http_get. Requires a permitting policy.",
    ),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file for live side effects."),
    sovereign: bool = typer.Option(False, "--sovereign"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Generate declaration-driven cases, score with eval, freeze distinct failures."""
    from readyagents.config import get_settings
    from readyagents.errors import SimulateRefused
    from readyagents.firewall.policy_file import load_resolved
    from readyagents.simulate.run import simulate_workflow

    settings = get_settings()
    loaded = load_resolved(
        explicit=policy, workflow_dir=path.parent if path.is_file() else None, stored=None
    )
    persona_list = [p.strip() for p in (personas or "").split(",") if p.strip()]
    use_model = None if deterministic_only else model
    sov = sovereign or bool(settings.sovereign)
    llm = None
    if use_model and not sov:
        from readyagents.llm.registry import get_provider

        llm, _ = get_provider(use_model, settings=settings)
    try:
        report = simulate_workflow(
            path,
            seed=seed,
            cap=cases,
            out_dir=out,
            fail_on_new=fail_on == "new-failure",
            live_side_effects=live,
            policy=loaded,
            settings=settings,
            llm=llm,
            model=use_model,
            personas=persona_list or None,
            max_spend=max_spend,
            sovereign=sov,
        )
    except SimulateRefused as extra:
        payload = extra.report.as_dict() if getattr(extra, "report", None) is not None else {}
        if as_json:
            _print_json(
                _json_envelope(
                    "simulate",
                    ok=False,
                    error="SimulateRefused",
                    message=str(extra),
                    reason=extra.reason,
                    **payload,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "simulate",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    if as_json:
        _print_json(_json_envelope("simulate", ok=True, **body))
        return
    console.print(
        f"cases={report.cases} passed={report.passed} failed={report.failed} "
        f"clusters={len(report.clusters)} dry_run={report.dry_run}"
    )
    cov = report.coverage
    console.print(
        f"coverage reached={len(cov.get('reached') or [])} "
        f"unreached={len(cov.get('unreached') or [])} declared={cov.get('declared')}"
    )
    if report.new_failures:
        raise typer.Exit(code=1)


def studio_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Non-loopback binds are refused.",
    ),
    port: int = typer.Option(
        8790,
        "--port",
        min=1,
        max=65535,
        help="Bind port (default 8790).",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open",
        help="Open the loopback URL in a browser. The bootstrap token stays on stderr.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Disable every write path server-side (save, fork, freeze, decide).",
    ),
    actor: str | None = typer.Option(None, "--actor", envvar="READYAGENTS_ACTOR"),
) -> None:
    """Foreground localhost workflow studio. Stops when this process stops."""
    try:
        from readyagents.studio.server import serve_studio

        serve_studio(
            host=host,
            port=port,
            open_browser=open_browser,
            read_only=read_only,
            actor=actor,
        )
    except ReadyAgentsError as exc:
        _fail(exc)


def graph_cmd(
    path: Path = _WORKFLOW_ARG,
    direction: str = typer.Option("LR", "--direction", help="LR or TD."),
    output: Path | None = typer.Option(None, "--output", "--out", help="Write Mermaid to a file."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Deterministic Mermaid routing diagram. Executes nothing."""
    from readyagents.compliance.graph import render_mermaid
    from readyagents.config import get_settings

    try:
        spec = load_workflow(path)
        mermaid = render_mermaid(spec, direction=direction)
        if output is not None:
            dest = confine_under(output, get_settings().workspace_path(), what="graph output")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(mermaid, encoding="utf-8")
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "graph",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload: dict[str, Any] = {"mermaid": mermaid, "workflow": spec.name}
        if output is not None:
            payload["path"] = str(output)
        _print_json(_json_envelope("graph", ok=True, **payload))
        return
    console.print(mermaid, end="")


def register_import(app: typer.Typer) -> None:
    app.command("import", rich_help_panel="Extras: Authoring")(import_cmd)


def register_schema(app: typer.Typer) -> None:
    app.command("schema", rich_help_panel="Extras: Authoring")(schema_cmd)


def register_simulate(app: typer.Typer) -> None:
    app.command("simulate", rich_help_panel="Extras: Authoring")(simulate_cmd)


def register_studio(app: typer.Typer) -> None:
    app.command("studio", rich_help_panel="Extras: Authoring")(studio_cmd)


def register_graph(app: typer.Typer) -> None:
    app.command("graph", rich_help_panel="Extras: Authoring")(graph_cmd)
