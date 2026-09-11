"""Deterministic redacted failure fingerprints. Pure function of the failure."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from readyagents.health.layout import CLASS_RULES, FINGERPRINT_WIDTH, RULES_RESOURCE
from readyagents.policy import Redactor
from readyagents.replay.record import contains_secret, redact_value
from readyagents.simulate.redact import secret_shaped_values


@dataclass(frozen=True)
class FailureFingerprint:
    id: str
    error_type: str
    klass: str
    node_id: str
    tool: str
    provider: str
    normalized: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "error_type": self.error_type,
            "class": self.klass,
            "node_id": self.node_id,
            "tool": self.tool,
            "provider": self.provider,
            "normalized": self.normalized,
        }


def fingerprint(
    error: BaseException | str,
    *,
    node_id: str = "",
    tool: str = "",
    provider: str = "",
    secrets: list[str] | None = None,
    redactor: Any = None,
) -> FailureFingerprint:
    """Same typed error + node + tool + provider + message → same id on any machine."""
    error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
    message = str(error)
    klass = classify_failure(error_type, message)
    normalized = normalize_message(message, secrets=secrets, redactor=redactor)
    payload = {
        "error_type": error_type,
        "class": klass,
        "node_id": str(node_id or ""),
        "tool": str(tool or ""),
        "provider": str(provider or ""),
        "normalized": normalized,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()[:FINGERPRINT_WIDTH]
    return FailureFingerprint(
        id=digest,
        error_type=error_type,
        klass=klass,
        node_id=str(node_id or ""),
        tool=str(tool or ""),
        provider=str(provider or ""),
        normalized=normalized,
    )


def classify_failure(error_type: str, message: str) -> str:
    blob = f"{error_type} {message}".lower()
    for rule in CLASS_RULES:
        types = tuple(str(item) for item in (rule.get("error_types") or ()))
        if error_type in types:
            return str(rule["class"])
        needles = tuple(str(item) for item in (rule.get("needles") or ()))
        if any(needle.lower() in blob for needle in needles if needle):
            return str(rule["class"])
    return "other"


def normalize_message(
    message: str,
    *,
    secrets: list[str] | None = None,
    redactor: Any = None,
) -> str:
    text = str(message or "")
    for rule in load_normalize_rules():
        text = rule["compiled"].sub(str(rule["replace"]), text)
    extra = list(secrets or [])
    extra.extend(secret_shaped_values(text))
    scrubber = redactor if redactor is not None else Redactor(literals=extra)
    text = str(redact_value(scrubber, text))
    if extra and contains_secret(text, extra):
        for secret in extra:
            if secret:
                text = text.replace(secret, "[redacted]")
    return " ".join(text.split())


@lru_cache(maxsize=1)
def load_normalize_rules() -> tuple[dict[str, Any], ...]:
    raw = _read_rules()
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        pattern = str(row.get("pattern") or "")
        replace = str(row.get("replace") or "")
        ident = str(row.get("id") or "")
        if not pattern or not ident:
            continue
        out.append(
            {
                "id": ident,
                "replace": replace,
                "compiled": re.compile(pattern),
            }
        )
    return tuple(out)


def _read_rules() -> list[Any]:
    try:
        payload = (
            resources.files("readyagents.health")
            .joinpath(RULES_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, TypeError):
        path = Path(__file__).resolve().parent / RULES_RESOURCE
        payload = path.read_text(encoding="utf-8")
    data = json.loads(payload)
    return list(data) if isinstance(data, list) else []


def fingerprint_from_result(
    row: Any,
    *,
    secrets: list[str] | None = None,
    redactor: Any = None,
) -> FailureFingerprint | None:
    status = str(getattr(row, "status", "") or "")
    error = getattr(row, "error", None)
    if status not in {"failed", "error"} and not error:
        return None
    message = str(error or "")
    tool = ""
    rounds = getattr(row, "tool_rounds", None) or []
    if rounds and isinstance(rounds[0], dict):
        tool = str(rounds[0].get("name") or "")
    provider = ""
    route = getattr(row, "route", None) or {}
    if isinstance(route, dict):
        provider = str(route.get("provider") or route.get("model") or "")
    return fingerprint(
        message,
        node_id=str(getattr(row, "node_id", "") or ""),
        tool=tool,
        provider=provider,
        secrets=secrets,
        redactor=redactor,
    )
