# Supply-chain trust

A signature proves **who published** a workflow or pack, not that the content
is safe. Policy ([policy.md](policy.md)) is still the control for behaviour.
This is provenance data, not a SLSA level, not a certificate, and not a
hosted transparency log.

Without `--require-signed`, without `require_signed: true` in a policy file,
and without `--frozen`, existing workflows, `--pack` loads, and MCP runs are
unchanged. There is **no default-trusted key**. Core installs and runs without
the optional `sign` extra.

## What is hashed

Algorithm **v1** is SHA-256, stored as `sha256:<hex>` and recorded as
`digest_version: 1` in the lockfile. Same inputs produce the same digest on
every platform.

| Artifact | Canonical form |
| --- | --- |
| Workflow | Parsed YAML/JSON plus every resolved `include`, in sorted path order, with each include's digest embedded. Changing an include changes the parent. Includes stay confined to the parent workflow directory. |
| Pack | File bytes. |
| MCP server | Advertised tool names, descriptions, and schemas (same canonical JSON as the firewall pin). A description-only change is a digest change. |

## Detached signatures

```bash
openssl genpkey -algorithm Ed25519 -out publisher.pem
openssl pkey -in publisher.pem -pubout -out publisher.pub.pem
readyagents trust add publisher.pub.pem --name ops
readyagents sign flow.yaml --key publisher.pem
readyagents verify flow.yaml --json
```

The signature sits beside the artifact (`flow.yaml.sig`). It binds the digest
**and** the kind: a workflow signature cannot validate a pack. Ed25519 uses
the optional extra `readyagentsdev[sign]` (cryptography; **not** in `all`).
Unsigned default runs never import it.

Private keys are read from a path you pass to `sign`. ReadyAgents does not
generate keys into the repository, does not store them on a run record, and
does not log them. Distribute public keys out of band; there is no registry.

## Keyring

Trusted publishers live in `$READYAGENTS_HOME/keyring.json`
(`{key_id, name, public_key, added_at}`). Writes are atomic with restrictive
permissions. A malformed or unreadable keyring is a hard failure, not an
empty keyring. Missing is empty (nobody is trusted).

```bash
readyagents trust add PUBKEY --name NAME
readyagents trust list
readyagents trust remove KEY_ID
```

`readyagents identity trust` is a different file (OIDC issuers). This keyring
is for artifact publishers.

## Enforcement

```bash
readyagents run flow.yaml --require-signed
readyagents run flow.yaml --require-signed --frozen
```

Policy may set the requirement once for the machine:

```yaml
version: 1
require_signed: true
frozen: false
on_lock_mismatch: allow   # allow | gate | deny
```

Order is **resolve → digest → verify → execute**. A pack is verified
**before** it is imported; importing is executing. The bytes that were
digested are the bytes that execute (the file is not re-read after the
digest). Unsigned, untrusted-key, tampered, malformed, missing-lockfile-when
frozen, and unreadable-signature cases fail closed with `TrustError` naming
the artifact.

`--frozen` refuses a digest mismatch against `readyagents.lock`. Without
`--frozen` a mismatch is reported on the run record; `on_lock_mismatch: gate`
pauses, `deny` refuses.

## Lockfile

```bash
readyagents lock flow.yaml
readyagents lock flow.yaml --pack packs/connector.py --out readyagents.lock
```

```yaml
version: 1
digest_algorithm: sha256
digest_version: 1
generated_at: "2026-09-10T10:00:00Z"
artifacts:
  - kind: workflow
    path: flow.yaml
    digest: "sha256:..."
  - kind: include
    path: steps/triage.yaml
    digest: "sha256:..."
  - kind: pack
    path: packs/connector.py
    digest: "sha256:..."
  - kind: mcp_server
    name: files
    surface_digest: "sha256:..."
```

Detection happens at run time. There is no always-on watcher.

## SBOM

```bash
readyagents sbom flow.yaml --json
readyagents sbom flow.yaml --out sbom.json
```

CycloneDX-shaped JSON: the workflow and its includes, packs, MCP servers
(command and args, **never** env values), declared tools, model identifiers,
and the ReadyAgents version. Deterministic, no network. Review before you
share it; it lists what the workflow will execute.

## Provenance

Each run record's `metadata.supply_chain` holds digests and signature status
(`unsigned`, `present`, `verified`). The same object is copied onto the
`run_started` audit event. Signing still does not mean the workflow is safe.
