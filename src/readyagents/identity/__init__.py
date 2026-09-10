"""Verified approvers, local trust anchors, optional workload identity.

Interface freeze (TASK-06):

* VerifiedActor: method "none" is today's --actor path; "jwt"/"oidc" is identified.
  Signed (HMAC of the decision body) is a separate property.
* Trust anchors: local YAML + JWKS file. Missing/malformed/unreadable fails
  closed when a token is presented or require=true. Never silent method=none.
* Replay: consume_assertion binds token_id to run/node/decision.
"""

from __future__ import annotations

from readyagents.identity.actor import VerifiedActor, anonymous_actor
from readyagents.identity.anchors import (
    TrustAnchors,
    load_trust_anchors,
    resolve_trust_path,
)
from readyagents.identity.replay import consume_assertion
from readyagents.identity.verify import verify_token
from readyagents.identity.workload import sign_assertion, whoami, workload_configured

__all__ = [
    "TrustAnchors",
    "VerifiedActor",
    "anonymous_actor",
    "consume_assertion",
    "load_trust_anchors",
    "resolve_trust_path",
    "sign_assertion",
    "verify_token",
    "whoami",
    "workload_configured",
]
