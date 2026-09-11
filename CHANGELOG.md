# Changelog

All notable changes to ReadyAgents Core.

## Unreleased

### Added

- **Simulation.** `readyagents simulate` generates declaration-driven cases
  (seed-stable, no model required): boundaries, types, Unicode, control
  characters, firewall injection strings, and declared branches. Scoring
  reuses `readyagents eval` trajectories. Coverage lists reached and
  unreached condition/approval/foreach/error paths — not a completeness
  claim. Side-effecting tools default to dry-run; live calls need
  `--live-side-effects` and a permitting policy. Failures cluster by shape;
  `--out` freezes one fixture per cluster (secret-shaped text redacted).
  `--fail-on new-failure` blocks a new class in CI. Opt-in `--model`
  personas are metered, capped, and refused in sovereign mode. `--model`
  constructs a provider; persona cases reserve slots in `--cases`.
  `run_eval` scores pause/wait `RunState` attached to the exception.
  Frozen fixtures copy the workflow YAML beside `case.yaml` so
  `readyagents eval` is re-runnable offline. Error coverage marks only
  the node that failed. Repos that never simulate are unchanged. See
  [docs/simulation.md](docs/simulation.md).
- **Workflow packaging.** `readyagents.pkg.yaml` plus
  `readyagents package build|install|list|show|remove|upgrade`. Build emits a
  deterministic `.rapkg` archive (manifest, workflows, policy, fixtures,
  docs, member lockfile). Install verifies signature and digests, shows a
  capability review, and writes nothing without `--confirm`. Nothing executes
  during install. Extraction refuses zip-slip, symlinks, absolute paths, and
  size/count/depth caps. Local policy narrows package declarations and names
  the extras. Secret values are refused at build and install. Upgrade keeps
  `overlay.yaml` and still requires confirm when permissions widen. Installed
  fixtures run under `readyagents eval`. A signed static JSON index
  (`kind: package_index`) is verified; unsigned indexes are refused. Not a
  hosted marketplace. Repos that never package are unchanged. Secret
  scans cover `.pem` / `.key` and suffixless key files; `fixtures: evals/`
  is a directory. See [docs/packaging.md](docs/packaging.md).
- **Agent Skills interop.** Install open-format `SKILL.md` folders
  (`readyagents skills add|list|show|remove`). `type: skill` injects
  instructions as untrusted attributed text, runs bundled scripts only in
  the code sandbox, and filters `allowed-tools` through the policy engine.
  Progressive disclosure: name and description until the node selects the
  skill. Install refuses zip-slip, symlinks, and oversized archives. Skills
  are digested (excluding `*.sig`) and drift-detected. Optional
  `SKILL.md.sig` is a detached Ed25519 JSON signature of the folder digest
  (`kind: skill`) verified through `trust.sign`; a digest-only `.sig` is
  forged. `readyagents lock` pins installed skills; `--frozen` refuses
  skill-folder drift. `skills export` emits a valid skill folder (no secrets
  or local paths); `readyagents agents-md` emits run / validate / test
  context. Workflows without skill nodes are unchanged. See
  [docs/agent-skills.md](docs/agent-skills.md).
- **Event triggers (`triggers:`).** A workflow may declare accepted event
  shapes, input mapping, a required idempotency key and window, per-trigger
  budget and concurrency (`drop` or `defer` on the ceiling), and
  `require_signature`. Core validates and decides (`decide_trigger`); core
  starts no listener. Delivery is at-least-once plus idempotency, not
  exactly-once. Provenance records `cli` / `mcp` / `a2a` / `schedule` / named
  trigger with event id and digest. Payloads are taint-marked; size, depth,
  and rate caps apply before parse; unsigned or tampered signatures are
  refused and audited; dead letters are inspectable and replayable. Pack
  sources (webhook, file, queue, schedule) share that seam.
  `readyagents triggers list|show|test|events`. Default webhook posture is
  loopback. Workflows without `triggers:` are unchanged. Per-trigger spend
  accrues from the started run's real `cost_micros` / tokens and remaining
  cap is passed into the engine. See
  [docs/event-triggers.md](docs/event-triggers.md).
- **Long-horizon waits (`type: wait`).** Pause on `until`, `for_event`,
  `for_file`, and/or `for_run` with `whichever: first|all`. A wait without a
  deadline is a schema error. Status is `waiting`, distinct from `paused`.
  Wake is lazy (`readyagents wake` / `wake --all`); core starts no timer or
  daemon — latency is how often `wake` is called. `on_deadline` is fail,
  continue (never an approval), escalate (existing approval path), or branch.
  Signed `readyagents event` is audited, taint-marked, bounded, and
  idempotent; unsigned events do not wake. File waits use containment.
  Credentials are re-brokered on wake. `runs gc` never deletes `waiting`.
  `runs list --waiting` and `runs timeline` tell the truth. Approval/resume
  without wait nodes are unchanged. `for_file` `on: changed` compares mtime
  as instants (unix epoch or ISO), not a unix string against ISO
  `created_at`. A symlink at the watched path is refused before follow.
  See [docs/long-horizon.md](docs/long-horizon.md).
