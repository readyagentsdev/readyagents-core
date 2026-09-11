"""Redact generated secret-shaped content before freeze."""

from __future__ import annotations

import copy
import re
from typing import Any

from readyagents.replay.record import contains_secret, redact_value

_SK = re.compile(r"sk-[A-Za-z0-9]{8,}")
_GH = re.compile(r"ghp_[A-Za-z0-9]{8,}")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def secret_shaped_values(value: Any) -> list[str]:
    text = _as_text(value)
    found: list[str] = []
    for pattern in (_SK, _GH):
        found.extend(pattern.findall(text))
    return [item for item in found if len(item) >= 8]


def redact_generated(value: Any, *, extra_secrets: list[str] | None = None) -> Any:
    secrets = list(extra_secrets or [])
    secrets.extend(secret_shaped_values(value))
    if contains_secret(value, secrets):

        def _scrub(item: Any) -> Any:
            if isinstance(item, str):
                text = item
                for secret in secrets:
                    text = text.replace(secret, "<redacted>")
                text = _PEM.sub("<redacted-pem>", text)
                return text
            if isinstance(item, dict):
                return {k: _scrub(v) for k, v in item.items()}
            if isinstance(item, list):
                return [_scrub(v) for v in item]
            return item

        return _scrub(value)
    return redact_value(None, value)


def redact_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    return redact_generated(copy.deepcopy(inputs))  # type: ignore[return-value]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)
