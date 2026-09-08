# CLI

The `readyagents` command is a [Typer](https://typer.tiangolo.com/) app.

```bash
readyagents --help
readyagents --version
readyagents --log-format json run examples/calc_pipeline.yaml
```

## `readyagents init`

Writes `.env` from `.env.example` when `.env` is missing. Prints next steps. Does not overwrite an existing `.env`.

```bash
readyagents init
readyagents init --dest .env
```

## `readyagents new [name]`

Write a starter project: `workflow.yaml`, `README.md`, and `.env.example`. Refuses to overwrite those files if they already exist.

```bash
readyagents new my-flow
readyagents run my-flow/workflow.yaml
readyagents new demo --template gated
readyagents run demo/workflow.yaml --approve gate
```

Templates: `basic`, `approval`, `research` (parallel + approval), `pipeline` (default; calc/json/condition), `review` (read_file + approval), `foreach`, `agent-tools`, `gated`.

## `readyagents validate PATH`

Loads YAML/JSON and validates the Pydantic schema (unique node ids, dangling `next` / edges, cycles over `next` / `then` / `else` / `edges`, required fields per type, unique parallel branch ids). Does not call tools or LLMs. `--json` prints `{ok, command, name, start, nodes}` (or `{ok: false, command, error, message}` on failure). The table shows `then:` / `else:` routing, not only `next`.

## `readyagents eval PATH`

Score fixture workflows from a suite file using the same local harness as `readyagents.testing.run_eval`. **No network and no API keys** — cases must be keyless fixtures (builtin tools, transforms, recorded/scripted LLM), not live vendors.

```bash
readyagents eval examples/eval/pass.yaml
readyagents eval examples/eval/fail.yaml
readyagents eval examples/eval/pass.yaml --json
```

The suite is YAML or JSON with a `cases:` list. Each case has `name`, `workflow` (a path relative to the suite file, or an inline workflow mapping), and optional `inputs`, `decisions`, `expect_status` (default `succeeded`), `expect_outputs`, and `expect_contains`. An empty `cases:` list is refused.

Human output is one `PASS name` / `FAIL name: reason` line per case, then `passed=N failed=M`. `--json` prints `{ok, command, passed, failed, results}` with `command` `"eval"` and `results` as `{name, passed, reason}` rows. Exit `0` if every case passes, `1` if any fail or the suite cannot be loaded. A missing suite file is `ConfigError` (exit 1), same as a missing workflow; `--json` then prints `{ok: false, command: "eval", error, message}`.

## `readyagents run PATH`

```bash
readyagents run examples/calc_pipeline.yaml
readyagents run examples/list_dir.yaml
readyagents run examples/approval_gate.yaml --approve gate
readyagents run examples/research_brief.yaml --input topic="mcp servers"
readyagents run examples/support_triage.yaml -i message="billing question"
readyagents run examples/code_review.yaml --dry-run
readyagents run examples/research_brief.yaml --no-persist
```

| Flag | Meaning |
| --- | --- |
| `--input KEY=VALUE` / `-i` | Repeatable. `true`/`false`/`null` and integers are coerced |
| `--dry-run` | No LLM, no `http_get` or `write_file` (including when an agent allowlist names them); templates still interpolate. Read-only tools (`now`, `calc`, `json_get`, `read_file`, `list_dir`) still run. |
| `--no-persist` | Skip writing `.readyagents/runs/<run_id>.json` |
| `--approve NODE` | Supply an approval-node decision (repeatable) |
| `--reject NODE` | Reject an approval node (repeatable) |
| `--resume RUN_ID` | Continue a paused/failed run instead of starting fresh |
| `--json` | Print the run record as JSON on stdout (no tables; scripts/CI) |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--log-format` | `text` (default) or `json` (machine-parseable events with `run` / `node`) |
| `--decision-file PATH` | JSON approval decisions (same shapes as `readyagents decide --file`) |
| `--actor NAME` | Actor id for RBAC hooks (`READYAGENTS_ACTOR`) |
| `--no-cache` | Skip the local LLM response cache |
| `--pack PATH` | Load a local pack `.py` (repeatable). Confined to the workspace. Env: `READYAGENTS_PACK` |

Exit code `1` on validation or execution errors, including a missing workflow file (`ConfigError`). Exit code `2` is reserved for an **approval** node pausing for a decision. The CLI prints `ErrorClass: message` rather than a full traceback. Logs include `run=<id>` and `node=<id>`.

Failed runs print the **node timeline**, `run_id`, and a `readyagents resume RUN_ID` hint (same idea as approval pauses). `--json` is an envelope with additive `ok` and `command` plus existing keys (`run_id`, `error`, `message`, `run`). Pause is exit 2 and still includes `run_id`. `resume` and `runs replay` accept `--json` too. JSON is written without Rich markup, so values like `[dry-run]` stay intact.

State is persisted after **each** successful node (unless `--no-persist`).

## `readyagents resume RUN_ID`

Resume a paused or failed run from the last successful node. Uses the workflow path stored on the run record.

```bash
readyagents resume abcdef --approve gate
readyagents resume abcdef --workflow examples/approval_gate.yaml --reject gate
readyagents resume abcdef --json
readyagents resume abcdef --decision-file decision.json
```

## `readyagents decide RUN_ID`

Inject an approval decision from outside the CLI (a pack, a ticket webhook, a file drop) and resume.

Core still does not auto-start a listener. An optional foreground MCP HTTP door (`readyagents mcp serve --transport streamable-http`) exposes `POST /runs/{id}/decide`; see [mcp.md](mcp.md). A pack can still receive a webhook and call this CLI.

```bash
readyagents decide abcdef --node gate --decision approve
readyagents decide abcdef --file decision.json
```

Accepted JSON shapes: `{"gate": "approve"}`, `{"node_id": "gate", "decision": "approve"}`, `{"decisions": {"gate": "approve"}}`, or a list of those objects.

## `readyagents runs list`

List persisted runs (newest first). Each line includes `run_id`. `--json` prints a JSON array. Filters: `--status`, `--workflow`, `--limit`.

## `readyagents runs show RUN_ID`

Show status, pending node (if paused), **pending prompt** (HITL), **node timeline**, inputs, and outputs. `readyagents runs inspect RUN_ID` is an alias. `--json` prints the stored run record including `pending` and per-node `tool_rounds`. Missing or ambiguous ids print `{ok: false, error, message, run_id}` (exit 1).

## `readyagents runs replay RUN_ID`

Start a **new** run with the stored workflow path and inputs (not a resume).

## `readyagents runs delete RUN_ID`

Delete one local run JSON file. Requires `--yes`.

```bash
readyagents runs delete abcdef --yes
```

## `readyagents runs gc`

Delete succeeded/failed/cancelled run files. **Paused** runs are kept unless `--include-paused`. Requires `--yes`. `--keep N` leaves the newest N runs.

```bash
readyagents runs gc --yes
readyagents runs gc --yes --status succeeded --keep 20
```

Ctrl-C during a persisted run stores status `cancelled` (not leftover `running`).

## `readyagents runs report RUN_ID`

Write a local HTML summary (timeline, usage, outputs). Open the file in a browser.

```bash
readyagents runs report <run_id>
readyagents runs report <run_id> --out /tmp/run.html
```

## `readyagents mcp serve`

MCP server. Requires `pip install -e ".[mcp]"`. **Stdio is the default** and needs no new flag. See [mcp.md](mcp.md).

```bash
readyagents mcp serve
readyagents mcp serve --transport stdio
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
```

| Flag | Meaning |
| --- | --- |
| `--transport` | `stdio` (default) or `streamable-http` |
| `--host` | HTTP bind address (default `127.0.0.1`). HTTP only. v0.9 rejects non-loopback binds. |
| `--port` | HTTP port (default `8765`, range 1–65535). HTTP only. |
| `--auth` | `token` (default) or `none`. `none` is allowed only on an exact loopback bind and prints a warning. |
| `--token-env` | Env var that holds the bearer token (default `READYAGENTS_MCP_TOKEN`). There is **no** token-value CLI flag. |
| `--max-concurrent-runs` | In-process executor cap (default `4`). HTTP only. |
| `--max-pending-runs` | Queue cap (default `32`); extra `POST /runs` return `429` without a record. HTTP only. |

If `--auth token` and the env var is empty, the process generates at least 256 bits of entropy and prints the token once to stderr. It is never persisted or logged.

`streamable-http` mounts official MCP Streamable HTTP at `/mcp` and the ReadyAgents `/runs` extension on the same loopback listener. `/runs` is not an MCP JSON-RPC method and is not official MCP Tasks. Stopping the command stops both the listener and the in-process executor; work does not survive process death. A keyless `/runs` client is `examples/mcp_http_client.py` (does not start the server).

## `readyagents packs`

Lists packs discovered via entry points. Empty when only core is installed. `--json` prints `{ok, packs}` (or `{ok: false, error, message}` if a pack fails to load).
A local `.py` loads with `--pack PATH`, for example `readyagents packs --pack examples/packs/connector_pack.py`.

## `readyagents version`

Prints the package version.

## Python API

```python
from pathlib import Path
from readyagents.workflow.runner import run_workflow_file
from readyagents.testing import ScriptedLLM, run_workflow_spec

state = run_workflow_file(Path("examples/calc_pipeline.yaml"), persist=False)
print(state.status, state.output_keys)

state = run_workflow_spec(
    {"name": "t", "nodes": [{"id": "a", "type": "agent", "prompt": "hi", "output_key": "t"}]},
    llm=ScriptedLLM().enqueue("ok"),
)
```
