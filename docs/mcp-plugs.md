# MCP paste catalog (client plugs)
> **Tier:** stable — documentation only. See [stability](stability.md). Companion to [mcp.md](mcp.md).

Curated **one-shot / client-side** MCP plugs for local workflow YAML. Current package **2.0.5**: `pip install "readyagentsdev[mcp]"`.

| This is | This is not |
| --- | --- |
| Snippets you paste under a workflow's `mcp_servers:` for **that run** | A marketplace, plaza, or hosted registry |
| Stdio children launched with the run, torn down after | Always-on daemons, watchers, or schedulers |
| Discoverability for careful engineers ("what can I attach?") | Certification, endorsement, or supply-chain blessing |
| Companion to `readyagents mcp probe` (read-only discover) | A substitute for reading the server's own docs |

Prefer ReadyAgents builtins (`list_dir`, `read_file`, `write_file`, `http_get` opt-in, …) unless you need a remote-shaped or richer MCP surface.

## How to attach

```bash
pip install "readyagentsdev[mcp]"
# or: pip install -U "readyagentsdev[mcp]"
readyagents version   # expect 2.0.5
```

1. Paste a block under top-level **`mcp_servers:`** in your workflow YAML.
2. Reference tools as **`servername.toolname`** (product registers MCP tools that way; they cannot shadow sandbox builtins).
3. **Probe before trust.** `readyagents mcp probe` is **HTTP(S)-only** (read-only discover / initialize; never calls a tool). Stdio plugs are not probe URLs — use the server's docs, MCP Inspector, or a tiny dry-run that only lists tools. If you temporarily expose a **loopback** Streamable HTTP door (`127.0.0.1` only), probe that URL.
4. These children live for **one workflow run** — same lifecycle as a foreground `mcp serve` door.

### Field names (product)

| Surface | Name | Notes |
| --- | --- | --- |
| Workflow YAML | **`mcp_servers`** | snake_case. Not `mcpServers` (host JSON like Claude Desktop). |
| Per server | **`command`** (required), **`args`**, **`env`**, **`cwd`** | `MCPServerSpec` in 2.0.x — **stdio only**. No `url` / `transport` on the client side. |
| Tool id | **`{name}.{tool}`** | e.g. `filesystem.list_directory` |
| Extra | **`[mcp]`** | Declaring `mcp_servers` without the extra → clear `MCPError`. |
| Probe | `readyagents mcp probe URL` | http(s) only; optional bearer via `READYAGENTS_MCP_TOKEN`. |
| Secrets | `mcp_servers.<name>.env` | Stdio children do **not** inherit `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` unless set here. `cwd` cannot escape the workspace. |

## Paste plugs

Placeholders like `/path/to/...` must stay **inside** your ReadyAgents workspace (or be relative so `cwd`/args resolve under it).

### 1. `filesystem` — directory-scoped file ops (Node)

**What it does:** Read/write/list/search files under explicitly allowed directories.  
**Verified:** `@modelcontextprotocol/server-filesystem` (npm; official reference server).  
**When to use:** Need MCP-shaped FS tools beyond builtins, or a host already expects this server. Prefer builtins for simple sandbox I/O.  
**Safety:** Arg paths are the sandbox boundary — do not point at `$HOME`, secrets dirs, or multi-tenant roots. Writes are destructive. Prefer a single project subdir.  
**Probe tip:** Stdio-only. Confirm package + allowed dirs before run; not a remote URL for `mcp probe`.

```yaml
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

### 2. `git` — local repo read/status/diff/commit tools (Python)

**What it does:** `git_status`, diffs, log, branch, optional commit/add — against a local repository.  
**Verified:** `uvx mcp-server-git` / PyPI `mcp-server-git` (official reference).  
**When to use:** One-shot review/diff workflows over a checkout already on disk.  
**Safety:** Can mutate the repo (commit/add/reset/checkout). Point `--repository` only at an intended workspace path. No tokens in YAML.  
**Probe tip:** Stdio. Dry-run read-only tools (`git_status` / `git_log`) first.

```yaml
mcp_servers:
  git:
    command: uvx
    args: ["mcp-server-git", "--repository", "."]
```

### 3. `fetch` — fetch URL → markdown (Python)

**What it does:** Pull a public URL and return extracted markdown (optional length/window).  
**Verified:** `uvx mcp-server-fetch` / PyPI `mcp-server-fetch` (official reference).  
**When to use:** Ingest public docs into a one-shot pipeline. Prefer builtin `http_get` (opt-in + private/loopback blocked) when that is enough.  
**Safety:** **SSRF / exfil risk** — the child can reach the network. Do not feed untrusted URLs. No secrets in args.  
**Probe tip:** Stdio. Treat as untrusted egress; review tool schemas before production-ish runs.

```yaml
mcp_servers:
  fetch:
    command: uvx
    args: ["mcp-server-fetch"]
