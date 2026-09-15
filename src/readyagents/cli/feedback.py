"""CLI group: feedback (split from cli.py; see H-05)."""

from __future__ import annotations

from pathlib import Path

import typer

from readyagents.cli._common import (
    _fail,
    _json_envelope,
    _print_json,
    console,
)
from readyagents.errors import ReadyAgentsError

feedback_app = typer.Typer(
    help="Export consented corrections. Production data in a portable file. No hosted dataset.",
    no_args_is_help=True,
)


@feedback_app.command("export")
def feedback_export_cmd(
    fmt: str = typer.Option("eval", "--format", help="eval (default), sft, or dpo."),
    out: Path = typer.Option(..., "--out", help="Destination file under the workspace."),
    scope: str | None = typer.Option(None, "--scope", help="Recorded consent scope to include."),
    node: str | None = typer.Option(None, "--node"),
    since: str | None = typer.Option(None, "--since"),
    min_rating: int | None = typer.Option(None, "--min-rating"),
    yes: bool = typer.Option(False, "--yes", help="Acknowledge the production-data warning."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Export consented corrections. Unconsented runs are excluded from every format."""
    from readyagents.audit import audit_dir_for, make_auditor
    from readyagents.config import get_settings
    from readyagents.errors import FeedbackRefused
    from readyagents.feedback.export import export_feedback
    from readyagents.policy import Redactor
    from readyagents.replay.record import known_secret_values

    settings = get_settings()
    console.print("Warning: an export is production data in a portable file.")
    if not yes:
        typer.confirm("Write the export?", abort=True)
    try:
        report = export_feedback(
            settings=settings,
            dest=out,
            fmt=fmt,
            scope=scope,
            node=node,
            since=since,
            min_rating=min_rating,
            yes=yes,
            secrets=known_secret_values(settings),
            redactor=Redactor(literals=known_secret_values(settings)),
            auditor=make_auditor(audit_dir_for(settings.home_path())),
        )
    except FeedbackRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback export",
                    ok=False,
                    error="FeedbackRefused",
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
                    "feedback export",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    ok = bool(body.pop("ok", True))
    if as_json:
        _print_json(_json_envelope("feedback export", ok=ok, **body))
        return
    console.print(
        f"export format={report.format} written={report.written} excluded={report.excluded}"
    )


@feedback_app.command("stats")
def feedback_stats_cmd(
    by: str = typer.Option("node", "--by", help="node, model, label, or week."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Correction rates with sample sizes. Does not imply statistical significance."""
    from readyagents.config import get_settings
    from readyagents.errors import FeedbackRefused
    from readyagents.feedback.stats import feedback_stats

    try:
        report = feedback_stats(settings=get_settings(), by=by)
    except FeedbackRefused as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "feedback stats",
                    ok=False,
                    error="FeedbackRefused",
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
                    "feedback stats", ok=False, error=type(extra).__name__, message=str(extra)
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    body = report.as_dict()
    ok = bool(body.pop("ok", True))
    if as_json:
        _print_json(_json_envelope("feedback stats", ok=ok, **body))
        return
    console.print(f"stats by={report.by} n={report.sample_size} (no significance claim)")
    for row in report.rows:
        console.print(
            f"{row.get(report.by)} n={row['n']} rate={row['correction_rate']} significance=None"
        )
