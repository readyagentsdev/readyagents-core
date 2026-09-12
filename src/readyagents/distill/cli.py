"""Typer app for `readyagents distill`. Core never trains."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from readyagents.errors import DistillRefused

distill_app = typer.Typer(
    help=(
        "Plan, dataset, pack-train, evaluate, and promote a local adapter. "
        "Core never trains. Not a quality claim beyond your fixtures."
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


def _emit_error(command: str, extra: BaseException, *, as_json: bool) -> NoReturn:
    if as_json:
        body: dict[str, Any] = {"error": type(extra).__name__, "message": str(extra)}
        reason = getattr(extra, "reason", None)
        if reason is not None:
            body["reason"] = reason
        comparison = getattr(extra, "comparison", None)
        if comparison is not None:
            body["comparison"] = comparison
        _print_json(_envelope(command, ok=False, **body))
        raise typer.Exit(code=1) from extra
    err_console.print(f"[red]{type(extra).__name__}:[/red] {extra}")
    raise typer.Exit(code=1) from extra


@distill_app.command("plan")
def distill_plan_cmd(
    node: str = typer.Option(..., "--node", help="Node id to inspect."),
    workflow: str | None = typer.Option(None, "--workflow"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Honest viability verdict from recorded runs and feedback."""
    from readyagents.config import get_settings
    from readyagents.distill.plan import plan

    try:
        report = plan(node, settings=get_settings(), workflow=workflow)
    except DistillRefused as extra:
        _emit_error("distill plan", extra, as_json=as_json)
    body = report.model_dump(mode="python")
    if as_json:
        _print_json(_envelope("distill plan", ok=True, **body))
        return
    console.print(f"{report.verdict} examples={report.examples} {report.reason}")


