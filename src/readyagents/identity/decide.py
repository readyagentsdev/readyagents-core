"""Apply a token to a decision: verify, bind, consume replay."""

from __future__ import annotations

from pathlib import Path

from readyagents.errors import IdentityError
from readyagents.identity.actor import VerifiedActor
from readyagents.identity.anchors import load_trust_anchors, resolve_trust_path
from readyagents.identity.replay import consume_assertion
from readyagents.identity.verify import verify_token


def load_token_file(path: Path | str) -> str:
    file = Path(path)
    if not file.is_file():
        raise IdentityError(f"token file not found: {file}")
    try:
        text = file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise IdentityError(f"token file unreadable: {file}: {exc}") from exc
    if not text:
        raise IdentityError(f"token file is empty: {file}")
    return text


def identify_approver(
    token: str,
    *,
    home: Path,
    run_id: str,
    node_id: str,
    decision: str,
    trust_path: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> VerifiedActor:
    resolved = resolve_trust_path(explicit=trust_path, home=home, env=env)
    if resolved is None:
        raise IdentityError(
            "identity is required for --token-file but no trust-anchor file is configured"
        )
    anchors = load_trust_anchors(resolved)
    actor = verify_token(token, anchors, base=resolved.parent)
    issuer = anchors.issuer_for(actor.issuer)
    replay = issuer.replay if issuer is not None else "never"
    consume_assertion(
        home,
        actor,
        run_id=run_id,
        node_id=node_id,
        decision=decision,
        replay=replay,
    )
    return actor
