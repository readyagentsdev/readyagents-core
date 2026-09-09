# ReadyAgents Core (Unreleased)

Operators can explicitly start a local ReadyAgents approval page, review paused prompts, and
approve or reject them without copying CLI commands. The page binds only to loopback, exposes a
minimal redacted view, and protects bootstrap and decision actions with expiring one-use tokens.
It is not hosted, does not start automatically, and adds no frontend toolchain.

```bash
readyagents run examples/browser_approval.yaml    # exit 2
readyagents approvals serve --host 127.0.0.1 --port 8766
# open the stderr bootstrap URL once, then Approve or Reject
```

The optional Continuous pack (`readyagents-pack-continuous`) can run configured workflows from
cron, portable file watching, or authenticated local webhooks. It is a separate Python package
and foreground command. Installing Core alone remains entirely one-shot. See
[docs/continuous-pack.md](docs/continuous-pack.md).

# ReadyAgents Core 0.9.0

**Opt-in loopback Streamable HTTP and local task handles. Stdio unchanged. No hosted recovery.**

Packs are waitlisted, not for sale.

ReadyAgents v0.9 can expose its MCP toolkit through an explicitly started, loopback-only Streamable HTTP server. Long workflows may be submitted for an immediate durable run_id, then polled, approved, or cancelled without holding the original request open. The task-handle routes are a documented ReadyAgents extension; MRTR/elicitation and restart-surviving workers are not claimed. Existing stdio hosts continue to work unchanged.

## Try it (no API keys)

```bash
pip install readyagentsdev==0.9.0
readyagents new my-flow
```

Or from a clone:

```bash
git clone https://github.com/readyagentsdev/readyagents-core.git
cd readyagents-core
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"
readyagents run examples/calc_pipeline.yaml
```

Terminal 1 (foreground; token is `READYAGENTS_MCP_TOKEN`, or printed once to stderr if generated):

