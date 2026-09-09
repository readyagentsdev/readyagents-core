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
| `approval` | Human-in-the-loop gate; pauses until `--approve` / `--reject` or an injected decision |
| `parallel` | Run independent branch nodes concurrently |
| `include` | Run another workflow file and take its outputs |
| `foreach` | Sequential map over a list (`{{item}}` / `{{index}}`; default 32 / max 100; no nest) |

Packs may register additional node types.

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

Run records are written after each node. The default backend is JSON files under `$READYAGENTS_HOME/runs/`; optional local SQLite is `READYAGENTS_RUN_STORE=sqlite` ([run-stores.md](run-stores.md)). Resume a paused or failed run with `readyagents resume <run_id>` or inject a decision with `readyagents decide`. Inspect with `readyagents runs list` and `readyagents runs show <run_id>`. Structured logs include `run=<id>` and `node=<id>` (JSON format adds `run` / `node` keys). Agent usage is stored per node and rolled up on the run. An append-only audit log lives under `$READYAGENTS_HOME/audit/`.

## Extension: packs

Core is complete on its own. A pack is an installed Python package that exposes an entry point in group `readyagents.packs`. It can register tools, node types, and bundled workflows. See [packs.md](packs.md).
A local `.py` loads with `--pack PATH`, for example `readyagents packs --pack examples/packs/connector_pack.py`.

## MCP

MCP is optional. Builtin tools are Python. You do not need Node.js unless you attach an MCP server that requires it.
