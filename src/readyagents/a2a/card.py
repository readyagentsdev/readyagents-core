"""Deterministic Agent Card generation and remote-card validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.errors import A2ACardError
from readyagents.mcp.protocol import sanitize_prompt
from readyagents.workflow.schema import WorkflowSpec

PROTOCOL_VERSION = "0.3.0"
WELL_KNOWN_CARD = "/.well-known/agent-card.json"
WELL_KNOWN_ALIAS = "/.well-known/agent.json"
MAX_CARD_BYTES = 100_000
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def build_agent_card(
    workflow: WorkflowSpec,
    *,
    url: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Derive a reviewable Agent Card from a workflow. Deterministic key order."""
    approval = any(str(node.type) == "approval" for node in workflow.nodes)
    outputs = sorted(
        {str(node.output_key) for node in workflow.nodes if node.output_key},
    )
    required = list(workflow.required_inputs or [])
    name = str(workflow.name)
    description = sanitize_prompt(workflow.description or name, limit=2000)
    card: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": str(url).rstrip("/"),
        "version": str(version or workflow.version or __version__),
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "humanInput": approval,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": name,
                "name": name,
                "description": description,
                "tags": ["readyagents", "workflow"],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/json"],
            }
        ],
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
        },
        "security": [{"bearer": []}],
        "readyagents": {
            "workflow": name,
            "approvalCapable": approval,
            "requiredInputs": required,
            "outputs": outputs,
            "wellKnown": WELL_KNOWN_CARD,
            "alias": WELL_KNOWN_ALIAS,
        },
    }
    card["digest"] = card_digest(card)
    return json.loads(canonical_card_json(card))


def canonical_card_json(card: Mapping[str, Any]) -> str:
    return json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def card_digest(card: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in card.items()
        if key not in {"digest", "signature", "signatures"}
    }
    payload = canonical_card_json(body).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def card_signing_body(card: Mapping[str, Any]) -> bytes:
    """Canonical bytes a card signature covers (excludes digest/signature)."""
    body = {
        key: value
        for key, value in card.items()
        if key not in {"digest", "signature", "signatures"}
    }
    return canonical_card_json(body).encode("utf-8")


def card_signature_status(card: Mapping[str, Any], *, secret: str | None = None) -> str:
    """Return ``unsigned``, ``verified``, or ``invalid``. Fail closed; no secrets."""
    raw = card.get("signature")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        extra = card.get("signatures")
        if extra is None or extra == [] or extra == "":
            return "unsigned"
        return "invalid"
    if not isinstance(raw, str) or not raw.strip():
        return "invalid"
    if not secret:
        return "invalid"
    from readyagents.decisions.signing import verify_signed_body

    try:
        verify_signed_body(str(secret), card_signing_body(card), raw)
    except ValueError:
        return "invalid"
    return "verified"


def validate_card(raw: Any) -> dict[str, Any]:
    """Reject oversized, non-object, or hostile/malformed remote cards."""
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_CARD_BYTES:
            raise A2ACardError("agent card exceeds size cap")
        try:
            raw = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as extra:
            raise A2ACardError("agent card is not JSON") from extra
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_CARD_BYTES:
            raise A2ACardError("agent card exceeds size cap")
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as extra:
            raise A2ACardError("agent card is not JSON") from extra
    if not isinstance(raw, Mapping):
        raise A2ACardError("agent card must be a JSON object")
    dumped = canonical_card_json(raw)
    if len(dumped.encode("utf-8")) > MAX_CARD_BYTES:
        raise A2ACardError("agent card exceeds size cap")
    name = raw.get("name")
    url = raw.get("url")
    if not isinstance(name, str) or not name.strip():
        raise A2ACardError("agent card missing name")
    if not isinstance(url, str) or not url.strip():
        raise A2ACardError("agent card missing url")
    description = raw.get("description")
    for value in (name, url, description if isinstance(description, str) else ""):
        if value and _CONTROL_RE.search(value):
            raise A2ACardError("agent card contains control characters")
    cleaned_url = url.strip()
    if not (cleaned_url.startswith("http://") or cleaned_url.startswith("https://")):
        raise A2ACardError("agent card url must be http or https")
    return dict(raw)


def load_workflow_card(path: Path, *, url: str) -> dict[str, Any]:
    from readyagents.workflow.runner import load_workflow

    workflow = load_workflow(path)
    return build_agent_card(workflow, url=url)