```bash
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

Terminal 2:

```bash
export READYAGENTS_MCP_TOKEN=...   # same token as the server
python examples/mcp_http_client.py
python examples/mcp_http_client.py --path examples/calc_pipeline.yaml
```

Stdio still needs no new flag:

```bash
readyagents mcp serve
```

HITL over `/runs` reuses `examples/approval_gate.yaml` (no separate async-approval fixture).

## What we deliberately left out of core

- Scheduler, cron, watcher, queue scanner, retry daemon, auto-start
- Hosted control plane; recovery of incomplete runs on startup
- Official MCP Tasks, MRTR, protocol elicitation
- Non-loopback binds (v0.9 rejects them)
- Work that survives process death: stopping the command stops the listener and in-process executor

## Compatibility

- Python 3.11+
- `readyagents mcp serve` still defaults to stdio
- Synchronous `run_workflow` MCP tool remains
- Existing 0.8.x workflow YAML and stdio hosts still work
- Core install without `[mcp]` still runs keyless workflows; HTTP imports fail only when used (`MCPError`)

## Docs

- [MCP](docs/mcp.md)
- [CLI](docs/cli.md)
- [Packs](docs/packs.md)
- [Continuous pack](docs/continuous-pack.md)
- [Configuration](docs/configuration.md)
- [Why ReadyAgents](docs/why-readyagents.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

---

# ReadyAgents Core 0.8.1

**`readyagents` is on PyPI.** Same engine as 0.8.0. Install docs no longer say the package is missing.

```bash
pip install readyagentsdev
readyagents new my-flow
```

The wheel does not ship `examples/`. Clone-and-run still uses `examples/calc_pipeline.yaml`.

---

# ReadyAgents Core 0.8.0

**Eval CLI, local packs, workspace `list_dir`, extra `new` templates, unified `--json`. Still local. No always-on. No Node.js.**

0.8.0 is the engine cut after 0.7.0. Install with `pip install readyagentsdev` or from a clone (`pip install -e .`).

## Why this release matters

1. **`readyagents eval`** scores fixture suites without a network or API keys.
2. **`--pack`** loads `examples/packs/connector_pack.py` without an entry point (path confined to the workspace).
3. **`list_dir`** lists a workspace without an MCP filesystem server and without Node.js.
4. **`readyagents new --template foreach|agent-tools|gated`** matches engine features already shipped.
5. **`--json`** always includes `ok` and `command` on the operator-facing commands.

## Try it (no API keys)

```bash
git clone https://github.com/readyagentsdev/readyagents-core.git
cd readyagents-core
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
readyagents eval examples/eval/pass.yaml
readyagents run examples/list_dir.yaml
readyagents run examples/connector_demo.yaml --pack examples/packs/connector_pack.py --no-persist
readyagents new demo --template foreach
readyagents run examples/gated_write.yaml --approve gate --no-persist
```

## What we deliberately left out of core

- Nested foreach, `http_request` POST, inbound listeners, streaming
- Hosted control plane, extra databases, billing, always-on workers, Node.js

## Compatibility

- Python 3.11+
- Existing 0.7.0 workflow YAML still runs. `--json` objects gain `ok` and `command`; previous keys remain.
- CLI exit code **2** is still “paused for approval”.

## Docs

- [README](README.md)
- [Changelog](CHANGELOG.md)
- [CLI](docs/cli.md)
- [MCP](docs/mcp.md)
- [Packs](docs/packs.md)

---

# ReadyAgents Core 0.7.0

**Honest composition resume and a real MCP client. Still local. No always-on.**

0.7.0 is the engine cut after 0.6.0. Install from a clone (`pip install -e .`). This package is not on PyPI.

## Why this release matters

1. **Resume does not re-run paid work** inside `include` or `parallel` (same idea as 0.6 foreach snapshots).
2. **MCP tools share one stdio session** per named server; schemas reach agent `tools:`; `cwd` cannot leave the workspace.
3. **Pause webhooks cannot hit loopback/private/metadata** — same pin as `http_get`. HITL still pauses if notify fails.
4. **YAML glue:** `{{x | default}}`, `{{list | len}}`, `{{list | join}}`, and `when: a == 1 and b == 2` with no Python `eval`.

## Try it (no API keys)

```bash
pip install -e .
readyagents run examples/calc_pipeline.yaml
readyagents run examples/foreach_calc.yaml
readyagents run examples/composed_gate.yaml --approve gate
```

## What we deliberately left out of core

- Nested foreach, unbounded map, `http_request` POST, inbound listeners
- Hosted control plane, extra databases, billing, always-on workers

## Compatibility

- Python 3.11+
- Existing 0.6.0 workflow YAML still runs. Filters, `and`/`or`, and MCP session behavior are additive.
- CLI exit code **2** is still “paused for approval”.

## Docs

- [README](README.md)
- [Changelog](CHANGELOG.md)
- [Workflows](docs/workflows.md)
- [MCP](docs/mcp.md)

---

# ReadyAgents Core 0.6.0

**List-shaped local YAML, inspectable tool-use, operable run store. No always-on.**

0.6.0 is the engine cut after 0.4.0 docs honesty. Install from a clone (`pip install -e .`). This package is not on PyPI.

## Why this release matters

A careful local YAML-first team can now:

1. **Process a list** with bounded sequential `foreach` (`{{item}}` / `{{index}}`, resume skips ok items).
2. **Mutate JSON** with `json_set` / `json_merge` (same size caps as `json_get`).
3. **Let an agent call allowlisted tools**, see each round on the run record, and recover from a bad tool call without killing the node.
4. **Operate the local run store:** pause prompt on the JSON, `runs delete` / `runs gc`, Ctrl-C → `cancelled`.

Core stays small. Packs still own always-on listeners, hosted control planes, extra databases, and billing.

## What is new

### Foreach

```yaml
- id: each
  type: foreach
  items: expressions
  max_items: 32
  body:
    id: math
    type: tool
    tool: calc
    arguments:
      expression: "{{item}}"
  output_key: results
```

`readyagents run examples/foreach_calc.yaml` (no keys). Nested foreach is rejected.

### JSON mutate

```bash
readyagents run examples/json_mutate.yaml
```

`json_set` / `json_merge` refuse empty path segments and `__` keys.

### Agent tools

```yaml
- id: worker
  type: agent
  prompt: Use calc if needed. What is 2+2?
  tools: [calc]
  max_tool_rounds: 4
