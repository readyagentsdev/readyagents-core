"""Export eval/sft/dpo datasets. Consent and redaction are the design."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.errors import FeedbackRefused
from readyagents.feedback.collect import collect_corrections
from readyagents.feedback.consent import permits
from readyagents.feedback.diff import apply_diff
from readyagents.feedback.layout import DEFAULT_FORMAT, FORMATS, HUMAN, IDENTITY_KEYS
from readyagents.feedback.record import Correction, ExportReport
from readyagents.policy import Redactor
from readyagents.replay.record import contains_secret, known_secret_values, redact_value
from readyagents.simulate.redact import secret_shaped_values
from readyagents.workflow.runner import confine_under


def export_feedback(
    *,
    settings: Any,
    dest: Path | str,
    fmt: str = DEFAULT_FORMAT,
    scope: str | None = None,
    node: str | None = None,
    since: str | None = None,
    min_rating: int | None = None,
    yes: bool = False,
    secrets: list[str] | None = None,
    redactor: Any | None = None,
    auditor: Any | None = None,
) -> ExportReport:
    kind = str(fmt or DEFAULT_FORMAT).strip().lower()
    if kind not in FORMATS:
        raise FeedbackRefused(f"unknown export format {fmt!r}", reason="format")
    workspace = settings.workspace_path()
    target = confine_under(dest, workspace, what="feedback export")
    secret_list = list(secrets or [])
    if settings is not None:
        secret_list.extend(known_secret_values(settings))
    scrubber = redactor if redactor is not None else Redactor(literals=secret_list)
    excluded = 0
    excluded_unconsented = 0
    excluded_secret = 0
    rows: list[dict[str, Any]] = []
    for state, corr in collect_corrections(settings):
        if node and corr.node_id != node:
            continue
        if since and corr.ts and corr.ts < since:
            continue
        if min_rating is not None and (corr.rating is None or corr.rating < min_rating):
            continue
        if not permits(state, scope):
            excluded += 1
            excluded_unconsented += 1
            continue
        payload = _row(corr, kind)
        scrubbed = redact_value(scrubber, payload)
        residual = _residual_secrets(scrubbed, secret_list)
        if residual:
            excluded += 1
            excluded_secret += 1
            continue
        if _has_identity(scrubbed):
            excluded += 1
            continue
        rows.append(scrubbed)
    warned = True
    text = _render(kind, rows)
    atomic_write_text(target, text)
    if auditor is not None:
        auditor(
            "feedback_export",
            path=str(target),
            format=kind,
            written=len(rows),
            excluded=excluded,
        )
    return ExportReport(
        ok=True,
        format=kind,
        written=len(rows),
        excluded=excluded,
        excluded_unconsented=excluded_unconsented,
        excluded_secret=excluded_secret,
        path=str(target),
        warned=warned,
    )


def _row(corr: Correction, kind: str) -> dict[str, Any]:
    edited = apply_diff(corr.original, corr.diff) if corr.diff else corr.original
    provenance = {
        "run_id": corr.run_id,
        "node_id": corr.node_id,
        "model": corr.model,
        "ts": corr.ts,
        "role": corr.role,
    }
    if kind == "sft":
        return {
            "instruction": corr.original or corr.reason,
            "response": edited,
            "provenance": provenance,
        }
    if kind == "dpo":
        chosen = edited
        rejected = corr.original
        if corr.kind != HUMAN:
            chosen = corr.reason or edited
            rejected = corr.original or corr.signal or ""
        return {
            "prompt": corr.reason or corr.node_id,
            "chosen": chosen,
            "rejected": rejected,
            "provenance": provenance,
        }
    # eval default: constructed passing case so readyagents eval can round-trip
    safe = edited.replace("{{", "{").replace("}}", "}")
    return {
        "name": f"feedback-{corr.id[:12]}",
        "provenance": provenance,
        "workflow": {
            "name": f"feedback-{corr.id[:12]}",
            "start": "out",
            "nodes": [
                {
                    "id": "out",
                    "type": "transform",
                    "template": safe,
                    "output_key": "output",
                }
            ],
        },
        "expect_status": "succeeded",
        "expect_contains": {"output": safe},
    }


def _render(kind: str, rows: list[dict[str, Any]]) -> str:
    if kind == "eval":
        return yaml.safe_dump({"cases": rows}, sort_keys=False, allow_unicode=True)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    return ("\n".join(lines) + ("\n" if lines else "")) if rows else ""


def _residual_secrets(value: Any, secrets: list[str]) -> list[str]:
    extra = list(secrets)
    extra.extend(secret_shaped_values(value))
    if extra and contains_secret(value, extra):
        return extra
    return []


def _has_identity(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in IDENTITY_KEYS and lowered != "role":
                if item:
                    return True
            if _has_identity(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_identity(item) for item in value)
    return False