- **Data pipeline nodes (`type: table`, `type: classify`).** A typed table
  part (columns, row count, content hash) stores rows on disk, never in the
  run record or cassette. Eight deterministic ops (`select`, `filter`, `join`,
  `aggregate`, `sort`, `dedupe`, `union`, `derive`) are pure and recorded by
  hash. Stdlib is sufficient; an optional `table` extra (pandas/pyarrow) must
  match. Schema validation names the row and column and never prints the cell
  value. `classify` applies rules first and sends only remainder rows to a
  model in declared batches, with per-row provenance and remainder-only spend.
  `on_row_error` is fail, skip, or quarantine. CSV/JSONL/Parquet I/O uses the
  containment helper; CSV injection (`=`/`+`) is prefixed on export. Row and
  byte caps raise a typed error. `readyagents table head|schema|stats`
  inspects an intermediate table. Foreach default cap 32/100 is unchanged;
  `scale_items` / `concurrency` are opt-in. `json_get` / `json_set` / existing
  node types stay byte-identical. Not a warehouse or Spark. See
  [docs/data-pipelines.md](docs/data-pipelines.md).
- **Knowledge pipelines (`type: ingest`).** Ingest a file, directory, or
  connector into the shipped memory store with declared chunk strategies
  (`fixed` with overlap, `paragraph`, heading-aware Markdown, row-group
  tabular). Each chunk carries document id, version, byte/page range, and
  heading path. Unchanged re-ingest is a no-op; changes version under
  `supersede` or `keep_versions`. Retrieval returns structured citations;
  `readyagents knowledge cite` resolves the exact span under the same scope
  gate. `require_citation: {from: retrieved}` fails an uncited answer.
  A declared staleness threshold can refuse a run. Hybrid BM25/embedding
  blend weights are recorded. `knowledge list|show|sync|forget|cite` is
  foreground; `sync` reports added/updated/unchanged/removed. Ingested
  chunks are taint-untrusted; forgetting a document removes every chunk,
  vector, and cite target. No vector database, crawler, or always-on
  watcher. Plain `type: memory` writes are unchanged. Plumbing and
  governance, never a retrieval-quality claim. See
  [docs/knowledge.md](docs/knowledge.md).
- **Typed multimodal I/O.** A run-state value may be text or a `MediaPart`
  (kind, mime, confined path, content hash, dimensions/duration, provenance).
  Agent nodes attach declared media where the capability matrix `media` flag
  allows and fail typed before spend otherwise. `type: document` turns a PDF
  into ordered page parts (page image, extracted text, citable page index)
  under declared page/byte/dpi caps. `type: transcribe` uses a declared
  provider; a local model never sends audio off-machine. Codecs and detectors
  are optional extras (`image`, `pdf`, `audio`); core installs are unchanged.
  Size/dimension/page/time caps, metadata strip on ingest, untrusted taint,
  declared and opt-in detected-class redaction before persist/record/send,
  per-part tokens/cost on the record and ledger, and hash-addressed cassette
  replay with no re-upload. Text-only workflows are unchanged. Plumbing and
  governance, never an extraction-accuracy or OCR claim. See
  [docs/multimodal.md](docs/multimodal.md).
- **Opt-in model routing and native Gemini / Bedrock / Vertex extras.** Optional
  `routing:` policy (`cheapest_capable`, `fastest`, `highest_quality`,
  `local_only`, explicit pin). First matching rule wins. A shipped, versioned,
  operator-overridable capability matrix fails unsupported requests before
  spend; malformed or stale overrides are refused. `local_only` never sends
  tainted or memory-derived content to a hosted provider (indeterminate taint
  fails closed). Run record and cassette store the model, rule, and fallback;
  offline replay reproduces the route. Route-level spend/token ceilings sit on
  top of the run cap; an open circuit is skipped. `readyagents models
  list|show|route --explain` is a dry catalog. Without `routing:`, model
  selection is unchanged. No hosted router, no quality inference, no bundled
  benchmarks. See [docs/model-routing.md](docs/model-routing.md).
- **Opt-in `readyagents studio`.** Foreground loopback canvas and run
  inspector from bundled vanilla HTML/CSS/JS. Token-protected, `--read-only`
  disables writes server-side, schema-backed YAML edit preserves comments
  and refuses a disk change, fork/freeze/diff/approvals reuse the shipped
  paths. The file on disk stays the source of truth. Not a hosted product
  and not a safety claim. See [docs/studio.md](docs/studio.md).
- **Opt-in `type: team`.** Supervisor plus a closed declared member set
  (`route`, `plan_then_execute`, `debate`, `pipeline`). Engine-enforced
  `max_rounds` / spend / wall-clock / goal with distinct typed reasons,
  permissioned scratchpad, typed handoffs, per-member accounting, approval
  members, cassette replay, and fork of `metadata.teams`. Nested teams are
  refused. No routing-quality claim. See [docs/teams.md](docs/teams.md).
- **Opt-in output contracts (`contract:`).** Structural schema plus named
  deterministic content rules (`deny_regex`, literal deny, citation, language,
  length, PII via existing detectors). Deterministic JSON repair runs before
  any extra model call; model repairs are bounded, metered, and get the
  validation error verbatim. Actions are exactly `fail`, `repair`, `fallback`,
  `gate`, and `redact_and_continue` (declared, never inferred). Refusal is
  distinct from malformed. An opt-in `judge:` is never default and never the
  only check. Rejected content is redacted and truncated. No classifier extra,
  no hosted moderation, no claim that guardrails make outputs safe. Judge
  rule rows record a numeric score. `gate` pause extras do not store the
  unredacted payload. See [docs/guardrails.md](docs/guardrails.md).
- **Sandboxed `type: code`.** Opt-in Python node: JSON stdin, JSON stdout
  validated against `output_schema`, `subprocess` isolation by default
  (minimal env, closed fds, confined cwd, import allowlist, rlimits where
  the OS has them). `container` is an optional pack and fails closed when
  missing. Recorded and replayable offline without executing. `subprocess`
  is accident-grade, not hostile-code-proof. See
  [docs/code-sandbox.md](docs/code-sandbox.md).
