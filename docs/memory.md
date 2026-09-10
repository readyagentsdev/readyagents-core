# Memory

**Delayed prompt injection is the reason memory is dangerous. Scope escape is
the breach.** This page describes a local, scoped store with retrieval — not
semantic quality, not a memory product, not a benchmark claim.

ReadyAgents memory is a **declared workflow step** (`type: memory`) writing to a
file under `$READYAGENTS_HOME/memory/`. Nothing is remembered unless a node
says so. Nothing is retrieved unless a node asks. There is no ambient global
store, no always-on indexer, and no vector database.

## Security (read this first)

- **Delayed injection.** Content written on Monday is retrieved on Friday.
  Memory output is always **untrusted** (`source=memory`). A poisoned write
  cannot by itself cause a denied tool to run; policy `on_tainted` still
  decides. The two-run test is the contract.
- **Scope escape.** A templated `scope: "subject:{{ ticket_id }}"` that expands
  into another kind (`ns:…`), another subject, `..`, `/`, `*`, or `?` is
  refused. After interpolation the value must be a safe token and must match
  the declared `scope_pattern` / workflow `memory_scopes`.
- **Secret persistence.** Redaction runs before write. A known secret value is
  refused and the block is audited (`memory_secret_refused`).
- **Erasure.** `forget` deletes the record, its BM25 presence (records are the
  index), and any stored vector. A tombstone that remains searchable is a
  compliance failure.
- **Export leakage.** `readyagents memory export` writes the file an attacker
  wants. It is workspace-confined, requires `--yes`, and is audited.

## Scopes

Scopes are explicit:

| Form | Meaning |
| --- | --- |
| `workflow:<name>` | This workflow only (`name` must match) |
| `ns:<name>` | A named namespace you declared |
| `subject:<key>` | A ticket, customer, or other subject token |

No other kinds. Values are `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.

## Node

```yaml
- id: remember
  type: memory
  op: write          # write | read | search | forget
  scope: "subject:{{ ticket_id }}"
  scope_pattern: "subject:*"
  text: "{{ note }}"
  ttl: 90d
  output_key: stored

- id: recall
  type: memory
  op: search
  scope: "subject:{{ ticket_id }}"
  scope_pattern: "subject:*"
  query: "{{ note }}"
  limit: 5
  output_key: prior
```

`embed: true` requests BYOK embeddings and stores vectors locally. If embeddings
are unavailable, search **degrades to BM25** and records a note. It does not
fail closed at retrieval time.

## Compaction

Declared, never implicit:

```yaml
context:
  max_tokens: 8000
  on_exceed: truncate    # truncate | summarize | fail
```

The node result records `strategy`, `tokens_before`, `tokens_after`, and
`dropped`. `fail` is the honest choice when silent loss is unacceptable.

## CLI

```bash
readyagents memory list --json
readyagents memory show ID
readyagents memory search --scope subject:T-1 "printer"
readyagents memory forget --scope subject:T-1 --yes
readyagents memory export --scope subject:T-1 --out mem.json --yes
```

JSON backend is the default. `READYAGENTS_MEMORY_STORE=sqlite` is opt-in stdlib
SQLite (WAL). Both are local files. No daemon.

## What this is not

Not a hosted memory service. Not a vector database. Not agent-chosen persist
(the graph decides write and scope). Not a claim that BM25 equals semantic
recall. See [policy.md](policy.md) and [SECURITY.md](../SECURITY.md).
