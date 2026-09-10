# ReadyAgents Core 1.8.0

**Sovereign mode: process-level egress refuse, attest, bundle, doctor.**

`--sovereign` / `READYAGENTS_SOVEREIGN=1` refuses non-loopback egress at the
process socket boundary for the run (model calls, tools, packs, threads).
Loopback is allowed; private endpoints are explicit (`--sovereign-allow`).
`readyagents attest` emits a residency document that marks MCP stdio
`network_uncontrolled: true` and does not claim legal compliance.
`readyagents bundle` writes wheels plus checksums for
`pip install --no-index --find-links`. `readyagents doctor` reports whether
sovereign would succeed here and loopback model presence, never secret values.
Keyless local OpenAI-compatible endpoints are allowed. In-process is not an OS
sandbox. See [docs/sovereign.md](docs/sovereign.md) and
[docs/local-models.md](docs/local-models.md).

Also fixed: scoped delegation matches the gate's `approver_roles`; JSON
run-record reads retry Windows sharing violations; sequential HITL resume waits
out a prior in-flight executor when the next gate is already paused.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.8.0
readyagents run examples/calc_pipeline.yaml --sovereign
readyagents attest RUN --json
readyagents bundle --out ./offline-wheels
readyagents doctor
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.



# ReadyAgents Core 1.7.0

**Enterprise HITL: quorum, roles, deadlines, delegation.**

Opt-in quorum (`approvals_required`), distinct actors, `deny_actor` (including
`$initiator`), role routing (`approver_roles` / `require: any|all`), lazy
deadlines (`expires_in` with `on_expire: reject | escalate | fail` — `approve`
is refused at validation), time-bounded revocable single-hop delegation
(`readyagents delegate` / `delegations list|revoke`), `require_reason` and
override recording, file / command / webhook notify channels, and
`readyagents approvals list`. Core starts no timer; expiry is evaluated on
resume, decide, and status query. A gate with none of the new fields is
unchanged.

Also: MCP decide can resume a recorded decision that is still paused, so
sequential approval resumes do not stall after the first decide.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.7.0
readyagents approvals list
readyagents delegate --help
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.



# ReadyAgents Core 1.6.0

**Supply-chain trust: digests, detached signatures, keyring, lock, SBOM.**

Canonical SHA-256 digests (algorithm v1) for workflows including every resolved
`include`, pack file bytes, and MCP advertised tool surfaces. `readyagents sign`
/ `verify` write a detached Ed25519 signature beside the artifact that binds
digest and kind. `readyagents trust add|list|remove` manages a local publisher
keyring under `$READYAGENTS_HOME`. `--require-signed` and policy `require_signed`
refuse unsigned or untrusted artifacts before a pack is imported.
`readyagents lock` and `--frozen` pin digests; `readyagents sbom` emits a
deterministic CycloneDX-shaped inventory. Optional `sign` extra is not in `all`.
No default-trusted key. Signing proves origin, not safety.

