# Concepts

ReadyAgents Core is a small engine that runs a **graph of nodes**. Each node is typed. State is a JSON-like document that grows as nodes finish.

## Workflow

A workflow is a YAML (or JSON) file with:

- `name` and optional `description`
- `inputs` — default values for `{{variables}}`
- `nodes` — the work
- optional `edges` — explicit routing (otherwise `next` or list order)
- optional `mcp_servers` — extra tools started as MCP subprocesses
- `allow_http` — opt-in for the builtin `http_get` tool

## Node types

| Type | Role |
| --- | --- |
| `agent` | LLM call with a prompt template. Optional `tools:` allowlist runs a bounded tool-use loop (real registry tools). |
| `tool` | Call a named builtin, pack, or MCP tool |
| `condition` | Branch on a small expression |
| `transform` | Template, JSON parse, or dotted-path extract |
| `approval` | Human-in-the-loop gate; pauses until `--approve` / `--reject` or an injected decision. Opt-in quorum, roles, lazy deadlines: [approvals.md](approvals.md). |
| `parallel` | Run independent branch nodes concurrently |
| `include` | Run another workflow file and take its outputs |
| `foreach` | Sequential map over a list (`{{item}}` / `{{index}}`; default 32 / max 100; no nest) |
| `document` | PDF → ordered page parts (text, image ref, citable page). Caps before decode. See [multimodal](multimodal.md). |
| `transcribe` | Audio → text plus timing. Local never leaves the machine. See [multimodal](multimodal.md). |
| `ingest` | File/directory → chunked memory records with document id, version, range. See [knowledge](knowledge.md). |

Packs may register additional node types. `MediaPart` is a new state shape that only appears where a workflow asks for it; text-only runs are unchanged.

## State

For a run, the engine keeps:

- `inputs` — merged defaults + `--input`
- `node_outputs` — raw output keyed by node id
- `output_keys` — optional aliases (`output_key: brief`)
- `metadata` — source path, dry-run flag, workspace, `allow_http`, lineage (`forked_from`, `replayed_from`)
- `errors` — if the run failed
- `record_version` — `1` on newly written records; 0.9-era files without the field still load

Opt-in `--record` writes a cassette of LLM and tool calls. See [time-machine.md](time-machine.md).

Templates see a merged namespace: inputs, metadata, node ids, and output keys. `{{topic}}` and `{{plan}}` both work if those names exist.

## Reliability

Each node may set:

```yaml
timeout_seconds: 60
retry:
  max_attempts: 3
  backoff_seconds: 1
  backoff_multiplier: 2
```

Failures raise typed errors (`NodeError`, `LLMError`, `MCPError`, `ApprovalRequired`, …) instead of a bare stack dump in the CLI.

Run records are written after each node. The default backend is JSON files under `$READYAGENTS_HOME/runs/`; optional local SQLite is `READYAGENTS_RUN_STORE=sqlite` ([run-stores.md](run-stores.md)). Resume a paused or failed run with `readyagents resume <run_id>` or inject a decision with `readyagents decide`. Inspect with `readyagents runs list` and `readyagents runs show <run_id>`. Structured logs include `run=<id>` and `node=<id>` (JSON format adds `run` / `node` keys). Agent usage is stored per node and rolled up on the run.

An append-only, hash-chained audit log lives under `$READYAGENTS_HOME/audit/`. `readyagents audit verify` walks the chain (tamper-*evident*, **not** tamper-proof). `readyagents evidence RUN_ID` writes a local evidence pack of the run, decisions, audit slice, and routing graph — evidence, **not** legal compliance or certification. `readyagents graph PATH` prints declared Mermaid routing and executes nothing. `runs gc` deletes run records (not audit JSONL) and respects `READYAGENTS_RETENTION_DAYS` unless `--override-retention`; that window is local hygiene, not a legal archive. See [compliance.md](compliance.md).

Packs may register observers. Events fire after a durable persist and cannot change the run. The optional OpenTelemetry pack is content-free and off unless `READYAGENTS_OTEL=1`; import starts no collector. See [observability.md](observability.md).

## Extension: packs

Core is complete on its own. A pack is an installed Python package that exposes an entry point in group `readyagents.packs`. It can register tools, node types, and bundled workflows. See [packs.md](packs.md).
A local `.py` loads with `--pack PATH`, for example `readyagents packs --pack examples/packs/connector_pack.py`.
`--require-signed` verifies that pack **before** import. See [supply-chain.md](supply-chain.md).

## MCP

MCP is optional. Builtin tools are Python. You do not need Node.js unless you attach an MCP server that requires it.
