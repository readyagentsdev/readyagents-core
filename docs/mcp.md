# MCP toolkit

ReadyAgents includes an MCP **client** (call other servers from a workflow) and an MCP **server** (expose this toolkit to other agents).

MCP is an **optional extra**. Builtin tools are Python and need no Node.js. A core install without the extra still runs keyless workflows. MCP/HTTP imports fail only when those paths are used (`MCPError`). No new mandatory dependency.

```bash
pip install -e ".[mcp]"
```

## Builtin tools (always available)

| Tool | Arguments | Notes |
| --- | --- | --- |
| `now` | — | UTC ISO-8601 |
| `calc` | `expression` | Arithmetic only (`+ - * / // % **`) |
| `json_get` | `data`, `path` | Dotted path into JSON/dict |
| `json_set` | `data`, `path`, `value` | Set a dotted path; returns the full document |
| `json_merge` | `data`, `path`, `value` | Merge an object at a dotted path (`""` / `"."` merges at the root) |
| `list_dir` | `path` (default `.`) | Sandboxed directory listing; skips dotfiles unless `include_hidden` |
| `read_file` | `path` | Sandboxed to workspace |
| `write_file` | `path`, `content` | Sandboxed to workspace |
| `http_get` | `url` | Off until `READYAGENTS_ALLOW_HTTP=1` or `allow_http: true`; private/loopback/metadata URLs stay blocked |

When a firewall policy file is present, each MCP server's tool names,
descriptions, and schemas are hashed on first use and stored under
`$READYAGENTS_HOME/mcp-pins/`. A later change — including on a fresh run — is
a policy event (`on_description_change`, default gate). See [policy.md](policy.md).

The same surface hash is the MCP digest in `readyagents.lock` and
`readyagents sbom` (algorithm v1). `--frozen` refuses a description-only
rug-pull that drifted from the lockfile. See [supply-chain.md](supply-chain.md).

## List a workspace without MCP

Builtin `list_dir` is Python and needs no Node.js and no MCP filesystem server:

```bash
readyagents run examples/list_dir.yaml
readyagents run examples/list_dir.yaml --dry-run
```

## Client: MCP servers in a workflow

Optional. A workflow may call a third-party MCP server (that server may be written in any language). ReadyAgents itself does not require Node.js. Prefer builtin `list_dir` / `read_file` / `write_file` unless you need a remote server.

```yaml
mcp_servers:
  other:
    command: readyagents
    args: ["mcp", "serve"]
```

Each named server keeps **one stdio session** for the run (`list_tools` and `call_tool` reuse it). Tool JSON Schema from the server is passed through to agent `tools:`. `cwd` defaults to the workflow workspace and cannot escape it.

If the workflow declares `mcp_servers` but `mcp` is not installed, you get a clear `MCPError` with the pip extra to install.

Core examples do **not** require MCP servers.

## Server: expose ReadyAgents

Stdio is the **default** and is unchanged. The synchronous MCP tool `run_workflow` remains.

