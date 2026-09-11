# Knowledge pipelines

`type: ingest` builds a memory scope from files or directories with a declared
chunk strategy. Retrieval stays on the shipped memory store (BM25, optional
BYOK embeddings). This is plumbing and governance — document identity,
citations, freshness, and complete forget — **not** a retrieval-quality or
“safe RAG” claim.

Plain `type: memory` write/read/search/forget is unchanged.

## Ingest

```yaml
- id: load_policies
  type: ingest
  source: {kind: directory, path: "docs/policies", glob: "**/*.md"}
  chunk: {strategy: heading, max_chars: 1200, overlap: 120}
  scope: "ns:policies"
  on_change: supersede          # supersede | keep_versions
```

`source.kind` is `file`, `directory`, or `connector`. Paths are confined to the
workspace. Walks are bounded (file count, bytes, depth). Archives refuse
zip-slip (`..` / absolute names).

### Chunk strategies

| Strategy | Behaviour |
| --- | --- |
| `fixed` | Character windows with declared overlap |
| `paragraph` | Pack paragraphs; do not split a short paragraph |
| `heading` | Markdown headings; `heading_path` is the chain |
| `row_group` | CSV/TSV; never split mid-row; header repeated |

Each chunk stores, as additive memory metadata: `document_id`,
`document_version` (content hash), byte or page range, heading path.

Re-ingesting an unchanged file is a no-op. A change versions under
`supersede` (replace chunks) or `keep_versions` (retain prior versions).
A source that disappeared is counted as `removed`; `knowledge sync` can
forget it.

## Citations

Search hits include a structured `citation`:

```json
{
  "document_id": "retention.md",
  "version": "<sha256>",
  "range": {"kind": "bytes", "start": 0, "end": 120},
  "heading_path": "Retention > Exceptions"
}
```

`readyagents knowledge cite` returns that exact span. Scope permissions
apply to `cite` exactly as to retrieval.

```yaml
contract:
  rules: [{require_citation: {from: retrieved}}]
```

If the answer does not mention a retrieved document id, the contract fails.
Existing `{from: ticket_id}` input/output citation is unchanged. When
`from: retrieved` and nothing was retrieved, the agent is refused **before**
spend.

## Freshness and hybrid retrieval

```yaml
freshness: {max_age: 30d}
blend: {bm25: 0.6, embedding: 0.4}
```

`max_age` refuses the run (`KnowledgeStale`) when the oldest ingested stamp
in the scope is older than the threshold. Hybrid blend weights are stored on
the search result and the run record so the same query reproduces. Missing
BYOK embeddings degrade to BM25 as they already do.

## CLI

```bash
readyagents knowledge list --scope ns:policies
readyagents knowledge show retention.md --scope ns:policies
readyagents knowledge cite '{"document_id":"retention.md",...}' --scope ns:policies
readyagents knowledge sync workflow.yaml
readyagents knowledge forget retention.md --scope ns:policies --yes
```

`sync` is foreground. It re-walks ingest sources and reports
added / updated / unchanged / removed. There is no always-on watcher.

## Security

Ingested chunks are taint-untrusted with document provenance. Delayed
injection cannot run a denied tool. Forgetting a document removes every
chunk, index entry, vector, and citation target — unretrievable by search,
get, cite, and list.

## What this is not

- A vector database or hosted embedder beyond the operator's BYOK path
- A reranker, crawler, or filesystem watcher
- A claim that chunking or retrieval is best-in-class or benchmarked
