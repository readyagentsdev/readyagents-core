# Changelog

All notable changes to ReadyAgents Core.

## Unreleased

### Added

- Windows and macOS are now tested in CI across Python 3.11–3.14, alongside a wheel-install verification
  job that runs the pip-only first-run flow on each platform.
- `readyagents doctor` reports platform, Python, extras, workspace writability, permission
  enforceability, filesystem case sensitivity, loopback availability, and the resolved run-store backend.
- `python scripts/smoke.py` (and `make smoke`) runs the keyless example set without a POSIX shell.

### Security

- Workspace containment is now enforced by a single audited helper that rejects Windows reserved names,
  alternate data streams, short-name and trailing-dot forms, and that compares resolved paths with
  runtime-probed case sensitivity, so a case-only variant cannot escape the sandbox on Windows or macOS.
- File-permission claims are now accurate per platform and reported by `readyagents doctor`.

- `readyagents schema` emits a JSON Schema 2020-12 document generated from the workflow models, shipped
  as `schemas/workflow-v1.json` and referenced from every scaffold, so YAML editors offer completion and
  inline validation while authoring.
- Workflow validation errors now report file, line, column, and a caret excerpt, including inside
  `parallel` branches, `foreach` bodies, and included sub-workflows. `validate --json` gains an additive
  `problems` array.

## 0.10.1 — 2026-09-09

### Security

- Streamable HTTP `2026-07-28` POSTs validate `Mcp-Method` / `Mcp-Name` before any SDK
  dispatch, including ordinary `tools/call`. MCP `tasks/update` no longer treats
  `_meta` `clientInfo.name` as an RBAC actor. `readyagents mcp probe` sends the loopback
  bearer token from `READYAGENTS_MCP_TOKEN` when set.

## 0.10.0 — 2026-09-09

### Added

- Explicit localhost browser UI for redacted pending approvals, protected by short-lived
  single-use tokens and the existing RBAC/audit/signed-decision path. It uses vanilla assets,
  starts only on command, and does not relax outbound SSRF protection.
- Optional stdlib SQLite run-store with indexed run queries, revision conflict detection, and a
  verified non-destructive `runs migrate` command. JSON files remain the default and existing
  persistence/CLI behavior is preserved.
- MCP `2026-07-28` conformance: `server/discover`, stateless per-request `_meta` negotiation,
  `resultType` on every result, and the official `io.modelcontextprotocol/tasks` extension implemented
  over the existing durable run record.
- Approval gates are now reachable over MCP as Multi Round-Trip Request `input_required` items and
  resolvable with `tasks/update`, routed through the same signed-decision, RBAC, and audit path as
  `readyagents decide`.
- `readyagents mcp probe URL` reports a remote server's supported protocol revisions and extensions.

### Deprecated

- The ReadyAgents `/runs` HTTP extension is superseded by the official tasks extension. It continues to
  work and is scheduled for removal no earlier than v0.12.

### Security

- MCP `tasks/update` approvals use the same RBAC, optional HMAC, and append-only audit path as
  `readyagents decide`. Unsigned (when `READYAGENTS_DECISION_SECRET` is set) or unauthorized MCP
  approvals are refused and leave the run paused. Loopback-only bind and bearer auth are unchanged.

### Docs

- Documented the separately installed `readyagents-pack-continuous` package for explicit
  foreground cron, file-watch, and authenticated-webhook triggers. ReadyAgents Core itself still
  starts no scheduler or listener and gains no mandatory dependency.

## 0.9.0 — 2026-09-09

### Added

- Opt-in authenticated MCP Streamable HTTP transport and a local asynchronous run API with
  start, poll, decide, and cooperative-cancel task handles. Stdio remains the default, and core
  still has no scheduler, recovery worker, hosted service, or mandatory HTTP dependency.

### Security

- Optional MCP HTTP is loopback-only in v0.9, authenticates with a bearer token from the
  environment (never argv), and rejects DNS-rebinding Host/Origin values.

### Docs

- MCP Streamable HTTP (`/mcp`) and the ReadyAgents `/runs` task-handle extension are
  documented separately. Official MCP Tasks, MRTR, and elicitation are not claimed.