```bash
readyagents mcp serve
readyagents mcp serve --transport stdio
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

Requires `pip install -e ".[mcp]"`. Flags: [cli.md](cli.md).

Point your MCP host at the `readyagents` CLI command. Example Claude Desktop / host config sketch (**stdio**):

```json
{
  "mcpServers": {
    "readyagents": {
      "command": "readyagents",
      "args": ["mcp", "serve"]
    }
  }
}
```

Registry `server.json` still describes that stdio package. Streamable HTTP is an explicit local command, not a hosted remote.

### Protocol revisions

The same server speaks `2026-07-28`, `2025-11-25`, and `2025-06-18` when the installed `mcp` extra can honour them. `server/discover` lists only revisions and extensions this process fully implements. An out-of-range `io.modelcontextprotocol/protocolVersion` is JSON-RPC `-32022` (`UnsupportedProtocolVersionError`) with the supported list — never a silent downgrade and never a bare HTTP 500.

A `2026-07-28` client does not need `initialize`. Per-request `_meta` carries protocol version, client capabilities, and optional W3C `traceparent` / `tracestate` / `baggage` (shape-validated, length-capped, dropped when malformed). Core parses trace context; it does not export OpenTelemetry and gains no OTEL dependency.

Every result includes `resultType`: `complete` for ordinary results, `input_required` for synchronous MRTR (not used for approval gates), and `task` for `CreateTaskResult`.

### MCP-standard vs ReadyAgents extension

When Streamable HTTP is enabled, one foreground process exposes **two different** HTTP surfaces. Do not mix them up.

| Surface | What it is | What it is not |
| --- | --- | --- |
| `/mcp` | Official Python MCP SDK **Streamable HTTP** plus ReadyAgents JSON-RPC: `server/discover`, tools, and `io.modelcontextprotocol/tasks` | Not a custom WebSocket. Not SSE resumability / `Last-Event-ID`. |
| `/runs` | **Deprecated** ReadyAgents JSON alias of the same durable run record (start, poll, decide, cancel) | Not a second store. Removal no earlier than v1.2. |

`taskId` **is** the 32-hex `run_id`. Prefix lookup is CLI-only and is rejected on the protocol surface. Missing and unauthorized ids return the same not-found body.

### Official tasks and MRTR approvals

`run_workflow` for a client that declares `io.modelcontextprotocol/tasks` returns `resultType: "task"` only after the run record is durable (`tasks/get` for that id already resolves). Short tools (`calc`, `now`, …) stay `resultType: "complete"`.

Status map: running/queued → `working`, paused → `input_required`, succeeded → `completed`, failed → `failed`, cancelled → `cancelled`. `pollIntervalMs` is 1000. `ttlMs` is JSON `null`: records persist until `readyagents runs gc`, not an implied expiry.

A paused approval is one `inputRequests` entry. The key is `readyagents.approval.{run_id}.{gate_id}.{occurrence}` (unique over the task lifetime, including foreach/parallel). The client answers with `tasks/update` `inputResponses`. That payload is converted into the existing decision object and travels `readyagents decide`: RBAC, optional HMAC (`READYAGENTS_DECISION_SECRET`), append-only audit, then resume. Reject is first-class (`decision: reject`). Duplicate keys with the same decision are a no-op; a conflicting second decision is a typed conflict. `tasks/update` when the task is not `input_required` is a typed error and a no-op.

Worked round trip (approval_gate.yaml):

```text
tools/call run_workflow  →  resultType=task, taskId=<32-hex>
tasks/get                →  status=input_required, inputRequests[readyagents.approval.<id>.gate.0]
tasks/update             →  inputResponses[key]={action: accept, content: {decision: approve}}
tasks/get                →  status=completed
```

A keyless in-process transcript is `examples/mcp_tasks_client.py`.

### What is not implemented

Roots, Sampling, `logging/setLevel`, OAuth authorization server, Dynamic Client Registration, Client ID Metadata Documents, SSE resumability / Last-Event-ID / redelivery, hosted recovery, and non-loopback bind. The loopback bearer token remains the only HTTP authentication. Process death still loses the in-flight executor.

### SDK extras

`pip install -e ".[mcp]"` keeps `mcp>=1.2,<3`. `pip install -e ".[mcp2]"` pins the 2.x line. `server/discover` and `readyagents mcp serve --json` report the installed pin and a `full` / `legacy` / `absent` tier. A 1.x pin advertises the reduced version list rather than claiming `2026-07-28`.

### `/mcp` — MCP Streamable HTTP

```bash
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. Transport framing, session IDs, protocol-version headers, and JSON vs SSE response negotiation are handled by the installed Python MCP SDK. ReadyAgents does not claim conformance beyond that SDK.

Over `/mcp`, tools are the same as stdio: `now`, `calc`, `json_get`, `json_set`, `json_merge`, `list_dir`, `read_file`, `write_file`, `http_get` (if enabled), and `run_workflow`. On `2026-07-28` with the tasks extension declared, `run_workflow` returns a durable task handle. Otherwise it still waits and returns the run record as JSON. On Streamable HTTP POST, `2026-07-28` requests must send `Mcp-Method` (and `Mcp-Name` for `tools/call` and `tasks/*`) matching the JSON-RPC body; a mismatch is `-32020` before dispatch.

v0.10 binds **loopback only**. Non-loopback hosts are rejected.

### `/runs` — deprecated ReadyAgents JSON alias

Deprecated in 0.10.0; removal no earlier than 1.2.0. Same coordinator as `tasks/*`. Responses include additive `deprecated: true` and `successor: "tasks/*"`.

Authenticated JSON API on the same foreground process. Persistence cannot be disabled. The identifier is the full opaque 32-hex `run_id` (no prefixes).

A stdlib example that talks **only** to `/runs` (not MCP JSON-RPC) is `examples/mcp_http_client.py`. It does not start the server. HITL reuses `examples/approval_gate.yaml`; there is no separate `async_approval.yaml`.

#### `POST /runs` → `202` after a durable queued/running record

```http
POST /runs
Authorization: Bearer <token>
Content-Type: application/json

{"path": "examples/calc_pipeline.yaml", "inputs": {}, "actor": "local-integrator", "dry_run": false}
```

