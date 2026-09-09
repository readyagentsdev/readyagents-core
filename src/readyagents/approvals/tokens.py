"""In-memory bootstrap, session, and single-use action tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict

_GENERIC = "invalid token"
_MAX_REPLAY = 4096


class TokenError(Exception):
    """Invalid, expired, or already-used token."""

    def __init__(self, message: str = _GENERIC) -> None:
        super().__init__(message)


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    pad = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:  # noqa: BLE001
        raise TokenError(_GENERIC) from exc


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class TokenService:
    def __init__(
        self,
        *,
        secret: bytes | None = None,
        bootstrap_ttl: float = 300,
        session_ttl: float = 1800,
        action_ttl: float = 300,
    ) -> None:
        self._secret = secret if secret else secrets.token_bytes(32)
        if len(self._secret) < 16:
            raise ValueError("token secret must be at least 16 bytes")
        self.bootstrap_ttl = float(bootstrap_ttl)
        self.session_ttl = float(session_ttl)
        self.action_ttl = float(action_ttl)
        self._lock = threading.Lock()
        self._bootstrap: dict[bytes, float] = {}
        self._sessions: dict[bytes, float] = {}
        self._replay: OrderedDict[bytes, float] = OrderedDict()

    def __repr__(self) -> str:
        return f"TokenService(bootstrap_ttl={self.bootstrap_ttl!r})"

    def issue_bootstrap(self) -> str:
        token = secrets.token_urlsafe(32)
        exp = time.time() + self.bootstrap_ttl
        with self._lock:
            self._bootstrap[_digest(token)] = exp
        return token

    def consume_bootstrap(self, token: str) -> str:
        if not isinstance(token, str) or not token:
            raise TokenError(_GENERIC)
        digest = _digest(token)
        now = time.time()
        with self._lock:
            exp = self._bootstrap.pop(digest, None)
            if exp is None or exp < now:
                raise TokenError(_GENERIC)
        return self._issue_session()

    def verify_session(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        digest = _digest(token)
        now = time.time()
        with self._lock:
            exp = self._sessions.get(digest)
            if exp is None or exp < now:
                self._sessions.pop(digest, None)
                return False
            return True

    def issue_action(
        self,
        *,
        run_id: str,
        node_id: str,
        revision: int,
        decision: str,
    ) -> str:
        nonce = secrets.token_urlsafe(16)
        exp = time.time() + self.action_ttl
        payload = {
            "p": "action",
            "run_id": run_id,
            "node_id": node_id,
            "revision": int(revision),
            "decision": decision,
            "exp": exp,
            "nonce": nonce,
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        mac = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return f"{_b64(raw)}.{_b64(mac)}"

    def consume_action(
        self,
        token: str,
        *,
        run_id: str,
        node_id: str,
        revision: int,
        decision: str,
    ) -> None:
        if not isinstance(token, str) or "." not in token:
            raise TokenError(_GENERIC)
        left, right = token.split(".", 1)
        try:
            raw = _unb64(left)
            mac = _unb64(right)
        except TokenError:
            raise
        expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, mac):
            raise TokenError(_GENERIC)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TokenError(_GENERIC) from exc
        if not isinstance(payload, dict):
            raise TokenError(_GENERIC)
        if payload.get("p") != "action":
            raise TokenError(_GENERIC)
        try:
            exp = float(payload.get("exp"))
        except (TypeError, ValueError) as exc:
            raise TokenError(_GENERIC) from exc
        if exp < time.time():
            raise TokenError(_GENERIC)
        if (
            str(payload.get("run_id")) != run_id
            or str(payload.get("node_id")) != node_id
            or int(payload.get("revision", -1)) != int(revision)
            or str(payload.get("decision")) != decision
        ):
            raise TokenError(_GENERIC)
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise TokenError(_GENERIC)
        nonce_digest = _digest(nonce)
        with self._lock:
            if nonce_digest in self._replay:
                raise TokenError(_GENERIC)
            self._replay[nonce_digest] = exp
            while len(self._replay) > _MAX_REPLAY:
                self._replay.popitem(last=False)

    def _issue_session(self) -> str:
        token = secrets.token_urlsafe(32)
        exp = time.time() + self.session_ttl
        with self._lock:
            self._sessions[_digest(token)] = exp
        return token
