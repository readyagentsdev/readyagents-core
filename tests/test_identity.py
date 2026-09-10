from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import ApprovalRequired, IdentityError
from readyagents.identity.anchors import load_trust_anchors
from readyagents.identity.verify import verify_token
from readyagents.secrets import MappingSecrets
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://login.example.com/"
AUD = "readyagents"


def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "k1"
    jwk.pop("key_ops", None)
    return pem, jwk


def _write_trust(tmp_path: Path, jwk: dict, **extra) -> Path:
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    trust = tmp_path / "trust.yaml"
    payload = {
        "version": 1,
        "issuers": [
            {
                "issuer": ISSUER,
                "audience": AUD,
                "jwks_file": str(jwks),
                "actor_claim": extra.get("actor_claim", "email"),
                "role_claims": extra.get("role_claims", ["groups"]),
                "role_map": extra.get("role_map", {}),
                "max_skew_seconds": extra.get("max_skew_seconds", 60),
            }
        ],
    }
    trust.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return trust


def _token(pem: bytes, claims: dict, *, kid: str = "k1", alg: str = "RS256") -> str:
    import jwt

    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUD,
        "sub": "user-1",
        "email": "approver@example.com",
        "iat": now,
        "nbf": now - 5,
        "exp": now + 600,
        "jti": "jti-1",
        **claims,
    }
    return jwt.encode(body, pem, algorithm=alg, headers={"kid": kid, "alg": alg})


