# A2A interoperability

**Remote content is untrusted. Delegation can exfiltrate.** This page describes
a served mapping over the durable run record, not A2A certification and not a
hosted agent directory.

A2A (Agent2Agent) is a projection of an existing ReadyAgents run: the same
record the CLI, MCP tasks, and `/runs` already use. There is no second state
machine, no always-on process, and no Node.js toolchain. `a2a serve` is an
explicit foreground command. Stopping it stops the listener.

## What this is not

- Not A2A v1.0 certification or a conformance claim beyond the in-repo suite.
- Not a public bind by default. Loopback only unless you pass
  `--allow-public-bind` (you then own the exposure).
- Not a registry, marketplace, crawler, or hosted directory.
- Not streaming. The card advertises `streaming: false`. Callers poll.
- Not an OAuth authorization server. Bearer token from the environment, as
  with MCP HTTP.

## Security (read this first)

A remote agent is an attacker. Its Agent Card, task messages, artifacts, and
`input-required` questions are attacker-controllable.

- Everything returned from a remote agent is marked **untrusted** (`source=a2a`),
  size-bounded, redacted for display, and stripped of control characters.
- A remote question shown to a human is a **phishing vector**. The local
  approval prompt attributes it to the remote agent and says it is not an
  instruction.
- What you put in `message:` **leaves the building**. Policy can deny `a2a`
  (`default: deny` with no `tools.a2a` rule, or `tools.a2a.allow_hosts`),
  restrict destination hosts, and require `nodes.<id>.require_approval`
  before a first delegation.
- Card fetch and task URLs reuse `http_get` public-IP SSRF pinning. Loopback,
  RFC1918, link-local, and metadata addresses are refused. Authorization
  never follows a cross-host redirect.
- When a policy file exists, the card digest is pinned under
  `$READYAGENTS_HOME/a2a-pins/`. Drift is `on_description_change` (default
  **gate**).
- Serving requires a bearer token (`READYAGENTS_A2A_TOKEN`). An A2A answer to
  a **local** gate travels the existing signed-decision / RBAC / audit path.
  Unsigned or unauthorized answers are refused, audited, and leave the run
  paused.
- A signed card (when present) proves identity, not good behaviour.

Do not reverse-proxy `a2a serve` onto the public internet.

## Serve one workflow

```bash
readyagents a2a card examples/approval_gate.yaml --json
readyagents a2a serve examples/approval_gate.yaml --port 8770
```

Loopback bind (`127.0.0.1`) is the default. The process prints a generated
bearer token once to stderr if `READYAGENTS_A2A_TOKEN` is empty. `--json`
prints the card and bind, then serves.

Well-known paths (both served, same document):

- `/.well-known/agent-card.json` (A2A v1.0)
- `/.well-known/agent.json` (TASK alias)

JSON-RPC (polling, not streaming) on `POST /` and `POST /a2a`:

| Method | Meaning |
| --- | --- |
| `message/send` | Start a run, or answer an `input-required` task |
| `tasks/get` | Project the run record |
| `tasks/cancel` | Cooperative cancel |

Task id **is** the run id (32-char hex). Unknown and unauthorized ids share
one `Task not found` body after a valid bearer. Missing bearer is `401` for
every path.

### Task states

| Run record | A2A wire |
| --- | --- |
| `queued` | `submitted` |
| `running` / `cancel_requested` | `working` |
| `paused` (approval) | `input-required` (objective name: `input_required`) |
| `succeeded` | `completed` |
| `failed` | `failed` |
| `cancelled` | `canceled` |

Terminal states do not leave. Invalid transitions are a typed error.

Human input on a served workflow is the existing approval pause. The client's
reply becomes `approve` / `reject` and is checked by HMAC
(`READYAGENTS_DECISION_SECRET`), RBAC, and audit before the run resumes.

## Call a remote agent as a node

```yaml
- id: delegate
  type: a2a
  agent_url: "https://partner.example.com"
  message: "Summarise {{ document }}"
  timeout_seconds: 120
  on_input_required: gate      # gate (default) | fail
  output_key: summary
```

The node fetches and validates the card, submits a task, polls, times out, and
maps artifacts into run state (untrusted). Remote `input-required` becomes a
**local** approval gate so a human answers inside this workflow.

`examples/a2a_delegate.yaml` validates and `--dry-run`s with no network.

## Probe

```bash
readyagents a2a probe http://127.0.0.1:8770 --json
```

Read-only. Prints capabilities, auth *scheme types*, digest, and
`approvalCapable`. Never prints secret values.

## Policy

```yaml
version: 1
default: deny
egress:
  allow_hosts: ["partner.example.com"]
tools:
  a2a:
    allow_hosts: ["partner.example.com"]
    on_description_change: deny
nodes:
  delegate:
    require_approval: true
```

`default: deny` without a `tools.a2a` rule refuses delegation. Card-digest
pins are inert without a policy file.

See [policy.md](policy.md), [security-model.md](security-model.md), and
[SECURITY.md](../SECURITY.md).
