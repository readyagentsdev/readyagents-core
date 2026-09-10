"""Data-residency attestation. Technical evidence, not legal compliance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.errors import ConfigError
from readyagents.trust.digest import canonical_dumps, digest_bytes
from readyagents.workflow.state import RunState

ATTESTATION_VERSION = 1
KIND_ATTESTATION = "attestation"


def build_attestation(
    state: RunState,
    *,
    workflow_digest: str | None = None,
    mcp_names: list[str] | None = None,
) -> dict[str, Any]:
    meta = dict(state.metadata or {})
    network = meta.get("network") if isinstance(meta.get("network"), dict) else {}
    attempts = list(network.get("egress_attempts") or [])
    allowed = list(network.get("allowed_endpoints") or [])
    subprocesses: list[dict[str, Any]] = []
    names = list(mcp_names or [])
    raw_mcp = meta.get("mcp_servers")
    if isinstance(raw_mcp, list):
        names.extend(str(item) for item in raw_mcp)
    for name in names:
        subprocesses.append(
            {
                "kind": "mcp_stdio",
                "name": str(name),
                "network_uncontrolled": True,
            }
        )
    mode = "sovereign" if meta.get("sovereign") else "default"
    model = meta.get("model") if isinstance(meta.get("model"), dict) else {}
    digest = workflow_digest
    supply = meta.get("supply_chain") if isinstance(meta.get("supply_chain"), dict) else {}
    if not digest:
        artifacts = supply.get("artifacts") if isinstance(supply.get("artifacts"), list) else []
        for row in artifacts:
            if isinstance(row, dict) and row.get("kind") == "workflow":
                digest = str(row.get("digest") or "") or None
                break
    body: dict[str, Any] = {
        "attestation_version": ATTESTATION_VERSION,
        "run_id": state.run_id,
        "mode": mode,
        "network": {
            "egress_attempts": attempts,
            "allowed_endpoints": allowed,
            "dns": list(network.get("dns") or []),
        },
        "model": {
            "provider": model.get("provider"),
            "endpoint": model.get("endpoint"),
            "model": model.get("model"),
        },
        "workspace": meta.get("workspace"),
        "workflow_digest": digest,
        "run_digest": digest_bytes(
            canonical_dumps(
                {"run_id": state.run_id, "status": state.status, "workflow": state.workflow_name}
            ).encode("utf-8")
        ),
        "subprocesses": subprocesses,
        "readyagents_version": __version__,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limits": {
            "in_process_socket_guard": True,
            "os_sandbox": False,
            "dns_may_leak": True,
            "native_extension_bypass": True,
        },
    }
    if subprocesses:
        body["network"]["claims_no_egress"] = False
        body["network"]["note"] = (
            "MCP stdio subprocesses are network_uncontrolled; "
            "this document does not claim no egress"
        )
    return body


def sign_attestation(payload: dict[str, Any], *, key: Path) -> dict[str, Any]:
    """Detached Ed25519 over the canonical attestation JSON. Requires the sign extra."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError as extra:
        raise ConfigError("Signing attestations requires the optional 'sign' extra.") from extra
    import base64

    pem = Path(key).read_bytes()
    loaded = load_pem_private_key(pem, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ConfigError("attestation --key must be an Ed25519 private key")
    message = canonical_dumps(payload).encode("utf-8")
    signature = loaded.sign(message)
    pub = loaded.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        "version": 1,
        "algorithm": "ed25519",
        "kind": KIND_ATTESTATION,
        "digest": digest_bytes(message),
        "signature": base64.b64encode(signature).decode("ascii"),
        "key_id": digest_bytes(pub)[-16:],
    }


def dump_attestation(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
