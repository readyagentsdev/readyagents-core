# Security

## Reporting

If you find a vulnerability in ReadyAgents Core, please **do not** open a public issue.

The GitHub account [readyagentsdev](https://github.com/readyagentsdev) is a **User**, not an Organization.

Use [private vulnerability reporting](https://github.com/readyagentsdev/readyagents-core/security/advisories/new) on this repository for vulnerability reports.

Public contact: [info@readyagents.dev](mailto:info@readyagents.dev)

Include:

- A description of the issue
- Steps to reproduce
- Impact (file sandbox escape, prompt/tool injection, secret leakage, etc.)

We will acknowledge the report and work on a fix before any disclosure.

## Sovereign mode is not an OS sandbox

`--sovereign` refuses non-loopback connects in-process for that run. DNS
lookups can still leak. MCP stdio children are `network_uncontrolled`. A native
extension can bypass the wrapper. Attestation is technical evidence, not legal
data-residency compliance. See [docs/sovereign.md](docs/sovereign.md).

## Agent firewall (defence in depth)

Prompt injection is not solved. With a policy file, the engine taint-tracks
untrusted tool/HTTP/file/MCP/model values, enforces allow/gate/deny at the
single dispatch seam, pins MCP tool descriptions, and refuses known secrets in
model requests. Gates reuse the existing signed approval path. See
[docs/security-model.md](docs/security-model.md) and [docs/policy.md](docs/policy.md).
Without a policy file the 1.0 behaviour is unchanged.

## Supply-chain signatures prove origin, not safety

`readyagents sign` / `verify` and `--require-signed` check that a workflow or
pack was signed by a key in the local publisher keyring. That is provenance,
not a safety proof. Policy still decides what tools may run. There is no
default-trusted key, no Sigstore/CA, and no network key fetch. Pack
verification happens **before** import. See
[docs/supply-chain.md](docs/supply-chain.md).

## Identity and credentials

Approver JWTs are verified against a local trust-anchor file with a real JWT
library (`readyagentsdev[jwt]`, not in `all`). Fail closed on a bad anchor
when a token is presented. `--actor NAME` stays the default. Signed (HMAC of
the decision body) is not the same as identified (verified subject). Optional
credential brokering grants named secrets per tool at the dispatch seam; a
non-granted tool cannot read them from `os.environ` during the call. See
[docs/identity.md](docs/identity.md) and [docs/credentials.md](docs/credentials.md).

## Scope notes

- `read_file` / `write_file` / `list_dir` are sandboxed by a single helper (`resolve_within`): both sides are fully resolved (symlinks, junctions, macOS `/tmp` → `/private/tmp`), then compared with `Path.is_relative_to` plus case-aware equality from a **runtime probe** of the root filesystem (not `sys.platform`). Windows reserved names, alternate data streams, trailing dots/spaces, UNC/`\\?\` (unless the root is one), drive-relative `C:file.txt`, and 8.3 names that resolve outside are refused. Writes are atomic (temp file in the destination dir, restrictive mode on the temp **before** `os.replace`).
- `type: include` paths must stay under the parent workflow directory
- `http_get` is disabled unless explicitly opted in, and even then refuses loopback, private, link-local, and metadata hosts (including after redirects)
- `calc` is a restricted arithmetic evaluator, not Python `eval`
- API keys live in the environment / local env files and must never be committed

## Audit hash chaining is tamper-evident, not tamper-proof

Each new audit JSONL line carries `seq`, `prev_hash`, and `entry_hash`.
`readyagents audit verify` detects a mutated or truncated line. Pre-chain
files are reported as unchained, not failed. A local attacker who can
rewrite the whole file can rewrite the chain. Copy the chain anchor off-box
if you need an independent check. ReadyAgents does not encrypt the audit
trail or run records at rest.

## Evidence packs contain prompts and outputs

`readyagents evidence RUN_ID` writes a local pack that may include recorded
model prompts, inputs, and outputs. Redaction runs at pack time. Treat the
directory as sensitive. The pack is evidence, not a certificate.

## Cassettes contain prompts and completions

`readyagents run --record` writes a cassette under `$READYAGENTS_HOME/cassettes/`. That
file holds **full prompts and full completions** (and recorded tool results). It is the
most sensitive artifact Core writes.

- Recording is opt-in (`--record` / `READYAGENTS_RECORD=1`).
- The configured redactor runs before any cassette write. A pre-write scan blocks
  entries that contain known secret values and marks that node unsealable.
- `runs freeze` re-verifies redaction. It warns in the terminal and in the generated
  README that the fixture must be reviewed before commit.
- Cassettes are untrusted input: schema-validated, version-checked, size-bounded, never
  used to select a code path. A replay is labelled as a replay on the new run record.
- `--out` and cassette paths go through `resolve_within`. `--set` on fork cannot inject
  an approval or bypass the authorizer.
- Offline replay never falls through to a live provider call.

## Optional MCP HTTP door

v0.10 Streamable HTTP is an explicit foreground command (`readyagents mcp serve --transport streamable-http`). It is not started on install or import.

- Bind is loopback-only (`127.0.0.1`). Non-loopback binds are rejected.
- With `--auth token` (default), all `/mcp` and `/runs` requests require `Authorization: Bearer`. Token bytes are compared in constant time. Missing or wrong tokens return `401` with `WWW-Authenticate: Bearer`.
- The token comes from `READYAGENTS_MCP_TOKEN` (or `--token-env`). There is no token-value CLI flag; process listings expose argv. If the env var is empty, the process generates at least 256 bits of entropy and prints the token once to stderr. It is never persisted or logged.
- `--auth none` is allowed only on an exact loopback bind and prints a warning. Do not expose this listener to the internet.
- `Host` and browser `Origin` are checked against the configured loopback listener (DNS-rebinding defense). Responses use `Cache-Control: no-store`.
- Workspace confinement (`confine_under`), SSRF public-IP pinning, secrets/RBAC/PII hooks, append-only audit, and signed decisions still apply. Provider keys are never accepted in request JSON.
- Cooperative cancel does not kill a blocking tool or provider call. Status stays `cancel_requested` until a safe engine point. Retry backoff is a safe point and finishes as `cancelled`.

MCP `tasks/update` approvals have the **same authority** as `readyagents decide` and the localhost approval UI. They travel the existing RBAC authorizer, optional HMAC (`READYAGENTS_DECISION_SECRET`), and append-only audit path. An unsigned (when a decision secret is configured) or unauthorized MCP approval is refused, audited, and leaves the run paused. Do not treat a 2026 MCP client as a weaker door.

Do not reverse-proxy this door onto the public internet.

## Enterprise approvals

Quorum, roles, lazy deadlines, and delegation are opt-in workflow fields. Core
starts no timer; an unattended `expires_in` does not fire until resume, decide,
or a status query. `on_expire: approve` is refused at validation so stalling a
gate cannot mint an approval. Delegation is single-hop, time-bounded,
non-widening, and checked at decision time. Notify payloads are redacted and
bounded; webhook destinations use the existing SSRF pin. `approvals list`
does not distinguish unauthorized from missing. Every new path uses the
existing signed decision object. See [docs/approvals.md](docs/approvals.md).

## Localhost approval UI

`readyagents approvals serve` is an explicit foreground loopback page, not a hosted dashboard.

- Bind is loopback-only. Non-loopback hosts are rejected before a socket opens.
- A one-use bootstrap URL is printed on stderr. After one GET it 303s to `/approvals` and sets an HttpOnly `SameSite=Strict` session cookie on `/approvals`.
- Approve/reject uses one-use HMAC action tokens bound to run id, pending node, revision, and decision. Replay, stale revision, and wrong node do not resume twice.
- Host/Origin checks, CSP (`default-src 'none'` plus self scripts/styles), `nosniff`, `no-store`, and frame denial apply. Query strings are not written to the access log.
- The page is a redacted view. It does not display full inputs, outputs, tool traces, or secrets.
- Trust boundary is the local operator on that host. A privileged local process can still read loopback traffic.
- Outbound `on_pause_url` still refuses loopback/private/metadata URLs; the UI does not add a loopback webhook exception.

## Local run records

JSON run files (`$READYAGENTS_HOME/runs/`) and the optional SQLite file (`$READYAGENTS_HOME/runs.sqlite3`, plus `-wal` / `-shm` companions) can contain prompts, outputs, and other workflow state. ReadyAgents does **not** encrypt run records at rest.

On POSIX, new run files and cache entries are created with owner-only modes (`0o600` files, `0o700` dirs) when the filesystem honours `chmod`. **On Windows those modes are not enforceable** through the standard library (`chmod` is effectively a no-op; no ACL dependency is installed). `readyagents doctor` reports `permissions_enforceable`. Do not rely on file modes alone to hide API keys or PII on Windows.

Trust boundary is the local host. A privileged local process can read these files. For a consistent SQLite backup, copy the database together with `-wal` and `-shm`, or checkpoint first. Network filesystems are unsupported for SQLite (locking is not guaranteed).

JSON-to-SQLite migration never deletes the JSON sources. The append-only audit trail under `$READYAGENTS_HOME/audit/` is a separate JSONL log and is not imported into the run store.

ReadyAgents does **not** provide encryption at rest. See [docs/compliance.md](docs/compliance.md).

## Secrets in issues and PRs

Do not paste API keys, `.env`, `.env-ai`, or MCP bearer tokens into GitHub.
