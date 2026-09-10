# Run stores

ReadyAgents writes a run record after each node so you can inspect, resume, and decide later. Opening a store is **command/request driven**. There is no always-on process, watcher, or database server.

JSON files are the **default**. SQLite is an **optional local** backend that uses Python's standard-library `sqlite3` (no extra pip dependency). It is not hosted recovery, not a network database, and not a control plane.

Keyless walkthrough: [sqlite_run_store.md](../examples/sqlite_run_store.md).

## Select a backend

| Variable | Default | Purpose |
| --- | --- | --- |
| `READYAGENTS_RUN_STORE` | `json` | `json` or `sqlite` |
| `READYAGENTS_RUN_DB` | `$READYAGENTS_HOME/runs.sqlite3` | SQLite file when the backend is `sqlite` |
| `READYAGENTS_SQLITE_BUSY_TIMEOUT_MS` | `5000` | Bounded wait when the SQLite file is busy |

`READYAGENTS_HOME` defaults to `.readyagents`. Relative `READYAGENTS_RUN_DB` paths resolve under `READYAGENTS_HOME`, not the current working directory. Absolute paths are used as given.

Changing `READYAGENTS_RUN_STORE` selects which backend CLI and local API commands see. There is **no union view**. JSON files do not disappear when you switch to SQLite; they are simply not consulted until you switch back.

`run`, `resume`, `decide`, and `runs list` / `show` / `replay` / `delete` / `gc` / `report` keep the same syntax against the selected backend.

`readyagents batch` defaults to `--run-store sqlite` (WAL, compare-and-swap) because many in-flight rows contend on the store. JSON remains the low-concurrency default for `readyagents run`. Pass `--run-store json` to batch if you want the file backend. See [scale.md](scale.md).

## JSON (default)

```bash
export READYAGENTS_RUN_STORE=json
```

Records are `$READYAGENTS_HOME/runs/<run_id>.json` (gitignored). Each write replaces the file atomically. `runs list` scans that directory. Older records without a store revision still load.

JSON is the right default for a readable, portable, one-shot local history. Prefer SQLite when several local clients mutate runs at the same time.

## SQLite (optional)

```bash
export READYAGENTS_RUN_STORE=sqlite
```

The default file is `$READYAGENTS_HOME/runs.sqlite3`. The same logical run record is stored (canonical JSON plus indexed columns for list/show). A newer unknown schema version is refused without modifying the file.

SQLite is opened when a command or request needs it, then released with that command. It does not start a daemon.

### Concurrency

SQLite uses WAL, a bounded busy timeout, short transactions, and revision compare-and-swap. A stale revision raises a typed conflict instead of silently overwriting. Use this backend when the loopback MCP `/runs` door, the localhost approval UI, and the CLI may write at the same time.

### Network filesystems

Network filesystems (NFS, SMB, and similar) are **unsupported**. SQLite locking is not guaranteed there. Keep the database on a local disk.

## Backup

Copy files you control. For a consistent SQLite backup while WAL is on, copy all three:

- `runs.sqlite3`
- `runs.sqlite3-wal`
- `runs.sqlite3-shm`

Or checkpoint first, then copy the database file. Copying only `runs.sqlite3` while `-wal` / `-shm` exist can yield a torn backup.

ReadyAgents does not encrypt run records at rest. Restrict filesystem permissions on `READYAGENTS_HOME`. See [SECURITY.md](../SECURITY.md).

## Migrate JSON → SQLite

Migration **copies**. JSON sources are never deleted. There is no source-delete flag and no automatic conversion when you set `READYAGENTS_RUN_STORE`.

```bash
readyagents runs migrate --from json --to sqlite
readyagents runs migrate --from json --to sqlite --dry-run
readyagents runs migrate --from json --to sqlite --on-conflict skip-identical
```

`--from json` and `--to sqlite` are required.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--source PATH` | `$READYAGENTS_HOME/runs` | JSON directory |
| `--database PATH` | configured `READYAGENTS_RUN_DB` | Destination SQLite file |
| `--on-conflict` | `error` | `error` aborts on an existing destination id; `skip-identical` resumes when the canonical record already matches |
| `--skip-invalid` | off | Opt-in: skip unreadable source files instead of aborting during planning |
| `--verify` / `--no-verify` | `--verify` | Re-read destination rows and compare canonical records |
| `--dry-run` | off | Plan and validate without writing |
| `--json` | off | Stable envelope on stdout |

By default the command aborts **before writes** if any source record is invalid or a `run_id` already exists in SQLite with different content. A rerun with `--on-conflict skip-identical` is resumable and idempotent for identical rows. Non-identical collisions still fail; nothing overwrites a different SQLite run.

`--json` success envelope:

```json
{
  "ok": true,
  "command": "runs migrate",
  "source_backend": "json",
  "destination_backend": "sqlite",
  "scanned": 42,
  "imported": 42,
  "skipped": 0,
  "conflicts": 0,
  "invalid": 0,
  "verified": true
}
```

Exit `0` on the selected successful policy. Exit `1` on config, schema, invalid, conflict, or verification failure.

Audit JSONL, the LLM cache, pack state, HTML reports, and workflow YAML are **not** migrated.

## Rollback

Keep using the JSON files. They remain after migrate.

```bash
export READYAGENTS_RUN_STORE=json
# or: unset READYAGENTS_RUN_STORE
readyagents runs list
```

New runs then go back to `$READYAGENTS_HOME/runs/`. The SQLite file is left in place until you delete it yourself.

## Related

- [Configuration](configuration.md)
- [CLI](cli.md)
- [MCP](mcp.md)
- [Localhost approval UI](browser-approval-ui.md)
