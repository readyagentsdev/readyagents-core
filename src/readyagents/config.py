"""BYOK settings: environment, then `.env`, then `.env-ai`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from readyagents.errors import ConfigError, LLMError

DEFAULT_MCP_TOKEN_ENV = "READYAGENTS_MCP_TOKEN"
MAX_CONCURRENT_RUNS_HARD = 32
MAX_PENDING_RUNS_HARD = 256
MAX_HTTP_BODY_BYTES = 1_048_576
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


def _env_files() -> tuple[Path, ...]:
    """Lowest-priority first: `.env-ai` then `.env`. OS env still wins."""
    cwd = Path.cwd()
    files: list[Path] = []
    for name in (".env-ai", ".env"):
        path = cwd / name
        if path.is_file():
            files.append(path)
    return tuple(files)


class Settings(BaseSettings):
    """Runtime settings. Secrets are never logged by this class."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "READYAGENTS_OPENAI_API_KEY"),
    )
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "READYAGENTS_ANTHROPIC_API_KEY"),
    )
    openai_compat_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPENAI_COMPAT_API_KEY",
            "READYAGENTS_OPENAI_COMPAT_API_KEY",
            "GROQ_API_KEY",
        ),
    )
    openai_compat_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPENAI_COMPAT_BASE_URL",
            "READYAGENTS_OPENAI_COMPAT_BASE_URL",
        ),
    )
    default_model: str = Field(
        default="openai:gpt-4o-mini",
        validation_alias=AliasChoices("READYAGENTS_DEFAULT_MODEL", "DEFAULT_MODEL"),
    )
    allow_http: bool = Field(
        default=False,
        validation_alias=AliasChoices("READYAGENTS_ALLOW_HTTP"),
    )
    workspace: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_WORKSPACE"),
    )
    home: Path = Field(
        default=Path(".readyagents"),
        validation_alias=AliasChoices("READYAGENTS_HOME"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("READYAGENTS_LOG_LEVEL"),
    )
    log_format: str = Field(
        default="text",
        validation_alias=AliasChoices("READYAGENTS_LOG_FORMAT"),
    )
    max_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_MAX_TOKENS"),
    )
    max_cost_usd: float | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_MAX_COST_USD"),
    )
    fallback_models: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_FALLBACK_MODELS"),
    )
    circuit_failure_threshold: int = Field(
        default=3,
        validation_alias=AliasChoices("READYAGENTS_CIRCUIT_FAILURE_THRESHOLD"),
    )
    circuit_cooldown_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("READYAGENTS_CIRCUIT_COOLDOWN_SECONDS"),
    )
    llm_cache: bool = Field(
        default=False,
        validation_alias=AliasChoices("READYAGENTS_LLM_CACHE"),
    )
    redact: bool = Field(
        default=False,
        validation_alias=AliasChoices("READYAGENTS_REDACT"),
    )
    redact_literals: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_REDACT_LITERALS"),
    )
    redact_patterns: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_REDACT_PATTERNS"),
    )
    actor: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_ACTOR"),
    )
    pause_notify_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_PAUSE_NOTIFY_URL"),
    )
    mcp_http_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("READYAGENTS_MCP_HTTP_HOST"),
    )
    mcp_http_port: int = Field(
        default=8765,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("READYAGENTS_MCP_HTTP_PORT"),
    )
    mcp_max_concurrent_runs: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("READYAGENTS_MCP_MAX_CONCURRENT_RUNS"),
    )
    mcp_max_pending_runs: int = Field(
        default=32,
        ge=1,
        validation_alias=AliasChoices("READYAGENTS_MCP_MAX_PENDING_RUNS"),
    )
    run_store: str = Field(
        default="json",
        validation_alias=AliasChoices("READYAGENTS_RUN_STORE"),
    )
    run_db: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_RUN_DB"),
    )
    sqlite_busy_timeout_ms: int = Field(
        default=5000,
        ge=1,
        le=60000,
        validation_alias=AliasChoices("READYAGENTS_SQLITE_BUSY_TIMEOUT_MS"),
    )
    mcp_protocol_max: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_MCP_PROTOCOL_MAX"),
    )
    decision_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_DECISION_SECRET"),
    )
    record: bool = Field(
        default=False,
        validation_alias=AliasChoices("READYAGENTS_RECORD"),
    )
    cassette_max_entry_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        validation_alias=AliasChoices("READYAGENTS_CASSETTE_MAX_ENTRY_BYTES"),
    )
    cassette_max_bytes: int = Field(
        default=10_485_760,
        ge=1024,
        validation_alias=AliasChoices("READYAGENTS_CASSETTE_MAX_BYTES"),
    )
    retention_days: int = Field(
        default=180,
        ge=0,
        validation_alias=AliasChoices("READYAGENTS_RETENTION_DAYS"),
    )
    prices: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_PRICES"),
    )
    trust_anchors: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_TRUST_ANCHORS"),
    )
    credentials: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("READYAGENTS_CREDENTIALS"),
    )

    def fallback_model_list(self) -> list[str]:
        if not self.fallback_models:
            return []
        return [part.strip() for part in self.fallback_models.split(",") if part.strip()]

    def redact_literal_list(self) -> list[str]:
        if not self.redact_literals:
            return []
        return [part.strip() for part in self.redact_literals.split(",") if part.strip()]

    def redact_pattern_list(self) -> list[str]:
        if not self.redact_patterns:
            return []
        return [part.strip() for part in self.redact_patterns.split(",") if part.strip()]

    def cache_dir(self) -> Path:
        return self.home_path() / "cache"

    def cassettes_dir(self) -> Path:
        return self.home_path() / "cassettes"

    def audit_dir(self) -> Path:
        return self.home_path() / "audit"

    def ledger_dir(self) -> Path:
        return self.home_path() / "ledger"

    def prices_path(self) -> Path | None:
        return self.prices.expanduser() if self.prices is not None else None

    def retention_seconds(self) -> float:
        return float(self.retention_days) * 86400.0

    @field_validator(
        "openai_api_key",
        "anthropic_api_key",
        "openai_compat_api_key",
        "decision_secret",
        "mcp_protocol_max",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("mcp_max_concurrent_runs", mode="after")
    @classmethod
    def _clamp_mcp_max_concurrent_runs(cls, value: int) -> int:
        if value > MAX_CONCURRENT_RUNS_HARD:
            return MAX_CONCURRENT_RUNS_HARD
        return value

    @field_validator("mcp_max_pending_runs", mode="after")
    @classmethod
    def _clamp_mcp_max_pending_runs(cls, value: int) -> int:
        if value > MAX_PENDING_RUNS_HARD:
            return MAX_PENDING_RUNS_HARD
        return value

    @field_validator("run_store", mode="after")
    @classmethod
    def _validate_run_store(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"json", "sqlite"}:
            raise ValueError(f"Invalid run_store '{value}'. Expected json or sqlite.")
        return normalized

    def workspace_path(self) -> Path:
        return (self.workspace or Path.cwd()).expanduser().resolve()

    def home_path(self) -> Path:
        path = self.home
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.expanduser().resolve()

    def runs_dir(self) -> Path:
        return self.home_path() / "runs"

    def run_db_path(self) -> Path:
        if self.run_db is None:
            return self.home_path() / "runs.sqlite3"
        path = self.run_db.expanduser()
        if not path.is_absolute():
            path = self.home_path() / path
        return path.resolve()

    def sqlite_busy_timeout(self) -> float:
        return self.sqlite_busy_timeout_ms / 1000

    def api_key_for(self, provider: str) -> str | None:
        provider = provider.lower()
        if provider in {"openai"}:
            return self.openai_api_key
        if provider in {"anthropic"}:
            return self.anthropic_api_key
        if provider in {"openai-compat", "openai_compat", "compat", "groq", "ollama"}:
            return self.openai_compat_api_key or self.openai_api_key
        return None


def load_settings(*, env_file: tuple[Path, ...] | None = None) -> Settings:
    """Load settings from OS env, then `.env`, then `.env-ai`."""
    files = env_file if env_file is not None else _env_files()
    try:
        return Settings(_env_file=files)  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Failed to load settings: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()


def require_api_key(
    provider: str,
    settings: Settings | None = None,
    *,
    secrets: Any = None,
) -> str:
    settings = settings or get_settings()
    key = settings.api_key_for(provider)
    if not key and secrets is not None:
        from readyagents.secrets import secret_for_provider

        key = secret_for_provider(provider, settings=None, secrets=secrets)
    if key:
        return key
    hints = {
        "openai": ("Set OPENAI_API_KEY or READYAGENTS_OPENAI_API_KEY (copy .env.example to .env)."),
        "anthropic": (
            "Set ANTHROPIC_API_KEY or READYAGENTS_ANTHROPIC_API_KEY (copy .env.example to .env)."
        ),
        "openai-compat": (
            "Set OPENAI_COMPAT_API_KEY (and OPENAI_COMPAT_BASE_URL) "
            "or OPENAI_API_KEY for a compatible endpoint."
        ),
    }
    hint = hints.get(provider.lower(), f"Set an API key for provider '{provider}'.")
    raise LLMError(f"No API key configured for provider '{provider}'. ReadyAgents is BYOK — {hint}")
