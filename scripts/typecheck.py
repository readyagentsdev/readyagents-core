"""Run mypy on src/readyagents; fail only on errors not in the committed baseline.

mypy itself exits 1 whenever the baseline is non-empty. Pipe its stdout through
mypy-baseline so CI fails on new errors and stays green on known debt.
A mypy crash (exit >= 2) is still a hard failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    mypy = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "src/readyagents",
            "--ignore-missing-imports",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if mypy.returncode >= 2:
        sys.stderr.write(mypy.stdout)
        sys.stderr.write(mypy.stderr)
        return mypy.returncode
    filt = subprocess.run(
        [sys.executable, "-m", "mypy_baseline", "filter"],
        cwd=ROOT,
        input=mypy.stdout,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(filt.stdout)
    sys.stderr.write(filt.stderr)
    if filt.returncode != 0:
        return filt.returncode
    if mypy.returncode not in (0, 1):
        return mypy.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