- **Opt-in streaming.** `readyagents run --stream` and `--stream --json`
  (newline-delimited events). Provider `stream()` assembles the same complete
  result as `complete()`. Bounded partials, incremental redaction with a
  lookback window, output-schema nodes buffer, mid-stream cancel is resumable.
  SSE on MCP `GET /runs/{id}/events` and A2A `GET /tasks/{id}/stream` (capped,
  authorised). TTFT/latency on streamed node results. Not audio. See
  [docs/streaming.md](docs/streaming.md).
- **Opt-in batch and concurrency governor.** `readyagents batch` runs one
  workflow over JSONL or CSV rows with a declared `--concurrency`, per-row
  isolation, `--continue-on-error` (default), a spend cap across rows, a
  progress stream, and `--out results.jsonl`. Failed rows are recorded and do
  not stop the batch. A process-local governor enforces global / per-workflow /
  per-provider limits, interactive-before-batch scheduling, `Retry-After`
  back-pressure, and a bounded queue. The synchronous engine is unchanged; the
  async path is `asyncio.to_thread` over it. Batch persistence defaults to
  SQLite WAL; JSON remains the `run` default. Not a distributed worker. See
  [docs/scale.md](docs/scale.md).
- **Scoped local memory.** JSON default store and opt-in SQLite (`type: memory`
  with `write|read|search|forget`), explicit `workflow:` / `ns:` / `subject:`
  scopes, stdlib BM25, optional BYOK embeddings that degrade to keyword search,
  TTL, auditable forget/subject sweep, and declared compaction that records
  what was dropped. Memory is untrusted. Delayed injection and scope escape
  are first-class tests. Not a quality or benchmark claim. See
  [docs/memory.md](docs/memory.md).
- **A2A interoperability.** Serve one workflow as an A2A agent
  (`readyagents a2a serve|card|probe`) and delegate with `type: a2a`. Tasks are
  a projection over the durable run record (not a second state machine).
  Loopback by default; polling only; remote content untrusted; SSRF public-IP
  pin; credentials never follow a cross-host redirect; unsigned answers to a
  local gate are refused and audited. This is a served mapping, not A2A
  certification. See [docs/a2a.md](docs/a2a.md).

### Fixed

- Concurrent foreach checkpoints the contiguous completed prefix on a later
  item failure, so resume does not redo earlier ok rows.
- Schema-inference table ingest stops at ``max_rows + 1`` instead of slurp-
  then-check, so ``limits.max_rows`` bounds the read.
- Table `on_row_error` skip/quarantine applies on ingest schema mismatches as
  well as classify, so a single bad row does not poison the result table.
- `type: ingest` with `embed: true` persists BYOK vectors on each chunk
  (`store.write(..., vector=)`). A later memory search with `blend:` can then
  run hybrid retrieval and record the weights on the search result and
  `metadata.knowledge_blend`. Missing embeddings still degrade to keyword.
- Routing taint follows ``{{outputs.<id>}}`` / ``{{inputs.<key>}}`` to the
  nested provenance key. Skipping the ``outputs`` namespace let a hosted pin
  fire on untrusted tool output.
- Offline replay uses the cassette-recorded route (surviving model and
  fallback), not a fresh `select_route` that can CassetteMiss on a recovered
  primary. Capability checks honour `routing.capability_matrix` / ctx matrix
  rather than always loading the bundled file.
- Team member interpolation sees only granted scratchpad keys across the
  whole metadata namespace (`scratchpad`, `teams.<id>.scratchpad`, dumping
  `teams`, last_output, handoff payloads). Mid-team checkpoints persist via
  `on_persist`. `runs fork` from a paused mid-team checkpoint reconstructs
  from `metadata.teams` when the team node has no completed result. Per-member
  and per-role tokens, cost, and tool calls are written on the spend ledger.
- Team member `max_cost_usd` is enforced before the next member call. A paused
  team resumes from `metadata.teams` pending_member.
- `redact_and_continue` masks the firing `deny` / `deny_regex` / PII match
  (not only email/key patterns) in the continued output, `rejected` field,
  cassette, and audit. `require_citation` uses inputs/prior outputs only, not
  the node's own fields. Fallback recovery is re-checked against the contract.
- Code-node wall-clock is `communicate` timeout only; CPU is `RLIMIT_CPU` plus
  a child `process_time` watchdog so `time.sleep` under wall is not a CPU kill.
  `memory_mb` is an RSS watchdog in the child plus a parent RSS poll (macOS
  cannot lower `RLIMIT_AS`; Windows has no `resource`). Each limit still maps
  to its own typed error.
- Incremental streaming redaction uses a common-prefix holdback so a secret
  that straddles the lookback split is not emitted unredacted.
- OpenAI and Anthropic `stream()` re-raise `CancellationRequested` (and other
  `ReadyAgentsError`) from `on_token` instead of falling back to `complete()`,
  which would record a phantom ok node.
- MCP `GET /runs/{id}/events` and A2A `GET /tasks/{id}/stream` return a JSON
  event snapshot unless `Accept` prefers `text/event-stream`. SSE is a finite
  snapshot body (no wait loop), so a client cannot hang on the generator.
- Provider and connector HTTP 429/`Retry-After` notify the process concurrency
  governor (token bucket + wait), so `readyagents batch` backs off instead of
  retry-storming. `--concurrency` is capped by `READYAGENTS_MAX_CONCURRENCY`
  (default 4096), not a hidden 32. Declared global / per-workflow /
  per-provider limits are env and CLI flags. Batch acquire uses the workflow
  `default_model` / settings provider (CLI has no `llm=`), so an OpenAI 429
  actually delays the next row.

