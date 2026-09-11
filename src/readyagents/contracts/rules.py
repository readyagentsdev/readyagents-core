"""Named deterministic content rules. Each firing rule reports its name."""

from __future__ import annotations

import json
from typing import Any

from readyagents.contracts.regex import compile_bounded, search_bounded
from readyagents.contracts.spec import ContractRule, _normalize_citation, _normalize_max_chars
from readyagents.policy import DEFAULT_REDACT_PATTERNS, REDACTED, Redactor


def payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def field_text(value: Any, field: str | None) -> str:
    if not field:
        return payload_text(value)
    if isinstance(value, dict) and field in value:
        return payload_text(value.get(field))
    return ""


def eval_rule(
    rule: ContractRule,
    output: Any,
    *,
    mapping: dict[str, Any],
) -> tuple[bool, str]:
    """Return (fired, reason). fired True means the contract is not met."""
    text = payload_text(output)
    kind = rule.kind()
    if kind == "deny_regex":
        if search_bounded(str(rule.deny_regex), text):
            return True, f"{rule.recorded_name()} matched deny_regex"
        return False, ""
    if kind == "deny":
        needle = str(rule.deny or "")
        if needle and needle in text:
            return True, f"{rule.recorded_name()} matched literal deny"
        return False, ""
    if kind == "require_citation":
        spec = _normalize_citation(rule.require_citation)
        source = str(spec["from"])
        if source not in mapping or mapping.get(source) is None:
            return True, f"{rule.recorded_name()} missing citation source {source}"
        expected = mapping.get(source)
        token = payload_text(expected).strip().strip('"')
        if not token or token in {"null", "None"}:
            return True, f"{rule.recorded_name()} missing citation source {source}"
        if token not in text:
            return True, f"{rule.recorded_name()} fabricated or missing citation of {source}"
        return False, ""
    if kind == "language":
        if not _language_ok(text, str(rule.language or "")):
            return True, f"{rule.recorded_name()} language is not {rule.language}"
        return False, ""
    if kind == "max_chars":
        spec = _normalize_max_chars(rule.max_chars)
        blob = field_text(output, spec.get("field"))
        if len(blob) > int(spec["value"]):
            return True, f"{rule.recorded_name()} exceeded max_chars {spec['value']}"
        return False, ""
    if kind == "pii":
        if _pii_hit(text):
            return True, f"{rule.recorded_name()} matched PII detector"
        return False, ""
    if kind == "judge":
        return False, ""
    return False, ""


def mask_firing_match(value: Any, rule: ContractRule | None) -> Any:
    """Replace the deny / deny_regex / PII match with the standard redaction token."""
    if rule is None:
        return value
    kind = rule.kind()
    if kind == "deny":
        needle = str(rule.deny or "")
        if not needle:
            return value
        return _map_strings(value, lambda text: text.replace(needle, REDACTED))
    if kind == "deny_regex":
        compiled = compile_bounded(str(rule.deny_regex))
        return _map_strings(value, lambda text: compiled.sub(REDACTED, text))
    if kind == "pii":
        return Redactor().redact(value)
    return value


def _map_strings(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {str(k): _map_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, tuple):
        return [_map_strings(v, fn) for v in value]
    return value


def _pii_hit(text: str) -> bool:
    return any(pat.search(text) for pat in DEFAULT_REDACT_PATTERNS)


def _language_ok(text: str, expected: str) -> bool:
    code = expected.strip().lower()
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    if code in {"en", "eng", "english", "latin"}:
        latin = sum(1 for c in letters if ord(c) < 128)
        return (latin / len(letters)) >= 0.8
    if code in {"zh", "ja", "ko", "cjk", "chinese", "japanese", "korean"}:
        cjk = sum(
            1
            for c in letters
            if 0x4E00 <= ord(c) <= 0x9FFF
            or 0x3040 <= ord(c) <= 0x30FF
            or 0xAC00 <= ord(c) <= 0xD7AF
        )
        return (cjk / len(letters)) >= 0.3
    return False
