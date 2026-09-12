"""Frozen feedback interface: schemas, formats, signal kinds."""

from __future__ import annotations

SCHEMA_CORRECTION = "readyagents.feedback.correction.v1"
SCHEMA_EXPORT = "readyagents.feedback.export.v1"
SCHEMA_STATS = "readyagents.feedback.stats.v1"
FORMATS = ("eval", "sft", "dpo")
DEFAULT_FORMAT = "eval"
HUMAN = "human"
IMPLICIT = "implicit"
SIGNALS = (
    "guardrail_rejection",
    "contract_repair",
    "refusal",
    "retry",
    "fallback",
)
STATS_BY = ("node", "model", "label", "week")
IDENTITY_KEYS = ("actor", "email", "subject", "reviewer", "user", "actor_id")
