# Stability contract (ReadyAgents Core 1.0)

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
