"""Verify OIDC ID tokens / JWTs against local trust anchors via PyJWT.

Core does not parse JOSE by hand. The ``jwt`` extra is required to verify.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.errors import IdentityError
from readyagents.identity.actor import VerifiedActor
from readyagents.identity.anchors import TrustAnchors, load_jwks_file
from readyagents.identity.claims import map_actor, map_roles, sanitise_claims
from readyagents.workflow.state import utc_now

MAX_TOKEN_BYTES = 16_384
# Asymmetric only. HS* against a JWKS RSA key is algorithm confusion.
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA")
_RSA = frozenset({"RS256", "RS384", "RS512"})
_EC = frozenset({"ES256", "ES384", "ES512"})
_OKP = frozenset({"EdDSA"})


def require_jwt_lib() -> Any:
    try:
        import jwt
    except ImportError as exc:
        raise IdentityError(
            "JWT extra is not installed. Install with: pip install 'readyagentsdev[jwt]'"
        ) from exc
    return jwt


def verify_token(
    token: str,
    anchors: TrustAnchors,
    *,
    base: Path | None = None,
) -> VerifiedActor:
    """Verify signature, iss, aud, exp/nbf/skew. Fail closed on every error."""
    jwt = require_jwt_lib()
    raw = (token or "").strip()
    if not raw:
        raise IdentityError("empty identity token")
    if len(raw.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise IdentityError(f"identity token exceeds {MAX_TOKEN_BYTES} bytes")
    try:
        header = jwt.get_unverified_header(raw)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"malformed identity token: {exc}") from exc
    if not isinstance(header, dict):
        raise IdentityError("malformed identity token header")
    alg = str(header.get("alg") or "")
    if not alg or alg.lower() == "none":
        raise IdentityError("identity token refused: alg none")
    if alg not in ALLOWED_ALGS:
        raise IdentityError(f"identity token refused: algorithm '{alg}' is not allowed")
    try:
        unverified = jwt.decode(raw, options={"verify_signature": False, "verify_exp": False})
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"malformed identity token payload: {exc}") from exc
    if not isinstance(unverified, dict):
        raise IdentityError("malformed identity token payload")
    issuer = str(unverified.get("iss") or "")
    if not issuer:
        raise IdentityError("identity token refused: missing issuer")
    anchor = anchors.issuer_for(issuer)
    if anchor is None:
        raise IdentityError(f"identity token refused: unknown issuer '{issuer}'")
    if anchor.allow_jwks_fetch:
        raise IdentityError("JWKS network fetch is not enabled in this build (offline default)")
    jwks = load_jwks_file(anchor.jwks_file, base=base)
    key = _key_for_kid(jwks, header.get("kid"), alg=alg)
    audiences = anchor.audiences()
    try:
        claims = jwt.decode(
            raw,
            key=key,
            algorithms=[alg],
            audience=audiences[0] if len(audiences) == 1 else audiences,
            issuer=anchor.issuer,
            leeway=anchor.max_skew_seconds,
            options={
                "require": ["exp", "iss", "sub"],
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_nbf": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise IdentityError("identity token refused: expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise IdentityError("identity token refused: not yet valid") from exc
    except jwt.InvalidAudienceError as exc:
        raise IdentityError("identity token refused: audience mismatch") from exc
    except jwt.InvalidIssuerError as exc:
        raise IdentityError("identity token refused: issuer mismatch") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise IdentityError(f"identity token refused: algorithm '{alg}'") from exc
    except jwt.InvalidTokenError as exc:
        raise IdentityError(f"identity token refused: {exc}") from exc
    if not isinstance(claims, dict):
        raise IdentityError("identity token refused: claims are not a mapping")
    actor = map_actor(claims, claim=anchor.actor_claim)
    roles = map_roles(claims, role_claims=anchor.role_claims, role_map=anchor.role_map)
    names = (anchor.actor_claim, "sub", "iss", *anchor.role_claims)
    allowlisted = sanitise_claims(claims, names=names)
    token_id = str(claims.get("jti") or "") or _token_fingerprint(raw)
    method = "oidc" if "nonce" in claims or str(claims.get("token_use") or "") == "id" else "jwt"
    return VerifiedActor(
        actor=actor,
        subject=str(claims.get("sub") or ""),
        issuer=anchor.issuer,
        claims=allowlisted,
        verified_at=utc_now(),
        method=method,
        roles=tuple(roles),
        token_id=token_id,
    )


def _key_for_kid(jwks: dict[str, Any], kid: Any, *, alg: str) -> Any:
    jwt = require_jwt_lib()
    keys = [item for item in jwks.get("keys") or [] if isinstance(item, dict)]
    if kid:
        matches = [item for item in keys if str(item.get("kid") or "") == str(kid)]
        if not matches:
            raise IdentityError(f"identity token refused: unknown kid '{kid}'")
        material = matches[0]
    elif len(keys) == 1:
        material = keys[0]
    else:
        raise IdentityError("identity token refused: missing kid and JWKS has multiple keys")
    kty = str(material.get("kty") or "").upper()
    if alg in _RSA and kty != "RSA":
        raise IdentityError("identity token refused: algorithm does not match JWKS key type")
    if alg in _EC and kty not in {"EC", "EC2"}:
        raise IdentityError("identity token refused: algorithm does not match JWKS key type")
    if alg in _OKP and kty != "OKP":
        raise IdentityError("identity token refused: algorithm does not match JWKS key type")
    if kty in {"oct", "OCT"}:
        raise IdentityError("identity token refused: symmetric JWKS keys are not accepted")
    try:
        return jwt.PyJWK.from_dict(material).key
    except Exception as exc:  # noqa: BLE001
        raise IdentityError(f"identity token refused: JWKS key is unusable: {exc}") from exc


def _token_fingerprint(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