- Sovereign egress guard is refcounted so concurrent `batch` rows share one
  process wrapper instead of raising "already installed" or uninstalling
  while another row is still running. Per-handle attempt lists stay isolated.
- Security-model docs: MCP description/schema pins gate only when a firewall
  policy file is present. Without a policy, shipped `evaluate` stays identity
  allow (`pin_changed` does not pause an unconfigured run).
- MCP and A2A missing-extra errors name `readyagentsdev[mcp]` (PyPI), not only
  clone-only `pip install -e ".[mcp]"`.
- README CLI table lists shipped `connectors`, `sign`/`verify`/`lock`/`sbom`/`trust`,
  `runs fork|diff|freeze|migrate`, and `approvals serve`. Optional OTel emits
  `readyagents.total_tokens` instead of a non-registry `gen_ai.usage.total_tokens`.
- First-ten-minutes and getting-started versions match the package; the
  first-ten-minutes path documents `pip install readyagentsdev` (the wheel
  does not ship `examples/`). Extra-missing OpenAI/Anthropic errors name
  `readyagentsdev`. `readyagents init` next-steps are wheel-safe. A2A docs
  distinguish the v1.0 well-known path from card `protocolVersion` 0.3.0.
  README labels A2A and memory as Unreleased on this checkout, not the 1.9.0 tag.
  The PyPI walkthrough uses distinct `new` destinations (`my-flow`, `demo`,
  `starter`) so a linear follow does not hit overwrite.
- Cancel of a paused (input-required) run finishes immediately even if the
  worker has not yet dropped the in-flight set, so A2A `tasks/cancel` does not
  sit in `working` and starve the 120/min poll cap.
- Logging stream handler swallows closed-stream emit errors so a background
  worker cannot stall on pytest/stderr teardown.
- Memory read/search apply compacted text to the node output so truncate and
  summarize do not still return dropped content.
- SQLite `forget` of a missing record id returns 0, matching the JSON store.
- A2A card pins use the locally computed digest; a hostile `digest` field
  cannot freeze drift detection.
- Card-digest `on_description_change: gate` honors `--approve` (stores the new
  digest and continues), matching MCP pin resume.
- The A2A bearer is sent only to the operator-supplied `agent_url` host, not to
  a card `url` on another host.
- `readyagents a2a probe --json` reports `signatureStatus`
  (`unsigned` / `verified` / `invalid`) and never prints secret values.

## 1.9.0 — 2026-09-10

### Added

- **Connector suite.** Typed contract above the tool registry (`ConnectorSpec` /
  `ConnectorContext`), SDK with `Retry-After`, pagination and size caps, and a
  small first-party set (`rest`, `sql`, `object_storage`, `message`, `ingest`).
  Write-shaped connectors gate by default; idempotency keys de-dupe retries.
  `readyagents connectors list|show|test` is the catalog. Conformance harness
  fails own-socket / non-granted-secret / cap-bypass connectors. Catalog is
  small by design; governance is the differentiator, not certification. See
  [docs/connectors.md](docs/connectors.md) and
  [docs/connector-sdk.md](docs/connector-sdk.md).

### Fixed

- REST write operations are classified from `connector_config` HTTP method
  before the default write gate, so `create_ticket` (POST) pauses.
- `message` pins destinations to the declaration; a call URL host that is not
  declared is refused.
- Connector HTTP reuses `http_get` public-IP SSRF pinning, including redirects
  (loopback and RFC1918 refused).

## 1.8.1 — 2026-09-10

### Fixed

- `readyagents bundle` fails if `pip download` cannot collect declared runtime
  wheels; a `--no-index` install needs those wheels, not only the project.
- Resuming a sovereign run restores the socket guard and stored allowlist from
  the run record even when `resume` is invoked without `--sovereign`.
- Keyless OpenAI-compat for allowlisted private bases uses the same parsed
  CLI/env allowlist as the egress guard (`--sovereign-allow 10.0.0.8`).

## 1.8.0 — 2026-09-10

### Added

- **Sovereign mode.** `--sovereign` / `READYAGENTS_SOVEREIGN=1` refuses
  non-loopback egress at the process socket boundary for the run (model
  calls, tools, packs, threads). Loopback is allowed; private endpoints are
  explicit (`--sovereign-allow`). `readyagents attest` emits a residency
  document that marks MCP stdio `network_uncontrolled: true` and does not
  claim legal compliance. `readyagents bundle` writes wheels plus checksums
  for `pip install --no-index --find-links`. `readyagents doctor` reports
  whether sovereign would succeed here and loopback model presence, never
  secret values. See [docs/sovereign.md](docs/sovereign.md) and
  [docs/local-models.md](docs/local-models.md).

### Fixed

- JSON run-record reads retry Windows sharing violations, so a poll during
  persist does not fail with `PermissionError`.
- Sequential HITL resume waits out a prior in-flight executor when the run is
  already paused at the next gate, instead of returning "resume in flight".
- Scoped delegation is matched against the gate's `approver_roles` as well as
  the actor's JWT/RBAC roles, so a finance grant still applies when the
  delegate already holds a different role.

## 1.7.0 — 2026-09-10

### Added

