from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from readyagents.approvals.tokens import TokenError, TokenService


def test_bootstrap_once_then_fail() -> None:
    svc = TokenService(bootstrap_ttl=60)
    token = svc.issue_bootstrap()
    session = svc.consume_bootstrap(token)
    assert svc.verify_session(session)
    with pytest.raises(TokenError, match="invalid token"):
        svc.consume_bootstrap(token)
    assert "token=" not in repr(svc)
    assert token not in repr(svc)


def test_bootstrap_expires() -> None:
    svc = TokenService(bootstrap_ttl=0.05)
    token = svc.issue_bootstrap()
    time.sleep(0.08)
    with pytest.raises(TokenError):
        svc.consume_bootstrap(token)


def test_garbage_session() -> None:
    svc = TokenService()
    assert svc.verify_session("nope") is False
    assert svc.verify_session("") is False


def test_action_once_and_replay() -> None:
    svc = TokenService(action_ttl=60)
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    svc.consume_action(token, run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(token, run_id="a" * 32, node_id="gate", revision=3, decision="approve")


def test_action_wrong_bindings() -> None:
    svc = TokenService()
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(token, run_id="b" * 32, node_id="gate", revision=3, decision="approve")
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(token, run_id="a" * 32, node_id="other", revision=3, decision="approve")
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(token, run_id="a" * 32, node_id="gate", revision=9, decision="approve")
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=3, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(token, run_id="a" * 32, node_id="gate", revision=3, decision="reject")


def test_forged_truncated_malformed() -> None:
    svc = TokenService()
    with pytest.raises(TokenError):
        svc.consume_action(
            "not-a-token", run_id="a" * 32, node_id="g", revision=1, decision="approve"
        )
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=1, decision="approve")
    with pytest.raises(TokenError):
        svc.consume_action(
            token[:-4], run_id="a" * 32, node_id="gate", revision=1, decision="approve"
        )
    left, right = token.split(".", 1)
    forged = left + "." + ("A" * len(right))
    with pytest.raises(TokenError):
        svc.consume_action(forged, run_id="a" * 32, node_id="gate", revision=1, decision="approve")


def test_concurrent_consume_one_winner() -> None:
    svc = TokenService()
    token = svc.issue_action(run_id="a" * 32, node_id="gate", revision=1, decision="approve")

    def _go() -> str:
        try:
            svc.consume_action(
                token, run_id="a" * 32, node_id="gate", revision=1, decision="approve"
            )
            return "ok"
        except TokenError:
            return "fail"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _go(), range(8)))
    assert results.count("ok") == 1
    assert results.count("fail") == 7
