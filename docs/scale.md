# Scale and batch

Opt-in concurrency for many local runs of one workflow. This is **not** a
distributed worker, a broker, or an always-on daemon. `readyagents batch` is a
foreground command that ends when the input file is done.

The synchronous engine is unchanged. `readyagents run` / `resume` / `--json`
keys, exit codes, and per-node checkpoints stay on the same path. The opt-in
async API is `asyncio.to_thread` over that engine — there is not a second
engine. Nested `asyncio.run` is not used inside `run_workflow` (that pattern
deadlocks).

## `readyagents batch`

```bash
readyagents batch examples/batch_echo.yaml \
  --input-file examples/batch_rows.jsonl \
  --concurrency 8 \
  --max-spend 50.00 \
  --continue-on-error \
  --out results.jsonl \
  --json
```

| Flag | Meaning |
| --- | --- |
| `--input-file` | JSONL (one object per line, or a JSON array) or CSV. Each row is the input map for one run. |
| `--concurrency` | Max in-flight rows. Capped by `READYAGENTS_MAX_CONCURRENCY` (default 4096; a workflow cannot raise it). |
| `--per-workflow-limit` | Max in-flight rows for this workflow (`READYAGENTS_PER_WORKFLOW_CONCURRENCY`). |
| `--per-provider-limit` | Max in-flight rows per provider (`READYAGENTS_PER_PROVIDER_CONCURRENCY`). |
| `--provider-rate` | Token-bucket tokens/sec per provider (`READYAGENTS_PROVIDER_RATE`). A 429 `Retry-After` also arms a burst-1 bucket. |
| `--continue-on-error` | Default on. A failed row is recorded; the rest keep running. `--no-continue-on-error` stops submitting new rows after the first failure. |
| `--max-spend USD` | Hard cap **across rows**, consulted before each model call. |
| `--out PATH` | Per-row JSONL, sorted by `index`. Workspace-confined. |
| `--json` | Summary envelope on stdout (`command` is `batch`). Progress stays on stderr. |
| `--run-store sqlite\|json` | Batch defaults to **sqlite** (WAL). `readyagents run` still defaults to JSON. |
| `--no-persist` | Skip run records. |

Exit `0` if every row succeeded, `2` if some paused and none failed, `1` otherwise.

Per-row isolation: each row is a fresh `run_workflow_file` (own run id, inputs,
taint, spend snapshot, audit events). One row's error does not include another
row's outputs. The `--json` summary omits per-row inputs and outputs; those
live in `--out`.

Results file order is **sorted by index**. Progress lines are unordered (completion order).

## Governor

Process-local limits: global, per-workflow, per-provider. A bounded wait queue
raises `GovernorBackpressure` instead of growing without limit. Interactive
`readyagents run` is served before batch waiters so a long batch does not
starve a foreground run. Provider `Retry-After` (OpenAI, Anthropic, and connector HTTP 429/503) calls
`get_governor().note_retry_after` so other in-flight work for that provider
waits, then a burst-1 token bucket lets one waiter through at a time instead
of a retry storm.

`READYAGENTS_MAX_CONCURRENCY` is the hard ceiling (default 4096). The process
governor's global limit defaults to that ceiling, not a hidden 32.
`READYAGENTS_GLOBAL_CONCURRENCY` may lower it. Workflow YAML cannot raise it.

Graceful shutdown: SIGINT/KeyboardInterrupt stops submitting rows; in-flight
runs hit the existing cancellation checkpoints and persist.

## Benchmarks

`scripts/bench_batch.py` times a keyless transform workflow sequentially vs
batch. CI runs the harness to prove it still executes. Wall times depend on
hardware (developer laptop vs GitHub-hosted runner) and are **not** a
marketing throughput claim. If you publish a number, publish this method
beside it. See `baselines/bench_batch.json`.

## What this is not

No cluster, no message broker, no hosted queue, no always-on worker in core,
no mandatory async extra, no persistence-format change.
