"""Adversarial suite for TASK-06 §7. Drive shipped code only; fail closed."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import ApprovalRequired, IdentityError
from readyagents.identity.anchors import load_trust_anchors
from readyagents.identity.decide import identify_approver, load_token_file
from readyagents.identity.replay import consume_assertion
from readyagents.identity.verify import verify_token
from readyagents.secrets import MappingSecrets
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file
from tests.test_identity import AUD, ISSUER, _rsa_pair, _token, _write_trust

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
_SECRET = "adv-partner-token-LEAK-ME-99"


def _stdout_json(text: str) -> dict:
    start = text.find("{")
    assert start != -1, text
    return json.loads(text[start:])


# --- 1. alg: none refused ---


def test_alg_none_refused_verify_and_cli(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUD,
                    "sub": "attacker",
                    "email": "attacker@example.com",
                    "exp": int(time.time()) + 600,
                    "jti": "none-1",
                }
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    forged = f"{header}.{payload}."
    with pytest.raises(IdentityError, match="alg none"):
        verify_token(forged, anchors, base=tmp_path)

    token_path = tmp_path / "none.jwt"
    token_path.write_text(forged, encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(trust))
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    result = runner.invoke(
        app, ["identity", "verify", "--token-file", str(token_path), "--json"]
    )
    assert result.exit_code != 0
    blob = (result.stdout + result.stderr).lower()
    assert "alg none" in blob or "identityerror" in blob


# --- 2. Algorithm confusion (HS256 vs RSA JWKS) refused ---


def test_hs256_vs_rsa_jwks_confusion_refused(tmp_path: Path) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "k1"
    jwk.pop("key_ops", None)
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": AUD,
        "sub": "confused",
        "email": "confused@example.com",
        "exp": now + 600,
        "jti": "hs-confusion",
    }
    # Classic confusion: HMAC with the RSA public key material as the secret.
    confused = jwt.encode(body, pub_der, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(IdentityError, match="algorithm"):
        verify_token(confused, anchors, base=tmp_path)
    # Also refuse a random HMAC secret presented as HS256 against RSA JWKS.
    other = jwt.encode(
        body, "not-an-rsa-key-but-long-enough-for-hs256!!", algorithm="HS256", headers={"kid": "k1"}
    )
    with pytest.raises(IdentityError, match="algorithm"):
        verify_token(other, anchors, base=tmp_path)
    # Sanity: a real RS256 token still verifies.
    good = _token(pem, {"jti": "ok-after-confusion"})
    actor = verify_token(good, anchors, base=tmp_path)
    assert actor.identified() is True


# --- 3. Unknown kid refused ---


def test_unknown_kid_refused(tmp_path: Path) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    with pytest.raises(IdentityError, match="unknown kid"):
        verify_token(_token(pem, {"jti": "kid-x"}, kid="attacker-kid"), anchors, base=tmp_path)


# --- 4. Missing/wrong audience, wrong issuer, expired, nbf ---


def test_audience_issuer_exp_nbf_refused(tmp_path: Path) -> None:
    import jwt

    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    anchors = load_trust_anchors(trust)
    now = int(time.time())

    with pytest.raises(IdentityError, match="audience|aud"):
        verify_token(_token(pem, {"aud": "evil-audience", "jti": "aud-bad"}), anchors, base=tmp_path)

    # Missing aud claim.
    missing_aud = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "user-1",
            "email": "approver@example.com",
            "iat": now,
            "nbf": now - 5,
            "exp": now + 600,
            "jti": "aud-missing",
        },
        pem,
        algorithm="RS256",
        headers={"kid": "k1", "alg": "RS256"},
    )
    with pytest.raises(IdentityError, match="audience|aud"):
        verify_token(missing_aud, anchors, base=tmp_path)

    with pytest.raises(IdentityError, match="unknown issuer|issuer"):
        verify_token(
            _token(pem, {"iss": "https://evil.example/", "jti": "iss-bad"}),
            anchors,
            base=tmp_path,
        )

    with pytest.raises(IdentityError, match="expired"):
        verify_token(
            _token(pem, {"exp": now - 120, "nbf": now - 180, "jti": "exp"}),
            anchors,
            base=tmp_path,
        )

    with pytest.raises(IdentityError, match="not yet valid"):
        verify_token(
            _token(pem, {"nbf": now + 600, "exp": now + 1200, "jti": "nbf"}),
            anchors,
            base=tmp_path,
        )


# --- 5. Unreadable / malformed trust-anchor fails closed when verifying ---


def test_malformed_and_unreadable_trust_fails_closed_on_verify(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, _jwk = _rsa_pair()
    token_path = tmp_path / "id.jwt"
    token_path.write_text(_token(pem, {"jti": "anchor-check"}), encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))

    malformed = tmp_path / "malformed-trust.yaml"
    malformed.write_text("{[ not yaml", encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(malformed))
    bad_cli = runner.invoke(
        app, ["identity", "verify", "--token-file", str(token_path), "--json"]
    )
    assert bad_cli.exit_code != 0
    assert "IdentityError" in (bad_cli.stdout + bad_cli.stderr) or "malformed" in (
        bad_cli.stdout + bad_cli.stderr
    ).lower()

    with pytest.raises(IdentityError):
        identify_approver(
            load_token_file(token_path),
            home=tmp_settings.home_path(),
            run_id="run-malformed",
            node_id="gate",
            decision="approve",
            trust_path=malformed,
        )

    unreadable = tmp_path / "unreadable-trust.yaml"
    unreadable.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "issuers": [
                    {
                        "issuer": ISSUER,
                        "audience": AUD,
                        "jwks_file": "missing-jwks.json",
                        "actor_claim": "email",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(unreadable, 0)
    try:
        monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(unreadable))
        unread_cli = runner.invoke(
            app, ["identity", "verify", "--token-file", str(token_path), "--json"]
        )
        assert unread_cli.exit_code != 0
        blob = (unread_cli.stdout + unread_cli.stderr).lower()
        assert "identityerror" in blob or "unreadable" in blob or "permission" in blob
        with pytest.raises(IdentityError, match="unreadable|Permission|not found|malformed"):
            identify_approver(
                load_token_file(token_path),
                home=tmp_settings.home_path(),
                run_id="run-unreadable",
                node_id="gate",
                decision="approve",
                trust_path=unreadable,
            )
    finally:
        os.chmod(unreadable, 0o600)

    # Missing JWKS referenced by a readable trust file also fails closed on verify.
    dangling = tmp_path / "dangling-trust.yaml"
    dangling.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "issuers": [
                    {
                        "issuer": ISSUER,
                        "audience": AUD,
                        "jwks_file": str(tmp_path / "no-such-jwks.json"),
                        "actor_claim": "email",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    anchors = load_trust_anchors(dangling)
    with pytest.raises(IdentityError, match="unreadable|not found|JWKS"):
        verify_token(_token(pem, {"jti": "dangling"}), anchors, base=tmp_path)


# --- 6. Replay: same token twice on one gate refused ---


def test_replay_same_token_twice_on_gate_refused(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, jwk = _rsa_pair()
    trust = _write_trust(tmp_path, jwk)
    token = _token(pem, {"jti": "replay-once"})
    token_path = tmp_path / "id.jwt"
    token_path.write_text(token, encoding="utf-8")
    home = tmp_settings.home_path()

    first = identify_approver(
        token,
        home=home,
        run_id="run-replay",
        node_id="gate",
        decision="approve",
        trust_path=trust,
    )
    assert first.identified() is True
    assert first.token_id == "replay-once"

    with pytest.raises(IdentityError, match="replay"):
        identify_approver(
            token,
            home=home,
            run_id="run-replay",
            node_id="gate",
            decision="approve",
            trust_path=trust,
        )

    # consume_assertion itself refuses a second binding for the same token_id.
    with pytest.raises(IdentityError, match="replay"):
        consume_assertion(
            home,
            first,
            run_id="run-replay",
            node_id="gate",
            decision="approve",
            replay="never",
        )

    # End-to-end via decide CLI / run_workflow_file gate.
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    run_id = exc.value.run_id
    fresh = _token(pem, {"jti": "cli-replay"})
    token_path.write_text(fresh, encoding="utf-8")
    monkeypatch.setenv("READYAGENTS_TRUST_ANCHORS", str(trust))
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    ok = runner.invoke(
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
    assert ok.exit_code == 0, ok.stdout + ok.stderr
    again = runner.invoke(
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
    assert again.exit_code != 0
    assert "replay" in (again.stdout + again.stderr).lower()


# --- 7. Secret leakage: run record / logs; non-granted tool cannot read env ---


def test_brokered_secret_not_leaked_to_record_logs_or_other_tool(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PARTNER_API_TOKEN", _SECRET)
    seen: dict[str, str | None] = {}

    def grab(**_k: object) -> str:
        seen["grab_env"] = os.environ.get("PARTNER_API_TOKEN")
        from readyagents.credentials.broker import current_granted

        seen["grab_map"] = current_granted().get("PARTNER_API_TOKEN")
        return "grabbed"

    def other(**_k: object) -> str:
        seen["other_env"] = os.environ.get("PARTNER_API_TOKEN")
        from readyagents.credentials.broker import current_granted

        seen["other_map"] = current_granted().get("PARTNER_API_TOKEN")
        # Hostile attempt: read process environ dump.
        seen["environ_dump_has"] = _SECRET if _SECRET in " ".join(
            f"{k}={v}" for k, v in os.environ.items()
        ) else None
        return "other-ok"

    creds = tmp_path / "readyagents.credentials.yaml"
    creds.write_text(
        "version: 1\ncredentials:\n  grab:\n    secrets: [PARTNER_API_TOKEN]\n"
        "  other:\n    secrets: []\n",
        encoding="utf-8",
    )
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        yaml.safe_dump(
            {
                "name": "adv-cred",
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
    tools.register(FunctionTool(name="grab", description="g", handler=grab, schema={}))
    tools.register(FunctionTool(name="other", description="o", handler=other, schema={}))

    logger = logging.getLogger("readyagents")
    with caplog.at_level(logging.DEBUG, logger="readyagents"):
        logger.setLevel(logging.DEBUG)
        state = run_workflow_file(
            wf,
            settings=tmp_settings,
            persist=True,
            extra_tools=tools,
            secrets=MappingSecrets({"PARTNER_API_TOKEN": _SECRET}),
            credentials=creds,
        )

    assert state.status == "succeeded"
    assert seen["grab_env"] == _SECRET
    assert seen["grab_map"] == _SECRET
    assert seen["other_env"] is None
    assert seen["other_map"] is None
    assert seen["environ_dump_has"] is None
    # After the brokered call, process env is restored (original fixture value).
    assert os.environ.get("PARTNER_API_TOKEN") == _SECRET

    record = json.dumps(state.to_record())
    assert _SECRET not in record
    cred_meta = state.metadata.get("credentials") or {}
    assert cred_meta["grab"]["secrets"] == ["PARTNER_API_TOKEN"]
    assert _SECRET not in json.dumps(cred_meta)

    log_blob = caplog.text
    assert _SECRET not in log_blob

    # Persisted run + audit under home must not contain the secret value.
    home = tmp_settings.home_path()
    persisted = []
    for path in home.rglob("*"):
        if path.is_file():
            try:
                persisted.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    joined = "\n".join(persisted)
    assert _SECRET not in joined


# --- §7 extras: claim injection + signed ≠ identified ---


def test_hostile_claims_do_not_become_roles(tmp_path: Path) -> None:
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
        verify_token(_token(pem, {"email": "../../etc/passwd", "jti": "path"}), anchors, base=tmp_path)
    actor = verify_token(
        _token(
            pem,
            {
                "groups": ["approvers", "../root", "admin;rm", "not-mapped"],
                "jti": "roles",
            },
        ),
        anchors,
        base=tmp_path,
    )
    assert "approver" in actor.roles
    assert "../root" not in actor.roles
    assert "admin;rm" not in actor.roles
    assert "not-mapped" not in actor.roles


def test_signed_is_not_identified_without_token(
    tmp_path: Path, tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed-but-anonymous decision must not look identified."""
    monkeypatch.setenv("READYAGENTS_DECISION_SECRET", "adv-hmac-secret")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    wf = tmp_path / "approval_gate.yaml"
    wf.write_text(
        (ROOT / "examples" / "approval_gate.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    result = runner.invoke(
        app,
        [
            "decide",
            exc.value.run_id,
            "--node",
            "gate",
            "--decision",
            "approve",
            "--actor",
            "local-only",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _stdout_json(result.stdout)
    rec = payload.get("run") or payload
    identity = (rec.get("metadata") or {}).get("identity")
    assert identity is None or identity.get("identified") in {None, False}
    assert identity is None or identity.get("method") in {None, "none"}
    # Actor string is recorded, but that is not cryptographic identification.
    assert (rec.get("metadata") or {}).get("actor") == "local-only" or payload.get(
        "actor"
    ) in {None, "local-only"}
