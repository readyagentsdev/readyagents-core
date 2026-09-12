"""Frozen prompt-registry interface: schema, hash, sidecar name, bounds."""

from __future__ import annotations

import hashlib
from pathlib import Path

SCHEMA_REGISTRY = "readyagents.prompt.registry.v1"
SCHEMA_OPTIMIZE = "readyagents.optimize.run.v1"
MAX_PROMPT_CHARS = 32_000
SIDECAR_SUFFIX = ".prompts.json"
OPTIMIZE_SUFFIX = ".optimize.json"


def sidecar_path(workflow: Path | str) -> Path:
    file = Path(workflow)
    return file.parent / f"{file.stem}{SIDECAR_SUFFIX}"


def optimize_state_path(workflow: Path | str) -> Path:
    file = Path(workflow)
    return file.parent / f"{file.stem}{OPTIMIZE_SUFFIX}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
