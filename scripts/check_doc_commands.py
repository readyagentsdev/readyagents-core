#!/usr/bin/env python3
"""Run every ````bash command block in README.md and docs/getting-started.md.

Each block executes line-by-line against an installed ``readyagents`` (in CI:
the just-built wheel in a fresh venv) inside a pristine copy of the repo, so
examples, the Makefile, and dotfiles resolve exactly as they do for a user.

Lines that cannot run in CI are skipped with a logged reason instead of being
executed:

* environment setup the CI job itself performs (``pip install``, ``venv``,
  ``source``/``activate``, ``export``, ``cd``, ``git clone``),
* ``<placeholder>`` lines the reader must fill in (e.g. ``runs show <run_id>``),
* commands needing credentials or daemons absent in CI (LLM API keys for
  ``eval`` and non-``--dry-run`` runs of flows with agent nodes,
  ``docker``, long-running ``approvals serve``),
* ``make`` targets when ``make`` is not installed on the runner.

One documented behaviour is an expected non-zero exit: running
``examples/approval_gate.yaml`` *without* ``--approve`` pauses for approval
and exits 2. The EXPECT_EXIT table pins that; every other executed line must
exit 0.

Exit status is 0 only when every executed line matches its expected exit code.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

DEFAULT_FILES = ("README.md", "docs/getting-started.md")

# Line prefixes that set up the reader's shell rather than exercise the CLI.
# The CI job performs its own equivalent setup, so these are skipped.
SETUP_PREFIXES = (
    "pip ",
    "pip3 ",
    "python -m venv",
    "python3 -m venv",
    "source ",
    ". venv",
    ". .venv",
    "export ",
    "set ",
    "cd ",
    "git clone",
)

COPY_IGNORE = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "*.egg-info",
    "dist",
    "build",
    ".tox",
)

# (substring predicate, reason) for lines that genuinely need something CI
# cannot provide. Evaluated in order; first match wins.
SKIP_POLICIES: tuple[tuple[str, str], ...] = (
    ("docker", "needs a docker daemon"),
    ("approvals serve", "starts a long-running server"),
    ("readyagents eval", "needs an LLM API key"),
    ("OPENAI_API_KEY=", "needs an LLM API key"),
    ("ANTHROPIC_API_KEY=", "needs an LLM API key"),
)

PLACEHOLDER_RE = re.compile(r"<[^<>\s][^<>]*>")

# Documented non-zero exits: normalized line -> expected exit code.
EXPECT_EXIT = {
    "readyagents run examples/approval_gate.yaml": 2,  # pauses for approval
}

LINE_TIMEOUT_SECONDS = 600


def iter_bash_blocks(path: str) -> list[tuple[int, list[tuple[int, str]]]]:
    """Return [(fence_line_no, [(line_no, text), ...]), ...] for bash blocks."""
    blocks: list[tuple[int, list[tuple[int, str]]]] = []
    current: list[tuple[int, str]] | None = None
    fence_no = 0
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            stripped = raw.strip()
            if stripped.startswith("```"):
                tag = stripped.strip("`").strip()
                if current is None and tag in ("bash", "sh"):
                    current = []
                    fence_no = lineno
                elif current is not None and tag == "":
                    blocks.append((fence_no, current))
                    current = None
                continue
            if current is not None:
                current.append((lineno, raw.rstrip("\n")))
    return blocks


def classify(line: str) -> tuple[str, str]:
    """Return (action, detail) for a block line.

    Actions: "skip" (detail is the reason), "run" (detail is ""), or
    "run-expect" (detail is the expected exit code as a string).
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return ("skip", "blank/comment")
    lowered = text.lower()
    if lowered.startswith(SETUP_PREFIXES) or "activate" in lowered:
        return ("skip", "reader shell setup (CI job provides its own)")
    if PLACEHOLDER_RE.search(text):
        return ("skip", "contains a <placeholder> for the reader to fill in")
    for needle, reason in SKIP_POLICIES:
        if needle in text:
            return ("skip", reason)
    if (
        (
            "research_brief.yaml" in text
            or "support_triage.yaml" in text
            or "code_review.yaml" in text
            or "agent_tools.yaml" in text
        )
        and "--dry-run" not in text
        and not text.startswith("readyagents validate")
    ):
        return ("skip", "flow has agent nodes: needs an LLM API key")
    if text.startswith("make ") and shutil.which("make") is None:
        return ("skip", "make is not installed on this runner")
    normalized = " ".join(text.split())
    if normalized in EXPECT_EXIT:
        return ("run-expect", str(EXPECT_EXIT[normalized]))
    return ("run", "")


def run_blocks(repo: str, files: tuple[str, ...], python: str, keep: bool) -> int:
    """Execute every bash block; return the number of failed lines."""
    failures = 0
    ran = 0
    skipped = 0
    scratch = tempfile.mkdtemp(prefix="doc-commands-")
    try:
        for rel in files:
            path = os.path.join(repo, rel)
            for fence_no, lines in iter_bash_blocks(path):
                workdir = tempfile.mkdtemp(prefix=f"block-{fence_no}-", dir=scratch)
                shutil.copytree(
                    repo,
                    os.path.join(workdir, "repo"),
                    ignore=shutil.ignore_patterns(*COPY_IGNORE),
                )
                cwd = os.path.join(workdir, "repo")
                home = os.path.join(workdir, "home")
                os.makedirs(home, exist_ok=True)
                env = dict(
                    os.environ,
                    READYAGENTS_HOME=home,
                    PATH=os.path.dirname(os.path.abspath(python))
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                )
                tag = f"{rel}:#{fence_no}"
                for lineno, text in lines:
                    action, detail = classify(text)
                    where = f"{tag}:{lineno}"
                    if action == "skip":
                        skipped += 1
                        print(f"SKIP {where}: {text.strip()}\n     reason: {detail}")
                        continue
                    expected = int(detail) if action == "run-expect" else 0
                    ran += 1
                    try:
                        proc = subprocess.run(
                            text.strip(),
                            shell=True,
                            cwd=cwd,
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=LINE_TIMEOUT_SECONDS,
                        )
                    except subprocess.TimeoutExpired:
                        failures += 1
                        print(
                            f"FAIL {where}: timed out after {LINE_TIMEOUT_SECONDS}s: {text.strip()}"
                        )
                        continue
                    if proc.returncode != expected:
                        failures += 1
                        print(
                            f"FAIL {where}: exit {proc.returncode}, "
                            f"expected {expected}: {text.strip()}"
                        )
                        if proc.stdout.strip():
                            print("--- stdout ---")
                            print(proc.stdout.strip()[-2000:])
                        if proc.stderr.strip():
                            print("--- stderr ---")
                            print(proc.stderr.strip()[-2000:])
                    else:
                        print(f"ok   {where}: {text.strip()} (exit {proc.returncode})")
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            print(f"scratch kept at {scratch}")
    print(f"\n{ran} lines executed, {skipped} skipped, {failures} failed")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run README/getting-started bash blocks against an installed readyagents."
    )
    parser.add_argument(
        "--repo",
        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        help="repo root to copy each block workdir from",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="python of the venv with readyagents installed",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=list(DEFAULT_FILES),
        help="repo-relative markdown files to check",
    )
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="keep per-block workdirs for debugging",
    )
    args = parser.parse_args(argv)
    failures = run_blocks(args.repo, tuple(args.files), args.python, args.keep_scratch)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