Accepted fields: `path` (required string), `inputs` (object, default `{}`), `actor` (optional string), `dry_run` (boolean, default false). Unknown fields → `400`. Never send provider keys in this JSON.

Optional `Idempotency-Key`: the same key and body replay the original handle for the life of this process; a conflicting body → `409`. A full queue (`--max-pending-runs`, default 32) → `429` with no run record.

```http
HTTP/1.1 202 Accepted
Location: /runs/<32-hex>
```

```json
{
  "ok": true,
  "run_id": "<32-hex>",
  "status": "queued",
  "links": {
    "self": "/runs/<32-hex>",
    "decide": "/runs/<32-hex>/decide",
    "cancel": "/runs/<32-hex>/cancel"
  }
}
```

#### `GET /runs/{full_id}` → `200`

Returns `ok`, the existing `RunState.to_record()` fields, and additive `links`. A paused record includes `pending_node` and the persisted `pending.prompt`. `404` unknown, `400` malformed or prefix ids. Unique-prefix lookup is CLI-only.

#### `POST /runs/{id}/decide`

```json
{"node_id": "gate", "decision": "approve", "actor": "reviewer"}
```

`decision` is `approve` or `reject`. The node must be the currently pending approval node. `202` with status `running` when resume is submitted, `409` conflict, `403` RBAC, `404` unknown. Broader decision-file shapes stay on the CLI.

#### `POST /runs/{id}/cancel`

```json
{"actor": "local-integrator", "reason": "caller timeout"}
```

`202` with `cancel_requested` until a safe engine point, then `cancelled`. Retry backoff is a safe point. Already-terminal → `200` (idempotent). Cooperative: does not kill a blocking tool or provider call.

#### Error envelope

Every `/runs` error uses:

```json
{
  "ok": false,
  "error": "RunConflict",
  "message": "Run is not awaiting a decision",
  "run_id": "<32-hex or null>",
  "request_id": "..."
}
```

### Authentication

With `--auth token` (default), all `/mcp` and `/runs` requests require:

```http
Authorization: Bearer <opaque-token>
```

- `--auth token` (default). Token from `--token-env` (default `READYAGENTS_MCP_TOKEN`).
- There is **no** token-value CLI flag (process listings expose argv).
- If token auth is on and the env var is empty, the process generates at least 256 bits of entropy and prints the token once to **stderr**. It is never persisted or logged.
- `--auth none` is allowed only on an exact loopback bind and prints a warning.
- Compare token bytes in constant time. Missing or wrong credentials → `401` with `WWW-Authenticate: Bearer`.
- `Host` and browser `Origin` are checked against the configured loopback listener (DNS rebinding).
- Responses use `Cache-Control: no-store`.

### Lifecycle limits (v0.10)

This is a **request-driven foreground door**. It is not a scheduler, cron, watcher, queue scanner, retry daemon, auto-start, or hosted control plane. It does not recover incomplete runs on startup.

Stopping the command stops the listener and the in-process executor. Work does **not** survive process death. A run record may remain on disk; nothing resumes it automatically.

For concurrent local mutations (this door plus CLI `decide` or the localhost approval UI), optional SQLite is recommended (`READYAGENTS_RUN_STORE=sqlite`). JSON remains the default. SQLite is not hosted recovery and is not required. See [run-stores.md](run-stores.md).

`--max-concurrent-runs` defaults to 4. `--max-pending-runs` defaults to 32.

### Example client

```bash
# terminal 1
pip install -e ".[mcp]"
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765

# terminal 2 (same token; printed to stderr if generated)
export READYAGENTS_MCP_TOKEN=...
python examples/mcp_http_client.py
python examples/mcp_http_client.py --path examples/calc_pipeline.yaml
```

## Security notes

- `list_dir` / `read_file` / `write_file` cannot escape the workspace directory
- YAML `workspace:` cannot relocate the sandbox outside `READYAGENTS_WORKSPACE`
- MCP `run_workflow` and `POST /runs` only load a workflow file under that same root
- MCP client tools are registered as `server.tool` and cannot replace sandbox builtins (`read_file`)
- MCP stdio children do not inherit `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` unless set in `mcp_servers.*.env`
- `http_get` is opt-in
- `calc` does not evaluate arbitrary Python
- Never send provider keys in `/runs` request JSON; use env / pack secret hooks
- Do not expose the loopback HTTP door to the internet
- MCP `tasks/update` approvals have the same authority as `readyagents decide`; unsigned (when `READYAGENTS_DECISION_SECRET` is set) or unauthorized decisions are refused, audited, and leave the run paused
