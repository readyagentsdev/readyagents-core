"""CLI group: run (split from cli.py; see H-05)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from readyagents.cli._common import (
    _PACK_HELP,
    _WORKFLOW_ARG,
    _emit_env_error,
    _emit_run,
    _emit_run_exception,
    _fail,
    _feedback_payload,
    _json_envelope,
    _print_json,
    console,
    err_console,
)
from readyagents.errors import (
    ApprovalRequired,
    ConverseRequired,
    EnvRefused,
    ReadyAgentsError,
    WaitingRequired,
)
from readyagents.logging import configure_logging
from readyagents.packs.loader import collect_pack_specs
from readyagents.workflow.runner import (
    resume_run,
    run_workflow_file,
)
from readyagents.workflow.state import (
    build_decisions,
    parse_input_pairs,
)


def _emit_estimate(path: Path, *, inputs: dict[str, Any], as_json: bool) -> None:
    from readyagents.config import get_settings
    from readyagents.cost.estimate import estimate_workflow_file

    try:
        result = estimate_workflow_file(
            path,
            inputs=inputs or None,
            default_model=get_settings().default_model,
        )
    except ReadyAgentsError as extra:
        if as_json:
            _print_json(
                _json_envelope(
                    "run",
                    ok=False,
                    error=type(extra).__name__,
                    message=str(extra),
                    estimate=True,
                )
            )
            raise typer.Exit(code=1) from extra
        _fail(extra)
        return
    payload = result.as_dict()
    if as_json:
        _print_json(_json_envelope("run", ok=True, **payload))
        return
    floor_usd = payload["floor_cost_usd"]
    ceil_usd = payload["ceiling_cost_usd"]
    floor_s = "unpriced" if floor_usd is None else f"${floor_usd:.6f}"
    ceil_s = "unpriced" if ceil_usd is None else f"${ceil_usd:.6f}"
    console.print(
        f"estimate: {result.floor_tokens}–{result.ceiling_tokens} tokens  {floor_s}–{ceil_s}"
    )
    if result.unpriced:
        models = ", ".join(result.unpriced_models) or "unknown"
        console.print(f"unpriced models (not $0): {models}")
    console.print("assumptions:")
    for item in result.assumptions:
        console.print(f"  - {item}")
    raise typer.Exit(code=0)


def run(
    path: Path = _WORKFLOW_ARG,
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-i",
        help="Input as KEY=VALUE (repeatable).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Walk the graph without calling an LLM, http_get, or write_file.",
    ),
    no_persist: bool = typer.Option(False, "--no-persist", help="Do not write a run record."),
    approve: list[str] = typer.Option(
        [],
        "--approve",
        help="Approve an approval node by id (repeatable).",
    ),
    reject: list[str] = typer.Option(
        [],
        "--reject",
        help="Reject an approval node by id (repeatable).",
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="Resume this run id instead of starting a new run.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the run record as JSON on stdout (no tables).",
    ),
    log_level: str | None = typer.Option(
        None,
        "--log-level",
        help="DEBUG, INFO, WARNING, or ERROR (same as the root flag).",
    ),
    log_format: str | None = typer.Option(
        None,
        "--log-format",
        help="text or json (same as the root flag).",
    ),
    decision_file: Path | None = typer.Option(
        None,
        "--decision-file",
        help="JSON file injecting approval decisions (not only --approve flags).",
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Actor id for RBAC hooks (env: READYAGENTS_ACTOR).",
        envvar="READYAGENTS_ACTOR",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Skip the local LLM response cache for this run.",
    ),
    pack: list[str] = typer.Option([], "--pack", help=_PACK_HELP),
    record: bool = typer.Option(
        False,
        "--record",
        help="Write a content-addressed cassette (prompts and completions). Opt-in.",
        envvar="READYAGENTS_RECORD",
    ),
    policy: Path | None = typer.Option(
        None,
        "--policy",
        help="Firewall policy file (env: READYAGENTS_POLICY).",
        envvar="READYAGENTS_POLICY",
    ),
    estimate: bool = typer.Option(
        False,
        "--estimate",
        help="Print a spend range and exit. No node execution, no network.",
    ),
    max_spend: float | None = typer.Option(
        None,
        "--max-spend",
        help="Hard USD cap consulted before each model call.",
    ),
    max_tokens: int | None = typer.Option(
        None,
        "--max-tokens",
        help="Hard token cap consulted before each model call.",
    ),
    label: list[str] = typer.Option(
        [],
        "--label",
        help="Attribution label KEY=VALUE (repeatable). Stored on the run and ledger.",
    ),
    override_budget: bool = typer.Option(
        False,
        "--override-budget",
        help="Start even when the preflight estimate exceeds a cap (audited).",
    ),
    max_model_calls: int | None = typer.Option(
        None,
        "--max-model-calls",
        help="Runaway guard: maximum LLM complete() attempts.",
    ),
    max_run_tool_rounds: int | None = typer.Option(
        None,
        "--max-run-tool-rounds",
        help="Runaway guard: maximum agent tool rounds across the run.",
    ),
    max_wall_seconds: float | None = typer.Option(
        None,
        "--max-wall-seconds",
        help="Runaway guard: maximum wall-clock seconds.",
    ),
    require_signed: bool = typer.Option(
        False,
        "--require-signed",
        help="Refuse unsigned or untrusted workflow and pack artifacts.",
    ),
    frozen: bool = typer.Option(
        False,
        "--frozen",
        help="Refuse to run when readyagents.lock digests do not match.",
    ),
    sovereign: bool = typer.Option(
        False,
        "--sovereign",
        help="Refuse non-loopback egress at the socket boundary (env: READYAGENTS_SOVEREIGN).",
        envvar="READYAGENTS_SOVEREIGN",
    ),
    sovereign_allow: list[str] = typer.Option(
        [],
        "--sovereign-allow",
        help="Private endpoint allowlisted under --sovereign (repeatable).",
    ),
    stream_flag: bool = typer.Option(
        False,
        "--stream",
        help="Emit incremental run events (tokens, partials, node start/finish).",
    ),
    edit: str | None = typer.Option(None, "--edit", help="Edited output for a feedback gate."),
    rating: int | None = typer.Option(None, "--rating", help="Declared rating on a feedback gate."),
    feedback_label: str | None = typer.Option(
        None, "--feedback-label", help="Declared taxonomy label on a feedback gate."
    ),
    env: str | None = typer.Option(
        None,
        "--env",
        help="Run the pinned release for this declared environment, not the working copy.",
    ),
) -> None:
    """Execute a workflow."""
    if log_level or log_format:
        configure_logging(log_level or "INFO", **({"fmt": log_format} if log_format else {}))
    persist = not no_persist
    try:
        parsed = parse_input_pairs(inputs)
        decisions = build_decisions(approve, reject)
        pack_specs = collect_pack_specs(pack)
        from readyagents.cost.ledger import parse_labels

        labels = parse_labels(label) if label else None
        feedback = _feedback_payload(list(decisions), edit, rating, feedback_label)
        if env and resume:
            raise EnvRefused(
                "resume a paused run with readyagents resume; --env starts from the pin",
                reason="resume",
            )
        if estimate:
            if env:
                from readyagents.env.run import pinned_workflow_for

                path = pinned_workflow_for(env)
            _emit_estimate(path, inputs=parsed, as_json=as_json)
            return
        session = None
        if stream_flag:
            from readyagents.workflow.stream import StreamSession

            def _write_stream(line: str) -> None:
                if as_json:
                    typer.echo(line, nl=False)
                else:
                    err_console.print(line.rstrip("\n"), markup=False)

            session = StreamSession(ndjson=as_json, write=_write_stream)
        if env:
            from readyagents.env.run import run_in_environment

            state = run_in_environment(
                path,
                env,
                inputs=parsed,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                record=record,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
        elif resume:
            state = resume_run(
                resume,
                path=path,
                inputs=parsed or None,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
        else:
            state = run_workflow_file(
                path,
                inputs=parsed,
                dry_run=dry_run,
                persist=persist,
                pack_specs=pack_specs,
                decisions=decisions,
                decision_file=decision_file,
                actor=actor,
                no_cache=no_cache,
                record=record,
                policy=policy,
                max_spend=max_spend,
                max_tokens_cap=max_tokens,
                labels=labels,
                override_budget=override_budget,
                max_model_calls=max_model_calls,
                max_run_tool_rounds=max_run_tool_rounds,
                max_wall_seconds=max_wall_seconds,
                require_signed=require_signed,
                frozen=frozen,
                sovereign=sovereign,
                sovereign_allow=sovereign_allow,
                stream=session,
                feedback=feedback,
            )
    except KeyboardInterrupt:
        if stream_flag and as_json:
            typer.echo(
                json.dumps({"event": "run.cancelled", "status": "cancelled"}) + "\n",
                nl=False,
            )
            raise typer.Exit(code=1) from None
        if as_json:
            _print_json(_json_envelope("run", ok=False, error="cancelled", status="cancelled"))
        else:
            err_console.print("[yellow]cancelled[/yellow]")
        raise typer.Exit(code=1) from None
    except EnvRefused as extra:
        _emit_env_error("run", extra, as_json=as_json)
    except ReadyAgentsError as extra:
        if stream_flag and as_json:
            payload = {
                "event": "run.finished",
                "status": (
                    "paused"
                    if type(extra).__name__ in {"ApprovalRequired", "ConverseRequired"}
                    else "failed"
                ),
                "error": type(extra).__name__,
            }
            rid = getattr(extra, "run_id", None)
            if rid:
                payload["run_id"] = rid
            typer.echo(json.dumps(payload) + "\n", nl=False)
            if isinstance(extra, (ApprovalRequired, WaitingRequired, ConverseRequired)):
                raise typer.Exit(code=2) from extra
            raise typer.Exit(code=1) from extra
        _emit_run_exception(extra, as_json=as_json, persist=persist, command="run")
    if stream_flag and as_json:
        if state.status != "succeeded":
            raise typer.Exit(code=2 if state.status in {"paused", "waiting"} else 1)
        return
    _emit_run(state, as_json=as_json, command="run")


def register_run(app: typer.Typer) -> None:
    app.command(rich_help_panel="Core")(run)
