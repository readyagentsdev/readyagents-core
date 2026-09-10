# Workflows

Workflows are YAML or JSON documents validated with Pydantic. A derived JSON Schema 2020-12 document describes the **file** format for editors (`readyagents schema`, `schemas/workflow-v1.json`). See [authoring.md](authoring.md) for completion setup, the `$schema` / modeline convention, and source-located errors.

The schema `$id` path segment `v1` tracks the workflow file format, not the package version. Additive changes (new optional field, new node type, widened enum) keep `v1`. A breaking change to the file format would mint `v2` and ship both files for at least one minor release. `x-readyagents-version` on a generated copy records which Core release produced it.

`https://readyagents.dev/schema/workflow/v1.json` is the identifier; the website does not serve that URL today. Scaffolds point at a local `./workflow.schema.json` instead. ReadyAgents never fetches a `$schema` URL.

A top-level `$schema` key in a JSON workflow is ignored at load time. A `yaml-language-server` modeline is a comment and is already inert.

## Minimal example

```yaml
name: hello
inputs:
  name: world
nodes:
  - id: greet
    type: transform
    template: "hello {{name}}"
    output_key: message
```

Run:

```bash
readyagents run hello.yaml --input name=Ada
```

## Fields

| Field | Meaning |
| --- | --- |
| `name` | Workflow name (stored on the run record) |
| `version` | Free-form string |
| `inputs` | Default inputs. A value may be a scalar or `{default, description}` |
| `required_inputs` | Keys that must be present after defaults + CLI. Missing keys fail with `Pass --input KEY=...`. |
| `start` | First node id (defaults to the first node) |
| `nodes` | List of node objects |
| `edges` | Optional `{from, to, when}` routing |
| `mcp_servers` | Map of name → `{command, args, env, cwd}` |
| `allow_http` | Enable builtin `http_get` |
| `workspace` | Sandbox directory; must stay under `READYAGENTS_WORKSPACE` if set, else the workflow file's directory |
| `default_model` | Override `READYAGENTS_DEFAULT_MODEL` for agent nodes |
| `budget` | `{max_tokens, max_cost_usd}` — further LLM calls raise `BudgetExceeded` (accumulated usage; unchanged) |
| `runaway` | `{max_model_calls, max_tool_rounds, max_wall_seconds}` — optional per-run guards; raise `RunawayGuard` |
| `fallback_models` | Model refs tried after the primary LLM fails |
| `circuit` | `{failure_threshold, cooldown_seconds}` process-local breaker |
| `on_pause_url` | Outbound webhook URL when an approval node pauses (same public-IP pin as `http_get`; failures do not abort the pause) |
| `cache_llm` | Opt in to the local LLM cache for this workflow |
| `redact` | Opt in to PII redaction for this workflow |

## Node fields (all types)

| Field | Meaning |
| --- | --- |
| `id` | Unique id |
| `type` | `agent` \| `tool` \| `condition` \| `transform` \| `approval` \| `parallel` \| `include` \| `foreach` |
| `next` | Default successor if no edges |
| `output_key` | Alias for templates (`{{brief}}` instead of `{{write}}`) |
| `timeout_seconds` | Soft timeout |
| `retry` | `{max_attempts, backoff_seconds, backoff_multiplier}` |

If `next` and `edges` are omitted, the engine uses **list order** (the following node).

## Agent

```yaml
- id: draft
  type: agent
  model: openai:gpt-4o-mini   # optional
  system: You are concise.
  prompt: |
    Write a haiku about {{topic}}.
  output_key: haiku
```

Missing `{{variables}}` fail with `TemplateError` unless you use `| default`. Closed filters: `default`, `len`, `join` (e.g. `{{results | len}}`, `{{results | join ", "}}`).

Optional agent fields:

| Field | Meaning |
| --- | --- |
| `fallback_models` | Extra `provider:model` refs if this node’s primary fails |
| `output_schema` | JSON Schema object; the LLM payload is parsed and validated with Pydantic (`StructuredOutputError` on mismatch) |
| `cache` | `true` / `false` to override workflow/settings LLM cache for this node |
| `tools` | Allowlist of registry tool names the agent may call (`calc`, `read_file`, MCP `server.tool`, …). Omit or `[]` for a one-shot complete (0.4.0 behavior). The model cannot call anything else. |
| `max_tool_rounds` | Cap on tool-call rounds (default 8, max 20). Exceeding raises `NodeError`. |

```yaml
- id: worker
  type: agent
  prompt: Use calc if needed. What is 2+2?
  tools: [calc]
  max_tool_rounds: 4
  output_key: answer
```

`--dry-run` still skips the LLM and does not execute `write_file` / `http_get` even when those names are on the allowlist.

```yaml
- id: classify
  type: agent
  prompt: Return JSON for {{message}}
  output_schema:
    type: object
    required: [priority]
    properties:
      priority: {type: string}
```

## Tool

```yaml
- id: math
  type: tool
  tool: calc
  arguments:
    expression: "1 + {{n}}"
  output_key: total
```

Builtin tools: `now`, `calc`, `json_get`, `json_set`, `json_merge`, `list_dir`, `read_file`, `write_file`, `http_get` (opt-in).

MCP tools are named `server.tool` (and also the bare tool name if unique).

## Condition

```yaml
- id: branch
  type: condition
  when: classified.priority == "urgent"
  then: urgent_reply
  else: normal_reply
```

Supported expressions (no Python `eval`):

