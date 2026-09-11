# Workflow packaging and distribution

A ReadyAgents **package** is a signed, review-before-install archive of a
workflow plus its policy, fixtures, and declared dependencies. This is not a
hosted marketplace, not an account system, and not automatic updates. Teams
that want a catalog host a **signed static JSON index** on any file server.

Python `--pack` files stay a separate surface (`kind: pack`). A workflow
package uses `kind: package`. Mixing the two kinds is refused.

A repository that never packages anything is unchanged.

## Manifest

`readyagents.pkg.yaml` in the package root:

```yaml
name: support-triage
version: 1.2.0
description: Triage inbound support with a budget and an approval gate.
licence: Apache-2.0
entry: workflows/triage.yaml
requires:
  readyagents: ">=1.9"
  packs: []
  connectors: [rest]
  skills: []
  mcp: []
secrets: [SUPPORT_API_TOKEN]   # names only, never values
inputs: [ticket]
policy: policy/triage.policy.yaml
fixtures: evals/suite.yaml
docs: README.md
budget: {max_cost_usd: 0.75}
```

Unknown keys, bad semver, a missing entry, absolute paths, and secret
**values** are refused.

## Build

```bash
readyagents package build ./support-triage --out support-triage-1.2.0.rapkg
readyagents sign support-triage-1.2.0.rapkg --key publisher.pem
```

The archive is a deterministic zip (sorted names, stored compression, fixed
timestamps) containing the manifest, entry workflow, declared files, policy,
fixtures, docs, and a member digest lockfile. Two builds of the same tree are
byte-identical. Signing binds the **archive bytes** as `kind: package`.

## Install is a review step

```bash
readyagents package install support-triage-1.2.0.rapkg --json
readyagents package install support-triage-1.2.0.rapkg --confirm
```

Without `--confirm` the command prints a capability review (tools, egress
hosts, secret **names**, budget, approval gates, signature status, and any
policy-narrowing diff) and writes nothing. Confirmation is the only write
path. Nothing is imported or executed during install. Extraction refuses
zip-slip, symlinks, absolute paths, and size/count/depth caps.

Local policy always wins: a package that declares more than the local policy
allows is installed but **constrained**, and the extra tools/hosts are named
in the review.

## Catalog, overlay, upgrade

```bash
readyagents package list
readyagents package show support-triage
readyagents package upgrade support-triage ./support-triage-1.3.0.rapkg --confirm
readyagents package remove support-triage
```

Installed trees live under `$READYAGENTS_HOME/packages/<name>/<version>/`.
A sibling `overlay.yaml` holds local model, budget, policy tightening, and
input defaults. Upgrade replaces the version tree and keeps the overlay. An
upgrade that widens tools, hosts, secrets, or budget shows a diff and still
requires `--confirm`.

Installed fixtures run under shipped `readyagents eval` with no network and
no keys — before real inputs.

## Signed static index

```json
{
  "version": 1,
  "packages": [
    {
      "name": "support-triage",
      "version": "1.2.0",
      "url": "https://example.invalid/support-triage-1.2.0.rapkg",
      "digest": "sha256:..."
    }
  ]
}
```

```bash
readyagents sign readyagents.index.json --key publisher.pem
readyagents package index readyagents.index.json --json
```

The index is `kind: package_index`. Unsigned or untrusted indexes are
refused. ReadyAgents does not host an index.

Conflicts (core version below a declared minimum, name mismatch on upgrade)
are reported. There is no transitive dependency solver.
