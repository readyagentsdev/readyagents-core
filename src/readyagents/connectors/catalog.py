"""Catalog helpers for `readyagents connectors list|show|test`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.connectors.conformance import run_conformance
from readyagents.connectors.registry import (
    first_party_connectors,
    get_connector,
    installed_specs,
    register,
)
from readyagents.errors import ToolError


def ensure_installed() -> None:
    if installed_specs():
        return
    for connector in first_party_connectors():
        register(connector)


def list_payload() -> dict[str, Any]:
    ensure_installed()
    rows = []
    for spec in installed_specs():
        rows.append(
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "auth": spec.auth.kind if spec.auth else "none",
                "secret": spec.auth.secret if spec.auth else None,
                "destinations": list(spec.destinations),
                "determinism": spec.determinism,
                "idempotent": spec.idempotent,
                "idempotency_key": spec.idempotency_key,
                "side_effects": spec.side_effects,
            }
        )
    rows.sort(key=lambda item: item["name"])
    return {"connectors": rows}


def show_payload(name: str) -> dict[str, Any]:
    ensure_installed()
    spec = get_connector(name).spec
    return spec.as_dict()


def test_payload(
    name: str, *, fixtures: Path | None = None, workspace: Path | None = None
) -> dict[str, Any]:
    del fixtures
    ensure_installed()
    connector = get_connector(name)
    failures = run_conformance(connector, workspace=workspace)
    return {
        "name": name,
        "ok": not failures,
        "failures": [{"check": item.check, "message": item.message} for item in failures],
    }


def redact_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop values that look like tokens. Names of secrets stay (they are declarations)."""
    blob = str(payload).lower()
    if "sk-" in blob or "bearer " in blob:
        raise ToolError("catalog payload leaked a secret value")
    return payload