## 0.8.2 — 2026-09-03

### Added

- MCP Registry metadata: the packaged README carries the ownership marker and `server.json` describes the local stdio MCP server.

## 0.8.1 — 2026-09-03

### Fixed

- Install docs and the PyPI long description now match that `readyagents` is on PyPI (`pip install readyagents`). Clone-and-run still uses `examples/calc_pipeline.yaml`. The wheel does not ship `examples/`; pip-only first run is `readyagents new`.

## 0.8.0 — 2026-09-02

### Added

- **`readyagents eval PATH`.** Score a YAML/JSON `cases:` suite with the existing local harness. Exit 0 if every case passes, 1 if any fail. `--json` prints `ok`, `command`, `passed`, `failed`, and `results`. No network and no API keys.
- **Local packs.** Repeatable `--pack PATH` and `READYAGENTS_PACK` load a `.py` pack (`get_pack()`) confined to the workspace. `examples/connector_demo.yaml` runs with `--pack examples/packs/connector_pack.py`.
- **`list_dir` builtin.** Sandboxed directory listing (dotfiles skipped, entry cap, no `..` / symlink escape). `--dry-run` still lists. Example: `examples/list_dir.yaml`.
- **`readyagents new` templates** `foreach`, `agent-tools`, and `gated`. Default remains `pipeline`. Overwrite refusal unchanged.
- **Unified `--json` envelope.** Additive `ok` and `command` on validate, run, resume, packs, eval, and `runs show` (including missing ids). Existing keys stay. No Rich markup on JSON. Approval pause is still exit 2 and still includes `run_id`.

### Docs

- MCP docs lead with builtin `list_dir`, not `npx`. ReadyAgents remains Python-only.

### Fixed

- MCP `serve` registers `list_dir` (same workspace sandbox as `read_file` / `write_file`).

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, billing, or Node.js toolchain. Install with `pip install readyagents` or from a clone.

## 0.7.0 — 2026-08-27

### Added

- **Include / parallel resume snapshots.** Successful child nodes and parallel branches are stored on the parent run record; resume does not re-run them. Nested foreach stays invalid.
- **MCP client sessions.** One stdio child per named server for the run; `inputSchema` is copied onto tools; `cwd` is confined to the workspace.
- **Template filters** `default`, `len`, `join` and condition `and` / `or` / `not` (no Python `eval`).

### Security

- Pause notify (`on_pause_url`) uses the same public-IP pin as `http_get` (loopback/private/metadata refused, including after redirects). A blocked URL does not prevent the HITL pause.

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or billing.

## 0.6.0 — 2026-08-27

### Added

- **Bounded sequential `foreach`.** `type: foreach` iterates a list from prior state (`items:` path, `max_items` default 32 / max 100). Body is one node; output is the list of per-item results. Resume skips already-ok items. Nested foreach is rejected. Example: `examples/foreach_calc.yaml`.
- **`json_set` / `json_merge` builtins** (size-capped like `json_get`). Dotted paths; `__` segments refused. Example: `examples/json_mutate.yaml`.
- **Bounded agent tool-use loop.** Agent nodes may set `tools: [calc, …]` (an allowlist of registry/MCP names) and optional `max_tool_rounds` (default 8, hard max 20). The engine passes tool specs into `complete(..., tools=)`, runs **real** registry tools, and uses the **final** model text as the node output. Omit `tools` for a one-shot complete (0.4.0). Unknown YAML names fail before any LLM call; a model request off the allowlist is not executed. `--dry-run` still skips the LLM and does not run `write_file` / `http_get`. Example: `examples/agent_tools.yaml`.
- **Agent tool-round traces and recoverable tool errors.** Allowlisted `ToolError` is fed back as a tool observation (loop continues until the round cap). Persisted `node_results[].tool_rounds` and `runs show --json` name the tools.
- **Run-store hygiene.** Paused records store the approval **prompt** plus resume/decide copy-paste. `readyagents runs delete RUN_ID --yes` and `runs gc --yes` (paused kept unless `--include-paused`). KeyboardInterrupt persists status `cancelled`. `pending` is cleared on resume and success.

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or billing.

