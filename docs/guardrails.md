# Guardrails and output contracts

**Unreleased on this checkout — not on the 1.9.0 tag.** Opt-in `contract:` on a
value-producing node. The engine enforces a declared structural schema and
named content rules. This does **not** make outputs safe. There is no shipped
toxicity, bias, or safety classifier and no hosted moderation API.

```yaml
- id: summarise
  type: agent
  prompt: "Summarise the ticket."
  contract:
    schema: {type: object, required: [summary, ticket_id]}
    rules:
      - deny_regex: "\\b\\d{16}\\b"
        on_fail: redact_and_continue
      - require_citation: {from: ticket_id}
      - max_chars: {field: summary, value: 1200}
    on_invalid: repair
    max_repairs: 2
    on_exhausted: gate
```

A workflow with no `contract:` keeps today's `output_schema` fail-closed
behaviour (no repair). See [examples/contract_reshape.yaml](../examples/contract_reshape.yaml).

## What is enforced

- **Schema** — JSON Schema on the node output. Deterministic repairs (trailing
  commas, fenced JSON, a single wrapping object) run **before** any extra model
  call. Model re-prompts get the validation error verbatim, are bounded by
  `max_repairs` (1–5, required when `on_invalid: repair`), and are metered.
- **Content rules**, ordered, each recorded by name: `deny_regex`, literal
  `deny`, `require_citation`, `language`, `max_chars`, `pii` (existing redaction
  detectors). `deny_regex` is complexity-bounded and time-limited.
- **Refusal** — a prose refusal is distinct from malformed JSON (`on_refusal`).
- **Judge** — opt-in only, never the only check, never on by default. The judge
  sees the output as delimited untrusted text with a fixed rubric and records a
  score.

## Actions (declared, never inferred)

| Action | Effect |
| --- | --- |
| `fail` | Typed error; default when a rule omits `on_fail`. |
| `repair` | Structural only (`on_invalid`). Requires `max_repairs` and `on_exhausted`. |
| `fallback` | One pass on `fallback_models`. |
| `gate` | Pause; the offending output is shown **redacted**. |
| `redact_and_continue` | Redact, continue, and **record** the redaction. Never silent. |

Malformed contracts fail at `readyagents validate`, not at run. Unknown actions
fail closed. `on_exhausted` cannot be `repair`.

## Recording

Every rejection, repair attempt, named rule result, and disposition is stored on
the run record (`metadata.contracts`), the audit trail, and the cassette.
`runs replay --offline` replays the contract node without re-calling the model.
Rejected content is redacted and truncated in logs, errors, records, and the
gate prompt.

This feature enforces **declared rules only**.
