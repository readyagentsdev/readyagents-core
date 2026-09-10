# First ten minutes

No API keys. Current version is **1.9.0** (`readyagents version`). Install from PyPI
(`pip install readyagentsdev`) or from a clone (`pip install -e .`) as in the
[README](../README.md). The wheel does **not** ship `examples/`.

## 1. Prove the engine works

From PyPI (no repo):

```bash
readyagents doctor
readyagents new my-flow
readyagents run my-flow/workflow.yaml
readyagents runs list
```

From a clone (this repository):

```bash
readyagents doctor
readyagents run examples/calc_pipeline.yaml
readyagents runs list
readyagents runs report <run_id>
readyagents run examples/list_dir.yaml
```

Windows: activate with `.venv\Scripts\activate` then the same commands. Attach
`readyagents doctor --json` if something fails.

`calc_pipeline` / `list_dir` and the `pipeline` scaffold are builtin tools only
(`calc`, `now`, `json_get`, `list_dir`). The HTML report is local. Nothing is
uploaded.

## 2. Pause is not a crash

From a clone:

```bash
readyagents run examples/approval_gate.yaml
```

From PyPI, `readyagents new my-flow --template gated` then run that workflow.

The CLI exits **2** and the run status is `paused`. That means a human gate is
waiting — a decision is pending — not that the install crashed.

```bash
readyagents resume <run_id> --approve gate
# or
readyagents resume <run_id> --reject gate
```

Same-shot: `readyagents run examples/approval_gate.yaml --approve gate`.

Or inject a JSON decision without `--approve` flags:

```bash
readyagents decide <run_id> --node gate --decision approve
```

One gate is enough. `examples/gated_write.yaml` does `calc`, then a single
approval, then `write_file`. Exit **2** means the file is still absent.
`--approve gate` writes once. Reject never writes. That is not a rubber-stamp
prompt on every tool. Quorum, lazy deadlines, and delegation are opt-in:
[approvals.md](approvals.md).

```bash
readyagents run examples/gated_write.yaml
readyagents resume <run_id> --approve gate
```

The local page `readyagents approvals serve --host 127.0.0.1 --port 8766` is
shipped (not Unreleased). It is not a hosted dashboard.

## 3. Start your own file

```bash
readyagents new my-flow
readyagents run my-flow/workflow.yaml
```

`pipeline` is keyless. The scaffold writes `workflow.schema.json` next to
`workflow.yaml` and a `yaml-language-server` modeline so VS Code (YAML
extension), Neovim, or JetBrains can complete `type`, `retry`, and node fields.
Offline: `readyagents schema --output .readyagents/workflow.schema.json`. See
[authoring.md](authoring.md).

Add an `agent` node only after you install an extra
(`pip install "readyagentsdev[openai]"` or `"readyagentsdev[anthropic]"`; from a
clone, `pip install -e ".[openai]"`) and put your own key in `.env`.

## Optional (clone / Unreleased)

Tagged **1.9.0** is the connector suite. This checkout also has **Unreleased**
A2A and memory — not on the 1.9.0 tag. From a clone, keyless:

```bash
readyagents run examples/connector_rest.yaml
readyagents connectors list
readyagents run examples/calc_pipeline.yaml --sovereign
readyagents run examples/calc_pipeline.yaml --estimate
readyagents eval examples/eval/pass.yaml
readyagents run examples/memory_triage.yaml
readyagents run examples/a2a_delegate.yaml --dry-run
```

Memory is untrusted. A2A is a 0.3 JSON-RPC projection on the v1.0 well-known
path, not certification. `--sovereign` is in-process egress refuse, not an OS
sandbox.

## Next

- [Getting started](getting-started.md) — more examples, including LLM ones
- [Workflows](workflows.md) — YAML syntax
- [CLI](cli.md) — every command
