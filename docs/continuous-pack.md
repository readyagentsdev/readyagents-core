# Continuous pack (optional)

`readyagents-pack-continuous` is a separately installed Python distribution. ReadyAgents Core remains one-shot: installing or importing Core starts no scheduler, watcher, thread, or listener.

The pack's `get_pack()` entry point (`continuous = readyagents_pack_continuous:get_pack`) registers no tools or node types. Importing or discovering the pack also starts nothing. Only the pack CLI starts work:

```bash
pip install /path/to/readyagents-pack-continuous
readyagents-continuous validate continuous.yaml --json
readyagents-continuous tick continuous.yaml --at 2026-09-08T09:00:00+00:00 --json
readyagents-continuous trigger continuous.yaml JOB_ID --json
readyagents-continuous serve continuous.yaml
```

`readyagents-continuous` is **not** a `readyagents` subcommand. Stopping `serve` stops cron ticks, file polling, and the webhook listener.

## What it does

Configured jobs only. Each job names a workspace-confined workflow and one trigger:

- five-field cron (IANA or local timezone, no catch-up by default, DST gaps skipped)
- bounded polling file-watch (debounce/settle, glob, entry caps; not inotify/FSEvents)
- authenticated webhook: `POST /hooks/{job_id}` (or the job's `trigger.path`) with bearer or HMAC-SHA256

Child runs are `python -m readyagents run <workflow> --json ...` with `shell=False`. Exit `0` is succeeded, `2` is a durable approval pause (`paused`, not retried), other nonzero is failed.

## What it does not do

- Hosted control plane, distributed queue, clustering, billing, or exactly-once delivery
- Arbitrary webhook workflow paths, CLI flags, env vars, or shell commands
- Automatic systemd/launchd installation (operator-authored units only)
- MCP task-handle / process-death recovery (that remains Core's optional HTTP `/runs` API)

Webhooks may set allowlisted JSON input keys only. Static job inputs win. `path`, `actor`, `packs`, `workflow`, and `dry_run` cannot be taken from the request.

Compatible with ReadyAgents Core `>=0.9.0,<0.11`. Examples live in the pack repository, not in Core. Tagging Core 0.10.0 does not add a scheduler to Core.
