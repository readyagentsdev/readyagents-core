#!/usr/bin/env python3
"""Reproducible batch vs sequential harness. Not a marketing throughput claim.

Method: keyless transform workflow, N rows, CPython, wall clock of this process.
Hardware varies (developer laptop vs GitHub-hosted runner). Record the method
with any number you publish; the CI job checks that the harness completes, not
that wall time matches a baseline.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from readyagents.workflow.batch import run_batch  # noqa: E402
from readyagents.workflow.governor import ConcurrencyGovernor  # noqa: E402
from readyagents.workflow.runner import run_workflow_file  # noqa: E402


def _workflow(dir_path: Path) -> Path:
    path = dir_path / "echo.yaml"
    path.write_text(
        "name: bench_echo\nrequired_inputs: [n]\nnodes:\n"
        "  - id: t\n    type: transform\n    template: 'n={{n}}'\n    output_key: msg\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = max(1, int(args.rows))
    concurrency = max(1, int(args.concurrency))
    payload = [{"n": i} for i in range(rows)]
    with tempfile.TemporaryDirectory(prefix="ra-bench-") as raw:
        tmp = Path(raw)
        wf = _workflow(tmp)
        t0 = time.perf_counter()
        for item in payload:
            state = run_workflow_file(wf, inputs=item, persist=False)
            if state.status != "succeeded":
                raise SystemExit(f"sequential row failed: {state.status}")
        sequential_s = time.perf_counter() - t0
        gov = ConcurrencyGovernor(global_limit=concurrency, max_concurrency=concurrency)
        t1 = time.perf_counter()
        report = run_batch(
            wf,
            payload,
            concurrency=concurrency,
            persist=False,
            governor=gov,
        )
        batch_s = time.perf_counter() - t1
        if report.succeeded != rows:
            raise SystemExit(f"batch succeeded={report.succeeded} expected={rows}")
        result = {
            "method": (
                "scripts/bench_batch.py keyless transform workflow; "
                "CPython wall clock; not a marketing throughput claim"
            ),
            "rows": rows,
            "concurrency": concurrency,
            "sequential_s": round(sequential_s, 6),
            "batch_s": round(batch_s, 6),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "implementation": platform.python_implementation(),
        }
        text = json.dumps(result, indent=2)
        if args.json:
            print(text)
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
