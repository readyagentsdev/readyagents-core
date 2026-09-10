# Sovereign mode

**Technical enforcement and evidence — not legal compliance, certification, or
accreditation.** In-process socket wrapping is **not** an OS sandbox. A native
extension can bypass it. **DNS resolution can leak** even when connect is
denied. **MCP stdio subprocesses are network-uncontrolled**; `readyagents
attest` marks them `network_uncontrolled: true` and never claims “no egress”
when one ran.

`--sovereign` (or `READYAGENTS_SOVEREIGN=1`) installs a process-level connect
guard for that run only. Non-loopback destinations are refused unless the
operator allowlists a **private** endpoint (`--sovereign-allow` /
`READYAGENTS_SOVEREIGN_ALLOW`). The guard is a floor on top of existing SSRF
pinning and policy egress; it never widens. It is removed when the run ends.

Default mode is unchanged: without the flag, `http_get` and remote model
providers work as before.

```bash
readyagents run examples/calc_pipeline.yaml --sovereign
readyagents run examples/ollama_local.yaml --sovereign --dry-run
readyagents attest RUN_ID --json
readyagents bundle --out ./offline-wheels
readyagents doctor --json
```

Allowlisted destinations and every connect attempt (permitted or refused) are
recorded on the run. A refusal raises `EgressDenied` naming destination and
node.

## What the attestation proves

`readyagents attest RUN_ID` emits mode, recorded attempts, allowlist, model
endpoint, workspace, workflow/run digests, version, and subprocess markers.
Optional `--sign --key publisher.pem` writes a detached Ed25519 signature.

It does **not** prove legal data residency. It does not observe sockets inside
an MCP stdio child. It does not claim an OS network namespace.

## Air-gapped install

On a networked machine:

```bash
readyagents bundle --out ./offline-wheels --python 3.12
```

Transfer the directory. On the air-gapped host:

```bash
pip install --no-index --find-links ./offline-wheels readyagentsdev
readyagents doctor --json
```

Verify `manifest.json` checksums before install (`files[].digest`).

See [local-models.md](local-models.md).
