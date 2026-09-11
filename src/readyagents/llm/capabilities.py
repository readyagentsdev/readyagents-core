"""Versioned, overridable model capability matrix. Malformed overrides fail closed."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from readyagents.errors import CapabilityError, ConfigError
from readyagents.llm.base import parse_model_ref

BUNDLED_CAPABILITIES = Path(__file__).resolve().parent / "capabilities.json"
MAX_CAPABILITY_FILE_BYTES = 1_048_576
MAX_MODEL_KEYS = 10_000
SUPPORTED_VERSION = 1
ENV_CAPABILITIES = "READYAGENTS_CAPABILITY_MATRIX"

LATENCY_RANK = {"fast": 0, "standard": 1, "slow": 2}
QUALITY_RANK = {"frontier": 0, "standard": 1, "compact": 2}
HOSTED_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "bedrock", "vertex", "groq"})
LOCAL_PROVIDERS = frozenset({"ollama"})


@dataclass(frozen=True)
class ModelCapabilities:
    """Declared attributes for one model ref. Never inferred from output quality."""

    context_window: int
    tool_calling: bool
    structured_output: bool
    media: bool
    streaming: bool
    local: bool = False
    latency_class: str = "standard"
    quality_class: str = "standard"
    match: str = "exact"

    def supports(self, require: dict[str, Any] | None) -> bool:
        if not require:
            return True
        if require.get("tool_calling") and not self.tool_calling:
            return False
        if require.get("structured_output") and not self.structured_output:
            return False
        if require.get("media") and not self.media:
            return False
        if require.get("streaming") and not self.streaming:
            return False
        if require.get("local") and not self.local:
            return False
        min_ctx = require.get("min_context_window")
        if min_ctx is not None and int(self.context_window) < int(min_ctx):
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "tool_calling": self.tool_calling,
            "structured_output": self.structured_output,
            "media": self.media,
            "streaming": self.streaming,
            "local": self.local,
            "latency_class": self.latency_class,
            "quality_class": self.quality_class,
            "match": self.match,
        }


@dataclass
class CapabilityMatrix:
    version: int
    updated_at: str
    warn_after_days: int
    models: dict[str, ModelCapabilities]
    prefixes: dict[str, ModelCapabilities]
    source: str
    stale: bool = False

    def lookup(self, model: str) -> ModelCapabilities | None:
        return lookup_model(model, matrix=self)

    def is_stale(self, *, now: datetime | None = None) -> bool:
        if self.warn_after_days <= 0:
            return False
        parsed = _parse_updated_at(self.updated_at)
        if parsed is None:
            return True
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=UTC)
        return (clock - parsed).days > self.warn_after_days

    def catalog_refs(self) -> list[str]:
        return list(self.models.keys())


def load_capability_matrix(path: Path | str | None = None) -> CapabilityMatrix:
    """Load the bundled matrix, or ``READYAGENTS_CAPABILITY_MATRIX`` / ``path`` override."""
    resolved = _resolve_path(path)
    if resolved is None:
        table = _bundled_matrix()
        table.stale = table.is_stale()
        return table
    table = _load_from_path(resolved)
    if table.is_stale():
        raise ConfigError(
            f"Capability matrix override {resolved} is stale "
            f"(updated_at={table.updated_at}, warn_after_days={table.warn_after_days}). "
            "Refresh the file or raise warn_after_days."
        )
    return table


def lookup_model(model: str, *, matrix: CapabilityMatrix | None = None) -> ModelCapabilities | None:
    matrix = matrix or load_capability_matrix()
    ref = (model or "").strip()
    if not ref:
        return None
    exact = matrix.models.get(ref)
    if exact is not None:
        return exact
    if ":" in ref:
        _provider, name = ref.split(":", 1)
        bare = name.strip()
        if bare and bare in matrix.models:
            hit = matrix.models[bare]
            return ModelCapabilities(
                context_window=hit.context_window,
                tool_calling=hit.tool_calling,
                structured_output=hit.structured_output,
                media=hit.media,
                streaming=hit.streaming,
                local=hit.local,
                latency_class=hit.latency_class,
                quality_class=hit.quality_class,
                match="exact-id",
            )
    prefix_hit = _longest_prefix(ref, matrix.prefixes)
    if prefix_hit is not None:
        key, caps = prefix_hit
        return ModelCapabilities(
            context_window=caps.context_window,
            tool_calling=caps.tool_calling,
            structured_output=caps.structured_output,
            media=caps.media,
            streaming=caps.streaming,
            local=caps.local,
            latency_class=caps.latency_class,
            quality_class=caps.quality_class,
            match=f"prefix:{key}",
        )
    return None


def assert_capable(
    model: str,
    require: dict[str, Any] | None,
    *,
    matrix: CapabilityMatrix | None = None,
) -> ModelCapabilities:
    """Raise ``CapabilityError`` before spend when the request is unsupported."""
    caps = lookup_model(model, matrix=matrix)
    if caps is None:
        raise CapabilityError(
            model,
            "model is not in the capability matrix",
            require=require,
        )
    if not caps.supports(require):
        raise CapabilityError(
            model,
            "request is not supported by the capability matrix",
            require=require,
        )
    return caps


def is_local_ref(model: str, *, matrix: CapabilityMatrix | None = None) -> bool:
    ref = (model or "").strip()
    if not ref:
        return False
    caps = lookup_model(ref, matrix=matrix)
    if caps is not None:
        return bool(caps.local)
    try:
        provider, _model_id = parse_model_ref(ref)
    except ValueError:
        return False
    return provider in LOCAL_PROVIDERS


def is_hosted_ref(model: str, *, matrix: CapabilityMatrix | None = None) -> bool:
    return not is_local_ref(model, matrix=matrix)


def _resolve_path(path: Path | str | None) -> Path | None:
    if path is not None:
        return Path(path)
    raw = os.environ.get(ENV_CAPABILITIES, "").strip()
    if raw:
        return Path(raw)
    return None


@lru_cache(maxsize=1)
def _bundled_matrix() -> CapabilityMatrix:
    return _load_from_path(BUNDLED_CAPABILITIES)


def clear_capability_cache() -> None:
    _bundled_matrix.cache_clear()


def _load_from_path(path: Path) -> CapabilityMatrix:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Capability matrix not found: {file}")
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise ConfigError(f"Capability matrix unreadable: {file}: {exc}") from exc
    if size > MAX_CAPABILITY_FILE_BYTES:
        raise ConfigError(
            f"Capability matrix {file} is {size} bytes; max is {MAX_CAPABILITY_FILE_BYTES}."
        )
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Capability matrix unreadable: {file}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Capability matrix {file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Capability matrix {file} must be a JSON object")
    return _validate(data, source=str(file))


def _validate(data: dict[str, Any], *, source: str) -> CapabilityMatrix:
    version = data.get("version")
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"Capability matrix {source}: unsupported version {version!r} "
            f"(expected {SUPPORTED_VERSION})"
        )
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ConfigError(f"Capability matrix {source}: 'updated_at' is required")
    if _parse_updated_at(updated_at) is None:
        raise ConfigError(f"Capability matrix {source}: 'updated_at' is not an ISO-8601 datetime")
    warn_after = data.get("warn_after_days", 90)
    try:
        warn_days = int(warn_after)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Capability matrix {source}: warn_after_days must be an integer"
        ) from exc
    if warn_days < 0:
        raise ConfigError(f"Capability matrix {source}: warn_after_days must be >= 0")
    models_raw = data.get("models")
    if not isinstance(models_raw, dict):
        raise ConfigError(f"Capability matrix {source}: 'models' must be an object")
    if len(models_raw) > MAX_MODEL_KEYS:
        raise ConfigError(f"Capability matrix {source}: too many model keys (max {MAX_MODEL_KEYS})")
    models = {str(k): _caps(v, source=source, where=f"models.{k}") for k, v in models_raw.items()}
    prefixes_raw = data.get("prefixes") or {}
    if not isinstance(prefixes_raw, dict):
        raise ConfigError(f"Capability matrix {source}: 'prefixes' must be an object")
    prefixes = {
        str(k): _caps(v, source=source, where=f"prefixes.{k}") for k, v in prefixes_raw.items()
    }
    return CapabilityMatrix(
        version=int(version),
        updated_at=updated_at.strip(),
        warn_after_days=warn_days,
        models=models,
        prefixes=prefixes,
        source=source,
        stale=False,
    )


def _caps(raw: Any, *, source: str, where: str) -> ModelCapabilities:
    if not isinstance(raw, dict):
        raise ConfigError(f"Capability matrix {source}: {where} must be an object")
    try:
        context_window = int(raw.get("context_window"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"Capability matrix {source}: {where}.context_window must be an integer"
        ) from exc
    if context_window < 1:
        raise ConfigError(f"Capability matrix {source}: {where}.context_window must be >= 1")
    latency = str(raw.get("latency_class") or "standard").strip().lower()
    if latency not in LATENCY_RANK:
        raise ConfigError(
            f"Capability matrix {source}: {where}.latency_class must be fast, standard, or slow"
        )
    quality = str(raw.get("quality_class") or "standard").strip().lower()
    if quality not in QUALITY_RANK:
        raise ConfigError(
            f"Capability matrix {source}: {where}.quality_class must be "
            "frontier, standard, or compact"
        )
    for flag in ("tool_calling", "structured_output", "media", "streaming"):
        if flag not in raw:
            raise ConfigError(f"Capability matrix {source}: {where}.{flag} is required")
        if not isinstance(raw.get(flag), bool):
            raise ConfigError(f"Capability matrix {source}: {where}.{flag} must be a boolean")
    local = raw.get("local", False)
    if not isinstance(local, bool):
        raise ConfigError(f"Capability matrix {source}: {where}.local must be a boolean")
    return ModelCapabilities(
        context_window=context_window,
        tool_calling=bool(raw["tool_calling"]),
        structured_output=bool(raw["structured_output"]),
        media=bool(raw["media"]),
        streaming=bool(raw["streaming"]),
        local=local,
        latency_class=latency,
        quality_class=quality,
        match="exact" if where.startswith("models.") else f"prefix:{where.split('.', 1)[-1]}",
    )


def _longest_prefix(
    ref: str, prefixes: dict[str, ModelCapabilities]
) -> tuple[str, ModelCapabilities] | None:
    hits = [(key, caps) for key, caps in prefixes.items() if key and ref.startswith(key)]
    if not hits:
        return None
    hits.sort(key=lambda item: len(item[0]), reverse=True)
    return hits[0]


def _parse_updated_at(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
