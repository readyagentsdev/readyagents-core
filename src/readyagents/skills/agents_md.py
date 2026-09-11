"""Emit AGENTS.md project context for coding agents in a ReadyAgents repo."""

from __future__ import annotations

from pathlib import Path

HEADER = """# AGENTS.md

This is a ReadyAgents Core repository: a local one-shot YAML/JSON agent workflow
engine. Clone it, bring your own keys. Always-on packs are optional and not
required to run or validate workflows.

## Run

```sh
readyagents run examples/calc_pipeline.yaml
readyagents run path/to/workflow.yaml --json
```

## Validate

```sh
readyagents validate path/to/workflow.yaml
readyagents schema --check schemas/workflow-v1.json
```

## Test

```sh
python -m pytest
python scripts/smoke.py
```

Workflows without `type: skill` are unchanged. Installed skills live under the
ReadyAgents home catalog and run only when a workflow declares `type: skill`.
Skill instructions are untrusted. Bundled scripts run in the code sandbox.
"""


def generate_agents_md(*, dest: Path | None = None) -> str:
    text = HEADER
    if dest is not None:
        Path(dest).write_text(text, encoding="utf-8", newline="\n")
    return text