## 0.4.0 — 2026-08-26

### Docs

- Empty Unreleased of items already shipped in 0.3.0 (#24)
- `docs/first-ten-minutes.md` uses `readyagents new my-flow` (default pipeline), not `--template pipeline` (#25)
- Leftover marks extras, size caps, DNS rebind, parallel timeout/retry, packs JSON, and Makefile/CONTRIBUTING smoke as shipped (#26)

## 0.3.0 — 2026-08-22

### Added

- **Structured JSON logs** (`--log-format json` / `READYAGENTS_LOG_FORMAT=json`) with `run` and `node` on every event
- **Per-node token/cost tracking** rolled up on the run (`usage.prompt_tokens`, `completion_tokens`, `cost_micros`). Include nodes count nested agent usage once; parallel branches merge onto the fan-out node.
- **Budget limits** (`budget.max_tokens` / `budget.max_cost_usd` or env) stop further LLM work with `BudgetExceeded`
- **Model fallback** (`fallback_models` on the node or workflow) and a process-local **circuit breaker**
- **External decision injection:** `readyagents decide RUN_ID --file decisions.json` and `--decision-file` on `run`/`resume` (no always-on HTTP listener). Outbound `on_pause_url` notify only
- **Multi-gate example** `examples/multi_gate.yaml` (two sequential approvals)
- **Secrets-manager hooks** (env/`.env` remains default BYOK; packs may register backends)
- **Append-only audit trail** under `$READYAGENTS_HOME/audit/<run_id>.jsonl`
- **RBAC hooks** (`--actor`, pack `register_authorizers`) and optional **PII redaction**
- **Pydantic `output_schema`** on agent nodes (`StructuredOutputError` on mismatch)
- **Opt-in local LLM cache** (`READYAGENTS_LLM_CACHE=1`, `--no-cache` to skip)
- **Example connector pack** (`examples/packs/connector_pack.py`) proving the pack seam
- **`readyagents.testing`**: `run_workflow_spec`, `ScriptedLLM`, `RecordedLLM`, `run_eval`
- Official **Dockerfile** + **docker-compose.yml**; `make smoke` / `make ci` cover lint, tests, and keyless examples (dry-run, resume, approval, parallel, include)

### Reliability (folded from post-0.2.0)

- `readyagents new` overwrite refusal; `--log-level` on `run`; MCP tools cannot shadow sandbox `read_file`
- Nested `include` approval pauses the parent; `validate` rejects cycles; success prints `run_id:`
- Missing workflow path is `ConfigError` (exit 1); `--dry-run` stubs `write_file`; node timeouts return when the budget expires
- `http_get` refuses private/loopback/metadata hosts; file tools and `include` stay sandboxed

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or vendor secrets SDK. Those remain waitlisted packs, not for sale.

## 0.2.0 — 2026-08-19

### Added

- **Approval node** (`type: approval`) — human-in-the-loop gate. Pauses the run (exit 2) until `--approve` / `--reject` or `readyagents resume`.
- **Per-node persistence** and **resume** from the last successful node (`readyagents resume RUN_ID`). Records are JSON under `.readyagents/runs/`, written atomically.
- **Run inspection:** `readyagents runs list|show|inspect|replay`, with `--json`, `--status`, `--workflow`, and `--limit`.
- **Scaffolding:** `readyagents new` with templates `basic`, `approval`, and `research`.
- **Parallel fan-out:** `type: parallel` with concurrent `branches`.
- **Sub-workflows:** `type: include` to compose another YAML workflow (depth-limited).
- **Dry-run token estimate** on agent nodes (`usage.estimated_tokens`).
- Examples: `approval_gate.yaml`, `fanout_gate.yaml`, `include_demo.yaml`.

### Changed

- Structured logs include `run=<id>` and `node=<id>`.
- `--dry-run` `parse_json` after an agent stub no longer crashes LLM examples.

### Safety

- Core still has no always-on scheduler, control plane, distributed recovery, alerting, or billing. Those remain waitlisted packs, not for sale.
