# Compliance evidence (EU AI Act Articles 12, 13, 14)

This document is **evidence** of what ReadyAgents Core records and exports. It is **not** legal compliance, **not** certification, and **not** legal advice. ReadyAgents does not make you compliant with the AI Act, does not guarantee compliance, and is not certified under it. Whether a particular deployment is a high-risk system, and whether these artifacts are sufficient, is an operator and counsel decision.

ReadyAgents is a local workflow engine. The operator authors the graph, chooses models, holds API keys, and decides who may approve a pause. The engine does not classify the composed system and does not attest that a human *meaningfully* oversaw it.

## What the engine produces

| Artifact | Where | Notes |
| --- | --- | --- |
| Run record | `$READYAGENTS_HOME/runs/<id>.json` (or the SQLite store) | Inputs, per-node outputs, usage, timestamps, status, pending HITL. May contain prompts and outputs. Not encrypted at rest. |
| Audit JSONL | `$READYAGENTS_HOME/audit/<id>.jsonl` | Append-only events (`run_started`, `node_ok`, `paused`, `decision`, `run_finished`, …). Hash-chained (`seq`, `prev_hash`, `entry_hash`). Tamper-*evident*, **not** tamper-proof. |
| Decision projection | `readyagents evidence` → `decisions.json` | One row per executed node: type, attempts, usage, optional human review. A projection over the run and audit, not a second store. |
| Evidence pack | `readyagents evidence RUN_ID --out DIR` | `run.json`, `decisions.json`, `audit.jsonl`, `workflow.yaml`, `graph.mmd`, `evidence.html`, `manifest.json`, `README.md`. Sensitive. |
| Routing graph | `readyagents graph PATH` | Deterministic Mermaid of declared edges. Executes nothing. |
| Chain verify | `readyagents audit verify` | Walks the hash chain. Unchained pre-chain lines are reported, not failed. A break exits 1. |
| HITL | approval nodes, `decide` / `resume`, localhost UI, MCP `tasks/update` | A pause mechanism. A rubber-stamp approve is still an approve. |
| Optional telemetry | `READYAGENTS_OTEL=1` and `OtelPack` | Content-free spans (usage / model / node / status / run id / cost). No prompts. See [observability.md](observability.md). |
| Supply-chain provenance | run `metadata.supply_chain`, `run_started` audit | Artifact digests and signature status. Signing proves origin, not safety. See [supply-chain.md](supply-chain.md). |

`readyagents runs freeze` is a **regression fixture**, not an evidence pack. Cassettes hold full prompts and completions when `--record` is on.

## Article 12 — record-keeping

Article 12 of Regulation (EU) 2024/1689 is about automatic logging, traceability of use, and keeping logs for a period appropriate to the intended purpose (with a six-month floor for high-risk systems unless other law says otherwise). ReadyAgents can write logs. It does not keep them for you, and it does not prove they were complete.

**What ReadyAgents produces**

- Automatic local events for run start/finish, node ok, pause, cancel, and recorded approval decisions.
- Timestamps, run id, node id, actor (when set), status, and per-node token/cost usage when the model reports it.
- A hash chain so a *partial* rewrite of a file is detectable with `audit verify`. Rotation writes a `chain_anchor` carrying the previous file's final hash.
- An evidence pack that snapshots the run, the audit slice, and a SHA-256 manifest including `chain_anchor`.

**What remains the operator's responsibility**

- Whether the composed system is in scope of Article 12 at all.
- Retention. `READYAGENTS_RETENTION_DAYS` (default 180) only skips young **run records** during `runs gc`. It is a local hygiene window, not a legal archive. `--override-retention` deletes inside that window (the override is audited). `gc` does **not** delete `$READYAGENTS_HOME/audit/*.jsonl`. Deleting the home directory deletes everything.
- Off-box copies. A local attacker who can rewrite the whole audit file can rewrite the chain. Copy the chain anchor (and the pack, if you need it) off the machine that wrote it.
- Completeness for *your* purpose: which inputs were in scope, which tools ran, which model version the vendor actually served. Core stores the model *name you configured*, not a vendor-signed model card.
- Access control, backup, and encryption. ReadyAgents does **not** provide encryption at rest.

## Article 13 — transparency and information to deployers

Article 13 is about making operation transparent enough that a deployer can interpret output and use it appropriately, plus instructions covering capabilities, limits, input data, oversight measures, and log maintenance.

**What ReadyAgents produces**

- The workflow file as the declared graph (nodes, edges, approval prompts, tool names).
- `readyagents graph` / `graph.mmd`: routing only, labels sanitized, no live data.
- Per-node usage, attempts, status, and configured model on the run record and evidence HTML.
- `readyagents policy explain` when a firewall policy is in force.
- This documentation set (`docs/`, `SECURITY.md`, CLI help).
- A self-contained `evidence.html` (no network fetches) that previews outputs and human decisions.

**What remains the operator's responsibility**

- Instructions for use of *your* system: intended purpose, user-facing limitations, residual risk, and how a person should read the model's output.
- Identity and contact of the provider of that system (not ReadyAgents unless you are shipping Core itself).
- Input-data specifications and data-governance measures beyond what the YAML happens to name.
- Translating engine artifacts into language a deployer can act on. A Mermaid diagram is not an instructions-for-use document.
- Telling people that recorded prompts and outputs in an evidence pack are sensitive.

## Article 14 — human oversight

Article 14 is about designing the system so natural persons can oversee it effectively: understand limits, resist automation bias, interpret output, decide not to use it, override, and interrupt.

**What ReadyAgents produces**

- `approval` nodes that pause the run (CLI exit 2) until `approve` or `reject`.
- Policy `gate` actions that reuse that same pause.
- `readyagents decide` / `resume`, the localhost approval UI, and MCP `tasks/update` as injection paths. When `READYAGENTS_DECISION_SECRET` is set, unsigned MCP approvals are refused and audited.
- Actor id on decisions when `--actor` / `READYAGENTS_ACTOR` is set.
- Cooperative cancel (`cancelled` / `cancel_requested`). Cancel does not kill a blocking tool or provider call already in flight.
- Evidence rows that show whether a human decision was recorded (`approve` → outcome `confirm`, `reject` → `override`) and whether a signature was present.

**What remains the operator's responsibility**

- Meaningful oversight. The engine will accept an immediate approve. That is not proof that a person understood the output, considered automation bias, or had time to override.
- Who is allowed to decide, how they are trained, and how they are shown the material (the localhost UI is a redacted view; the evidence pack may still hold full outputs).
- Choosing to put an approval node in the graph at all. A workflow with no gate has no human checkpoint.
- Interrupting a stuck provider call (process-level), and deciding that a reject should stop downstream effects that already happened.
- Article 14 is operational. An evidence pack can show *that a decision was recorded*. It cannot show that oversight was effective.

## What this mapping does not claim

- ReadyAgents is **not** certified, offers **no** certification, and is **not** compliant with the AI Act by virtue of being installed.
- Hash chaining is **not** tamper-proof. It is tamper-evident at best.
- There is **no** encryption at rest and **no** hosted evidence locker.
- Optional OpenTelemetry is **not** an audit trail. Spans are content-free metadata and are off unless explicitly enabled.
- `runs gc` can destroy run records. It is the opposite of a retention guarantee.

For the observer seam and the optional OTel pack, see [observability.md](observability.md). For hash-chain limits, see [SECURITY.md](../SECURITY.md).
