"""CLI group: bench (split from cli.py; see H-05)."""

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
from readyagents.workflow.state import parse_input_pairs

bench_app = typer.Typer(
    help="Offline-by-default benchmark suite. Not a model-quality claim.",
    no_args_is_help=True,
)


@bench_app.command("run")
def bench_run_cmd(
    suite: Path | None = typer.Option(None, "--suite", help="Suite YAML (default examples/bench)."),
    offline: bool = typer.Option(
        False, "--offline", help="Cassette replay (default). Engine timing, not provider latency."
    ),
    live: bool = typer.Option(
        False, "--live", help="Opt-in live end-to-end. Metered. Off by default."
    ),
    allow_ci_live: bool = typer.Option(
        False, "--allow-ci-live", help="Required with --live when CI=true."
    ),
    scenarios: str | None = typer.Option(
        None, "--scenarios", help="Comma-separated scenario names."
    ),
    max_spend: float | None = typer.Option(None, "--max-spend", help="Live spend cap in USD."),
    model: str | None = typer.Option(None, "--model", help="Optional model label for comparison."),
    out: Path | None = typer.Option(None, "--out", help="Write the JSON result document."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the scenario suite offline (default) or live. Zero cost offline."""
    from readyagents.bench.run import run_bench
    from readyagents.config import get_settings
    from readyagents.errors import BenchRefused

    names = [part.strip() for part in (scenarios or "").split(",") if part.strip()]
    try:
        if live and offline:
            raise BenchRefused("use --offline or --live, not both", reason="mode")
        report = run_bench(
            suite,
            live=live,
            allow_ci_live=allow_ci_live,
            settings=get_settings(),
            scenarios=names or None,
            max_spend=max_spend,
            model=model,
        )
    except BenchRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench run",
                    ok=False,
                    error="BenchRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench run", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    if out is not None:
        out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if as_json:
        _print_json(_json_envelope("bench run", ok=True, **body))
        return
    console.print(report.markdown())


@bench_app.command("compare")
def bench_compare_cmd(
    current: Path | None = typer.Argument(None, help="Current results JSON from bench run."),
    baseline: Path | None = typer.Option(None, "--baseline", help="Committed baseline JSON."),
    tolerance: float | None = typer.Option(
        None, "--tolerance", help="Wall-clock percent tolerance (optional override)."
    ),
    models: str | None = typer.Option(None, "--models", help="Comma-separated model refs."),
    workflows: str | None = typer.Option(
        None, "--workflows", help="Comma-separated workflow paths."
    ),
    scenario: str | None = typer.Option(None, "--scenario", help="Scenario name for --models."),
    suite: Path | None = typer.Option(None, "--suite"),
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="Shared KEY=VALUE for --workflows (repeatable).",
    ),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare a result document to a baseline, or models/workflows like-for-like."""
    from readyagents.bench.compare import (
        compare_models,
        compare_results,
        compare_workflows,
        load_baseline,
    )
    from readyagents.config import get_settings
    from readyagents.errors import BenchRefused

    try:
        if models:
            names = [part.strip() for part in models.split(",") if part.strip()]
            payload = compare_models(
                names,
                suite=suite,
                scenario=scenario,
                settings=get_settings(),
            )
            ok = bool(payload.pop("ok"))
            if as_json:
                _print_json(_json_envelope("bench compare", ok=ok, **payload))
            else:
                console.print(f"models {', '.join(names)} on identical inputs ok={ok}")
            if not ok:
                raise typer.Exit(code=1)
            return
        if workflows:
            paths = [part.strip() for part in workflows.split(",") if part.strip()]
            payload = compare_workflows(
                paths,
                inputs=parse_input_pairs(inputs),
                settings=get_settings(),
            )
            ok = bool(payload.pop("ok"))
            if as_json:
                _print_json(_json_envelope("bench compare", ok=ok, **payload))
            else:
                console.print(f"workflows {', '.join(paths)} on identical inputs ok={ok}")
            if not ok:
                raise typer.Exit(code=1)
            return
        if current is None or baseline is None:
            raise BenchRefused("bench compare needs results.json and --baseline", reason="args")
        data = json.loads(Path(current).read_text(encoding="utf-8"))
        base = load_baseline(baseline)
        extra_tol = {"wall_ms_pct": tolerance} if tolerance is not None else None
        report = compare_results(data, base, tolerance=extra_tol)
    except BenchRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench compare",
                    ok=False,
                    error="BenchRefused",
                    message=str(extra),
                    reason=extra.reason,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "bench compare", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    if as_json:
        payload = report.as_dict()
        payload.pop("ok", None)
        _print_json(_json_envelope("bench compare", ok=report.ok, **payload))
    else:
        if report.ok:
            console.print("compare ok")
        else:
            for line in report.regressions:
                console.print(f"[red]{line}[/red]")
    if not report.ok:
        raise typer.Exit(code=1)
