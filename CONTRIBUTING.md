# Contributing

Thanks for improving ReadyAgents Core.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

```bash
make test
make lint
make run-example
make smoke
```

`make lint` is `ruff check` plus `ruff format --check`. `make smoke` is the CI keyless set (`list_dir`, `eval`, `--pack`, dry-run, resume, approval, parallel, include). `make run-example` is the three-file shortcut.

GitHub CI's Ruff step matches `make lint`: `ruff check` plus `ruff format --check` on `src` and `tests` ([#32](https://github.com/readyagentsdev/readyagents-core/pull/32)).

Or:

```bash
python -m pytest
ruff check src tests
ruff format --check src tests
readyagents run examples/calc_pipeline.yaml
readyagents run examples/approval_gate.yaml --approve gate
readyagents run examples/composed_gate.yaml --approve gate
```

Tests must not use the network or real API keys. Mock LLM providers.
Approval nodes must be driven with `--approve` / `--reject`, `readyagents decide --file`, or `ExecutionContext.decisions` — never a blocking prompt.

Use `readyagents.testing` (`ScriptedLLM`, `RecordedLLM`, `run_workflow_spec`, `run_eval`) instead of live vendors.

## Guidelines

- Keep the core small. Always-on / continuous systems belong in a **pack**, not this repo.
- Typed errors over generic exceptions.
- BYOK only — never commit secrets. `.env`, `.env-ai`, and `.keys/` are gitignored.
- Apache-2.0 for original contributions unless you say otherwise in the PR.

## Pull requests

1. One focused change per PR
2. Tests for engine/schema/tool behavior you touch
3. No generated secrets, no run artifacts under `.readyagents/`

## Branches

One branch per shippable change, merged and deleted — the repo auto-deletes
head branches on merge. A branch that outlives its PR gets tagged
`archive/<name>` and deleted; nothing is lost, and the branch list stays
readable. Never force-push a shared branch.

## Release criteria

A release is a user-recognizable event, not a commit with a tag. All three hold
before any release:

1. **Green CI on the release commit.** The `2.0.0` tag was cut on a commit
   whose CI had failed; `publish.yml` now refuses to publish unless every CI
   check run on the tagged commit is green. Never bypass the gate with an
   empty release commit — fix the red first. Publish PyPI first
   (`workflow_dispatch` on `publish.yml` while CI is green), then cut the
   `v*` tag / GitHub Release so MCP Registry publish can see the PyPI
   version; rerun MCP publish if it raced.
2. **The release names its audience.** If the changelog entry does not say what
   a user can now do that they could not before, it is not a release; it is a
   commit. No release without a reason a user would recognise.
3. **A major requires an external user.** The major versions the core contract
   ([stability](docs/stability.md#what-the-version-number-means)); bumping it
   needs evidence of outside use — an `I-ran-this` report from someone other
   than the author — the same bar the `stable` tier sets for promotion.

Standing honesty re-verify (every release; not a build step):

- Re-verify the external quotes on [docs/why-readyagents.md](docs/why-readyagents.md)
  and refresh the "as of" dates.
- Re-verify the MCP paste catalog host config shapes and refresh their
  verification dates ([docs/mcp-catalog.md](docs/mcp-catalog.md) when it exists,
  otherwise [docs/mcp-plugs.md](docs/mcp-plugs.md)).
