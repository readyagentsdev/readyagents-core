"""Refuse assertion replay. Bind token id to run/node/decision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import IdentityError
from readyagents.identity.actor import VerifiedActor
from readyagents.permissions import restrict_file
from readyagents.workflow.state import utc_now

REPLAY_NAME = "replay.jsonl"


def replay_path(home: Path) -> Path:
    return Path(home) / "identity" / REPLAY_NAME


def consume_assertion(
    home: Path,
    actor: VerifiedActor,
    *,
    run_id: str,
    node_id: str,
    decision: str,
    replay: str = "never",
) -> str:
    """Record use of this assertion. Raise IdentityError on replay."""
    token_id = actor.token_id or actor.subject
    if not token_id:
        raise IdentityError("identity token refused: missing token id")
    binding = _binding(token_id, run_id, node_id, decision)
    path = replay_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    from readyagents.permissions import restrict_dir

    restrict_dir(path.parent)
    seen = _load(path)
    prior = seen.get(token_id)
    if prior is not None:
        if replay == "never":
            raise IdentityError("identity token refused: replay")
        if replay == "run" and (prior.get("run_id") != run_id or prior.get("node_id") != node_id):
            raise IdentityError("identity token refused: replay across gates")
        if prior.get("binding") == binding:
            raise IdentityError("identity token refused: replay")
        raise IdentityError("identity token refused: replay")
    row = {
        "token_id": token_id,
        "run_id": run_id,
        "node_id": node_id,
        "decision": decision,
        "binding": binding,
        "issuer": actor.issuer,
        "subject": actor.subject,
        "ts": utc_now(),
    }
    line = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write_text(path, existing + line, encoding="utf-8", newline="\n")
    restrict_file(path)
    return binding


def _binding(token_id: str, run_id: str, node_id: str, decision: str) -> str:
    payload = f"{token_id}|{run_id}|{node_id}|{decision}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("token_id"):
            out[str(row["token_id"])] = row
    return out
