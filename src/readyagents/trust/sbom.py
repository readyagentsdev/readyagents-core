"""Deterministic CycloneDX-shaped inventory of what a workflow will execute.

No network, no secrets, no wall-clock timestamp. Signing proves origin, not
safety — this file is shareable only after review.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.atomic import atomic_write_text
from readyagents.trust.digest import (
    KIND_INCLUDE,
    KIND_MCP,
    KIND_PACK,
    KIND_WORKFLOW,
    inspect_workflow,
)
from readyagents.trust.lock import build_lockfile

_SECRET_KEYS = frozenset(
    {
        "env",
        "token",
        "password",
        "secret",
        "authorization",
        "api_key",
        "apikey",
        "private_key",
        "access_key",
    }
)


def build_sbom(
    workflow_path: Path | str,
    *,
    pack_specs: Sequence[str] | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    source = Path(workflow_path).resolve()
    report = inspect_workflow(source)
    lock = build_lockfile(source, pack_specs=pack_specs, workspace=workspace)
    from readyagents.workflow.runner import load_workflow

    spec = load_workflow(source)
    components: list[dict[str, Any]] = []
    for item in lock.artifacts:
        if item.kind in {KIND_WORKFLOW, KIND_INCLUDE, KIND_PACK}:
            components.append(
                _file_component(item.path or source.name, item.digest or "", item.kind)
            )
        elif item.kind == KIND_MCP:
            components.append(
                {
                    "type": "application",
                    "name": f"mcp:{item.name}",
                    "bom-ref": f"mcp:{item.name}",
                    "properties": [{"name": "kind", "value": KIND_MCP}],
                }
            )
    for name, server in sorted((spec.mcp_servers or {}).items()):
        ref = f"mcp:{name}"
        existing = next((row for row in components if row.get("bom-ref") == ref), None)
        props = [
            {"name": "kind", "value": KIND_MCP},
            {"name": "command", "value": str(server.command)},
        ]
        if server.args:
            props.append(
                {"name": "args", "value": json.dumps(list(server.args), ensure_ascii=False)}
            )
        # Never include env values — they may hold secrets.
        if existing is None:
            components.append(
                {"type": "application", "name": ref, "bom-ref": ref, "properties": props}
            )
        else:
            existing["properties"] = props
    for tool in _declared_tools(spec):
        components.append(
            {
                "type": "library",
                "name": f"tool:{tool}",
                "bom-ref": f"tool:{tool}",
                "properties": [{"name": "kind", "value": "tool"}],
            }
        )
    for model in _declared_models(spec):
        components.append(
            {
                "type": "library",
                "name": f"model:{model}",
                "bom-ref": f"model:{model}",
                "properties": [{"name": "kind", "value": "model"}],
            }
        )
    components.sort(key=lambda row: (str(row.get("type")), str(row.get("name"))))
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "readyagents",
                        "version": __version__,
                    }
                ]
            },
            "component": {
                "type": "application",
                "name": spec.name,
                "version": spec.version,
                "bom-ref": f"workflow:{spec.name}",
                "hashes": [_sha(report.digest)],
            },
        },
        "components": components,
    }
    _assert_no_secrets(bom)
    return bom


def dumps_sbom(bom: dict[str, Any]) -> str:
    return json.dumps(bom, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def write_sbom(bom: dict[str, Any], path: Path | str) -> Path:
    dest = Path(path)
    atomic_write_text(dest, dumps_sbom(bom), encoding="utf-8", newline="\n", restrict=False)
    return dest


def _file_component(name: str, digest: str, kind: str) -> dict[str, Any]:
    hex_digest = digest.split(":", 1)[-1]
    return {
        "type": "file",
        "name": name,
        "bom-ref": f"{kind}:{name}",
        "hashes": [{"alg": "SHA-256", "content": hex_digest}],
        "properties": [{"name": "kind", "value": kind}],
    }


def _sha(digest: str) -> dict[str, str]:
    return {"alg": "SHA-256", "content": digest.split(":", 1)[-1]}


def _declared_tools(spec: Any) -> list[str]:
    names: set[str] = set()
    for node in spec.nodes:
        if getattr(node, "tool", None):
            names.add(str(node.tool))
        for item in getattr(node, "tools", None) or []:
            names.add(str(item))
        for branch in getattr(node, "branches", None) or []:
            if getattr(branch, "tool", None):
                names.add(str(branch.tool))
            for item in getattr(branch, "tools", None) or []:
                names.add(str(item))
        body = getattr(node, "body", None)
        if body is not None and getattr(body, "tool", None):
            names.add(str(body.tool))
    return sorted(names)


def _declared_models(spec: Any) -> list[str]:
    names: set[str] = set()
    if spec.default_model:
        names.add(str(spec.default_model))
    for item in spec.fallback_models or []:
        names.add(str(item))
    for node in spec.nodes:
        if getattr(node, "model", None):
            names.add(str(node.model))
        for item in getattr(node, "fallback_models", None) or []:
            names.add(str(item))
    return sorted(names)


def _assert_no_secrets(value: Any, *, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            token = str(child_key).lower().replace("-", "_")
            leaked = token in _SECRET_KEYS or any(
                part in token for part in ("secret", "password", "token")
            )
            if leaked:
                raise AssertionError(f"SBOM leaked secret-bearing key {child_key!r}")
            _assert_no_secrets(child, key=str(child_key))
    elif isinstance(value, list):
        for item in value:
            _assert_no_secrets(item, key=key)
