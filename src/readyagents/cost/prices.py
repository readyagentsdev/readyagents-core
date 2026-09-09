"""Versioned, overridable model price table. Unknown models are unpriced."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from readyagents.errors import ConfigError

BUNDLED_PRICES = Path(__file__).resolve().parent / "prices.json"
MAX_PRICE_FILE_BYTES = 1_048_576
MAX_MODEL_KEYS = 10_000
SUPPORTED_VERSION = 1
ENV_PRICES = "READYAGENTS_PRICES"


@dataclass(frozen=True)
class Rate:
    """USD per million tokens."""

    input: float
    output: float


@dataclass(frozen=True)
class PriceQuote:
    model: str
    priced: bool
    match: str
    rate: Rate | None
    unpriced_reason: str | None = None

    def cost_micros(self, prompt_tokens: int, completion_tokens: int) -> int | None:
        if not self.priced or self.rate is None:
            return None
        usd = (max(0, int(prompt_tokens)) / 1_000_000) * self.rate.input + (
            max(0, int(completion_tokens)) / 1_000_000
        ) * self.rate.output
        return int(round(usd * 1_000_000))


@dataclass
class PriceTable:
    version: int
    updated_at: str
    currency: str
    unit: str
    invoice_authoritative: bool
    warn_after_days: int
    models: dict[str, Rate]
    prefixes: dict[str, Rate]
    source: str
    stale: bool = False

    def quote(self, model: str) -> PriceQuote:
        return quote_model(model, table=self)

    def is_stale(self, *, now: datetime | None = None) -> bool:
        if self.warn_after_days <= 0:
            return False
        parsed = _parse_updated_at(self.updated_at)
        if parsed is None:
            return True
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=UTC)
        age = clock - parsed
        return age.days > self.warn_after_days


def load_price_table(path: Path | str | None = None) -> PriceTable:
    """Load the bundled table, or ``READYAGENTS_PRICES`` / ``path`` override."""
    resolved = _resolve_path(path)
    if resolved is None:
        return _bundled_table()
    return _load_from_path(resolved)


def quote_model(model: str, *, table: PriceTable | None = None) -> PriceQuote:
    """Match exact, then provider-prefixed. No catch-all default (never silent zero)."""
    table = table or load_price_table()
    ref = (model or "").strip()
    if not ref:
        return PriceQuote(
            model=ref,
            priced=False,
            match="empty",
            rate=None,
            unpriced_reason="empty model identifier",
        )
    exact = table.models.get(ref)
    if exact is not None:
        return PriceQuote(model=ref, priced=True, match="exact", rate=exact)
    if ":" in ref:
        _provider, name = ref.split(":", 1)
        bare = name.strip()
        if bare and bare in table.models:
            return PriceQuote(model=ref, priced=True, match="exact-id", rate=table.models[bare])
    prefix_hit = _longest_prefix(ref, table.prefixes)
    if prefix_hit is not None:
        key, rate = prefix_hit
        return PriceQuote(model=ref, priced=True, match=f"prefix:{key}", rate=rate)
    return PriceQuote(
        model=ref,
        priced=False,
        match="unpriced",
        rate=None,
        unpriced_reason="no exact or provider-prefix match",
    )


def _resolve_path(path: Path | str | None) -> Path | None:
    if path is not None:
        return Path(path)
    raw = os.environ.get(ENV_PRICES, "").strip()
    if raw:
        return Path(raw)
    return None


@lru_cache(maxsize=1)
def _bundled_table() -> PriceTable:
    return _load_from_path(BUNDLED_PRICES)


def _load_from_path(path: Path) -> PriceTable:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Price table not found: {file}")
    try:
        size = file.stat().st_size
    except OSError as exc:
        raise ConfigError(f"Price table unreadable: {file}: {exc}") from exc
    if size > MAX_PRICE_FILE_BYTES:
        raise ConfigError(f"Price table {file} is {size} bytes; max is {MAX_PRICE_FILE_BYTES}.")
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Price table unreadable: {file}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Price table {file} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Price table {file} must be a JSON object")
    return _validate(data, source=str(file))


def _validate(data: dict[str, Any], *, source: str) -> PriceTable:
    version = data.get("version")
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"Price table {source}: unsupported version {version!r} (expected {SUPPORTED_VERSION})"
        )
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ConfigError(f"Price table {source}: 'updated_at' is required")
    if _parse_updated_at(updated_at) is None:
        raise ConfigError(f"Price table {source}: 'updated_at' is not an ISO-8601 datetime")
    currency = str(data.get("currency") or "USD")
    unit = str(data.get("unit") or "per_million_tokens")
    invoice = bool(data.get("invoice_authoritative", True))
    warn_after = data.get("warn_after_days", 90)
    try:
        warn_days = int(warn_after)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Price table {source}: warn_after_days must be an integer") from exc
    if warn_days < 0:
        raise ConfigError(f"Price table {source}: warn_after_days must be >= 0")
    models_raw = data.get("models")
    if not isinstance(models_raw, dict):
        raise ConfigError(f"Price table {source}: 'models' must be an object")
    if len(models_raw) > MAX_MODEL_KEYS:
        raise ConfigError(f"Price table {source}: too many model keys (max {MAX_MODEL_KEYS})")
    models = {str(k): _rate(v, source=source, where=f"models.{k}") for k, v in models_raw.items()}
    prefixes_raw = data.get("prefixes") or {}
    if not isinstance(prefixes_raw, dict):
        raise ConfigError(f"Price table {source}: 'prefixes' must be an object")
    prefixes = {
        str(k): _rate(v, source=source, where=f"prefixes.{k}") for k, v in prefixes_raw.items()
    }
    table = PriceTable(
        version=int(version),
        updated_at=updated_at.strip(),
        currency=currency,
        unit=unit,
        invoice_authoritative=invoice,
        warn_after_days=warn_days,
        models=models,
        prefixes=prefixes,
        source=source,
    )
    table.stale = table.is_stale()
    return table


def _rate(raw: Any, *, source: str, where: str) -> Rate:
    if not isinstance(raw, dict):
        raise ConfigError(f"Price table {source}: {where} must be an object with input/output")
    try:
        inp = float(raw["input"])
        out = float(raw["output"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(
            f"Price table {source}: {where} needs numeric 'input' and 'output' USD/MTok"
        ) from exc
    if inp < 0 or out < 0:
        raise ConfigError(f"Price table {source}: {where} rates must be >= 0")
    if inp != inp or out != out:  # NaN
        raise ConfigError(f"Price table {source}: {where} rates must be finite")
    return Rate(input=inp, output=out)


def _longest_prefix(model: str, prefixes: dict[str, Rate]) -> tuple[str, Rate] | None:
    hits = [(key, rate) for key, rate in prefixes.items() if key and model.startswith(key)]
    if not hits:
        return None
    hits.sort(key=lambda item: len(item[0]), reverse=True)
    return hits[0]


def _parse_updated_at(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(value.strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
