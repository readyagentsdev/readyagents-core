# Extras

Everything here ships in the same install and works today. None of it is required to use
ReadyAgents, and nothing in the core path depends on it. Each entry carries a maturity tier
(see [stability.md](stability.md)) and its own scope note.

Tiers are the H-03 starting assignment: `stable` is the pre-sprint core surface, `preview`
has an end-to-end test and a doc page, and `experimental` is everything else. Tiers are
labels, not switches — experimental code runs exactly as it runs today.

## Governance and audit

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Agent firewall](policy.md) | `policy`, `--policy` | preview | Taint, tool policy, MCP pinning — defence in depth, not a solution to prompt injection. |
| [Output contracts](guardrails.md) | `contract:` | experimental | Declared schema and content rules on value-producing nodes; declared rules, not a safety classifier, not a safety claim. |
| [External decisions](approvals.md) | `decide` | stable | External approval injection (`readyagents decide`) and outbound pause notify. |
| [Evidence packs](compliance.md) | `evidence` | preview | Local evidence pack of a run — evidence, not legal compliance or certification; not a compliance certificate. |
| [Audit trail](compliance.md) | `audit` | preview | Append-only, hash-chained audit trail; `audit verify` walks the chain. |
| [Secrets and RBAC hooks](compliance.md) | `policy` hooks | preview | Secrets / RBAC / PII-redaction hooks. |
| [Identity](identity.md) | `identity` | preview | Approver assertions against local trust anchors; workload fingerprint, never the private key. |
| [Attestation](compliance.md) | `attest` | experimental | Data-residency attestation — technical evidence, not legal compliance. |
| [Supply-chain trust](supply-chain.md) | `sign`, `verify`, `lock`, `sbom`, `trust` | experimental | Detached signatures, lockfiles and SBOM — signatures prove origin, not safety. |
| [Approval web UI](approvals.md) | `approvals serve` | stable | Foreground localhost approval page; not a hosted dashboard. |

## Connectivity

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [A2A](a2a.md) | `a2a serve`, `a2a card`, `a2a probe`, `type: a2a` | preview | 0.3 JSON-RPC projection; remote content untrusted; delegation can exfiltrate; not certification. |
| [Connectors](connectors.md) | `connectors` | preview | Small governed catalog (`rest`, `sql`, `object_storage`, `message`, `ingest`) — not 500 SaaS. |

## Data and knowledge

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Memory](memory.md) | `memory`, `type: memory` | preview | Local JSON/SQLite, BM25, TTL/forget — untrusted; delayed injection and scope escape first; not a quality claim. |
| [Knowledge pipelines](knowledge.md) | `knowledge`, `type: ingest` | experimental | Citations and freshness; not a retrieval-quality claim; plain memory writes unchanged. |
| [Data pipelines](data-pipelines.md) | `table`, `type: table`, `type: classify` | experimental | Deterministic ops and remainder-only classify; not a warehouse; `json_get`/`foreach` defaults unchanged. |
| [Multimodal I/O](multimodal.md) | `type: document`, `type: transcribe` | experimental | Plumbing and governance for image/pdf/audio extras; not extraction accuracy, not an OCR claim; text-only runs unchanged. |

## Agent capability

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Multi-agent teams](teams.md) | `type: team` | experimental | Closed members, engine-enforced stop; no routing-quality claim, no quality claim. |
| [Agent skills](agent-skills.md) | `skills`, `agents-md`, `type: skill` | experimental | Open SKILL.md format; untrusted instructions; sandbox scripts; not a marketplace. |
| [Sandboxed code](code-sandbox.md) | `type: code` | experimental | Subprocess default — accident-grade isolation, not hostile-code-proof; no bundled container runtime. |
| [Governed browser use](browser-use.md) | `type: browser` | experimental | Declared actions, allowlist, taint, offline replay; driver in an optional pack; no CAPTCHA solving; not a free-running browser agent. |
| [Conversational sessions](conversational-sessions.md) | `sessions`, `serve chat`, `type: converse` | experimental | Turns are durable runs; loopback chat; no audio in core; not a hosted chat product. |

