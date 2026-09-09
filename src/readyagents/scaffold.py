"""Create a starter ReadyAgents project (workflow + README + .env pattern)."""

from __future__ import annotations

from pathlib import Path

from readyagents.errors import ConfigError

TEMPLATES = (
    "basic",
    "approval",
    "research",
    "pipeline",
    "review",
    "foreach",
    "agent-tools",
    "gated",
)

_ENV = """# ReadyAgents BYOK — fill in your keys. Never commit real keys.

READYAGENTS_DEFAULT_MODEL=openai:gpt-4o-mini
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
# READYAGENTS_ALLOW_HTTP=0
"""

_WORKFLOWS: dict[str, str] = {
    "basic": """name: {name}
version: "1"
description: >
  Basic starter from `readyagents new --template basic`. No API keys.
  Run: readyagents run workflow.yaml

start: stamp
nodes:
  - id: stamp
    type: tool
    tool: now
    output_key: timestamp
    next: greet

  - id: greet
    type: transform
    template: "{name} ok at {{{{timestamp}}}}"
    output_key: summary
""",
    "approval": """name: {name}
version: "1"
description: >
  Approval starter from `readyagents new --template approval`. No API keys.
  Run: readyagents run workflow.yaml --approve gate

start: stamp
nodes:
  - id: stamp
    type: tool
    tool: now
    output_key: timestamp
    next: greet

  - id: greet
    type: transform
    template: "{name} ok at {{{{timestamp}}}}"
    output_key: summary
    next: gate

  - id: gate
    type: approval
    prompt: "Accept starter summary? {{{{summary}}}}"
    then: done
    else: stopped

  - id: done
    type: transform
    template: "approved: {{{{summary}}}}"
    output_key: result

  - id: stopped
    type: transform
    template: "rejected: {{{{summary}}}}"
    output_key: result
""",
    "pipeline": """name: {name}
version: "1"
description: >
  Keyless pipeline starter (`--template pipeline`): calc, json_get, condition.
  Run: readyagents run workflow.yaml

start: add
nodes:
  - id: add
    type: tool
    tool: calc
    arguments:
      expression: "6 * 7"
    output_key: n
    next: pack

  - id: pack
    type: transform
    template: '{{"n": {{{{n}}}}}}'
    output_key: blob
    next: pick

  - id: pick
    type: tool
    tool: json_get
    arguments:
      data: "{{{{blob}}}}"
      path: n
    output_key: extracted
    next: check

  - id: check
    type: condition
    when: extracted == 42
    then: ok
    else: bad

  - id: ok
    type: transform
    template: "{name} pipeline ok: {{{{extracted}}}}"
    output_key: summary

  - id: bad
    type: transform
    template: "{name} pipeline unexpected: {{{{extracted}}}}"
    output_key: summary
""",
    "review": """name: {name}
version: "1"
description: >
  File-review starter (`--template review`). Reads a workspace file, then a
  transform you can later swap for an agent node. No API keys.
  Run: readyagents run workflow.yaml --input path=README.md

inputs:
  path: README.md

start: read
nodes:
  - id: read
    type: tool
    tool: read_file
    arguments:
      path: "{{{{path}}}}"
    output_key: source
    next: note

  - id: note
    type: transform
    template: "review {{{{path}}}} ({name}): {{{{source}}}}"
    output_key: summary
    next: gate

  - id: gate
    type: approval
    prompt: "Accept review of {{{{path}}}}?"
    then: done
    else: hold

  - id: done
    type: transform
    template: "accepted: {{{{path}}}}"
    output_key: result

  - id: hold
    type: transform
    template: "held: {{{{path}}}}"
    output_key: result
""",
    "research": """name: {name}
version: "1"
description: >
  Research-style starter. Fan-out two builtin tools, then an approval gate.
  No API keys. Run: readyagents run workflow.yaml --approve publish

start: fan
nodes:
  - id: fan
    type: parallel
    output_key: parts
    next: combine
    branches:
      - id: math
        type: tool
        tool: calc
        arguments:
          expression: "21 * 2"
      - id: when
        type: tool
        tool: now

  - id: combine
    type: transform
    template: "value={{{{parts.math}}}} at {{{{parts.when}}}}"
    output_key: brief
    next: publish

  - id: publish
    type: approval
    prompt: "Publish brief? {{{{brief}}}}"
    then: ok
    else: hold

  - id: ok
    type: transform
    template: "{name} published: {{{{brief}}}}"
    output_key: result

  - id: hold
    type: transform
    template: "{name} held: {{{{brief}}}}"
    output_key: result
""",
    "foreach": """name: {name}
version: "1"
description: >
  Foreach starter from `readyagents new --template foreach`. No API keys.
  Run: readyagents run workflow.yaml

inputs:
  expressions:
    - "1+1"
    - "2+2"

start: each
nodes:
  - id: each
    type: foreach
    items: expressions
    max_items: 32
    output_key: results
    body:
      id: math
      type: tool
      tool: calc
      arguments:
        expression: "{{{{item}}}}"
""",
    "agent-tools": """name: {name}
version: "1"
description: >
  Agent tools starter (`--template agent-tools`). Live run needs an API key.
  Keyless dry-run: readyagents run workflow.yaml --dry-run

start: worker
nodes:
  - id: worker
    type: agent
    prompt: |
      Use the calc tool if you need arithmetic.
      What is 2+2? Reply with the number only.
    tools:
      - calc
    max_tool_rounds: 4
    output_key: answer
    timeout_seconds: 60
""",
    "gated": """name: {name}
version: "1"
description: >
  Gated write starter (`--template gated`). Pause does not create the file.
  Pause:  readyagents run workflow.yaml
  Resume: readyagents resume <run_id> --approve gate
  One shot: readyagents run workflow.yaml --approve gate

start: add
nodes:
  - id: add
    type: tool
    tool: calc
    arguments:
      expression: "19 + 23"
    output_key: total
    next: gate

  - id: gate
    type: approval
    prompt: "Write gated.txt with total {{{{total}}}}?"
    then: write
    else: denied

  - id: write
    type: tool
    tool: write_file
    arguments:
      path: gated.txt
      content: "gated ok: {{{{total}}}}\\n"
    output_key: written

  - id: denied
    type: transform
    template: "gated denied: {{{{total}}}}"
    output_key: summary
""",
}