def test_valid_token_accepted(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    actor = verify_token(_token(pem, {}), anchors, base=tmp_path)
    assert actor.actor == "approver@example.com"
    assert actor.subject == "user-1"
    assert actor.issuer == ISSUER
    assert actor.identified() is True
    assert actor.method in {"jwt", "oidc"}


def test_alg_none_refused(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUD,
                    "sub": "x",
                    "email": "x@example.com",
                    "exp": int(time.time()) + 60,
                }
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(IdentityError, match="alg none"):
        verify_token(f"{header}.{payload}.", anchors, base=tmp_path)


def test_hmac_vs_rsa_confusion_refused(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    import jwt

    now = int(time.time())
    # HS256 JWT presented against an RSA JWKS (algorithm confusion).
    confused = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUD,
            "sub": "x",
            "email": "x@example.com",
            "exp": now + 60,
        },
        "hmac-secret-not-rsa",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(IdentityError, match="algorithm"):
        verify_token(confused, anchors, base=tmp_path)


def test_unknown_kid_refused(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    with pytest.raises(IdentityError, match="unknown kid"):
        verify_token(_token(pem, {}, kid="attacker"), anchors, base=tmp_path)


def test_wrong_audience_and_issuer(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    with pytest.raises(IdentityError, match="audience"):
        verify_token(_token(pem, {"aud": "other"}), anchors, base=tmp_path)
    with pytest.raises(IdentityError, match="unknown issuer"):
        verify_token(_token(pem, {"iss": "https://evil.example/"}), anchors, base=tmp_path)


def test_skew_boundary_accepts_inside_and_refuses_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC
    from datetime import datetime as real_datetime

    pem, jwk = _rsa_pair()
    skew = 60
    trust = _write_trust(tmp_path, jwk, max_skew_seconds=skew)
    anchors = load_trust_anchors(trust)
    frozen = real_datetime.now(UTC)

    class _FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr("jwt.api_jwt.datetime", _FrozenDateTime)
    now = int(frozen.timestamp())
    inside = _token(
        pem,
        {"exp": now - (skew - 1), "nbf": now - 120, "iat": now - 120, "jti": "skew-in"},
    )
    actor = verify_token(inside, anchors, base=tmp_path)
    assert actor.identified() is True
    outside = _token(
        pem,
        {"exp": now - (skew + 1), "nbf": now - 180, "iat": now - 180, "jti": "skew-out"},
    )
    with pytest.raises(IdentityError, match="expired"):
        verify_token(outside, anchors, base=tmp_path)


def test_expired_and_nbf(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    now = int(time.time())
    with pytest.raises(IdentityError, match="expired"):
        verify_token(_token(pem, {"exp": now - 120, "nbf": now - 180}), anchors, base=tmp_path)
    with pytest.raises(IdentityError, match="not yet valid"):
        verify_token(_token(pem, {"nbf": now + 600, "exp": now + 1200}), anchors, base=tmp_path)


def test_malformed_and_oversized(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    with pytest.raises(IdentityError, match="malformed"):
        verify_token("not-a-jwt", anchors, base=tmp_path)
    with pytest.raises(IdentityError, match="exceeds"):
        verify_token("a." * 20_000, anchors, base=tmp_path)


def test_missing_malformed_unreadable_anchor_fails_closed(tmp_path: Path) -> None:
    from readyagents.identity.anchors import load_trust_anchors as load

    with pytest.raises(IdentityError, match="unreadable|not found|malformed"):
        load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("{[", encoding="utf-8")
    with pytest.raises(IdentityError):
        load(bad)
    empty = tmp_path / "empty.yaml"
    empty.write_text("version: 1\nissuers: []\n", encoding="utf-8")
    with pytest.raises(IdentityError, match="empty"):
        load(empty)


def test_unmapped_and_hostile_claims(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(
        tmp_path,
        jwk,
        actor_claim="email",
        role_claims=["groups"],
        role_map={"approvers": "approver"},
    )
    anchors = load_trust_anchors(trust)
    with pytest.raises(IdentityError, match="actor claim"):
        verify_token(_token(pem, {"email": "../root"}), anchors, base=tmp_path)
    actor = verify_token(
        _token(pem, {"groups": ["approvers", "../etc", "not-mapped", "ok role"]}),
        anchors,
        base=tmp_path,
    )
    assert "approver" in actor.roles
    assert "../etc" not in actor.roles
    assert "not-mapped" not in actor.roles


def test_replay_and_decide_cli(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    token_path = tmp_path / "id.jwt"
    token_path.write_text(_token(pem, {"jti": "once"}), encoding="utf-8")
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    run_id = exc.value.run_id
    monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(trust))
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    first = runner.invoke(
        app,
        [
            "decide",
            run_id,
            "--node",
            "gate",
            "--decision",
            "approve",
            "--token-file",
            str(token_path),
            "--json",
        ],
    )
    assert first.exit_code == 0, first.stdout + first.stderr
    payload = json.loads(first.stdout[first.stdout.find("{") :])
    assert payload["status"] == "succeeded"
    rec = payload.get("run") or payload
    identity = (rec.get("metadata") or {}).get("identity") or {}
    assert identity.get("identified") is True
    assert identity.get("subject") == "user-1"
    assert identity.get("method") in {"jwt", "oidc"}
    second = runner.invoke(
        app,
        [
            "decide",
            run_id,
            "--node",
            "gate",
            "--decision",
            "approve",
            "--token-file",
            str(token_path),
            "--json",
        ],
    )
    assert second.exit_code == 1
    assert "replay" in (second.stdout + second.stderr).lower()


def test_verify_cli_deterministic(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    token_path = tmp_path / "id.jwt"
    token_path.write_text(_token(pem, {}), encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(trust))
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    a = runner.invoke(app, ["identity", "verify", "--token-file", str(token_path), "--json"])
    b = runner.invoke(app, ["identity", "verify", "--token-file", str(token_path), "--json"])
    assert a.exit_code == 0, a.stdout + a.stderr
    assert b.exit_code == 0, b.stdout + b.stderr
    pa = json.loads(a.stdout[a.stdout.find("{") :])
    pb = json.loads(b.stdout[b.stdout.find("{") :])
    assert pa["ok"] is True and pb["ok"] is True
    assert pa["subject"] == pb["subject"] == "user-1"
    assert pa["issuer"] == pb["issuer"] == ISSUER
    assert pa["command"] == "identity verify"


def test_broker_grant_and_env_isolation(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-partner-token"
    monkeypatch.setenv("PARTNER_API_TOKEN", secret)
    seen: dict[str, str | None] = {}

    def granted_tool(**_k: object) -> str:
        seen["granted_env"] = os.environ.get("PARTNER_API_TOKEN")
        from readyagents.credentials.broker import current_granted

        seen["granted_map"] = current_granted().get("PARTNER_API_TOKEN")
        return "ok-granted"

    def other_tool(**_k: object) -> str:
        seen["other_env"] = os.environ.get("PARTNER_API_TOKEN")
        from readyagents.credentials.broker import current_granted

        seen["other_map"] = current_granted().get("PARTNER_API_TOKEN")
        return "ok-other"

    creds = tmp_path / "readyagents.credentials.yaml"
    creds.write_text(
        "version: 1\ncredentials:\n  grab:\n    secrets: [PARTNER_API_TOKEN]\n  other:\n    secrets: []\n",
        encoding="utf-8",
    )
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        yaml.safe_dump(
            {
                "name": "cred-wf",
                "start": "grab",
                "nodes": [
                    {"id": "grab", "type": "tool", "tool": "grab", "next": "other"},
                    {"id": "other", "type": "tool", "tool": "other"},
                ],
            }
        ),
        encoding="utf-8",
    )
    tools = ToolRegistry()
    tools.register(FunctionTool(name="grab", description="g", handler=granted_tool, schema={}))
    tools.register(FunctionTool(name="other", description="o", handler=other_tool, schema={}))
    state = run_workflow_file(
        wf,
        settings=tmp_settings,
        persist=True,
        extra_tools=tools,
        secrets=MappingSecrets({"PARTNER_API_TOKEN": secret}),
        credentials=creds,
    )
    assert state.status == "succeeded"
    # Grants are not delivered via process os.environ (parallel-safe).
    assert seen["granted_env"] is None
    assert seen["granted_map"] == secret
    assert seen["other_env"] is None
    assert seen["other_map"] is None
    assert os.environ.get("PARTNER_API_TOKEN") == secret
    record = json.dumps(state.to_record())
    assert secret not in record
    cred_meta = state.metadata.get("credentials") or {}
    assert cred_meta["grab"]["kind"] == "static"
    assert cred_meta["grab"]["secrets"] == ["PARTNER_API_TOKEN"]


def test_parallel_non_granted_branch_never_sees_secret(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    secret = "parallel-secret-token"
    monkeypatch.setenv("PARTNER_API_TOKEN", secret)
    seen: dict[str, str | None] = {}
    barrier = threading.Barrier(2, timeout=5)

    def granted_tool(**_k: object) -> str:
        from readyagents.credentials.broker import current_granted

        barrier.wait()
        time.sleep(0.15)
        seen["granted_env"] = os.environ.get("PARTNER_API_TOKEN")
        seen["granted_map"] = current_granted().get("PARTNER_API_TOKEN")
        return "ok-granted"

    def other_tool(**_k: object) -> str:
        from readyagents.credentials.broker import current_granted

        barrier.wait()
        time.sleep(0.15)
        seen["other_env"] = os.environ.get("PARTNER_API_TOKEN")
        seen["other_map"] = current_granted().get("PARTNER_API_TOKEN")
        return "ok-other"

    creds = tmp_path / "readyagents.credentials.yaml"
    creds.write_text(
        "version: 1\ncredentials:\n  grab:\n    secrets: [PARTNER_API_TOKEN]\n  other:\n    secrets: []\n",
        encoding="utf-8",
    )
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        yaml.safe_dump(
            {
                "name": "cred-parallel",
                "nodes": [
                    {
                        "id": "fan",
                        "type": "parallel",
                        "branches": [
                            {"id": "grab", "type": "tool", "tool": "grab"},
                            {"id": "other", "type": "tool", "tool": "other"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    tools = ToolRegistry()
    tools.register(FunctionTool(name="grab", description="g", handler=granted_tool, schema={}))
    tools.register(FunctionTool(name="other", description="o", handler=other_tool, schema={}))
    state = run_workflow_file(
        wf,
        settings=tmp_settings,
        persist=False,
        extra_tools=tools,
        secrets=MappingSecrets({"PARTNER_API_TOKEN": secret}),
        credentials=creds,
    )
    assert state.status == "succeeded"
    assert seen["granted_map"] == secret
    assert seen["granted_env"] is None
    assert seen["other_env"] is None
    assert seen["other_map"] is None
    assert os.environ.get("PARTNER_API_TOKEN") == secret


def test_subprocess_minimal_env() -> None:
    from readyagents.credentials.broker import minimal_child_env

    env = minimal_child_env({"PARTNER_API_TOKEN": "abc"})
    assert env.get("PARTNER_API_TOKEN") == "abc"
    assert "OPENAI_API_KEY" not in env


def test_actor_flag_still_default(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state = run_workflow_file(
        wf, settings=tmp_settings, persist=True, decisions={"gate": "approve"}, actor="local-user"
    )
    assert state.status == "succeeded"
    assert state.metadata.get("actor") == "local-user"
    identity = state.metadata.get("identity")
    assert identity is None or identity.get("method") in {None, "none"}


def test_sign_assertion_on_notify_verifies_against_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jwt
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from readyagents.notify import post_json

    pem, _jwk = _rsa_pair()
    key_path = tmp_path / "workload.pem"
    key_path.write_bytes(pem)
    subject = "agent://readyagents/local"
    monkeypatch.setenv("READYAGENTS_WORKLOAD_SUBJECT", subject)
    monkeypatch.setenv("READYAGENTS_WORKLOAD_KEY", str(key_path))
    monkeypatch.setenv("READYAGENTS_WORKLOAD_KID", "wk1")
    monkeypatch.setattr(
        "readyagents.mcp.builtin.socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))],
    )
    seen: dict[str, object] = {}

    def fake_exchange(*_a: object, **kwargs: object) -> tuple[int, bytes, str | None]:
        seen["headers"] = kwargs.get("headers")
        seen["body"] = kwargs.get("body")
        return 204, b"", None

    monkeypatch.setattr("readyagents.notify._http_exchange", fake_exchange)
    post_json("https://hooks.example/pause", {"event": "approval_required"})
    headers = seen["headers"]
    assert isinstance(headers, dict)
    auth = str(headers.get("Authorization") or "")
    assert auth.startswith("Bearer ")
    token = auth[len("Bearer ") :]
    private = load_pem_private_key(pem, password=None)
    public = private.public_key()
    claims = jwt.decode(
        token,
        key=public,
        algorithms=["RS256"],
        audience="https://hooks.example",
        issuer=subject,
    )
    assert claims["sub"] == subject
    blob = repr(headers) + repr(seen.get("body"))
    assert "PRIVATE" not in blob
    assert pem.decode() not in blob
    from readyagents.identity.workload import sign_assertion

    again = sign_assertion(audience="https://hooks.example")
    jwt.decode(
        again, key=public, algorithms=["RS256"], audience="https://hooks.example", issuer=subject
    )


def test_whoami_no_private_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pem, _jwk = _rsa_pair()
    key = tmp_path / "workload.pem"
    key.write_bytes(pem)
    monkeypatch.setenv("READYAGENTS_WORKLOAD_SUBJECT", "agent://readyagents/local")
    monkeypatch.setenv("READYAGENTS_WORKLOAD_KEY", str(key))
    monkeypatch.setenv("READYAGENTS_WORKLOAD_KID", "wk1")
    result = runner.invoke(app, ["identity", "whoami", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout[result.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["configured"] is True
    assert payload["fingerprint"].startswith("sha256:")
    blob = result.stdout + result.stderr
    assert "BEGIN" not in blob
    assert "PRIVATE" not in blob
    assert pem.decode() not in blob


def test_token_file_without_anchors_fails_closed(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, _jwk = _rsa_pair()
    token_path = tmp_path / "id.jwt"
    token_path.write_text(_token(pem, {}), encoding="utf-8")
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.delenv("READYAGENTS_TRUST_ANCHORS", raising=False)
    result = runner.invoke(
        app,
        [
            "decide",
            exc.value.run_id,
            "--node",
            "gate",
            "--decision",
            "approve",
            "--token-file",
            str(token_path),
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert "IdentityError" in result.stdout or "trust-anchor" in result.stdout.lower()