## Operations

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Environments](environments.md) | `env status`, `env history`, `env diff`, `run --env` | preview | Pinned signed releases, gated promote, canary/shadow, lazy rollback; not a hosted deploy. |
| [Promote / rollback](environments.md) | `promote`, `rollback` | experimental | Gated promote copies a source pin onto a target; rollback restores the previous release atomically, never auto-forwards; not a hosted deploy. |
| [Self-healing](self-healing.md) | `health`, `recovery:` | experimental | Fingerprints and fail-safe gates; not prediction; not a hosted reliability service, not a hosted service. |
| [Long-horizon waits](long-horizon.md) | `wake`, `event`, `type: wait` | experimental | Lazy wake, no daemon; `waiting` ≠ `paused`; not a scheduler. |
| [Event triggers](event-triggers.md) | `triggers`, `triggers:` | experimental | Core contract only, no listener in core; at-least-once plus idempotency, not exactly-once; loopback-default webhook. |
| [Agent registry](registry.md) | `registry` | experimental | Inventory from declared roots; derived facts plus declared roles/tier; draft Annex VIII export, not a legal filing (a draft, not a filing); not a hosted registry. |
| [Batch / scale](scale.md) | `batch` | experimental | Foreground: one workflow, many JSONL/CSV rows; not a distributed worker; benchmarks are not a marketing claim. |

## Authoring

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Workflow studio](studio.md) | `studio` | experimental | Loopback canvas and run inspector; YAML on disk stays the source of truth; not a hosted product. |
| [Migration importers](migration.md) | `import` | preview | n8n / LangGraph / CrewAI / trigger-action; structural translation plus a fidelity report; never exec source Python; not behavioural equivalence. |
| [Simulation](simulation.md) | `simulate` | experimental | Declaration-driven cases, honest coverage, dry-run default; not exhaustive, not a hosted simulator. |
| [Testing helpers](cli.md) | `readyagents.testing` | experimental | Recorded LLM mocks and a tiny eval harness. |

## Model and prompt

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Model routing](model-routing.md) | `models`, `routing:` | experimental | Declared policy across providers (Gemini/Bedrock/Vertex extras); not quality inference, not a quality claim; no-policy selection unchanged. |
| [Prompt optimization](prompt-optimization.md) | `optimize`, `prompts` | experimental | Versioned prompts, offline reflective loop, gated promotion; not GEPA/DSPy parity; not a hosted optimizer; YAML that never optimizes is unchanged. |
| [Distillation](distillation.md) | `distill` | experimental | Consented hashed splits, pack-owned training, holdout-gated one-node adapters; not a quality claim; core never trains. |
| [Benchmark harness](benchmarks.md) | `bench` | experimental | Offline cassettes, labelled engine vs live timing; not a model-quality or competitor ranking, not a quality ranking. |
| [Feedback export](feedback.md) | `feedback` | experimental | Consent-gated corrections as eval/sft/dpo; not fine-tuning; not a hosted dataset, not a hosted service. |
| [Agent output schemas](workflows.md) | `output_schema` | experimental | Pydantic `output_schema` on agent nodes; opt-in local LLM cache. |

## Packaging and distribution

| Extra | Command / node | Tier | Scope |
| --- | --- | --- | --- |
| [Workflow packaging](packaging.md) | `package` | experimental | Review-before-install archives with policy, fixtures, and signature; signed static index; not a hosted marketplace, not a marketplace. |
| [Entry-point packs](cli.md) | `readyagents.packs` | experimental | Extra node types and tools via Python entry points. |
| [Offline bundle](cli.md) | `bundle` | experimental | Offline wheel set for `pip install --no-index --find-links`. |
| [Loopback serve surfaces](cli.md) | `serve` | experimental | Foreground loopback surfaces; stops when the process stops; not a hosted product. |
| [Continuous pack](continuous-pack.md) | `readyagents-pack-continuous` | experimental | Optional separate distribution, not a Core extra; runs configured workflows from an explicit foreground command; installing Core still starts no scheduler or listener. |

## Source bullets (verbatim)

The 26 bullets below moved verbatim from the README's `## What it does` section, disclaimers
intact (links relativized to this file; the MCP bullet stayed in the README as core). The
tables above are the scannable catalogue of the same content.