_READMES: dict[str, str] = {
    "basic": """# {name}

Basic ReadyAgents starter (`--template basic`). BYOK. No approval gate.

```bash
readyagents run workflow.yaml
readyagents runs list
readyagents runs show <run_id>
```

Copy `.env.example` to `.env` if you add agent nodes.
""",
    "approval": """# {name}

Approval starter (`--template approval`). BYOK.

```bash
readyagents run workflow.yaml --approve gate
# or pause, then:
readyagents run workflow.yaml
readyagents resume <run_id> --approve gate
```

Copy `.env.example` to `.env` if you add agent nodes.
""",
    "pipeline": """# {name}

Pipeline starter (`--template pipeline`). Builtin tools only.

```bash
readyagents run workflow.yaml
readyagents runs show <run_id>
readyagents runs report <run_id>
```
""",
    "review": """# {name}

File-review starter (`--template review`). Keyless transform; swap `note` for
an `agent` node when you add API keys.

```bash
readyagents run workflow.yaml --input path=README.md --approve gate
readyagents resume <run_id> --approve gate
```
""",
    "research": """# {name}

Research-style starter (`--template research`): parallel fan-out + approval.

```bash
readyagents run workflow.yaml --approve publish
readyagents run workflow.yaml --dry-run --approve publish
```

No API keys required. Add `type: agent` nodes and keys later.
""",
    "foreach": """# {name}

Foreach starter (`--template foreach`). Bounded list + `calc`. No API keys.

```bash
readyagents run workflow.yaml
```
""",
    "agent-tools": """# {name}

Agent tools starter (`--template agent-tools`). Allowlisted `calc`.

```bash
readyagents run workflow.yaml --dry-run
# with keys:
readyagents run workflow.yaml
```
""",
    "gated": """# {name}

Gated write starter (`--template gated`). Pause (exit 2) does not create the file.

```bash
readyagents run workflow.yaml
readyagents resume <run_id> --approve gate
readyagents run workflow.yaml --approve gate
```
""",
}


_SCHEMA_MODELINE = "# yaml-language-server: $schema=./workflow.schema.json\n"


def create_project(dest: Path, *, name: str, template: str = "pipeline") -> list[Path]:
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    kind = (template or "pipeline").strip().lower()
    if kind not in _WORKFLOWS:
        raise ConfigError(f"Unknown template '{template}'. Choose one of: {', '.join(TEMPLATES)}")
    workflow = dest / "workflow.yaml"
    readme = dest / "README.md"
    env_example = dest / ".env.example"
    schema_file = dest / "workflow.schema.json"
    for path in (workflow, readme, env_example, schema_file):
        if path.exists():
            raise ConfigError(f"Refusing to overwrite existing file: {path}")
    slug = _slug(name)
    from readyagents.workflow.jsonschema import workflow_json_schema_text

    body = _WORKFLOWS[kind].format(name=slug)
    if not body.startswith("# yaml-language-server:"):
        body = _SCHEMA_MODELINE + body
    workflow.write_text(body, encoding="utf-8")
    schema_file.write_text(workflow_json_schema_text(), encoding="utf-8")
    readme.write_text(_READMES[kind].format(name=slug), encoding="utf-8")
    env_example.write_text(_ENV, encoding="utf-8")
    return [workflow, readme, env_example, schema_file]


def _slug(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip())
    cleaned = cleaned.strip("-_") or "starter"
    return cleaned
