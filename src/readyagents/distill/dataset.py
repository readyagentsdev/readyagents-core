"""Consented, re-redacted, seeded splits with a reproducible dataset hash."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.config import Settings, get_settings
from readyagents.distill.schema import HOLDOUT_NAME, DatasetManifest, SplitSpec
from readyagents.errors import DistillRefused
from readyagents.feedback.collect import collect_corrections
from readyagents.feedback.consent import permits
from readyagents.feedback.diff import apply_diff
from readyagents.feedback.export import _has_identity, _residual_secrets
from readyagents.feedback.layout import HUMAN
from readyagents.policy import Redactor
from readyagents.replay.record import known_secret_values
from readyagents.trust.digest import digest_bytes
from readyagents.workflow.runner import confine_under
from readyagents.workflow.state import utc_now


def build_dataset(
    node_id: str,
    dest: Path | str,
    *,
    settings: Settings | None = None,
    seed: int = 1,
    split: tuple[float, float, float] = (0.8, 0.1, 0.1),
    fmt: str = "sft",
    scope: str | None = None,
    yes: bool = False,
) -> DatasetManifest:
    settings = settings or get_settings()
    if not yes:
        raise DistillRefused(
            "dataset build writes a training set; pass --yes to confirm",
            reason="confirm",
        )
    train_p, val_p, hold_p = split
    if abs((train_p + val_p + hold_p) - 1.0) > 1e-6 or hold_p <= 0:
        raise DistillRefused("split must sum to 1 and include a holdout share", reason="split")
    workspace = settings.workspace_path()
    target = confine_under(Path(dest), workspace, what="distill dataset")
    target.mkdir(parents=True, exist_ok=True)
    secret_list = known_secret_values(settings)
    scrubber = Redactor(literals=secret_list)
    rows: list[dict[str, Any]] = []
    excluded_unconsented = 0
    excluded_secret = 0
    for state, corr in collect_corrections(settings):
        if corr.node_id != node_id:
            continue
        if corr.kind != HUMAN:
            continue
        if not permits(state, scope or corr.consent_scope or None):
            excluded_unconsented += 1
            continue
        payload = _sft_row(corr)
        scrubbed = _redact(scrubber, payload)
        if _residual_secrets(scrubbed, secret_list) or _has_identity(scrubbed):
            excluded_secret += 1
            continue
        rows.append(scrubbed)
    before = len(rows)
    rows = _dedup(rows)
    deduped = before - len(rows)
    rows = _balance(rows)
    buckets = {"train": [], "validation": [], HOLDOUT_NAME: []}
    for row in rows:
        buckets[_bucket(row, seed, train_p, val_p)].append(row)
    _ensure_holdout(buckets, seed)
    for name, items in buckets.items():
        path = target / f"{name}.jsonl"
        text = "".join(
            json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n" for item in items
        )
        atomic_write_text(path, text, encoding="utf-8", newline="\n")
    counts = {
        "train": len(buckets["train"]),
        "validation": len(buckets["validation"]),
        HOLDOUT_NAME: len(buckets[HOLDOUT_NAME]),
        "excluded_unconsented": excluded_unconsented,
        "excluded_secret": excluded_secret,
        "deduped": deduped,
    }
    manifest = DatasetManifest(
        node_id=node_id,
        format=fmt,
        seed=int(seed),
        split=SplitSpec(train=train_p, validation=val_p, holdout=hold_p),
        counts=counts,
        consent_from_recorded_policy=True,
        redaction_reverified=True,
        holdout_named=HOLDOUT_NAME,
        created_at=utc_now(),
        path=str(target),
    )
    digest = _hash_dataset(target, manifest)
    manifest.hash = digest
    atomic_write_text(
        target / "manifest.json",
        json.dumps(manifest.model_dump(mode="python", by_alias=True), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def load_manifest(dest: Path | str) -> DatasetManifest:
    path = Path(dest)
    if path.is_dir():
        path = path / "manifest.json"
    if not path.is_file():
        raise DistillRefused(f"dataset manifest not found: {path}", reason="missing")
    return DatasetManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _sft_row(corr: Any) -> dict[str, Any]:
    edited = apply_diff(corr.original, corr.diff) if corr.diff else corr.original
    return {
        "id": corr.id,
        "instruction": corr.original or corr.reason,
        "response": edited,
        "label": corr.label,
        "provenance": {
            "run_id": corr.run_id,
            "node_id": corr.node_id,
            "model": corr.model,
            "ts": corr.ts,
            "role": corr.role,
        },
    }


def _redact(scrubber: Redactor, payload: dict[str, Any]) -> dict[str, Any]:
    from readyagents.replay.record import redact_value

    return redact_value(scrubber, payload)


def _dedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(
            {"instruction": row.get("instruction"), "response": row.get("response")},
            sort_keys=True,
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        out.append(row)
    return out


def _balance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = [str(row.get("label") or "") for row in rows if row.get("label")]
    if len(labels) < 2 or len(set(labels)) < 2:
        return rows
    counts = Counter(labels)
    cap = max(1, sorted(counts.values())[len(counts) // 2] * 2)
    used: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for row in rows:
        label = str(row.get("label") or "")
        if label and used[label] >= cap:
            continue
        if label:
            used[label] += 1
        out.append(row)
    return out


def _bucket(row: dict[str, Any], seed: int, train_p: float, val_p: float) -> str:
    token = str(row.get("id") or json.dumps(row, sort_keys=True))
    digest = hashlib.sha256(f"{seed}:{token}".encode()).digest()
    n = int.from_bytes(digest[:8], "big") % 1000
    train_n = int(train_p * 1000)
    val_n = int(val_p * 1000)
    if n < train_n:
        return "train"
    if n < train_n + val_n:
        return "validation"
    return HOLDOUT_NAME


def _ensure_holdout(buckets: dict[str, list], seed: int) -> None:
    if buckets[HOLDOUT_NAME]:
        return
    pool = buckets["train"] or buckets["validation"]
    if not pool:
        return
    idx = int(seed) % len(pool)
    buckets[HOLDOUT_NAME].append(pool.pop(idx))


def _hash_dataset(folder: Path, manifest: DatasetManifest) -> str:
    payload = {
        "seed": manifest.seed,
        "split": manifest.split.model_dump(),
        "node_id": manifest.node_id,
        "format": manifest.format,
        "files": {},
    }
    for name in ("train", "validation", HOLDOUT_NAME):
        path = folder / f"{name}.jsonl"
        payload["files"][name] = digest_bytes(path.read_bytes() if path.is_file() else b"")
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return digest_bytes(blob)
