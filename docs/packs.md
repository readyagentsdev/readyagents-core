# Packs

ReadyAgents is **open-core**. This repository is the free engine. Commercial or extra capability layers are **packs**: installed Python packages that register extra tools, node types, and workflows.

Core runs with **zero packs**. First-party connectors (`rest`, `sql`, …) are
core tools, not packs; they still go through `dispatch_tool`. Existing packs
do not need to implement the connector contract.

## Protocol

A pack implements:

```python
from readyagents.packs import BasePack
from readyagents.tools import FunctionTool


class ContinuousPack(BasePack):
    name = "continuous"
    version = "1.0.0"

    def register_tools(self):
        return [
            FunctionTool(
                name="watch_queue",
                description="example always-on helper (not published)",
                handler=lambda: "not in core",
            )
        ]

    def register_nodes(self):
        return {}  # optional NodeHandler keyed by type name

    def register_workflows(self):
        return []  # optional bundled workflow paths or dicts

    def register_secrets(self):
        return []  # optional SecretsBackend objects (Vault/AWS belong here, not in core)

    def register_authorizers(self):
        return []  # optional RBAC hooks; core default is allow-all

    def register_tool_seals(self):
        # Optional cassette classification. Unclassified pack/MCP tools stay
        # unsealable. Core builtins (calc, json_*, now, http_get, read_file,
        # list_dir, write_file) cannot be overridden.
        return {"watch_queue": "unsealable"}
```

`FunctionTool(..., determinism="recomputed")` is the same declaration on the
tool. First declaration wins. Invalid values (`"sometimes"`) fail at pack
collect time. Old packs without `register_tool_seals` still load.

`register_nodes()` values should expose `type_name` and `execute(node, state, context)`.

`type: browser` is a core node. The **driver** (Playwright/Chromium/Selenium or
the in-process stub) is an optional pack so core stays free of a browser
engine. See [browser-use.md](browser-use.md). The example stub is
`examples/packs/browser_pack.py`.

## Discovery

Packs are loaded from the `readyagents.packs` [entry point](https://packaging.python.org/en/latest/specifications/entry-points/) group.

The optional Continuous pack is a **separate** Python distribution, `readyagents-pack-continuous`. It is not bundled with Core, not a Core extra, and not started by `import readyagents` or `readyagents run`. Install it from that pack's checkout, then use its own foreground command:

```toml
[project]
name = "readyagents-pack-continuous"

[project.entry-points."readyagents.packs"]
continuous = "readyagents_pack_continuous:get_pack"
```

```python
from readyagents_pack_continuous import get_pack

pack = get_pack()  # no scheduler, watcher, thread, or listener
```

```bash
readyagents-continuous serve continuous.yaml
```

See [continuous-pack.md](continuous-pack.md) for the boundary, install, and trigger semantics.

The engine calls `discover_packs()` at run start and merges tools/node handlers. No change to core YAML is required except using the new tool or node type names. Core's `readyagents.packs` entry-point group stays empty.

## Design rule

Packs **compose on top** of core. They must not fork the engine. Always-on / continuous execution, hosted control planes, inbound webhook listeners, and premium connectors belong in packs — not in `readyagents-core`.

An in-tree example connector (local, no network) lives at `examples/packs/connector_pack.py` and registers the `connector_ping` tool. Core’s `readyagents.packs` entry-point group stays empty.

Load a pack from a Python file without installing an entry point. The path is confined to the workspace (`READYAGENTS_WORKSPACE` or the current directory). `..`, symlink escapes, and paths such as `/etc/passwd` are refused.

```bash
readyagents run examples/connector_demo.yaml --pack examples/packs/connector_pack.py
readyagents packs --pack examples/packs/connector_pack.py
```

`--pack` is repeatable. `READYAGENTS_PACK` may hold one path, or several separated by `os.pathsep` or commas.

Under `--require-signed` (or policy `require_signed: true`) a local pack is
verified **before** it is imported — importing is executing. The bytes that
were digested are the bytes that run. See [supply-chain.md](supply-chain.md).
Signing proves who published the pack, not that it is safe.

List what is installed (plus any `--pack` / `READYAGENTS_PACK` modules):

```bash
readyagents packs
```
