# Upgrading 0.x → 1.0

ReadyAgents Core 1.0 is additive. Existing 0.9 workflows, run records, and
CLI commands keep working.

## What froze

- Workflow format v1 (TASK-07 schema `$id`)
- Run record `record_version: 1` (0.9 records without the field still load)
- CLI command names and exit codes (approval pause remains 2)
- Documented `--json` keys `ok` and `command`
- Declared Python names in `readyagents.__all__` / [docs/stability.md](stability.md)

## What did not disappear

The ReadyAgents HTTP `/runs` alias from TASK-01/TASK-06 is **not** removed
at 1.0. Removal remains scheduled for **no earlier than v1.2** (was
“no earlier than v0.12”; restated so 1.0 does not silently drop it).

## New commands

`run --record`, `runs replay --offline`, `runs fork`, `runs diff`,
`runs freeze`. Recording is opt-in because a cassette contains prompts and
completions.

## Settings

- `READYAGENTS_RECORD` — opt-in recording
- `READYAGENTS_CASSETTE_MAX_ENTRY_BYTES` / `READYAGENTS_CASSETTE_MAX_BYTES`

Cassettes live under `$READYAGENTS_HOME/cassettes/`.
