# First ten minutes
> **Tier:** stable — core, covered by the stability contract. See [stability](stability.md).

No API keys. Current version is **2.0.3** (`readyagents version`). Install from PyPI
(`pip install readyagentsdev`) or from a clone (`pip install -e .`) as in the
[README](../README.md). The wheel ships `examples/`; one road below serves both.
The `2` versions the [core contract](stability.md#what-the-version-number-means)
only; extras carry their own maturity tiers.

## 1. Prove the engine works

```bash
readyagents doctor
readyagents new my-flow
readyagents run my-flow/workflow.yaml
readyagents new f --from-example calc_pipeline
readyagents run f/workflow.yaml
readyagents runs list
readyagents runs report <run_id>
readyagents new g --from-example list_dir
readyagents run g/workflow.yaml
```

`new --from-example` materializes any shipped example (`new --list-examples`
shows all of them), so a clone is never required for the rest of this page.

Windows: activate with `.venv\Scripts\activate` then the same commands. Attach
`readyagents doctor --json` if something fails.

`calc_pipeline` / `list_dir` and the `pipeline` scaffold are builtin tools only
(`calc`, `now`, `json_get`, `list_dir`). The HTML report is local. Nothing is
uploaded.

Runs are stored under `./.readyagents/runs` in the current directory;
`readyagents doctor` prints the resolved path and which source set it
(`default` / `READYAGENTS_HOME` / `config file`). If you ran a version before
this fix, your old runs may sit in `~/runs` — move them with
`mkdir -p .readyagents && mv ~/runs .readyagents/runs` (nothing is moved
automatically).

## 2. Pause is not a crash

```bash
readyagents new demo --from-example approval_gate
readyagents run demo/workflow.yaml
```

The CLI exits **2** and the run status is `paused`. That means a human gate is
waiting — a decision is pending — not that the install crashed.

```bash
readyagents resume <run_id> --approve gate
# or
readyagents resume <run_id> --reject gate
```

Same-shot: `readyagents run demo/workflow.yaml --approve gate`.

Or inject a JSON decision without `--approve` flags:

```bash
readyagents decide <run_id> --node gate --decision approve
```

One gate is enough. `gated_write` does `calc`, then a single
approval, then `write_file`. Exit **2** means the file is still absent.
`--approve gate` writes once. Reject never writes. That is not a rubber-stamp
prompt on every tool. Quorum, lazy deadlines, and delegation are opt-in:
[approvals.md](approvals.md).

```bash
readyagents new gw --from-example gated_write
readyagents run gw/workflow.yaml
readyagents resume <run_id> --approve gate
```

The local page `readyagents approvals serve --host 127.0.0.1 --port 8766` is
shipped (not Unreleased). It is not a hosted dashboard.

## 3. Start your own file

If you already ran section 1, pick a new directory name (`new` will not overwrite
`my-flow` or `demo`):

```bash
readyagents new starter
readyagents run starter/workflow.yaml
```

`pipeline` is keyless. The scaffold writes `workflow.schema.json` next to
`workflow.yaml` and a `yaml-language-server` modeline so VS Code (YAML
extension), Neovim, or JetBrains can complete `type`, `retry`, and node fields.
Offline: `readyagents schema --output .readyagents/workflow.schema.json`. See
[authoring.md](authoring.md).

Add an `agent` node only after you install an extra
(`pip install "readyagentsdev[openai]"` or `"readyagentsdev[anthropic]"`; from a
clone, `pip install -e ".[openai]"`) and put your own key in `.env`.

## Optional (2.0.3 surface)

**2.0.3** includes connectors, sovereign, memory, and A2A. Keyless examples
(each materialized first, same as above):

```bash
readyagents new ex-conn --from-example connector_rest && readyagents run ex-conn/workflow.yaml
readyagents connectors list
readyagents new ex-sov --from-example calc_pipeline && readyagents run ex-sov/workflow.yaml --sovereign
readyagents run ex-sov/workflow.yaml --estimate
readyagents new ex-eval --from-example eval/pass.yaml && readyagents eval ex-eval/workflow.yaml
readyagents new ex-mem --from-example memory_triage && readyagents run ex-mem/workflow.yaml
readyagents new ex-a2a --from-example a2a_delegate && readyagents run ex-a2a/workflow.yaml --dry-run
```

Memory is untrusted. A2A is a 0.3 JSON-RPC projection on the v1.0 well-known
path, not certification. `--sovereign` is in-process egress refuse, not an OS
sandbox.

## Next

- [Getting started](getting-started.md) — more examples, including LLM ones
- [Workflows](workflows.md) — YAML syntax
- [CLI](cli.md) — every command

## Scope

- No additional scope limits beyond the tier above.
