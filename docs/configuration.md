# Configuration (BYOK)

ReadyAgents is bring-your-own-key. Nothing in this repository contains LLM credentials.

## Load order

For each setting, the first non-empty value wins:

1. Process environment variables
2. `.env` in the current working directory
3. `.env-ai` in the current working directory (local operator file; gitignored)

Copy `.env.example` to `.env` and fill in keys.

## LLM keys and default model

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` or `READYAGENTS_OPENAI_API_KEY` | OpenAI |
| `ANTHROPIC_API_KEY` or `READYAGENTS_ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_COMPAT_API_KEY` or `READYAGENTS_OPENAI_COMPAT_API_KEY` | Groq / Ollama / compatible |
| `OPENAI_COMPAT_BASE_URL` | Base URL for compatible APIs |
| `READYAGENTS_DEFAULT_MODEL` | `provider:model`, e.g. `openai:gpt-4o-mini` |
| `READYAGENTS_SOVEREIGN` | `1` / true: refuse non-loopback egress at the socket boundary for the run |
| `READYAGENTS_SOVEREIGN_ALLOW` | Comma-separated private hosts allowed under sovereign (must resolve private) |

Model references:

- `openai:gpt-4o-mini`
- `anthropic:claude-sonnet-4-5`
- `openai-compat:llama-3.1-8b-instant` (requires `OPENAI_COMPAT_BASE_URL`)
- `groq:llama-3.1-8b-instant` (defaults Groq base URL)
- `ollama:llama3.2` (loopback `http://127.0.0.1:11434/v1`, no API key)

Loopback (and allowlisted private) OpenAI-compatible bases need no placeholder key. Remote compat URLs still do. See [local-models.md](local-models.md) and [sovereign.md](sovereign.md).
- `ollama:llama3` (defaults `http://127.0.0.1:11434/v1`)

Install extras to talk to a provider:

```bash
pip install -e ".[openai]"
pip install -e ".[anthropic]"
pip install -e ".[all]"
pip install -e ".[sign]"   # Ed25519 artifact signatures; not in all
```

`[sign]` is opt-in and unused on the unsigned default path. See [supply-chain.md](supply-chain.md).

If an agent node runs with no key, the CLI exits with a short `LLMError` telling you which variable to set — not a traceback dump.

If the node has no explicit `model:` and the default provider has no key, the engine uses the provider that *does* have a key (Anthropic, then OpenAI-compatible). An explicit `model: openai:...` still requires that key.

## Other settings

