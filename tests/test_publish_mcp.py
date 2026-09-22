"""MCP Registry tag-push waits until PyPI lists the tagged version."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPI_JSON = "https://pypi.org/pypi/readyagentsdev/2.0.10/json"
VERSION_NOT_FOUND = (
    'Error: publish failed: server returned status 400: {"title":"Bad Request",'
    '"status":400,"detail":"Failed to publish server","errors":[{"message":'
    '"registry validation failed for package 0 (readyagentsdev): PyPI package '
    "'readyagentsdev' exists, but version '2.0.10' was not found (status: 404). "
    "A newly published release can take a moment to appear on PyPI. Wait and "
    "retry, or publish version '2.0.10' before registering it\"}]}"
)
OWNERSHIP_400 = (
    'Error: publish failed: server returned status 400: {"title":"Bad Request",'
    '"status":400,"detail":"Failed to publish server","errors":[{"message":'
    '"authentication failed: you do not own this namespace"}]}'
)


def _load_wait_mod():
    path = ROOT / "scripts" / "wait_pypi_version.py"
    spec = importlib.util.spec_from_file_location("wait_pypi_version", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wait_pypi = _load_wait_mod()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_404_then_200_proceeds() -> None:
    clock = _Clock()
    statuses = [404, 200]
    urls: list[str] = []

    def get(url: str) -> int:
        urls.append(url)
        return statuses[len(urls) - 1]

    wait_pypi.wait_until_pypi_version_visible(
        "readyagentsdev",
        "2.0.10",
        timeout=60,
        interval=10,
        get=get,
        sleeper=clock.sleep,
        clock=clock.monotonic,
    )
    assert urls == [PYPI_JSON, PYPI_JSON]
    assert clock.sleeps == [10]


def test_wait_does_not_succeed_before_version_is_visible() -> None:
    clock = _Clock()
    statuses = [404, 404, 200]
    seen: list[int] = []
    finished = False

    def get(url: str) -> int:
        assert url == PYPI_JSON
        assert finished is False
        status = statuses[len(seen)]
        seen.append(status)
        return status

    wait_pypi.wait_until_pypi_version_visible(
        "readyagentsdev",
        "2.0.10",
        timeout=60,
        interval=10,
        get=get,
        sleeper=clock.sleep,
        clock=clock.monotonic,
    )
    finished = True
    assert seen == [404, 404, 200]
    assert clock.sleeps == [10, 10]
    assert clock.now == 20


def test_wait_persistent_404_fails_after_bound() -> None:
    clock = _Clock()
    n = {"gets": 0}

    def get(url: str) -> int:
        assert url == PYPI_JSON
        n["gets"] += 1
        return 404

    with pytest.raises(wait_pypi.PyPIVersionNotVisible, match="was not found") as exc:
        wait_pypi.wait_until_pypi_version_visible(
            "readyagentsdev",
            "2.0.10",
            timeout=30,
            interval=10,
            get=get,
            sleeper=clock.sleep,
            clock=clock.monotonic,
        )
    assert "last status: 404" in str(exc.value)
    assert PYPI_JSON in str(exc.value)
    assert n["gets"] >= 2
    assert clock.now >= 30


def test_wait_persistent_non_2xx_fails_after_bound() -> None:
    clock = _Clock()

    def get(url: str) -> int:
        assert url == PYPI_JSON
        return 500

    with pytest.raises(wait_pypi.PyPIVersionNotVisible, match="last status: 500"):
        wait_pypi.wait_until_pypi_version_visible(
            "readyagentsdev",
            "2.0.10",
            timeout=20,
            interval=10,
            get=get,
            sleeper=clock.sleep,
            clock=clock.monotonic,
        )


def test_wait_first_try_200_does_not_sleep() -> None:
    clock = _Clock()
    wait_pypi.wait_until_pypi_version_visible(
        "readyagentsdev",
        "2.0.10",
        timeout=600,
        interval=15,
        get=lambda url: 200,
        sleeper=clock.sleep,
        clock=clock.monotonic,
    )
    assert clock.sleeps == []


def test_retry_publish_version_not_found_then_success() -> None:
    clock = _Clock()
    calls: list[list[str]] = []

    def run(argv: list[str]):
        calls.append(list(argv))
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr=VERSION_NOT_FOUND)
        return SimpleNamespace(returncode=0, stdout="published\n", stderr="")

    wait_pypi.retry_mcp_publish(
        ["./mcp-publisher", "publish"],
        timeout=60,
        interval=15,
        run=run,
        sleeper=clock.sleep,
        clock=clock.monotonic,
    )
    assert calls == [["./mcp-publisher", "publish"], ["./mcp-publisher", "publish"]]
    assert clock.sleeps == [15]


def test_retry_publish_unrelated_400_fails_immediately() -> None:
    clock = _Clock()
    calls: list[list[str]] = []

    def run(argv: list[str]):
        calls.append(list(argv))
        return SimpleNamespace(returncode=1, stdout="", stderr=OWNERSHIP_400)

    with pytest.raises(wait_pypi.MCPPublishError, match="you do not own this namespace") as exc:
        wait_pypi.retry_mcp_publish(
            ["./mcp-publisher", "publish"],
            timeout=300,
            interval=15,
            run=run,
            sleeper=clock.sleep,
            clock=clock.monotonic,
        )
    assert exc.value.returncode == 1
    assert calls == [["./mcp-publisher", "publish"]]
    assert clock.sleeps == []


def test_retry_publish_persistent_version_not_found_fails_closed() -> None:
    clock = _Clock()

    def run(argv: list[str]):
        return SimpleNamespace(returncode=1, stdout="", stderr=VERSION_NOT_FOUND)

    with pytest.raises(wait_pypi.PyPIVersionNotVisible, match="status: 404"):
        wait_pypi.retry_mcp_publish(
            ["./mcp-publisher", "publish"],
            timeout=30,
            interval=10,
            run=run,
            sleeper=clock.sleep,
            clock=clock.monotonic,
        )
    assert clock.now >= 30


def test_main_wait_404_then_200(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    urls: list[str] = []
    codes = [404, 200]

    def get(url: str) -> int:
        urls.append(url)
        return codes[len(urls) - 1]

    monkeypatch.setattr(wait_pypi, "http_get", get)
    monkeypatch.setattr(wait_pypi, "sleep", clock.sleep)
    monkeypatch.setattr(wait_pypi, "monotonic", clock.monotonic)
    rc = wait_pypi.main(
        [
            "wait",
            "--package",
            "readyagentsdev",
            "--version",
            "2.0.10",
            "--timeout",
            "60",
            "--interval",
            "10",
        ]
    )
    assert rc == 0
    assert urls == [PYPI_JSON, PYPI_JSON]


def test_main_wait_timeout_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(wait_pypi, "http_get", lambda url: 404)
    monkeypatch.setattr(wait_pypi, "sleep", clock.sleep)
    monkeypatch.setattr(wait_pypi, "monotonic", clock.monotonic)
    rc = wait_pypi.main(["wait", "--version", "2.0.10", "--timeout", "20", "--interval", "10"])
    assert rc == 1


def test_publish_mcp_yml_waits_for_pypi_before_publish() -> None:
    text = (ROOT / ".github" / "workflows" / "publish-mcp.yml").read_text(encoding="utf-8")
    wait_cmd = "scripts/wait_pypi_version.py wait --package readyagentsdev --version"
    retry_cmd = "scripts/wait_pypi_version.py retry-publish"
    publish_cmd = "./mcp-publisher publish"
    login = "mcp-publisher login github-oidc"
    assert wait_cmd in text
    assert retry_cmd in text
    assert publish_cmd in text
    assert login in text
    assert text.index(wait_cmd) < text.index(login)
    assert text.index(login) < text.index(retry_cmd)
    assert text.index(retry_cmd) < text.index(publish_cmd)
    assert text.index(wait_cmd) < text.index(publish_cmd)
    assert "Align server.json version with tag" in text
    assert "startsWith(github.ref, 'refs/tags/v')" in text
    assert ".version = $v" in text
    assert "Wait until PyPI lists the tagged version" in text
    wait_step_at = text.index("Wait until PyPI lists the tagged version")
    publish_step_at = text.index("Publish server.json")
    assert wait_step_at < publish_step_at
    wait_block = text[wait_step_at:publish_step_at]
    assert "startsWith(github.ref, 'refs/tags/v')" in wait_block
