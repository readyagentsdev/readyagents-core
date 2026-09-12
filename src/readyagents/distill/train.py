"""Orchestrate training. Core never trains and never imports a trainer."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from readyagents.config import Settings, get_settings
from readyagents.distill.adapters import register_adapter
from readyagents.distill.dataset import load_manifest
from readyagents.distill.schema import AdapterRecord
from readyagents.distill.store import adapter_folder, load_config
from readyagents.errors import DistillRefused, DistillSovereignHosted, DistillTrainMissing
from readyagents.permissions import restrict_file
from readyagents.trust.digest import digest_bytes
from readyagents.workflow.runner import confine_under


@runtime_checkable
class Tuner(Protocol):
    """Optional pack-owned trainer. Core never implements this."""

    name: str

    def train(self, dataset_dir: Path, *, base: str, config: dict[str, Any]) -> Path: ...

    def hosted(self) -> bool: ...

    def complete(self, adapter: Path, prompt: str) -> str: ...


def collect_tuner(packs: list[Any] | None = None) -> Tuner | None:
    if packs is None:
        from readyagents.packs.loader import discover_packs

        packs = discover_packs()
    for pack in packs or []:
        fn = getattr(pack, "register_tuner", None)
        if not callable(fn):
            continue
        tuner = fn()
        if tuner is None:
            continue
        if isinstance(tuner, Tuner) or callable(getattr(tuner, "train", None)):
            return tuner
    return None


def train(
    dataset: Path | str,
    *,
    base: str,
    settings: Settings | None = None,
    tuner: Tuner | None = None,
    packs: list[Any] | None = None,
    config: dict[str, Any] | None = None,
    sign_key: Path | str | None = None,
    hosted: bool | None = None,
    node_id: str | None = None,
) -> AdapterRecord:
    """Run the pack trainer. Distinct typed error when the pack is absent."""
    settings = settings or get_settings()
    cfg = load_config(settings)
    folder = Path(dataset)
    if not folder.is_absolute():
        folder = settings.workspace_path() / folder
    folder = confine_under(folder, settings.workspace_path(), what="distill dataset")
    manifest = load_manifest(folder)
    if not manifest.hash or manifest.counts.get("holdout", 0) <= 0:
        raise DistillRefused("dataset must record a holdout split and hash", reason="holdout")
    want_hosted = bool(hosted if hosted is not None else cfg.hosted_tune)
    resolved = tuner if tuner is not None else collect_tuner(packs)
    if resolved is None:
        raise DistillTrainMissing()
    pack_hosted = bool(
        want_hosted or (callable(getattr(resolved, "hosted", None)) and resolved.hosted())
    )
    if pack_hosted and bool(getattr(settings, "sovereign", False)):
        raise DistillSovereignHosted()
    training = dict(config or {})
    training["hosted"] = pack_hosted
    artifact = resolved.train(folder, base=str(base), config=training)
    artifact = Path(artifact)
    blob = artifact.read_bytes() if artifact.is_file() else b""
    adapter_id = "adp_" + secrets.token_hex(8)
    dest_dir = adapter_folder(adapter_id, settings)
    dest = dest_dir / "adapter.json"
    if artifact.is_file():
        dest.write_bytes(blob)
    else:
        dest.write_text(json.dumps({"path": str(artifact)}, indent=2) + "\n", encoding="utf-8")
    restrict_file(dest)
    record = AdapterRecord(
        id=adapter_id,
        node_id=node_id or manifest.node_id,
        base_model=str(base),
        dataset_hash=manifest.hash,
        training_config=training,
        digest=digest_bytes(dest.read_bytes()),
        path=str(dest),
        incumbent=None,
        status="candidate",
    )
    return register_adapter(record, settings=settings, sign_key=sign_key, artifact=dest)