```

`--dry-run` still skips the LLM. `runs show --json` includes `tool_rounds`. Off-allowlist names are not executed.

### Runs

```bash
readyagents run examples/approval_gate.yaml          # exit 2; record has the prompt
readyagents runs show <run_id> --json
readyagents resume <run_id> --approve gate           # pending cleared on success
readyagents runs delete <run_id> --yes
readyagents runs gc --yes                            # paused kept unless --include-paused
```

## Try it (no API keys)

```bash
pip install -e .
readyagents run examples/calc_pipeline.yaml
readyagents run examples/foreach_calc.yaml
readyagents run examples/json_mutate.yaml
readyagents run examples/agent_tools.yaml --dry-run
readyagents run examples/approval_gate.yaml
```

## What we deliberately left out of core

- Always-on webhook listeners, workers, cron, queue consumers
- Hosted control plane, SSO, multi-tenant teams, billing
- Nested foreach, unbounded map, extra databases

## Compatibility

- Python 3.11+
- Existing 0.4.0 workflow YAML still runs. New fields (`tools`, `max_tool_rounds`, `type: foreach`, `json_set` / `json_merge`) are additive.
- CLI exit code **2** is still “paused for approval”.

## Docs

- [README](README.md)
- [Changelog](CHANGELOG.md)
- [Workflows](docs/workflows.md)
- [CLI](docs/cli.md)

---

# ReadyAgents Core 0.4.0

**Docs honesty after 0.3.0. No new nodes. No always-on.**

0.4.0 labels the honesty PRs that landed on main after the 0.3.0 engine:

- CHANGELOG Unreleased no longer lists items already in 0.3.0 (#24)
- first-ten-minutes matches README: `readyagents new my-flow` (default is pipeline) (#25)
- Leftover marks extras, size caps, DNS rebind, parallel timeout/retry, packs JSON, and Makefile/CONTRIBUTING smoke as shipped (#26)

The engine is still 0.3.0's. Install from a clone (`pip install -e .`). This package is not on PyPI.

---

# ReadyAgents Core 0.3.0

**Local hooks that still fit a small, YAML-first core.**

ReadyAgents Core remains the free engine: YAML workflows, BYOK, builtin tools, MCP optional, packs for extensions. **0.2.0** made runs durable and inspectable. **0.3.0** adds operator controls for a local clone — cost, approvals from outside the CLI, secrets/RBAC/audit hooks, structured output, a local cache — without an always-on control plane.

Apache-2.0. No vendor keys. No hosted runtime in core.

## Why this release matters

A careful engineer running agent graphs from a clone needs more than pause/resume:

1. **See cost per node** and stop when a budget is hit.
2. **Approve from a ticket or webhook pack**, not only `--approve` on a TTY.
3. **Ship the wheel** (local sdist/wheel, not on PyPI) and a one-service Dockerfile.
4. **Hook secrets, RBAC, and an append-only audit log** without vendoring Vault/AWS.
5. **Validate LLM JSON with Pydantic**, cache identical calls, and test workflows offline.

Core stays small. Packs still own connectors, inbound HTTP, and policy engines.

## What is new

### Observability and cost

JSON logs (`readyagents --log-format json …`) emit machine-parseable events with `run` and `node`. Agent nodes record `prompt_tokens` / `completion_tokens` / `cost_micros` on the node result; the run total is the sum. Dry-run still reports `estimated_tokens`.

```yaml
budget:
  max_tokens: 50000
  max_cost_usd: 1.0
fallback_models:
  - anthropic:claude-sonnet-4-5
circuit:
  failure_threshold: 3
  cooldown_seconds: 60
```

`BudgetExceeded` is typed. A failing primary model is retried on `fallback_models`. A process-local circuit breaker skips a recently failing model until cooldown.

### Approvals beyond the CLI

`--approve` / `--reject` and exit 2 still work. A paused run can also take a JSON payload:

```bash
readyagents run examples/approval_gate.yaml          # exit 2
readyagents decide <run_id> --file decision.json     # {"gate": "approve"}
# or
readyagents resume <run_id> --decision-file decision.json
readyagents run examples/multi_gate.yaml --approve first --approve second
```

Set `on_pause_url` (or `READYAGENTS_PAUSE_NOTIFY_URL`) for an **outbound** POST when a gate pauses. Core does not listen on a port.

### Security hooks

- Secrets backends via pack `register_secrets()`; env/`.env` remains the default BYOK path
- Append-only `$READYAGENTS_HOME/audit/<run_id>.jsonl` (resume snapshots still overwrite the run JSON)
- `--actor` plus pack `register_authorizers()` (`AuthorizationError` on deny)
- `READYAGENTS_REDACT=1` masks emails, `sk-…` keys, and `READYAGENTS_REDACT_LITERALS`

### Structured output, cache, packs

```yaml
- id: classify
  type: agent
  prompt: Return JSON.
  output_schema:
    type: object
    required: [priority]
    properties:
      priority: {type: string}
