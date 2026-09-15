# Stability contract

## What the version number means

The package is `2.0.x`, classified `Development Status :: 4 - Beta`. The major
covers the **core contract only**: the public Python names, frozen formats, and
exit codes below. Breaking any of those requires a major bump with a migration
note; nothing else in a release can force one.

What the major does **not** cover:

- Extras. Every extra carries its own maturity tier (`stable`, `preview`, or
  `experimental`), and non-`stable` extras may change shape on a minor.
- The `v1` / `record_version: 1` labels on the frozen formats. Those are
  *format* versions, independent of the package major; they move only when the
  format itself breaks.
- Maturity. `2.x` + `Beta` says the core contract is written down and tested,
  not that the package is production-proven. Promotion to `Production/Stable`
  waits on external validation reports, not on time.

Minor releases are additive (new commands, new optional settings, new exported
names). Patch releases are bug fixes, docs, and additive `--json` keys. No
release ships without a user-recognizable reason; see `CONTRIBUTING.md`.

## Maturity tiers

Tiers describe *stability of shape*, not *quality of implementation*. A `preview`
or `experimental` feature works, is tested, and is documented; the tier only says
how likely its shape — command names, flags, file formats, node fields — is to
change. `experimental` code is tested code: the label records unproven use, not
missing tests.

| Tier | Meaning | Compatibility promise | Honest entry condition |
| --- | --- | --- | --- |
| `stable` | Core. Covered by the stability contract and `tests/test_public_api.py`. | Breaking changes only on a major, with a migration note. | Has been used by someone outside the author for a real task. |
| `preview` | Works, tested, documented. Shape may still change. | May change on a minor, with a changelog entry. | Has an end-to-end test and a doc page; no external user yet. |
| `experimental` | Shipped and tested, but unproven in use. | May change or move to a pack at any time. | Everything else. |

Nothing built in the 2026-09-09 → 2026-09-12 sprint window is `stable`, regardless
of test coverage: test coverage proves the code does what the author expected,
while `stable` claims it does what a stranger expected, and that claim requires a
stranger. See `docs/extras.md` for the per-feature catalogue; each feature page
carries a one-line tier banner and a `## Scope` section holding its scope limits.

Promotion is by evidence, not by time and not by polish. A tier moves up when an
external `I-ran-this` report exists, or the author has used the feature in a real
job for two weeks — and the changelog entry for the promotion names that
evidence. No named evidence, no promotion.

## Command tiers

Every one of the 57 top-level commands has exactly one tier. `stable` is the
pre-sprint core surface; `preview` has an end-to-end test, a doc page, and real
author use; `experimental` is everything else. Tiers are labels, not switches:
`experimental` commands run exactly as they run today.

| Command | Tier |
| --- | --- |
| `run` | stable |
| `resume` | stable |
| `runs` | stable |
| `new` | stable |
| `validate` | stable |
| `eval` | stable |
| `decide` | stable |
| `approvals` | stable |
| `policy` | stable |
| `spend` | stable |
| `mcp` | stable |
| `doctor` | stable |
| `init` | stable |
| `version` | stable |
| `a2a` | preview |
| `audit` | preview |
| `connectors` | preview |
| `env` | preview |
| `evidence` | preview |
| `identity` | preview |
| `import` | preview |
| `memory` | preview |
| `agents-md` | experimental |
| `attest` | experimental |
| `batch` | experimental |
| `bench` | experimental |
| `bundle` | experimental |
| `delegate` | experimental |
| `delegations` | experimental |
| `distill` | experimental |
| `event` | experimental |
| `feedback` | experimental |
| `graph` | experimental |
| `health` | experimental |
| `knowledge` | experimental |
| `lock` | experimental |
| `models` | experimental |
| `optimize` | experimental |
| `package` | experimental |
| `packs` | experimental |
| `promote` | experimental |
| `prompts` | experimental |
| `registry` | experimental |
| `rollback` | experimental |
| `sbom` | experimental |
| `schema` | experimental |
| `serve` | experimental |
| `sessions` | experimental |
| `sign` | experimental |
| `simulate` | experimental |
| `skills` | experimental |
| `studio` | experimental |
| `table` | experimental |
| `triggers` | experimental |
| `trust` | experimental |
| `verify` | experimental |
| `wake` | experimental |

The rest of this document is the core contract for this major, unchanged.

This document is the public Python API snapshot. Removing a name from
`readyagents.__all__` without updating this file is a failing test.

Semver here:

- **Patch**: bug fixes, docs, and additive `--json` keys.
- **Minor**: additive CLI commands, new optional settings, new exported names.
- **Major**: removals or behaviour changes to the names, formats, and exit codes below.

Deprecation window: at least one minor release plus a named removal version.
Nothing is dropped silently.

## Public Python names (`readyagents.__all__`)

```python
__all__ = [
    "__version__",
    "ApprovalRequired",
    "AuthorizationError",
    "BudgetExceeded",
    "CircuitOpen",
    "ConfigError",
    "IdentityError",
    "LLMError",
    "MCPError",
    "NodeError",
    "ReadyAgentsError",
    "RunawayGuard",
    "StructuredOutputError",
    "TemplateError",
    "ToolError",
    "TrustError",
    "WorkflowError",
]
```

Everything else, including `readyagents.replay` internals, is not stable.

## Frozen formats

- Workflow file format v1 (`schemas/workflow-v1.json`, schema `$id`).
- Run-record format `record_version: 1`. A 0.9-era record without that field still loads.
- CLI command names and exit codes (approval pause remains **2**).
- Documented `--json` envelope keys `ok` and `command` are stable; other keys are additive.
- Pack protocol (`get_pack()`) and tool/node type names.

Not stable: internal module paths, log line text, human console formatting.

## Recording default

Recording is **opt-in** (`readyagents run --record` or `READYAGENTS_RECORD=1`). A cassette
holds full prompts and completions. Failed-run auto-recording is rejected because it would
surprise operators and write secrets they did not choose to capture. A first-run log hint
points at `--record`.