| Variable | Purpose |
| --- | --- |
| `READYAGENTS_ALLOW_HTTP` | `1` / `true` enables builtin `http_get` (still blocks private/loopback/metadata URLs) |
| `READYAGENTS_WORKSPACE` | Optional sandbox root for `read_file` / `write_file`. If unset, file tools use the workflow file's directory. |
| `READYAGENTS_HOME` | Artifact directory (default: `.readyagents` under the current working directory on every OS). Run JSON lives in `$READYAGENTS_HOME/runs/`. On Windows this is still a folder next to your prompt, not `%APPDATA%`. `readyagents doctor` reports writability and whether owner-only modes are enforceable there. |
| `READYAGENTS_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Log lines include `run=` and `node=` |
| `READYAGENTS_LOG_FORMAT` | `text` (default) or `json` |
| `READYAGENTS_MAX_TOKENS` | Stop further LLM calls when run `total_tokens` reaches this |
| `READYAGENTS_MAX_COST_USD` | Same for estimated USD (`cost_micros` on the run) |
| `READYAGENTS_PRICES` | Override path for the model price table (JSON). Unknown models stay unpriced, never silent zero. |
| `READYAGENTS_FALLBACK_MODELS` | Comma-separated `provider:model` list tried after the primary fails |
| `READYAGENTS_CIRCUIT_FAILURE_THRESHOLD` | Consecutive failures before skipping a model (default 3) |
| `READYAGENTS_CIRCUIT_COOLDOWN_SECONDS` | How long a skipped model stays skipped (default 60) |
| `READYAGENTS_LLM_CACHE` | `1` / `true` enables the local completion cache under `$READYAGENTS_HOME/cache/` |
| `READYAGENTS_RECORD` | `1` / `true` writes a content-addressed cassette under `$READYAGENTS_HOME/cassettes/`. Opt-in: cassettes contain full prompts and completions. |
| `READYAGENTS_CASSETTE_MAX_ENTRY_BYTES` | Per-entry cassette size cap (default 1 MiB). |
| `READYAGENTS_CASSETTE_MAX_BYTES` | Per-cassette size cap (default 10 MiB). |
| `READYAGENTS_REDACT` | `1` / `true` masks emails, `sk-…` keys, and configured literals in logs and persisted records |
| `READYAGENTS_REDACT_LITERALS` | Comma-separated extra strings to mask |
| `READYAGENTS_REDACT_PATTERNS` | Comma-separated extra regexes to mask |
| `READYAGENTS_ACTOR` | Default actor id for RBAC hooks |
| `READYAGENTS_TRUST_ANCHORS` | Local trust-anchor YAML (issuers + JWKS files). Missing/malformed fails closed when `--token-file` is used. |
| `READYAGENTS_CREDENTIALS` | Per-tool secret grant file. Without it, tools see the process environment as today. |
| `READYAGENTS_WORKLOAD_SUBJECT` | Optional workload identity subject |
| `READYAGENTS_WORKLOAD_KEY` | PEM private key path for workload assertions (never logged) |
| `READYAGENTS_WORKLOAD_KID` | Key id for workload assertions |
| `READYAGENTS_PAUSE_NOTIFY_URL` | Outbound POST when an approval node pauses (core does not listen for that webhook) |
| `READYAGENTS_MCP_TOKEN` | Bearer token for optional MCP Streamable HTTP (`/mcp`) and the ReadyAgents `/runs` extension. Never pass the token as a CLI flag. If empty at HTTP startup, the process generates ≥256 bits and prints it once to stderr (never persisted or logged). |
| `READYAGENTS_MCP_PROTOCOL_MAX` | Optional cap on advertised MCP protocol versions (default latest honoured). |
| `READYAGENTS_DECISION_SECRET` | Optional HMAC secret required on MCP `tasks/update`. When set, unsigned or forged MCP approvals are refused, audited, and leave the run paused. |
| `READYAGENTS_MCP_HTTP_HOST` | Bind host for `mcp serve --transport streamable-http` (default `127.0.0.1`). Loopback only. |
| `READYAGENTS_MCP_HTTP_PORT` | Bind port for that HTTP door (default `8765`). |
| `READYAGENTS_MCP_MAX_CONCURRENT_RUNS` | In-process executor cap for `/runs` (default `4`). |
| `READYAGENTS_MCP_MAX_PENDING_RUNS` | Queue cap for `/runs`; extra starts return `429` (default `32`). |
| `READYAGENTS_APPROVAL_UI_SECRET` | Optional HMAC secret for `readyagents approvals serve`. If unset, the process generates one for that lifetime. Never pass the secret as a CLI flag. Restart invalidates UI tokens. |
| `READYAGENTS_RUN_STORE` | `json` (default) or `sqlite`. JSON files stay under `$READYAGENTS_HOME/runs/`. SQLite is a local optional file, not hosted recovery. |
| `READYAGENTS_MEMORY_STORE` | `json` (default) or `sqlite` for `type: memory`. Files stay under `$READYAGENTS_HOME/memory/`. |
| `READYAGENTS_RUN_DB` | SQLite file when `READYAGENTS_RUN_STORE=sqlite` (default `$READYAGENTS_HOME/runs.sqlite3`). Relative paths resolve under `READYAGENTS_HOME`. Directories, special files, and unsafe symlink targets are rejected. Network filesystems are unsupported. |
| `READYAGENTS_SQLITE_BUSY_TIMEOUT_MS` | SQLite busy timeout (default 5000). |

Copy existing JSON records with `readyagents runs migrate --from json --to sqlite` (non-destructive). SQLite WAL may create `-wal`/`-shm` companions; back those up with the `.sqlite3` file. See [run-stores.md](run-stores.md).

Inspect and resume those records with `readyagents runs list`, `readyagents runs show`, and `readyagents resume`. Audit events (append-only JSONL) live in `$READYAGENTS_HOME/audit/`.

Optional MCP HTTP is not started by setting `READYAGENTS_MCP_TOKEN`. Start it explicitly:

```bash
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

`--token-env` selects a different variable name if you do not want `READYAGENTS_MCP_TOKEN`. `--auth none` is loopback-only and warns. v0.9 rejects non-loopback binds. `/runs` always persists; there is no network `--no-persist`. Never put provider keys in `/runs` JSON. Workspace confinement, SSRF public-IP pinning, secrets/RBAC/PII hooks, and append-only audit still apply. See [mcp.md](mcp.md).

Secrets backends and authorizers are pack hooks. Env / `.env` remains the default BYOK path; core does not vendor Vault or AWS SDKs.

Workflow YAML may also set `allow_http: true` and `workspace:`. Either the env flag or the workflow flag enables HTTP. `workspace:` must resolve under `READYAGENTS_WORKSPACE` when that env is set, otherwise under the workflow file's directory. It cannot point at `/` or a parent directory.

## Files you should never commit

- `.env`, `.env.local`, `.env-ai`
- `.keys/`
- `.readyagents/` (run records)

`.env.example` contains placeholders only and is safe to commit.