```

Invalid JSON raises `StructuredOutputError`. `READYAGENTS_LLM_CACHE=1` stores completions under `$READYAGENTS_HOME/cache/` (`--no-cache` skips). `examples/packs/connector_pack.py` is a local connector (no network) that registers `connector_ping`.

### Test helpers

```python
from readyagents.testing import EvalCase, RecordedLLM, ScriptedLLM, run_eval, run_workflow_spec
```

`RecordedLLM` replays a cassette with no network. `run_eval` scores pass/fail fixture workflows.

### Deploy

```bash
python -m build          # sdist + wheel (not published from this repo)
docker compose run --rm readyagents run examples/calc_pipeline.yaml
make smoke               # lint is separate: make ci
```

## Try it (no API keys)

```bash
pip install -e .
readyagents run examples/calc_pipeline.yaml
readyagents run examples/approval_gate.yaml
readyagents decide <run_id> --node gate --decision approve
readyagents run examples/multi_gate.yaml --approve first --approve second
readyagents run examples/support_triage.yaml --dry-run --input message=hello
```

## What we deliberately left out of core

- Always-on webhook listeners, workers, cron, queue consumers
- Hosted control plane, SSO, multi-tenant teams, billing
- OpenTelemetry stacks, extra databases, AWS/GCP/Vault SDKs
- New LLM vendors or live-network tests as a gate

## Compatibility

- Python 3.11+
- Existing 0.2.0 workflow YAML still runs. New fields (`budget`, `fallback_models`, `output_schema`, `cache`, `on_pause_url`) are additive.
- CLI exit code **2** is still “paused for approval”.

## Docs

- [README](README.md)
- [Changelog](CHANGELOG.md)
- [Getting started](docs/getting-started.md)
- [Workflows](docs/workflows.md)
- [CLI](docs/cli.md)
- [Configuration](docs/configuration.md)
- [Packs](docs/packs.md)

## Why this release matters

A careful engineer running agent graphs needs four things a playground does not:

1. **A human can stop a run.** Approval nodes pause (they do not hang a TTY).
2. **A failed step is not a lost run.** State is written after every node; `resume` continues from the last success.
3. **You can see what happened.** `runs list` / `show` / `inspect` / `replay`.
4. **Starting a project is boring in the good way.** `readyagents new` writes a workflow, README, and `.env.example`.

That is the difference between a demo engine and something you would actually use.

## What is new

### Human-in-the-loop

```yaml
- id: gate
  type: approval
  prompt: "Release payment of {{total}}?"
  then: receipt
  else: denied
```

Without a decision the CLI exits **2**, persists `paused`, and tells you how to continue:

```bash
readyagents run examples/approval_gate.yaml
readyagents resume <run_id> --approve gate
# or
readyagents run examples/approval_gate.yaml --approve gate
```

### Durable local runs

JSON under `.readyagents/runs/<run_id>.json`, written atomically after each node.

```bash
readyagents runs list
readyagents runs show <run_id>
readyagents runs show <run_id> --json
readyagents runs replay <run_id>
readyagents resume <run_id> --approve gate
```

Filters: `--status`, `--workflow`, `--limit`. Logs include `run=` and `node=`.

### Composition

- **`type: parallel`** — independent branches concurrently (`examples/fanout_gate.yaml`)
- **`type: include`** — run another workflow file (`examples/include_demo.yaml`)

### Scaffolding

```bash
readyagents new my-flow                     # approval template (default)
readyagents new my-flow --template basic
readyagents new my-flow --template research # parallel + approval
```

Each writes `workflow.yaml`, `README.md`, and `.env.example`.

### Dry-run

Agent nodes report `usage: estimated_tokens=…`. LLM examples that `parse_json` after an agent no longer crash on the dry-run stub.

## Try it (no API keys)

```bash
pip install -e .
readyagents run examples/calc_pipeline.yaml
readyagents runs list

readyagents run examples/approval_gate.yaml --approve gate
readyagents run examples/fanout_gate.yaml --approve gate
readyagents run examples/include_demo.yaml

readyagents new demo --template research
readyagents run demo/workflow.yaml --approve publish
```

With keys, the existing research / triage / review examples still work. `--dry-run` still walks them without calling a vendor.

## What we deliberately left out of core

These belong in waitlisted packs, not for sale:

- Always-on / continuous workers and schedulers
- Hosted control plane, teams, SSO
- Distributed recovery and remote run stores
- Alerting, paging, billing

Core stays small so those can plug in via `readyagents.packs`.

## Compatibility

- Python 3.11+
- Existing 0.1.0 workflow YAML still runs. New node types (`approval`, `parallel`, `include`) are additive.
- CLI exit code **2** now means “paused for approval”, not a generic failure.

## Docs

- [README](README.md)
- [Changelog](CHANGELOG.md)
- [Getting started](docs/getting-started.md)
- [Workflows](docs/workflows.md)
- [CLI](docs/cli.md)
