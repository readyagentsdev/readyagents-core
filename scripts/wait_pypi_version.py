#!/usr/bin/env python3
"""Wait until PyPI lists a package version; retry MCP publish only on that lag.

Used by ``.github/workflows/publish-mcp.yml`` so a ``v*`` tag push does not fail
solely because PyPI has not yet indexed the matching ``readyagentsdev`` version.
Stdlib only. Callers inject ``get`` / ``sleeper`` / ``clock`` in tests.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from time import monotonic, sleep

DEFAULT_PACKAGE = "readyagentsdev"
DEFAULT_WAIT_TIMEOUT = 600.0
DEFAULT_PUBLISH_TIMEOUT = 300.0
DEFAULT_INTERVAL = 15.0
_USER_AGENT = "readyagents-mcp-publish"


class PyPIVersionNotVisible(RuntimeError):
    """Package version never became visible on PyPI (or to the MCP Registry) in time."""


class MCPPublishError(RuntimeError):
    """``mcp-publisher`` failed with an error that must not be retried."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode if returncode else 1


def pypi_json_url(package: str, version: str) -> str:
    return f"https://pypi.org/pypi/{package}/{version}/json"


def is_http_success(status: int) -> bool:
    return 200 <= status < 300


def is_pypi_version_not_found_error(output: str) -> bool:
    """True only for the MCP Registry lag 400 (PyPI version 404)."""
    return "was not found (status: 404)" in output and "PyPI" in output


def http_get(url: str, timeout: float = 30.0) -> int:
    if not url.startswith("https://"):
        return 0
    req = urllib.request.Request(  # noqa: S310
        url,
        method="GET",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def wait_until_pypi_version_visible(
    package: str,
    version: str,
    *,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    get: Callable[[str], int] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Poll ``https://pypi.org/pypi/<package>/<version>/json`` until it is 2xx.

    Raises ``PyPIVersionNotVisible`` if the bound elapses on 404 or any non-2xx.
    Does not return while the version is missing.
    """
    getter = get if get is not None else http_get
    do_sleep = sleeper if sleeper is not None else sleep
    now = clock if clock is not None else monotonic
    emit = log if log is not None else (lambda _msg: None)
    url = pypi_json_url(package, version)
    start = now()
    last_status = 0
    while True:
        last_status = getter(url)
        if is_http_success(last_status):
            emit(f"PyPI lists {package}=={version} (GET {url} -> {last_status})")
            return
        elapsed = now() - start
        emit(f"GET {url} -> {last_status}; waiting {interval}s ({elapsed:.0f}/{timeout}s)")
        if elapsed >= timeout:
            raise PyPIVersionNotVisible(
                f"PyPI package {package!r} version {version!r} was not found "
                f"(last status: {last_status}) after {timeout}s. GET {url}"
            )
        do_sleep(interval)


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True)  # noqa: S603
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    return proc


def retry_mcp_publish(
    argv: list[str],
    *,
    timeout: float = DEFAULT_PUBLISH_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Run ``mcp-publisher publish``. Retry only the PyPI version-not-found 400."""
    if not argv:
        raise MCPPublishError("retry-publish requires a command", returncode=2)
    do_run = run if run is not None else run_command
    do_sleep = sleeper if sleeper is not None else sleep
    now = clock if clock is not None else monotonic
    emit = log if log is not None else (lambda _msg: None)
    start = now()
    last_output = ""
    while True:
        proc = do_run(argv)
        last_output = f"{proc.stdout or ''}{proc.stderr or ''}"
        if proc.returncode == 0:
            return
        if not is_pypi_version_not_found_error(last_output):
            raise MCPPublishError(
                last_output or f"exit {proc.returncode}",
                returncode=proc.returncode,
            )
        elapsed = now() - start
        emit(
            f"MCP Registry has not seen the PyPI version yet "
            f"({elapsed:.0f}/{timeout}s); retrying in {interval}s"
        )
        if elapsed >= timeout:
            raise PyPIVersionNotVisible(
                "MCP Registry still reports PyPI version not found "
                f"(status: 404) after {timeout}s. {last_output.strip()}"
            )
        do_sleep(interval)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    wait_p = sub.add_parser("wait", help="Poll PyPI JSON until the version is listed")
    wait_p.add_argument("--package", default=DEFAULT_PACKAGE)
    wait_p.add_argument("--version", required=True)
    wait_p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    wait_p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    pub_p = sub.add_parser(
        "retry-publish",
        help="Run mcp-publisher; retry only PyPI version-not-found 400s",
    )
    pub_p.add_argument("--timeout", type=float, default=DEFAULT_PUBLISH_TIMEOUT)
    pub_p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    pub_p.add_argument("publish_cmd", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "wait":
            wait_until_pypi_version_visible(
                args.package,
                args.version,
                timeout=args.timeout,
                interval=args.interval,
                log=print,
            )
        else:
            retry_mcp_publish(
                list(args.publish_cmd),
                timeout=args.timeout,
                interval=args.interval,
                log=print,
            )
    except PyPIVersionNotVisible as exc:
        print(exc, file=sys.stderr)
        return 1
    except MCPPublishError as exc:
        if str(exc):
            print(exc, file=sys.stderr)
        return int(exc.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
