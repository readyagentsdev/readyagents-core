# First ten minutes

No API keys. Current version is **0.8.0**. The package is **not on PyPI** — install from a clone (`pip install -e .`) as in the [README](../README.md).

## 1. Prove the engine works

```bash
readyagents doctor
readyagents run examples/calc_pipeline.yaml
readyagents runs list
readyagents runs report <run_id>
readyagents run examples/list_dir.yaml
```

Windows: activate with `.venv\Scripts\activate` then the same commands. Attach `readyagents doctor --json` if something fails.

`calc_pipeline` and `list_dir` are builtin tools only (`calc`, `now`, `json_get`, `list_dir`). The HTML report is local. Nothing is uploaded.

## 2. Pause is not a crash

```bash
readyagents run examples/approval_gate.yaml
```

The CLI exits **2** and the run status is `paused`. That means a human gate is waiting — a decision is pending — not that the install crashed.

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

One gate is enough. `examples/gated_write.yaml` does `calc`, then a single approval, then `write_file`. Exit **2** means the file is still absent. `--approve gate` writes once. Reject never writes. That is not a rubber-stamp prompt on every tool.

```bash
readyagents run examples/gated_write.yaml
readyagents resume <run_id> --approve gate
```

## 3. Start your own file

```bash
readyagents new my-flow
readyagents run my-flow/workflow.yaml
```

`pipeline` is keyless. The scaffold writes `workflow.schema.json` next to `workflow.yaml` and a `yaml-language-server` modeline so VS Code (YAML extension), Neovim, or JetBrains can complete `type`, `retry`, and node fields. Offline: `readyagents schema --output .readyagents/workflow.schema.json`. See [authoring.md](authoring.md).

Add an `agent` node only after you install an extra from this checkout (`pip install -e ".[openai]"` or `".[anthropic]"`) and put your own key in `.env`.

## Next

- [Getting started](getting-started.md) — more examples, including LLM ones
- [Workflows](workflows.md) — YAML syntax
- [CLI](cli.md) — every command
