"""Persist every iteration so an interrupted run resumes without re-spend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import OptimizeRefused
from readyagents.optimize.layout import SCHEMA_RUN
from readyagents.optimize.record import CandidateRecord, IterationRecord
from readyagents.prompts.layout import optimize_state_path


def load_state(workflow: Path | str) -> dict[str, Any] | None:
    path = optimize_state_path(workflow)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OptimizeRefused(f"malformed optimize state: {exc}", reason="resume") from exc
    if not isinstance(data, dict):
        raise OptimizeRefused("optimize state must be a mapping", reason="resume")
    schema = data.get("schema")
    if schema and schema != SCHEMA_RUN:
        raise OptimizeRefused(f"unknown optimize state schema {schema!r}", reason="resume")
    return data


def save_state(workflow: Path | str, payload: dict[str, Any]) -> Path:
    path = optimize_state_path(workflow)
    body = dict(payload)
    body["schema"] = SCHEMA_RUN
    atomic_write_text(path, json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return path


def iterations_from_state(data: dict[str, Any]) -> list[IterationRecord]:
    rows = []
    for raw in data.get("iterations") or []:
        if not isinstance(raw, dict):
            continue
        cands = []
        for item in raw.get("candidates") or []:
            if not isinstance(item, dict):
                continue
            cands.append(
                CandidateRecord(
                    prompt_id=str(item.get("prompt_id") or ""),
                    version=int(item.get("version") or 0),
                    content_hash=str(item.get("content_hash") or ""),
                    text=str(item.get("text") or ""),
                    train_score=float(item.get("train_score") or 0.0),
                    held_out_score=(
                        None
                        if item.get("held_out_score") is None
                        else float(item.get("held_out_score"))
                    ),
                    regressions=list(item.get("regressions") or []),
                    adopted=bool(item.get("adopted")),
                    config_shaped=bool(item.get("config_shaped")),
                )
            )
        rows.append(
            IterationRecord(
                index=int(raw.get("index") or 0),
                train=dict(raw.get("train") or {}),
                held_out=dict(raw.get("held_out") or {}),
                spend_usd=float(raw.get("spend_usd") or 0.0),
                candidates=cands,
                promoted=bool(raw.get("promoted")),
                regressions=list(raw.get("regressions") or []),
                blocked_cases=list(raw.get("blocked_cases") or []),
            )
        )
    return rows
