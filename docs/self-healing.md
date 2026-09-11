# Reliability and self-healing

`readyagents health` classifies failures into **deterministic fingerprints**,
clusters them across the existing run store, and ranks them by impact (count
and cost burned). A node may declare a **recovery** policy that responds per
failure class. Health scores can **gate** a node to a human. Nothing here
predicts failures, edits prompts, or ships telemetry.

A workflow without `recovery:` is unchanged. Existing retry, circuit-breaker,
and `fallback_models` behaviour remains the default.

## Fingerprints

A fingerprint is a pure function of typed error, node id, tool, provider, and a
normalised message. Variable parts (run ids, timestamps, addresses, token
counts, paths) are stripped by **data rules**
(`readyagents/health/normalize_rules.json`), then the shipped redactor runs.
The same bug on Tuesday and Thursday yields the same id.

```bash
readyagents health --json
readyagents health --workflow my-flow --window 50 --limit 64
```

Queries are bounded (`--limit` and a hard cap). There is no aggregator daemon
and no new store.

## Declared recovery

```yaml
- id: draft
  type: agent
  prompt: "..."
  retry: {max_attempts: 3}
  recovery:
    on:
      - {class: truncation, action: retry_with, max_tokens: 4000}
      - {class: rate_limit, action: backoff, seconds: 30}
      - {class: schema_violation, action: repair, max_repairs: 2}
      - {class: provider_error, action: fallback}
    health: {min_success_rate: 0.9, window: 50, below: gate}
```

Classes: `truncation`, `rate_limit`, `schema_violation`, `provider_error`.
Actions: `retry_with`, `backoff`, `repair`, `fallback`. An undeclared class
falls through to existing retry only. Every adaptation is recorded on the run
(`metadata.recovery`).

## Flaky vs broken

A node that fails intermittently on **identical cassette inputs** is flagged
**flaky**, not broken. Without a cassette the distinction is not claimed.

## Quarantine (fail-safe)

`health.below` must be `gate`. Skip/open is a schema error. When the success
rate over the window is below `min_success_rate`, the node opens an approval
(or takes a declared `fallback` successor). It never silently drops the node.
The threshold is a numeric literal — not a template and not influenced by
untrusted input.

## Root-cause bundle

```bash
readyagents health explain FINGERPRINT --out explain/ --yes
```

Writes a confined, redacted, audited directory: failing runs, cassette
excerpts, node inputs, the error, and a diff against the last successful run
of that node. `--yes` is required (diagnostic data). A cluster without a
fixture prints a `readyagents runs freeze` hint.

This is not a hosted reliability service and it does not change workflow YAML.