Also: `--require-signed` executes the digested workflow/include buffers (not a
later re-read), pack lock pins are checked before import, templated `include`
paths are refused under `--require-signed` / `--frozen`, and
`on_lock_mismatch: gate` persists a paused run that can be resumed.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install 'readyagentsdev[sign]==1.6.0'
readyagents trust list
readyagents verify examples/calc_pipeline.yaml
readyagents sbom examples/calc_pipeline.yaml
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e '.[sign]'`.


# ReadyAgents Core 1.5.1

**Identity skeptic fixes: outbound notify JWTs and brokered secret isolation.**

Workload `sign_assertion` is attached to outbound pause-notify (`post_json`)
when configured so a fixture peer can verify the JWT against the public key.
Credential grants are delivered via a thread-local mapping, not shared
`os.environ`, so parallel tool branches cannot see a sibling's secret.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.5.1
readyagents identity whoami
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.


# ReadyAgents Core 1.5.0

**Agent identity and credential brokering.**

Approvers may present an OIDC/JWT assertion verified against a local trust-anchor
file (`--token-file`, `--trust-anchors` / `READYAGENTS_TRUST_ANCHORS`). Verification
uses the optional `jwt` extra (not in `all`): signature, issuer, audience, expiry,
skew. `alg: none`, algorithm confusion, and unknown `kid` are refused. Fail closed
on a missing/malformed/unreadable anchor when a token is presented. Claims map to
`--actor` / RBAC roles; replay of the same token on a gate is refused. Signed
(HMAC of the decision body) and identified (verified subject) stay separate;
`--actor NAME` remains the default.

Optional `readyagents.credentials.yaml` grants named secrets per tool at the
dispatch seam; non-granted tools cannot read them from `os.environ` during the
call. `credential_kind` is `static` when the provider cannot mint.

Also: `readyagents identity verify|whoami|trust` (workload `whoami` prints a
fingerprint, never the private key), plus TokenOps spend-meter/ledger fixes for
parallel reserves and unpriced `cost_micros`.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.5.0
readyagents identity trust
readyagents identity whoami
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.


# ReadyAgents Core 1.4.0

**TokenOps: estimate, spend caps, ledger, and runaway guards.**

Versioned, overridable model price table (`READYAGENTS_PRICES`); unknown models
are explicitly unpriced, never a silent zero. `readyagents run PATH --estimate`
walks the engine's routing with no execute and no network and prints a range
with assumptions. `--max-spend` / `--max-tokens` are consulted before each model
call; parallel branches share one meter; a resumed run continues the same
budget. `--label KEY=VALUE` is stored on the run and in an append-only
hash-chained spend ledger (`readyagents spend`). Cache hits/misses/savings
appear on the run record, `runs report`, and the ledger. Runaway guards
(`--max-model-calls`, `--max-run-tool-rounds`, `--max-wall-seconds`, workflow
`runaway:`) raise `RunawayGuard`, distinct from `BudgetExceeded` and
`CircuitOpen`. Optional `tokenizer` extra is not in `all`. The provider invoice
is authoritative. Without the new flags, behaviour is unchanged.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.4.0
readyagents run examples/calc_pipeline.yaml --estimate
readyagents spend
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 1.3.0

**Traceability evidence: hash-chained audit, evidence packs, retention, observers.**

Audit JSONL is hash-chained (`seq`, `prev_hash`, `entry_hash`) — tamper-evident,
not tamper-proof. `readyagents audit verify` reports unchained ranges and the
first break. `readyagents evidence RUN_ID` writes a hash-manifested local pack
(machine JSON, self-contained HTML, decisions projection, audit slice, workflow
source, Mermaid graph); the pack may contain prompts and outputs.
`readyagents graph PATH` is deterministic and injection-safe. Configurable
retention (`READYAGENTS_RETENTION_DAYS`, default 180) makes `runs gc` refuse
in-window records unless `--override-retention` (audited). Pack observer seam
(`register_observers`) is backward-compatible. Optional content-free `otel`
extra is not in `all`, starts no collector on import, and is off unless
`READYAGENTS_OTEL=1`. Docs claim evidence, never compliance or certification.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.3.0
readyagents run examples/calc_pipeline.yaml
readyagents audit verify
readyagents evidence RUN_ID
readyagents graph examples/graph_complex.yaml
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 1.2.1

**Firewall skeptic fixes for taint, policy, and MCP pins.**

Agent tool-calls inherit taint from the calling prompt/system so `on_tainted`
applies when the model emits literal arguments. Foreach copies parent
provenance and marks `item`/`index` untrusted when the items source is
untrusted. The resolved policy path and MCP pins persist across resume/decide
and later runs, so omitting `--policy` on resume cannot fail-open a gate.
`nodes.<id>.require_approval`: only approve proceeds; reject is a policy deny.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.2.1
readyagents policy check examples/readyagents.policy.yaml
readyagents run examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 1.2.0

**Optional agent firewall at the tool-dispatch seam.**

An optional `readyagents.policy.yaml` (or `--policy` / `READYAGENTS_POLICY`) is
evaluated at the single tool-dispatch seam. Actions are allow, gate, or deny;
gate reuses the existing signed approval pause; malformed or missing referenced
policy fails closed. Without a policy file, behaviour is unchanged. Additive
provenance, injection heuristics that never rewrite content, MCP tool description
pinning, egress host allowlists, and a pre-send secret scan round it out, plus
`readyagents policy check` and `readyagents policy explain`.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.2.0
readyagents policy check examples/readyagents.policy.yaml
readyagents run examples/policy_gated.yaml --policy examples/readyagents.policy.yaml
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 1.1.0

**Freeze/eval CI contract and pack tool seals.**

`readyagents runs freeze` now writes determinism, node, tool, and usage ceilings into
`case.yaml`, and `readyagents eval` scores them so a fixture fails on classification drift,
dropped tool rounds, reordered nodes, or token ballooning — still keyless, still without an
LLM-as-judge. Packs may declare tool cassette classification via `register_tool_seals()` or
`FunctionTool.determinism` (`recomputed` / `sealable` / `unsealable`); unclassified pack and
MCP tools stay unsealable, and offline eval classifies them correctly.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==1.1.0
readyagents runs freeze RUN_ID --out fixtures/my-case
readyagents eval fixtures/my-case
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 1.0.0

**Every run is reproducible, forkable, and promotable to a test.**

Persistence after every node was already there. 1.0 turns it into a time machine. Record a run
once and you can replay it exactly — offline, with no key set and nothing spent — fork it from any
node, diff two runs to see where they parted, and freeze the whole thing into a regression test
that `readyagents eval` runs in CI for free. Where a run cannot be made reproducible, ReadyAgents
says so per node rather than pretending. Pure JSON builtins recompute on offline replay so the keyless freeze example does not need `--allow-unsealed`. The workflow format, the run record, the CLI contract,
and the public Python API now come with a written stability promise and a deprecation policy.
Still local, still one-shot, still bring your own keys.

Recording is opt-in (`--record` / `READYAGENTS_RECORD=1`) because a cassette holds full prompts
and completions.

Cross-platform sandbox parity shipped in 0.12.0 and is in this cut: Linux/macOS/Windows, Python
3.11–3.14, `readyagents doctor`, and a pip-only wheel-install job.

```bash
pip install readyagentsdev==1.0.0
readyagents run examples/calc_pipeline.yaml --record
readyagents runs replay RUN_ID --offline --json
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 0.12.0