- **Enterprise HITL.** Opt-in quorum (`approvals_required`), distinct actors,
  `deny_actor` (including `$initiator`), role routing (`approver_roles` /
  `require: any|all`), lazy deadlines (`expires_in` with `on_expire: reject |
  escalate | fail` — **`approve` is refused at validation**), time-bounded
  revocable single-hop delegation (`readyagents delegate` /
  `delegations list|revoke`), `require_reason` and override recording, file /
  command / webhook notify channels, and `readyagents approvals list`. Core
  starts no timer; expiry is evaluated on resume, decide, and status query.
  A gate with none of the new fields is unchanged. See
  [docs/approvals.md](docs/approvals.md).

### Fixed

- MCP decide can resume a recorded decision that is still paused, so sequential
  approval resumes do not stall after the first decide.

## 1.6.0 — 2026-09-10

### Added

- **Supply-chain trust.** Canonical SHA-256 digests (algorithm v1) for
  workflows including every resolved `include`, pack file bytes, and MCP
  advertised tool surfaces. `readyagents sign` / `verify` write a detached
  Ed25519 signature beside the artifact (`flow.yaml.sig`) that binds digest
  **and** kind. `readyagents trust add|list|remove` manages a local publisher
  keyring under `$READYAGENTS_HOME` (atomic, restrictive permissions; malformed
  fails closed). `--require-signed` and policy `require_signed` refuse unsigned
  or untrusted artifacts **before** a pack is imported. `readyagents lock` and
  `--frozen` pin digests; `readyagents sbom` emits a deterministic
  CycloneDX-shaped inventory (no network, no secrets). Optional `sign` extra
  is **not** in `all`. No default-trusted key. Signing proves origin, not
  safety. See [docs/supply-chain.md](docs/supply-chain.md).

### Fixed

- `--require-signed` executes the workflow and include bytes that were
  digested, not a later re-read of the path. Pack lock pins are checked
  **before** import. Templated `include` paths are refused under
  `--require-signed` / `--frozen`. `on_lock_mismatch: gate` persists a
  paused run that can be resumed.

## 1.5.1 — 2026-09-10

### Fixed

- Workload `sign_assertion` is attached to outbound pause-notify (`post_json`)
  when configured; a fixture peer can verify the JWT against the public key.
- Credential grants are delivered via a thread-local mapping, not shared
  `os.environ`, so parallel tool branches cannot see a sibling's secret.

## 1.5.0 — 2026-09-10

### Added

- **Agent identity.** Approvers may present an OIDC/JWT assertion verified
  against a local trust-anchor file (`--token-file`, `--trust-anchors` /
  `READYAGENTS_TRUST_ANCHORS`). Verification uses the optional `jwt` extra
  (**not** in `all`): signature, issuer, audience, expiry, skew. `alg: none`,
  algorithm confusion, and unknown `kid` are refused. Fail closed on a
  missing/malformed/unreadable anchor when a token is presented. Claims map
  to `--actor` / RBAC roles; replay of the same token on a gate is refused.
  *Signed* (HMAC of the decision body) and *identified* (verified subject)
  stay separate. `--actor NAME` remains the default. See
  [docs/identity.md](docs/identity.md).
- **Credential brokering.** Optional `readyagents.credentials.yaml` grants
  named secrets per tool at the dispatch seam; non-granted tools cannot read
  them from `os.environ` during the call. `credential_kind` is `static` when
  the provider cannot mint. See [docs/credentials.md](docs/credentials.md).
- `readyagents identity verify|whoami|trust`. Workload `whoami` prints a
  fingerprint, never the private key.

### Fixed

- Spend meter reserves only the in-flight call's estimated tokens/cost, not
  the unused remainder of `--max-tokens` / `--max-spend`, so overlapping
  parallel `complete()` calls that together fit under the cap succeed.
- Spend ledger `cost_micros` follows the meter snapshot, including `0` when
  the model is unpriced, instead of copying the legacy default-rate value
  from run `usage`.

## 1.4.0 — 2026-09-10

### Added

- **TokenOps.** Versioned, overridable model price table (`READYAGENTS_PRICES`).
  Unknown models are explicitly unpriced, never a silent zero.
  `readyagents run PATH --estimate` walks the engine's routing (no execute, no
  network) and prints a range with assumptions. `--max-spend` / `--max-tokens`
  are consulted before each model call; parallel branches share one meter; a
  resumed run continues the same budget. `--label KEY=VALUE` is stored on the
  run and in an append-only hash-chained spend ledger (`readyagents spend`).
  Cache hits/misses/savings appear on the run record, `runs report`, and the
  ledger. Runaway guards (`--max-model-calls`, `--max-run-tool-rounds`,
  `--max-wall-seconds`, workflow `runaway:`) raise `RunawayGuard`, distinct
  from `BudgetExceeded` and `CircuitOpen`. Optional `tokenizer` extra is **not**
  in `all`. The provider invoice is authoritative. Without the new flags,
  behaviour is unchanged. See [docs/cost.md](docs/cost.md).

## 1.3.0 — 2026-09-10

### Added

- **Traceability evidence.** Audit JSONL is hash-chained (`seq`, `prev_hash`,
  `entry_hash`). Chaining is tamper-evident, not tamper-proof; copy the chain
  anchor off-box if you need an independent check. `readyagents audit verify`
  reports unchained ranges and the first break. `readyagents evidence RUN_ID`
  writes a hash-manifested local pack (machine JSON, self-contained HTML,
  decisions projection, audit slice, workflow source, Mermaid graph). The pack
  may contain prompts and outputs. `readyagents graph PATH` is deterministic
  and injection-safe. Configurable retention (`READYAGENTS_RETENTION_DAYS`,
  default 180) makes `runs gc` refuse in-window records unless
  `--override-retention` (audited). That window is local hygiene, not a legal
  archive; gc does not delete audit JSONL. Pack observer seam
  (`register_observers`) is backward-compatible. Optional content-free `otel`
  extra is **not** in `all`, starts no collector on import, and is off unless
  `READYAGENTS_OTEL=1`. Docs claim evidence, never compliance or certification.