```

### 4. `memory` — local knowledge-graph memory (Node)

**What it does:** Persistent entity/relation/observation graph on disk (JSONL).  
**Verified:** `@modelcontextprotocol/server-memory` (npm; official reference).  
**When to use:** Cross-step notes inside a workspace for a batch of one-shot runs — still client-side, not a hosted memory service.  
**Safety:** Defaults may write beside the server install; set `MEMORY_FILE_PATH` under the workspace. Do not store secrets in the graph.  
**Probe tip:** Stdio. After first run, confirm the JSONL path is where you intended.

```yaml
mcp_servers:
  memory:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]
    env:
      MEMORY_FILE_PATH: "./.readyagents-memory.jsonl"
```

### 5. `time` — timezone now + convert (Python)

**What it does:** `get_current_time` / `convert_time` with IANA timezones.  
**Verified:** `uvx mcp-server-time` / PyPI `mcp-server-time` (official reference).  
**When to use:** Explicit TZ math in agent tools; otherwise builtin `now` (UTC ISO) may suffice.  
**Safety:** Low risk (no network, no FS writes of note). Still a third-party process — pin/upgrade consciously.  
**Probe tip:** Stdio; optional `--local-timezone=Europe/Istanbul` in `args` if you must override detection.

```yaml
mcp_servers:
  time:
    command: uvx
    args: ["mcp-server-time"]
```

### 6. `sequential-thinking` — structured multi-step reasoning tool (Node)

**What it does:** Exposes `sequential_thinking` for stepwise / revisable problem decomposition.  
**Verified:** `@modelcontextprotocol/server-sequential-thinking` (npm; official reference).  
**When to use:** Planning/debug workflows where the host should iterate thoughts as tool calls.  
**Safety:** Low direct I/O risk; can amplify token spend via many tool rounds — use runaway/budget guards.  
**Probe tip:** Stdio. Confirm the tool appears once before relying on multi-round behavior.

```yaml
mcp_servers:
  sequential-thinking:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-sequential-thinking"]
```

### 7. `sqlite` — local SQLite query/schema tools (Python) — ARCHIVED upstream

**What it does:** SELECT/write/create/list/describe against a local `.db` file.  
**Verified command still on PyPI:** `uvx mcp-server-sqlite --db-path …`.  
**Upstream status:** Official reference moved to **servers-archived** — treat as **legacy / maintenance-risk**, not an active steering-group reference.  
**When to use:** One-shot analytics on a **workspace-local** DB you already trust. Prefer application-owned SQL for long-term support.  
**Safety:** Write tools can destroy data. Keep `--db-path` inside the workspace; never point at production DBs with secrets.  
**Probe tip:** Stdio. Start with `list_tables` / `read_query` only.

```yaml
mcp_servers:
  sqlite:
    command: uvx
    args: ["mcp-server-sqlite", "--db-path", "./data/app.db"]
```

### 8. `readyagents` (dogfood) — call this toolkit as an MCP child (stdio)

**What it does:** Nested stdio MCP exposing ReadyAgents builtins + `run_workflow` (same as `readyagents mcp serve`).  
**Verified:** Product docs / `MCPServerSpec` pattern (`command: readyagents`, `args: ["mcp", "serve"]`). Requires `[mcp]` and `readyagents` on `PATH`.  
**When to use:** Composition experiments / "workflow calls toolkit over MCP" — still one-shot for the parent run.  
**Safety:** Nested `run_workflow` is still sandboxed to workspace; do not pass provider keys in tool JSON. Avoid recursive runaway (parent budget + child).  
**Probe tip:** For the **HTTP** door (loopback only), not for this stdio paste:

```bash
readyagents mcp serve --transport streamable-http --host 127.0.0.1 --port 8765
# other terminal (token from stderr / READYAGENTS_MCP_TOKEN):
readyagents mcp probe http://127.0.0.1:8765/mcp
```

```yaml
mcp_servers:
  readyagents:
    command: readyagents
    args: ["mcp", "serve"]
```

## Minimal workflow sketch

```yaml
name: mcp-filesystem-list
version: "1"
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
nodes:
  - id: list
    type: tool
    tool: filesystem.list_directory
    arguments:
      path: "."
```

(`type: tool` + `server.tool` matches the docs sketch; agent nodes can also list MCP tools under `tools:` once the client session registers them.)

## Honest limits

- **Client YAML is stdio-only** in 2.0.x (`command`/`args`/`env`/`cwd`). Loopback HTTP is for **serving / probing**, not for pasting a `url:` into `mcp_servers`.
- **Not a registry.** Names verified against public MCP reference READMEs + npm/PyPI on 2026-09-18; re-check before you ship. Marked ARCHIVED where upstream archived.
- **npx/uvx** pull at run time — supply-chain aware teams should pin versions / vendor binaries.
- GitHub / SaaS MCP servers are intentionally omitted here (secrets, always-on temptation). Add only with `env` from local secret hooks, never committed tokens.

## Scope

- Documentation and paste snippets only. No engine change. Not a hosted MCP catalog.