**Cross-platform CI, `readyagents doctor`, and a portable smoke path.**

Linux was never the only target, but the install and first-run story is now honest on Windows and macOS too: CI runs the matrix across Python 3.11–3.14 on three OSes, a wheel-install job exercises the pip-only first-run flow, and `readyagents doctor` reports platform, Python, extras, workspace writability, permission enforceability, filesystem case sensitivity, loopback availability, and the resolved run-store backend. `python scripts/smoke.py` (and `make smoke`) runs the keyless example set without a POSIX shell. Sandbox path containment and permission claims are accurate per platform, including Windows junctions.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==0.12.0
readyagents doctor
python -m readyagents  # or: readyagents new my-flow
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 0.11.0

**Workflow JSON Schema, located validation errors, editor wiring.**

Writing a ReadyAgents workflow no longer means writing YAML blind. `readyagents schema` produces a JSON
Schema generated from the same models the engine validates against, new scaffolds point your editor at
a local copy (the hosted `$id` URL is an identifier and 404s today), and you get completion and enum
hints for every node type as you type. When validation does fail, it now tells you the line and column
and shows you the offending line with a caret under it — inside nested branches and included files too.
No new runtime dependency, no network access, and nothing about the workflow format changed.

Packs are waitlisted, not for sale.

## Try it

```bash
pip install readyagentsdev==0.11.0
readyagents schema --output workflow.schema.json
readyagents new my-flow
```

Or clone https://github.com/readyagentsdev/readyagents-core and `pip install -e .`.

# ReadyAgents Core 0.10.1

**Security follow-up to 0.10.0. MCP header checks, probe bearer, actor isolation.**

v0.10.0 tagged the MCP 2026-07-28 tasks/MRTR surface. v0.10.1 is the build that includes the
post-tag security fixes: `Mcp-Method`/`Mcp-Name` are checked on every 2026 Streamable HTTP POST
before SDK dispatch; `tasks/update` does not treat `_meta` `clientInfo.name` as an RBAC actor;
`readyagents mcp probe` sends `READYAGENTS_MCP_TOKEN` when set. The `v0.10.0` tag remains on the
pre-fix commit and should not be used as the current source of truth.

```bash
pip install readyagentsdev==0.10.1
```

# ReadyAgents Core 0.10.0

**MCP 2026-07-28 tasks and MRTR approvals. `/runs` deprecated. No hosted recovery.**

ReadyAgents v0.10 brings the MCP surface up to the `2026-07-28` revision. The server answers
`server/discover`, handles requests statelessly, and implements the official tasks extension on top of
the run records it already persists — so the private `/runs` door becomes a standard one. The change
that matters most is human approval: a paused approval gate is now an `input_required` task that any
2026 MCP client can answer with `tasks/update`, and every such decision goes through the same signing,
RBAC, and append-only audit as a decision made at the CLI. Older clients and the `mcp` 1.x SDK keep
working, `/runs` keeps working with a deprecation notice (removal no earlier than v0.12), and core still
starts nothing on its own. Process death still loses the in-flight executor.

This release also includes the localhost approval UI and optional SQLite run-store that sat under
Unreleased after 0.9.0.

```bash
pip install readyagentsdev==0.10.0
readyagents mcp serve --json
readyagents mcp probe http://127.0.0.1:8765/mcp
```

See [docs/mcp.md](docs/mcp.md).

ReadyAgents keeps readable per-run JSON as its default and adds an opt-in local SQLite
backend for larger run histories and concurrent clients. The backend stores the same logical run
record, adds indexed queries and revision conflicts, and requires no third-party database package.
A dry-runnable `readyagents runs migrate --from json --to sqlite` copies and verifies JSON records
without deleting the originals. SQLite is not hosted recovery; network filesystems are unsupported.

Operators can explicitly start a local ReadyAgents approval page, review paused prompts, and
approve or reject them without copying CLI commands. The page binds only to loopback, exposes a
minimal redacted view, and protects bootstrap and decision actions with expiring one-use tokens.
It is not hosted, does not start automatically, and adds no frontend toolchain.

```bash
readyagents run examples/browser_approval.yaml    # exit 2
readyagents approvals serve --host 127.0.0.1 --port 8766
# open the stderr bootstrap URL once, then Approve or Reject
```

See [docs/run-stores.md](docs/run-stores.md) for backend selection, WAL backup files, and migration.

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
