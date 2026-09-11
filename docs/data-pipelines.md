# Data pipelines

`type: table` is a typed tabular value in run state: declared columns, a row
count, and a content hash. Rows live on disk under the run home, not in the
run record or cassette. `type: classify` applies deterministic rules first and
sends only the remaining rows to a model.

This is plumbing and governance — hashes, caps, containment, and remainder-only
spend — **not** a warehouse, Spark, or retrieval-quality claim.

`json_get`, `json_set`, and `foreach` defaults are unchanged. A workflow without
table nodes is byte-identical.

## Table part

```yaml
- id: load
  type: table
  op: read
  source: {kind: csv, path: "exports/august.csv"}
  schema: {id: int, email: str, amount: float}
  output_key: rows
```

The node output is a hash ref (`_table`, `sha256`, `row_count`, `columns`).
Large intermediates never bloat the record. Replay reuses the content-addressed
file.

`source.kind` is `csv`, `jsonl`, or `parquet` (parquet needs the optional
`table` extra and fails typed when it is missing). Paths go through the
containment helper. Declared `limits: {max_rows, max_bytes}` raise
`TableCapExceeded` instead of an OOM. Reads stream.

## Deterministic ops

`select`, `filter`, `join`, `aggregate`, `sort`, `dedupe`, `union`, `derive`.
Each is pure and recorded by input and output hash. Stdlib is sufficient. An
optional pandas extra may be used for speed; results must match the stdlib
path.

```yaml
- id: dupes
  type: table
  op: dedupe
  source: "{{ rows }}"
  keys: [email]
  keep: first
```

`filter` uses `when:` (the same comparison language as conditions). `derive`
uses `{name, expr}`. `join` takes `right:` and `on:`. `aggregate` takes
`keys:` and `metrics: {amount: sum}`. Schema mismatches name the row and
column and never print the cell value.

## Classify

```yaml
- id: triage
  type: classify
  source: "{{ dupes }}"
  rules: [{when: "amount < 10", label: auto_approve}]
  model_for_remainder: {model: gpt-4o-mini, batch: 25, labels: [approve, review, reject]}
  on_row_error: quarantine
  output_key: labelled
```

Rules run first. Only ambiguous rows are batched to the model. Each result row
has `label` and `decision` (`rule` or `model`). Remainder-only spend is on the
meter and in `metadata.classify`. `on_row_error` is `fail`, `skip`, or
`quarantine` (errors table, no cell values).

Rows sent to a model are untrusted (`source=table`).

## CSV injection and inspect

Export prefixes a leading `=`, `+`, `-`, or `@` with `'`. `readyagents table
head|schema|stats PATH` inspects an intermediate file. `schema` and `stats`
print counts and types, not every cell. `head` shows at most 50 rows.

## Foreach scale

The default foreach cap stays 32, hard cap 100. Set `scale_items` (101–100000)
to opt in to a higher cap, and `concurrency` (1–8) for parallel items with the
same per-item checkpoint.

## What this is not

- Distributed compute, Spark, a warehouse, or streaming dataframes
- A mandatory pandas dependency
- A SQL engine beyond the existing read-only connector
- Model-generated transform code (that is `type: code`)
- Unbounded in-memory tables
