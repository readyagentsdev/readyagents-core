"""Frozen health interface: classes, actions, query bounds, fingerprint width."""

from __future__ import annotations

FAILURE_CLASSES = frozenset({"truncation", "rate_limit", "schema_violation", "provider_error"})
RECOVERY_ACTIONS = frozenset({"retry_with", "backoff", "repair", "fallback"})
BELOW_GATE = "gate"
FINGERPRINT_WIDTH = 16
DEFAULT_WINDOW = 50
DEFAULT_LIMIT = 64
HARD_MAX_RUNS = 500
HARD_MAX_WINDOW = 500
CLUSTERS_NAME = "health-clusters.json"
RULES_RESOURCE = "normalize_rules.json"

# Classification is data: error type names and message needles. Order is first match.
CLASS_RULES: tuple[dict[str, object], ...] = (
    {
        "class": "truncation",
        "error_types": ("BudgetExceeded",),
        "needles": ("truncat", "max_tokens", "finish_reason", "context length", "token limit"),
    },
    {
        "class": "rate_limit",
        "error_types": (),
        "needles": ("429", "rate limit", "rate_limit", "too many requests", "retry-after"),
    },
    {
        "class": "schema_violation",
        "error_types": ("StructuredOutputError", "ContractError"),
        "needles": ("schema", "json schema", "did not match", "validation error"),
    },
    {
        "class": "provider_error",
        "error_types": ("LLMError", "CircuitOpen"),
        "needles": ("provider", "api error", "connection", "timeout", "503", "502"),
    },
)
