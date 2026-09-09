# Authoring workflows

ReadyAgents validates workflow files with Pydantic. The JSON Schema shipped with Core is a **derived authoring aid** — completion, enum hints, and inline squiggles in an editor. It is not a second runtime. `readyagents validate` / `readyagents run` still use the models in `WorkflowSpec`.

## Get the schema

```bash
readyagents schema
readyagents schema --output .readyagents/workflow.schema.json
readyagents schema --check schemas/workflow-v1.json
```

`--output` refuses to overwrite without `--force`, refuses directories, and will not follow a symlink out of the workspace. The command does not fetch URLs and does not execute a workflow.

The checked-in file is `schemas/workflow-v1.json`. It also ships in the wheel and the sdist. CI fails if generated output drifts from that file.

The document's `$id` is `https://readyagents.dev/schema/workflow/v1.json`. That string is an **identifier**. The website does not serve the file today (the URL 404s). Scaffolds therefore point at a local file, not at that URL.

The `v1` path tracks the **workflow file format**, not the package version. Additive changes keep `v1`. A breaking change to the file format would mint `v2` and ship both for at least one minor release. `x-readyagents-version` records which Core release produced a local copy.

## Editor setup

`readyagents new` writes `workflow.schema.json` next to `workflow.yaml` and starts the YAML file with:

```yaml
# yaml-language-server: $schema=./workflow.schema.json
```

JSON workflows may carry a top-level `"$schema"` key with the same relative path. ReadyAgents ignores that key at load time and never fetches it.

### Offline (no URL)

```bash
readyagents schema --output .readyagents/workflow.schema.json
```

Then point the modeline at the relative file:

```yaml
# yaml-language-server: $schema=./.readyagents/workflow.schema.json
```

### VS Code

Install [YAML (Red Hat)](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml). The modeline is enough for a scaffolded project. To map every workflow in a repo without a modeline, add to `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "./.readyagents/workflow.schema.json": ["*.yaml", "*.yml"]
  }
}
```

Use a tighter glob if the repo mixes unrelated YAML.

### Neovim

Use `yaml-language-server` through nvim-lspconfig, coc-yaml, or an equivalent client. The same modeline works. For a buffer-local schema without a modeline:

```lua
vim.lsp.config("yamlls", {
  settings = {
    yaml = {
      schemas = {
        ["./.readyagents/workflow.schema.json"] = { "*.yaml", "*.yml" },
      },
    },
  },
})
```

### JetBrains

Open the workflow file, click the schema picker in the status bar (or **Preferences → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings**), and map `workflow.schema.json` (or `.readyagents/workflow.schema.json`) to `*.yaml`.

### Generic yaml-language-server

Any client that honours `# yaml-language-server: $schema=...` will load the local file. Do not point it at `https://readyagents.dev/schema/workflow/v1.json` until that URL is actually served.

## Located errors

`readyagents validate` and `readyagents run` still print `Invalid workflow PATH:` plus the Pydantic field path. When a source position is known they also print a compiler-style excerpt:

```text
  review.yaml:14:18
    14 |     retry: {max_attempts: "three"}
       |                            ^
       = nodes.1.retry.max_attempts: Input should be a valid integer
```

`validate --json` keeps `{ok, command, error, message}` and adds a `problems` array of `{loc, message, file, line, column}`. File paths are the paths you passed in, not extra resolved absolutes. Errors inside an included workflow name the included file first and note the include site.

If a location cannot be mapped (YAML anchors/aliases, some graph-wide validators), ReadyAgents prints the message without a caret rather than guessing.

## What the schema does not check

Cycle detection, route targets, include graphs, and other whole-document rules stay in Pydantic. A file can look fine in the editor and still fail `readyagents validate`. Packs may add node types and fields; node objects are open (`additionalProperties` is not closed), so a pack field should not draw a false squiggle.
