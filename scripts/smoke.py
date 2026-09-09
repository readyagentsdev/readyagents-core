#!/usr/bin/env python3
"""Keyless smoke runner. Stdlib only. Works on Windows, macOS, and Linux."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

STEPS: list[list[str]] = [
    ["run", str(EXAMPLES / "calc_pipeline.yaml"), "--no-persist"],
    ["run", str(EXAMPLES / "list_dir.yaml"), "--no-persist"],
    ["eval", str(EXAMPLES / "eval" / "pass.yaml")],
    [
        "run",
        str(EXAMPLES / "connector_demo.yaml"),
        "--pack",
        str(EXAMPLES / "packs" / "connector_pack.py"),
        "--no-persist",
    ],
    ["run", str(EXAMPLES / "approval_gate.yaml"), "--approve", "gate", "--no-persist"],
    ["run", str(EXAMPLES / "browser_approval.yaml"), "--approve", "gate", "--no-persist"],
    ["run", str(EXAMPLES / "fanout_gate.yaml"), "--approve", "gate", "--no-persist"],
    ["run", str(EXAMPLES / "include_demo.yaml"), "--no-persist"],
    ["run", str(EXAMPLES / "composed_gate.yaml"), "--approve", "gate", "--no-persist"],
    [
        "run",
        str(EXAMPLES / "multi_gate.yaml"),
        "--approve",
        "first",
        "--approve",
        "second",
        "--no-persist",
    ],
    [
        "run",
        str(EXAMPLES / "support_triage.yaml"),
        "--dry-run",
        "--no-persist",
        "--input",
        "message=hello",
    ],
    ["run", str(EXAMPLES / "agent_tools.yaml"), "--dry-run", "--no-persist"],
    ["run", str(EXAMPLES / "foreach_calc.yaml"), "--no-persist"],
    ["run", str(EXAMPLES / "json_mutate.yaml"), "--no-persist"],
]


def _readyagents() -> list[str]:
    return [sys.executable, "-m", "readyagents"]


def _run(args: list[str], *, env: dict[str, str], expect: int = 0) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [*_readyagents(), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != expect:
        raise SystemExit(
            f"FAIL {' '.join(args)}\nexit={proc.returncode} expected={expect}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc


def _pause_resume(env: dict[str, str]) -> None:
    proc = _run(
        ["run", str(EXAMPLES / "approval_gate.yaml"), "--json"],
        env=env,
        expect=2,
    )
    text = proc.stdout
    start = text.find("{")
    if start < 0:
        raise SystemExit(f"FAIL pause: no JSON\n{text}\n{proc.stderr}")
    data = json.loads(text[start:])
    run_id = data.get("run_id") or (data.get("run") or {}).get("run_id")
    if not run_id:
        raise SystemExit(f"FAIL pause: missing run_id in {data!r}")
    _run(["resume", str(run_id), "--approve", "gate"], env=env, expect=0)


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="readyagents-smoke-"))
    env = os.environ.copy()
    env["READYAGENTS_HOME"] = str(home)
    failed = False
    try:
        for args in STEPS:
            print("SMOKE", " ".join(args[:3]), flush=True)
            _run(args, env=env)
        print("SMOKE pause-resume", flush=True)
        _pause_resume(env)
    except SystemExit as extra:
        print(str(extra), file=sys.stderr)
        failed = True
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if failed:
        print("smoke FAILED")
        return 1
    print("smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