## 1.2.1 — 2026-09-10

### Fixed

- Agent tool-calls inherit taint from the calling prompt/system, so
  `on_tainted` applies when the model emits literal arguments.
- Foreach copies parent provenance and marks `item`/`index` untrusted when
  the items source is untrusted.
- The resolved policy path and MCP pins persist across resume/decide and
  later runs; omitting `--policy` on resume cannot fail-open a gate.
- `nodes.<id>.require_approval`: only approve proceeds; reject is a policy
  deny.

## 1.2.0 — 2026-09-10

### Added

- **Agent firewall.** Optional `readyagents.policy.yaml` (or `--policy` /
  `READYAGENTS_POLICY`) is evaluated at the single tool-dispatch seam. Actions
  are allow, gate, or deny. Gate reuses the existing signed approval pause.
  Malformed or referenced-missing policy fails closed. Without a policy file,
  behaviour is unchanged.
- Additive provenance (`trusted` / `untrusted`) on run records, injection
  heuristics that route to policy and never rewrite content, MCP tool
  description pinning, egress host allowlists, and a pre-send scan that
  refuses known secrets in model requests.
- `readyagents policy check` and `readyagents policy explain`.

## 1.1.0 — 2026-09-09

### Added

- **Freeze/eval CI contract.** `readyagents runs freeze` writes `expect_determinism`,
  `expect_nodes`, `expect_tools`, and `expect_usage` ceilings into `case.yaml`.
  `readyagents eval` scores those fields so a fixture fails on classification drift,
  dropped tool rounds, reordered nodes, or token ballooning — still keyless, still
  without an LLM-as-judge.
- Packs may declare tool cassette classification via `register_tool_seals()` or
  `FunctionTool.determinism` (`recomputed` / `sealable` / `unsealable`). Unclassified
  pack and MCP tools stay unsealable. Core builtins cannot be overridden.

### Fixed

- Offline eval classifies unsealable tools correctly so fixtures do not silently
  mis-score pack/MCP tool rounds.

## 1.0.0 — 2026-09-09

### Added

- **Run Time Machine.** `readyagents run --record` captures a run's model and tool calls into a
  content-addressed cassette; `readyagents runs replay --offline` re-executes that run
  deterministically with no network, no API key, and no spend; `readyagents runs fork` branches a
  new run from any node checkpoint with optionally edited state; `readyagents runs diff` reports
  where two runs first diverged and the token and cost delta; and `readyagents runs freeze` turns
  a run into a redacted, offline regression fixture that `readyagents eval` runs in CI for free.
- Every replay reports per-node determinism — sealed, recomputed, or unsealable — so a run is
  never claimed to be reproducible when it is not.

### Security

- Cassettes contain full prompts and completions; recording is opt-in and freeze re-verifies
  redaction.

### Changed

- ReadyAgents Core is 1.0.0. The workflow file format, the run-record format, CLI command names
  and exit codes, the documented `--json` envelope keys, the pack protocol, and the declared
  Python API are covered by a published stability contract and deprecation policy.
- The ReadyAgents `/runs` HTTP alias is not removed at 1.0; removal is restated as no earlier
  than v1.2.

### Fixed

- Pure JSON builtins (`json_get`, `json_set`, `json_merge`) are recomputed on
  offline replay, so the keyless `calc_pipeline` freeze example does not need
  `--allow-unsealed`.

### Security

- `runs diff` always applies default secret redaction and strips ANSI/control
  sequences, even when `READYAGENTS_REDACT` is unset.

## 0.12.0 — 2026-09-09

### Added

- Windows and macOS are now tested in CI across Python 3.11–3.14, alongside a wheel-install verification
  job that runs the pip-only first-run flow on each platform.
- `readyagents doctor` reports platform, Python, extras, workspace writability, permission
  enforceability, filesystem case sensitivity, loopback availability, and the resolved run-store backend.
- `python scripts/smoke.py` (and `make smoke`) runs the keyless example set without a POSIX shell.

### Fixed

- Cooperative cancel during retry backoff finishes the run as `cancelled`. Backoff is a safe
  engine point, not an in-flight node body, so the persist listener no longer leaves
  `cancel_requested` as the terminal status.

### Security

- Workspace containment is now enforced by a single audited helper that rejects Windows reserved names,
  alternate data streams, short-name and trailing-dot forms, and that compares resolved paths with
  runtime-probed case sensitivity, so a case-only variant cannot escape the sandbox on Windows or macOS.
- File-permission claims are now accurate per platform and reported by `readyagents doctor`.
- Path-containment corpus covers Windows directory junctions / reparse points, which
  `Path.is_symlink()` does not report.

## 0.11.0 — 2026-09-09

### Added

- `readyagents schema` emits a JSON Schema 2020-12 document generated from the workflow models, shipped
  as `schemas/workflow-v1.json` and referenced from every scaffold, so YAML editors offer completion and
  inline validation while authoring.
- Workflow validation errors now report file, line, column, and a caret excerpt, including inside
  `parallel` branches, `foreach` bodies, and included sub-workflows. `validate --json` gains an additive
  `problems` array.

### Fixed

