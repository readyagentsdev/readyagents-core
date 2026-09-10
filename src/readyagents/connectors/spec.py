"""Typed connector declaration. Governance reads this, not author goodwill."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Determinism = Literal["recomputed", "sealable", "unsealable"]
SideEffects = Literal["none", "read", "write"]
AuthKind = Literal["none", "bearer", "header", "query"]


@dataclass(frozen=True)
class AuthSpec:
    kind: AuthKind = "none"
    secret: str | None = None
    header: str | None = None
    query_param: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "secret": self.secret,
            "header": self.header,
            "query_param": self.query_param,
        }


@dataclass(frozen=True)
class RateLimitSpec:
    requests: int
    window_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "window_seconds": self.window_seconds}


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    auth: AuthSpec | None = None
    destinations: tuple[str, ...] = ()
    determinism: Determinism = "unsealable"
    idempotent: bool = True
    idempotency_key: str | None = None
    rate_limit: RateLimitSpec | None = None
    side_effects: SideEffects = "read"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "auth": self.auth.as_dict() if self.auth else None,
            "destinations": list(self.destinations),
            "determinism": self.determinism,
            "idempotent": self.idempotent,
            "idempotency_key": self.idempotency_key,
            "rate_limit": self.rate_limit.as_dict() if self.rate_limit else None,
            "side_effects": self.side_effects,
        }


class Connector(Protocol):
    spec: ConnectorSpec

    def call(self, args: Mapping[str, Any], ctx: Any) -> Any: ...


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.text() or "null")