- dotted path truthiness: `allow_http`
- comparisons: `== != > < >= <= contains startswith endswith`
- `and` / `or` / `not` and parentheses: `a == 1 and b == 2`
- quoted strings, numbers, `true` / `false`
- `{{templates}}` on either side

## Approval (human-in-the-loop)

```yaml
- id: gate
  type: approval
  prompt: "Release payment of {{total}}?"
  then: receipt
  else: denied
```

The engine **pauses** (CLI exit **2**, status `paused`, `pending_node` set) until you pass an explicit decision. Exit 2 is paused / decision pending, not a crash. It does not block on a TTY. Put **one** gate in front of a risky step (`examples/gated_write.yaml`); do not wrap every tool.

```bash
readyagents run pay.yaml --approve gate
readyagents run pay.yaml --reject gate
readyagents resume <run_id> --approve gate
readyagents approvals serve   # optional localhost UI; YAML unchanged
```

The approval YAML is unchanged. The optional page is a foreground loopback UI, not a hosted dashboard. See [browser-approval-ui.md](browser-approval-ui.md).

Optional enterprise fields (all inert unless set): `approvals_required`, `distinct_actors`, `deny_actor`, `approver_roles`, `require`, `expires_in`, `on_expire` (`reject` / `escalate` / `fail`; **`approve` is refused**), `escalate_to`, `require_reason`, `reject_short_circuit`, `recommendation`, `notify`. Core starts **no timer** — expiry is lazy. Full model: [approvals.md](approvals.md).

`then` is the approve path; `else` is the reject path. `next` is used when approved if `then` is omitted.

Multiple gates in one graph are allowed. Each needs its own `--approve NODE` (or `--reject`), or an injected JSON decision (`readyagents decide` / `--decision-file`). See `examples/multi_gate.yaml`.

## Parallel

Independent branches run concurrently. Output is a mapping of branch id → result.

```yaml
- id: fan
  type: parallel
  output_key: parts
  next: join
  branches:
    - id: left
      type: tool
      tool: calc
      arguments:
        expression: "1+1"
    - id: right
      type: tool
      tool: now
```

Templates can use `{{parts.left}}`. Max 8 worker threads. Resume skips branches that already returned ok.

## Include (sub-workflow)

```yaml
- id: child
  type: include
  path: included_min.yaml   # relative to the parent file
  inputs:
    n: "{{n}}"
  output_key: nested
```

Includes are depth-limited (8) so cycles fail with a typed error. Nested runs do not write their own run records; child progress is stored on the parent so resume does not re-run successful child nodes. The `path` is resolved relative to the parent workflow file and must stay under that directory (no `..` or absolute escapes).

## Foreach (sequential map)

```yaml
- id: each
  type: foreach
  items: expressions    # list on inputs or prior outputs
  max_items: 32         # default 32, max 100
  body:
    id: math
    type: tool
    tool: calc
    arguments:
      expression: "{{item}}"
  output_key: results
```

The body runs once per item (`{{item}}` and `{{index}}`). Nested foreach is rejected. Resume skips items already stored as `ok`. Exceeding `max_items` raises `NodeError`.

## Transform

```yaml
- id: parse
  type: transform
  source: classification_raw
  parse_json: true
  output_key: classified

- id: pick
  type: transform
  source: classified
  json_path: priority
  output_key: priority

- id: line
  type: transform
  template: "status={{priority}}"
```

## Edges

```yaml
edges:
  - from: classify
    to: parse
  - from: parse
    to: urgent
    when: classified.priority == "urgent"
  - from: parse
    to: normal
```

An edge without `when` is the default if no conditioned edge matches.

## Run records

Runs persist to `.readyagents/runs/<run_id>.json` **after each node** unless you pass `--no-persist`. Paused and failed runs store `pending_node` so `readyagents resume` continues from the last successful node.

```bash
readyagents runs list
readyagents runs show <run_id>
readyagents runs replay <run_id>
```

## Examples in this repo

| File | Needs LLM | Notes |
| --- | --- | --- |
| `examples/calc_pipeline.yaml` | No | Clone-and-run smoke test |
| `examples/approval_gate.yaml` | No | Human-in-the-loop approval |
| `examples/quorum_gate.yaml` | No | Two-approver quorum gate |
| `examples/expiring_gate.yaml` | No | Lazy `expires_in` / `on_expire: reject` |
| `examples/gated_write.yaml` | No | calc → one approval → write_file; pause does not create the file |
| `examples/multi_gate.yaml` | No | Two sequential approval gates |
| `examples/fanout_gate.yaml` | No | Parallel fan-out + approval |
| `examples/include_demo.yaml` | No | Sub-workflow `include` |
| `examples/composed_gate.yaml` | No | Include + parallel + approval |
| `examples/research_brief.yaml` | Yes | Optional HTTP fetch |
| `examples/support_triage.yaml` | Yes | JSON classify then branch |
| `examples/code_review.yaml` | Yes | Builtin `read_file` + review |
| `examples/agent_tools.yaml` | Yes (dry-run: no) | Agent `tools: [calc]` allowlist |
| `examples/foreach_calc.yaml` | No | Sequential `foreach` + `calc` |
| `examples/json_mutate.yaml` | No | `json_set` / `json_merge` |
| `examples/list_dir.yaml` | No | Builtin `list_dir` (no MCP / no Node) |
| `examples/eval/pass.yaml` | No | Keyless `readyagents eval` fixture suite |
| `examples/connector_demo.yaml` | No | Local `--pack` connector (`examples/packs/connector_pack.py`) |
