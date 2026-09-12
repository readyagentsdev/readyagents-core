# Migration and importers

**Unreleased on this checkout — not on the 1.9.0 tag.** `readyagents import`
translates an **operator-exported** workflow into ReadyAgents YAML plus a
fidelity report. This is **structural translation only**, not behavioural
equivalence. There is no round-trip export, no scraping of a source UI or API,
and no execution of imported code.

```bash
readyagents import n8n exported.json --out workflows/migrated --json
readyagents import langgraph graph.py --out workflows/lg
readyagents import crewai crew.yaml --out workflows/crew
readyagents import trigger zap.json --out workflows/zap
readyagents import --explain n8n
```

Sources in this release: **n8n** JSON, **LangGraph** declarative Python (AST
only), **CrewAI** YAML or Python (AST only), and generic **trigger-action**
JSON (Zapier-shaped). Unknown export schema versions are refused.

## Mapping tables

Each source has a **data** table (`src/readyagents/importers/tables/*.json`):
source node kind → ReadyAgents type, fidelity, reason, nearest alternative.
Adding coverage is a table edit and a test, not a new parser. `--explain SOURCE`
prints the table and writes nothing.

## Fidelity report

Every source node is one of:

- `translated`
- `approximated` (what changed; structural diffs for branch / loop / parallel /
  sub-workflow / error handler / human step are always approximated)
- `unsupported` (why, and the nearest alternative)

Coverage is the share of nodes that are translated or approximated. The report
states that the result **must be tested**. Bias is toward `approximated`
whenever semantics differ.

## Failing stubs

An untranslatable node becomes a `calc` tool whose expression starts with
`UNSUPPORTED:` and names the original node. Run and dry-run **fail** at that
stub instead of succeeding with a string. It is never silently dropped.
Remove the stub to proceed. Representative examples keep stubs off the
executed path so a fully mapped import can still dry-run.

## Security

Python sources are **AST-parsed and never imported or `exec`'d**. A file whose
`import` would raise still parses. Non-statically-analysable constructs
(`exec`, `eval`, dynamic `add_node(name, …)`) are refused, not guessed.
Embedded scripts are text.

Credential **values** (`sk-`, `ghp_`, `AKIA`, PEM) are never written to the
workflow. The report redacts them and warns that the export file itself is a
secret. Names may be recorded as declared secret names.

Parsers bound size, nesting depth, and node count (`ImportBoundOversized` /
`ImportBoundDepth` / `ImportBoundNodes`). `--out` is workspace-confined.
Overwrite without `--force` is refused.

After emit, the workflow is schema-validated, `--dry-run` executed, and
rendered as Mermaid. Hosted runtimes of the source (n8n cloud, Zapier, LangGraph
platform) are named as unsupported, not faked.
