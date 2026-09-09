# Optional SQLite run store (keyless)

No API keys. Workflow YAML is unchanged. JSON remains the default; this copies existing run records into a local SQLite file.

```bash
readyagents run examples/calc_pipeline.yaml
readyagents runs list

readyagents runs migrate --from json --to sqlite --dry-run
readyagents runs migrate --from json --to sqlite

export READYAGENTS_RUN_STORE=sqlite
readyagents runs list
readyagents runs show <run_id>
```

JSON files under `.readyagents/runs/` stay on disk. Unset `READYAGENTS_RUN_STORE` (or set it to `json`) to keep using them.

See [run-stores.md](../docs/run-stores.md).
