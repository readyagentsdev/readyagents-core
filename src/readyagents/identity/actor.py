"""Verified-actor record. Signed (HMAC of the body) and identified are separate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

JSONScalar = str | int | float | bool | None


@dataclass(frozen=True)
class VerifiedActor:
    """Result of verifying an approver assertion — or the unconfigured path.

    ``method: "none"`` is today's ``--actor NAME`` behaviour and remains valid.
    A decision may be *signed* (HMAC of the payload) and/or *identified*
    (this record). Those properties are independent.
    """

    actor: str
    subject: str = ""
    issuer: str = ""
    claims: Mapping[str, JSONScalar] = field(default_factory=dict)
    verified_at: str = ""
    method: str = "none"
    roles: tuple[str, ...] = ()
    token_id: str = ""

    def identified(self) -> bool:
        return self.method in {"oidc", "jwt"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "subject": self.subject,
            "issuer": self.issuer,
            "claims": dict(self.claims),
            "verified_at": self.verified_at,
            "method": self.method,
            "roles": list(self.roles),
            "identified": self.identified(),
        }


def anonymous_actor(name: str | None) -> VerifiedActor:
    """Unconfigured path: a typed name, not a verified identity."""
    return VerifiedActor(actor=str(name or ""), method="none")
