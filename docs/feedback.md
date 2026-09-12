# Feedback and dataset export

Corrections your team already makes — edits, ratings, labels, and weak
implicit signals — can be exported as a **local** dataset. This is not
fine-tuning, not a hosted dataset service, and not a quality or
model-improvement claim. An export is **production data in a portable file**.

A workflow without a `feedback:` block is unchanged.

```yaml
- id: review
  type: approval
  approver_roles: [editor]
  feedback:
    allow_edit: true
    rating: {scale: 1-5}
    labels: [wrong_fact, wrong_tone, unsafe, incomplete]
    consent: internal_training
```

```bash
readyagents resume RUN_ID --approve review --actor editor --edit "corrected" --rating 4 --feedback-label wrong_fact --reason "fact"
readyagents feedback export --format eval --out datasets/corrections.yaml --yes --json
readyagents eval datasets/corrections.yaml
readyagents feedback stats --by node --json
```

## Consent

Consent is a **recorded run policy** (`data_policy.scopes`), never a CLI flag.
`--scope` filters which consented scope to include; it does not grant consent.
An unconsented run is excluded from **eval, sft, and dpo** under every flag,
and `excluded` / `excluded_unconsented` are reported.

## Redaction and identity

The shipped redactor **re-runs** at export. A residual known secret excludes
the **whole** record (`excluded_secret`), never a partial row. Exported
examples carry provenance (run id, node id, model, timestamp) and the
correction actor **role**, never reviewer identity.

Export paths are confined to the workspace, warn before writing, and are
audited. `--yes` acknowledges the warning in non-interactive use.

## Formats

- **eval** (default): a `cases:` suite that runs under `readyagents eval` offline.
- **sft**: instruction/response pairs.
- **dpo**: preference pairs derived from original-versus-edit (the stored diff).

## Stats

`readyagents feedback stats --by node|model|label|week` reports correction
rates with **sample sizes** (`n` / `sample_size`). `significance` is always
`null`. A tiny n is still a count, not a finding.

## Implicit signals

Guardrail rejections, contract repairs, refusals, retries, and fallbacks are
stored as `kind: implicit` with a `signal` field, distinct from human labels.