- Schema errors surface NodeType enums, did-you-mean hints, and settings redaction.
- Graph-validator carets no longer land on the top-level `name:` line for empty/mislocated errors.

## 0.10.1 — 2026-09-09

### Security

- Streamable HTTP `2026-07-28` POSTs validate `Mcp-Method` / `Mcp-Name` before any SDK
  dispatch, including ordinary `tools/call`. MCP `tasks/update` no longer treats
  `_meta` `clientInfo.name` as an RBAC actor. `readyagents mcp probe` sends the loopback
  bearer token from `READYAGENTS_MCP_TOKEN` when set.

## 0.10.0 — 2026-09-09

### Added

- Explicit localhost browser UI for redacted pending approvals, protected by short-lived
  single-use tokens and the existing RBAC/audit/signed-decision path. It uses vanilla assets,
  starts only on command, and does not relax outbound SSRF protection.
- Optional stdlib SQLite run-store with indexed run queries, revision conflict detection, and a
  verified non-destructive `runs migrate` command. JSON files remain the default and existing
  persistence/CLI behavior is preserved.
- MCP `2026-07-28` conformance: `server/discover`, stateless per-request `_meta` negotiation,
  `resultType` on every result, and the official `io.modelcontextprotocol/tasks` extension implemented
  over the existing durable run record.
- Approval gates are now reachable over MCP as Multi Round-Trip Request `input_required` items and
  resolvable with `tasks/update`, routed through the same signed-decision, RBAC, and audit path as
  `readyagents decide`.
- `readyagents mcp probe URL` reports a remote server's supported protocol revisions and extensions.

### Deprecated

- The ReadyAgents `/runs` HTTP extension is superseded by the official tasks extension. It continues to
  work and is scheduled for removal no earlier than v0.12.

### Security

- MCP `tasks/update` approvals use the same RBAC, optional HMAC, and append-only audit path as
  `readyagents decide`. Unsigned (when `READYAGENTS_DECISION_SECRET` is set) or unauthorized MCP
  approvals are refused and leave the run paused. Loopback-only bind and bearer auth are unchanged.

### Docs

- Documented the separately installed `readyagents-pack-continuous` package for explicit
  foreground cron, file-watch, and authenticated-webhook triggers. ReadyAgents Core itself still
  starts no scheduler or listener and gains no mandatory dependency.

## 0.9.0 — 2026-09-09

### Added

- Opt-in authenticated MCP Streamable HTTP transport and a local asynchronous run API with
  start, poll, decide, and cooperative-cancel task handles. Stdio remains the default, and core
  still has no scheduler, recovery worker, hosted service, or mandatory HTTP dependency.

### Security

- Optional MCP HTTP is loopback-only in v0.9, authenticates with a bearer token from the
  environment (never argv), and rejects DNS-rebinding Host/Origin values.

### Docs

- MCP Streamable HTTP (`/mcp`) and the ReadyAgents `/runs` task-handle extension are
  documented separately. Official MCP Tasks, MRTR, and elicitation are not claimed.

## 0.8.2 — 2026-09-03

### Added

- MCP Registry metadata: the packaged README carries the ownership marker and `server.json` describes the local stdio MCP server.

## 0.8.1 — 2026-09-03

### Fixed

- Install docs and the PyPI long description now match that `readyagents` is on PyPI (`pip install readyagents`). Clone-and-run still uses `examples/calc_pipeline.yaml`. The wheel does not ship `examples/`; pip-only first run is `readyagents new`.

## 0.8.0 — 2026-09-02

### Added

- **`readyagents eval PATH`.** Score a YAML/JSON `cases:` suite with the existing local harness. Exit 0 if every case passes, 1 if any fail. `--json` prints `ok`, `command`, `passed`, `failed`, and `results`. No network and no API keys.
- **Local packs.** Repeatable `--pack PATH` and `READYAGENTS_PACK` load a `.py` pack (`get_pack()`) confined to the workspace. `examples/connector_demo.yaml` runs with `--pack examples/packs/connector_pack.py`.
- **`list_dir` builtin.** Sandboxed directory listing (dotfiles skipped, entry cap, no `..` / symlink escape). `--dry-run` still lists. Example: `examples/list_dir.yaml`.
- **`readyagents new` templates** `foreach`, `agent-tools`, and `gated`. Default remains `pipeline`. Overwrite refusal unchanged.
- **Unified `--json` envelope.** Additive `ok` and `command` on validate, run, resume, packs, eval, and `runs show` (including missing ids). Existing keys stay. No Rich markup on JSON. Approval pause is still exit 2 and still includes `run_id`.

### Docs

- MCP docs lead with builtin `list_dir`, not `npx`. ReadyAgents remains Python-only.

### Fixed

- MCP `serve` registers `list_dir` (same workspace sandbox as `read_file` / `write_file`).

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, billing, or Node.js toolchain. Install with `pip install readyagents` or from a clone.

## 0.7.0 — 2026-08-27

### Added

- **Include / parallel resume snapshots.** Successful child nodes and parallel branches are stored on the parent run record; resume does not re-run them. Nested foreach stays invalid.
- **MCP client sessions.** One stdio child per named server for the run; `inputSchema` is copied onto tools; `cwd` is confined to the workspace.
- **Template filters** `default`, `len`, `join` and condition `and` / `or` / `not` (no Python `eval`).

### Security

- Pause notify (`on_pause_url`) uses the same public-IP pin as `http_get` (loopback/private/metadata refused, including after redirects). A blocked URL does not prevent the HITL pause.

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or billing.

## 0.6.0 — 2026-08-27

### Added