- Optional agent firewall: taint, tool policy, MCP pinning ([security model](security-model.md), [policy](policy.md)) — defence in depth, not a solution to prompt injection
- Optional [A2A](a2a.md) serve/probe and `type: a2a` delegation (0.3 JSON-RPC projection; remote content untrusted; not certification)
- Optional [memory](memory.md) (`type: memory`, local JSON/SQLite, BM25, TTL/forget) — untrusted; delayed injection and scope escape first; not a quality claim
- Optional [sandboxed code](code-sandbox.md) (`type: code`, subprocess default) — accident-grade isolation, not hostile-code-proof; no bundled container runtime
- Optional [output contracts](guardrails.md) (`contract:` on a value-producing node) — declared schema and content rules, not a safety classifier
- Optional [multi-agent teams](teams.md) (`type: team`) — closed members, engine-enforced stop, no routing-quality claim
- Optional [workflow studio](studio.md) (`readyagents studio`) — loopback canvas and run inspector; YAML on disk stays the source of truth; not a hosted product
- Optional [model routing](model-routing.md) (`routing:`, Gemini/Bedrock/Vertex extras, `readyagents models`) — declared policy, not quality inference; no-policy selection unchanged
- Optional [multimodal I/O](multimodal.md) (`MediaPart`, `type: document`, `type: transcribe`, image/pdf/audio extras) — plumbing and governance, not extraction accuracy; text-only runs unchanged
- Optional [knowledge pipelines](knowledge.md) (`type: ingest`, `readyagents knowledge`) — citations and freshness, not a retrieval-quality claim; plain memory writes unchanged
- Optional [data pipelines](data-pipelines.md) (`type: table`, `type: classify`, `readyagents table`) — deterministic ops and remainder-only classify; not a warehouse; `json_get`/`foreach` defaults unchanged
- Optional [long-horizon waits](long-horizon.md) (`type: wait`, `readyagents wake` / `event`) — lazy wake, no daemon; `waiting` ≠ `paused`; not a scheduler
- Optional [event triggers](event-triggers.md) (`triggers:`, `readyagents triggers`) — core contract only, no listener in core; at-least-once plus idempotency, not exactly-once; loopback-default webhook
- Optional [agent skills](agent-skills.md) (`type: skill`, `readyagents skills` / `agents-md`) — open SKILL.md format; untrusted instructions; sandbox scripts; not a marketplace
- Optional [workflow packaging](packaging.md) (`readyagents package`) — review-before-install archives with policy, fixtures, and signature; signed static index; not a hosted marketplace
- Optional [simulation](simulation.md) (`readyagents simulate`) — declaration-driven cases, honest coverage, dry-run default; not exhaustive, not a hosted simulator
- Optional [self-healing](self-healing.md) (`readyagents health`, `recovery:`) — fingerprints and fail-safe gates; not prediction; not a hosted reliability service
- Optional [benchmark harness](benchmarks.md) (`readyagents bench`) — offline cassettes, labelled engine vs live timing; not a model-quality or competitor ranking
- Optional [prompt optimization](prompt-optimization.md) (`readyagents optimize`, `readyagents prompts`) — versioned prompts, offline reflective loop, gated promotion; not GEPA/DSPy parity; YAML that never optimizes is unchanged
- Optional [feedback export](feedback.md) (`readyagents feedback export|stats`) — consent-gated corrections as eval/sft/dpo; not fine-tuning; not a hosted dataset
- Optional [governed browser use](browser-use.md) (`type: browser`) — declared actions, allowlist, taint, offline replay; driver in an optional pack; no CAPTCHA solving; not a free-running browser agent
- Optional [conversational sessions](conversational-sessions.md) (`type: converse`, `readyagents sessions`, `serve chat`) — turns are durable runs; loopback chat; no audio in core; not a hosted chat product
- Optional [environments and rollout](environments.md) (`readyagents.env.yaml`, `run --env`, `promote`, `rollback`, `env status|history|diff`) — pinned signed releases, gated promote, canary/shadow, lazy rollback; not a hosted deploy
- Optional [migration importers](migration.md) (`readyagents import`) — n8n / LangGraph / CrewAI / trigger-action; structural translation plus a fidelity report; never exec source Python; not behavioural equivalence
- Optional [agent registry](registry.md) (`readyagents registry`) — inventory from declared roots; derived facts plus declared roles/tier; draft Annex VIII export, not a legal filing; not a hosted registry
- Optional [distillation](distillation.md) (`readyagents distill`) — consented hashed splits, pack-owned training, holdout-gated one-node adapters; not a quality claim; core never trains
