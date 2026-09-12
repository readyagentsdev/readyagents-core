# Agent registry and fleet governance

**Unreleased on this checkout — not on the 1.9.0 tag.** `readyagents registry`
builds a **local inventory** from artifacts that already exist: workflows,
packages, pinned releases, and pack files under **declared roots**. It is not a
hosted registry, not an organisation directory, not SSO, and **not an Article 49
registration or a claim of EU AI Act compliance**.

Repos that never declare registry roots are unchanged. An agent without
registry metadata still runs exactly as it does today.

```bash
readyagents registry scan --json
readyagents registry annotate agt_… --owner payments_ops --tier high --purpose "…"
readyagents registry check --json
readyagents registry list --tier high --max-spend 1000 --min-health 0.9 --json
readyagents registry show agt_… --json
readyagents registry stats --json
readyagents registry card agt_… --json
readyagents registry export agt_… --format annex-viii --out annex-viii-draft.json --yes
```

## What is derived vs declared

Scan **recomputes** what the files and the run store already know: node types,
tools, connectors, models, egress hosts, approval gates, memory/knowledge use,
budget ceiling, run count, spend, failure rate, health score, first/last run,
signed-release and evidence-pack signals.

A human supplies what software cannot know, once, in
`$READYAGENTS_HOME/registry/agents/<id>.yaml`: owner and backup as **roles**
(not personal contact details), purpose, risk tier (`high` / `medium` / `low`),
data classes, retention, review cadence, decommission date.

`registry annotate` fills **missing declared fields only**. It refuses derived
keys such as egress hosts. Unknown tiers and data classes are refused. Derived
facts are never stored as editable truth; drift compares the current derivation
against a snapshot taken at last review.

## Discovery and identity

Config lives at `$READYAGENTS_HOME/registry/config.yaml`:

```yaml
version: 1
roots: [agents, packages]
enforce: false          # promotion gating; off until you switch it on
unused_after: 90d
data_classes: [customer_pii, payment_metadata, public, internal, secrets]
```

The registry **never scans a machine uninvited**. Roots are confined to the
workspace. A symlink that resolves outside a declared root is skipped.

Agent ids (`agt_` + 20 hex chars) are minted once and survive rename, directory
move, and version change. A **copied** workflow is a new agent. Pack files are
listed by digest; they are **never imported**. Scan does not execute a workflow.

## Check, unused, and enforce

`registry check` reports:

- missing declared metadata
- material drift since last review (egress hosts, tools, models, node types)
- overdue reviews
- unused **candidates** (no runs for `unused_after`, or past
  `decommission_after`)

It never deletes an agent.

Risk-tier **requirements** live only in registry config, never in workflow YAML.
A `risk_tier:` key in a workflow is ignored. Default high-tier requirements
(when the config omits `tiers`) are: approval gate, evidence pack, signed
release, 90-day review cadence.

`registry check --enforce` fails with a distinct typed reason
(`RegistryTierApproval` / `RegistryTierEvidence` / `RegistryTierSigned` /
`RegistryTierCadence`). `enforce: true` in config also gates `readyagents
promote`. Enforcement is off by default.

## Cards and Annex VIII drafts

`registry card` assembles a model-card-shaped document from evidence. Unknown
fields are named (`unknown: purpose`), never guessed. It is not a certification.

`registry export --format annex-viii` writes a **draft** record whose
**filename**, JSON `disclaimer` / `header`, and this page all say so. It is
assembled from local evidence, not a legal filing, not an Article 49
registration, and not a compliance claim. Every unknown Annex field is named.
The path is workspace-confined; `--yes` is required because the file is a
reconnaissance document (automation map, data flows, owners). Default views
redact egress hosts as `[redacted-endpoint]` and secretish tool names.
`--unredact` is an RBAC-checked action (`registry.unredact`).
`registry list` and `stats` filter by owner, tier, model, kind, spend
(`--min-spend` / `--max-spend` in micros), health (`--min-health` /
`--max-health`), and staleness. Missing health scores do not match a health
predicate. Promotion gating (`enforce: true`) binds the workflow path to the
inventory entry with that path; a byte-identical **copy** is a different agent
and uses the copy's declared tier. Digest matching is only a fallback when the
indexed path is gone (a rename/move that has not been re-scanned).

## Access

Registry commands use the existing RBAC hook (`Authorizer.check`). Contacts are
roles. Exports are confined, warned, and redacted. There is no hosted sync, no
telemetry, and no automatic model-chosen risk tier.