- **Bounded sequential `foreach`.** `type: foreach` iterates a list from prior state (`items:` path, `max_items` default 32 / max 100). Body is one node; output is the list of per-item results. Resume skips already-ok items. Nested foreach is rejected. Example: `examples/foreach_calc.yaml`.
- **`json_set` / `json_merge` builtins** (size-capped like `json_get`). Dotted paths; `__` segments refused. Example: `examples/json_mutate.yaml`.
- **Bounded agent tool-use loop.** Agent nodes may set `tools: [calc, …]` (an allowlist of registry/MCP names) and optional `max_tool_rounds` (default 8, hard max 20). The engine passes tool specs into `complete(..., tools=)`, runs **real** registry tools, and uses the **final** model text as the node output. Omit `tools` for a one-shot complete (0.4.0). Unknown YAML names fail before any LLM call; a model request off the allowlist is not executed. `--dry-run` still skips the LLM and does not run `write_file` / `http_get`. Example: `examples/agent_tools.yaml`.
- **Agent tool-round traces and recoverable tool errors.** Allowlisted `ToolError` is fed back as a tool observation (loop continues until the round cap). Persisted `node_results[].tool_rounds` and `runs show --json` name the tools.
- **Run-store hygiene.** Paused records store the approval **prompt** plus resume/decide copy-paste. `readyagents runs delete RUN_ID --yes` and `runs gc --yes` (paused kept unless `--include-paused`). KeyboardInterrupt persists status `cancelled`. `pending` is cleared on resume and success.

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or billing.

## 0.4.0 — 2026-08-26

### Docs

- Empty Unreleased of items already shipped in 0.3.0 (#24)
- `docs/first-ten-minutes.md` uses `readyagents new my-flow` (default pipeline), not `--template pipeline` (#25)
- Leftover marks extras, size caps, DNS rebind, parallel timeout/retry, packs JSON, and Makefile/CONTRIBUTING smoke as shipped (#26)

## 0.3.0 — 2026-08-22

### Added

- **Structured JSON logs** (`--log-format json` / `READYAGENTS_LOG_FORMAT=json`) with `run` and `node` on every event
- **Per-node token/cost tracking** rolled up on the run (`usage.prompt_tokens`, `completion_tokens`, `cost_micros`). Include nodes count nested agent usage once; parallel branches merge onto the fan-out node.
- **Budget limits** (`budget.max_tokens` / `budget.max_cost_usd` or env) stop further LLM work with `BudgetExceeded`
- **Model fallback** (`fallback_models` on the node or workflow) and a process-local **circuit breaker**
- **External decision injection:** `readyagents decide RUN_ID --file decisions.json` and `--decision-file` on `run`/`resume` (no always-on HTTP listener). Outbound `on_pause_url` notify only
- **Multi-gate example** `examples/multi_gate.yaml` (two sequential approvals)
- **Secrets-manager hooks** (env/`.env` remains default BYOK; packs may register backends)
- **Append-only audit trail** under `$READYAGENTS_HOME/audit/<run_id>.jsonl`
- **RBAC hooks** (`--actor`, pack `register_authorizers`) and optional **PII redaction**
- **Pydantic `output_schema`** on agent nodes (`StructuredOutputError` on mismatch)
- **Opt-in local LLM cache** (`READYAGENTS_LLM_CACHE=1`, `--no-cache` to skip)
- **Example connector pack** (`examples/packs/connector_pack.py`) proving the pack seam
- **`readyagents.testing`**: `run_workflow_spec`, `ScriptedLLM`, `RecordedLLM`, `run_eval`
- Official **Dockerfile** + **docker-compose.yml**; `make smoke` / `make ci` cover lint, tests, and keyless examples (dry-run, resume, approval, parallel, include)

### Reliability (folded from post-0.2.0)

- `readyagents new` overwrite refusal; `--log-level` on `run`; MCP tools cannot shadow sandbox `read_file`
- Nested `include` approval pauses the parent; `validate` rejects cycles; success prints `run_id:`
- Missing workflow path is `ConfigError` (exit 1); `--dry-run` stubs `write_file`; node timeouts return when the budget expires
- `http_get` refuses private/loopback/metadata hosts; file tools and `include` stay sandboxed

### Safety

- Core still has no always-on listener, scheduler, hosted control plane, extra database, or vendor secrets SDK. Those remain waitlisted packs, not for sale.

## 0.2.0 — 2026-08-19

### Added

- **Approval node** (`type: approval`) — human-in-the-loop gate. Pauses the run (exit 2) until `--approve` / `--reject` or `readyagents resume`.
- **Per-node persistence** and **resume** from the last successful node (`readyagents resume RUN_ID`). Records are JSON under `.readyagents/runs/`, written atomically.
- **Run inspection:** `readyagents runs list|show|inspect|replay`, with `--json`, `--status`, `--workflow`, and `--limit`.
- **Scaffolding:** `readyagents new` with templates `basic`, `approval`, and `research`.
- **Parallel fan-out:** `type: parallel` with concurrent `branches`.
- **Sub-workflows:** `type: include` to compose another YAML workflow (depth-limited).
- **Dry-run token estimate** on agent nodes (`usage.estimated_tokens`).
- Examples: `approval_gate.yaml`, `fanout_gate.yaml`, `include_demo.yaml`.

### Changed

- Structured logs include `run=<id>` and `node=<id>`.
- `--dry-run` `parse_json` after an agent stub no longer crashes LLM examples.

### Safety

- Core still has no always-on scheduler, control plane, distributed recovery, alerting, or billing. Those remain waitlisted packs, not for sale.