@distill_app.command("dataset")
def distill_dataset_cmd(
    node: str = typer.Option(..., "--node"),
    out: Path = typer.Option(..., "--out"),
    seed: int = typer.Option(1, "--seed"),
    split: str = typer.Option("80/10/10", "--split"),
    yes: bool = typer.Option(False, "--yes"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Build consented, re-redacted, hashed train/validation/holdout splits."""
    from readyagents.config import get_settings
    from readyagents.distill.dataset import build_dataset
    from readyagents.errors import ConfigError, PathError

    parts = [p.strip() for p in str(split).replace(":", "/").split("/") if p.strip()]
    if len(parts) != 3:
        extra = DistillRefused(
            "split must be train/validation/holdout like 80/10/10", reason="split"
        )
        _emit_error("distill dataset", extra, as_json=as_json)
    ratios = tuple(int(p) / 100.0 if float(p) > 1 else float(p) for p in parts)
    try:
        if not yes:
            err_console.print(
                "[yellow]Warning:[/yellow] dataset files are a lossy copy of production behaviour."
            )
        manifest = build_dataset(
            node,
            out,
            settings=get_settings(),
            seed=seed,
            split=ratios,  # type: ignore[arg-type]
            yes=yes,
        )
    except (DistillRefused, PathError, ConfigError) as extra:
        _emit_error("distill dataset", extra, as_json=as_json)
    if as_json:
        _print_json(
            _envelope(
                "distill dataset", ok=True, **manifest.model_dump(mode="python", by_alias=True)
            )
        )
        return
    console.print(f"dataset hash={manifest.hash} holdout={manifest.counts.get('holdout')}")


@distill_app.command("train")
def distill_train_cmd(
    dataset: Path = typer.Option(..., "--dataset"),
    base: str = typer.Option(
        ..., "--base", help="Operator-provided base model. Never auto-downloaded."
    ),
    node: str | None = typer.Option(None, "--node"),
    hosted: bool = typer.Option(
        False, "--hosted", help="Provider-hosted tuning. Refused in sovereign mode."
    ),
    sign_key: Path | None = typer.Option(None, "--sign-key"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Orchestrate pack training. Core never trains; absent pack is a typed error."""
    from readyagents.config import get_settings
    from readyagents.distill.train import train
    from readyagents.errors import DistillSovereignHosted, DistillTrainMissing

    settings = get_settings()
    if hosted and not getattr(settings, "sovereign", False):
        err_console.print(
            "[yellow]Warning:[/yellow] provider-hosted tuning sends the dataset off this machine."
        )
    try:
        record = train(
            dataset,
            base=base,
            settings=settings,
            sign_key=sign_key,
            hosted=hosted,
            node_id=node,
        )
    except (DistillRefused, DistillTrainMissing, DistillSovereignHosted) as extra:
        _emit_error("distill train", extra, as_json=as_json)
    if as_json:
        _print_json(
            _envelope(
                "distill train", ok=True, adapter=record.model_dump(mode="python", by_alias=True)
            )
        )
        return
    console.print(f"adapter {record.id} digest={record.digest}")


@distill_app.command("evaluate")
def distill_evaluate_cmd(
    adapter: str | None = typer.Option(None, "--adapter"),
    against: Path = typer.Option(..., "--against", help="Frozen eval suite."),
    dataset: Path = typer.Option(..., "--dataset"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Like-for-like comparison. Holdout score is mandatory."""
    from readyagents.config import get_settings
    from readyagents.distill.evaluate import evaluate
    from readyagents.distill.store import load_adapter
    from readyagents.distill.train import collect_tuner

    try:
        settings = get_settings()
        artifact = None
        if adapter:
            artifact = load_adapter(adapter, settings).path
        report = evaluate(
            suite=against,
            dataset=dataset,
            adapter=artifact,
            tuner=collect_tuner(),
            settings=settings,
        )
    except DistillRefused as extra:
        _emit_error("distill evaluate", extra, as_json=as_json)
    if as_json:
        _print_json(
            _envelope(
                "distill evaluate", ok=True, **report.model_dump(mode="python", by_alias=True)
            )
        )
        return
    console.print(
        f"holdout incumbent={report.incumbent.holdout} candidate={report.candidate.holdout}"
    )


@distill_app.command("promote")
def distill_promote_cmd(
    adapter: str = typer.Option(..., "--adapter"),
    node: str = typer.Option(..., "--node"),
    against: Path | None = typer.Option(None, "--against"),
    dataset: Path | None = typer.Option(None, "--dataset"),
    approve: bool = typer.Option(False, "--approve"),
    incumbent: str | None = typer.Option(None, "--incumbent"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Threshold-gated promotion onto one node. Falls back to the incumbent on failure."""
    from readyagents.config import get_settings
    from readyagents.distill.promote import promote

    try:
        record = promote(
            adapter,
            node,
            settings=get_settings(),
            suite=against,
            dataset=dataset,
            approve=approve,
            incumbent=incumbent,
        )
    except DistillRefused as extra:
        _emit_error("distill promote", extra, as_json=as_json)
    if as_json:
        _print_json(
            _envelope(
                "distill promote", ok=True, adapter=record.model_dump(mode="python", by_alias=True)
            )
        )
        return
    console.print(f"promoted {record.id} -> node {node}")


adapters_app = typer.Typer(
    help="Signed local adapters. Not a hosted model catalog.",
    no_args_is_help=True,
)


@adapters_app.command("list")
def adapters_list_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    from readyagents.config import get_settings
    from readyagents.distill.adapters import catalog

    rows = catalog(get_settings())
    if as_json:
        _print_json(_envelope("models adapters list", ok=True, adapters=rows, count=len(rows)))
        return
    if not rows:
        console.print("no adapters")
        return
    for row in rows:
        console.print(f"{row['id']} node={row.get('node_id')} status={row.get('status')}")


@adapters_app.command("show")
def adapters_show_cmd(
    adapter_id: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.config import get_settings
    from readyagents.distill.adapters import show_adapter

    try:
        row = show_adapter(adapter_id, settings=get_settings())
    except DistillRefused as extra:
        _emit_error("models adapters show", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("models adapters show", ok=True, adapter=row))
        return
    console.print(f"{row['id']} signed={row.get('signed')} digest={row.get('digest')}")


@adapters_app.command("remove")
def adapters_remove_cmd(
    adapter_id: str = typer.Argument(...),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from readyagents.config import get_settings
    from readyagents.distill.adapters import remove_adapter

    try:
        remove_adapter(adapter_id, settings=get_settings())
    except DistillRefused as extra:
        _emit_error("models adapters remove", extra, as_json=as_json)
    if as_json:
        _print_json(_envelope("models adapters remove", ok=True, adapter_id=adapter_id))
        return
    console.print(f"removed {adapter_id}")
