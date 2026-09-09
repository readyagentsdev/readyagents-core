# Getting started

## Requirements

- Python 3.11+
- A virtualenv (recommended)
- Optional: an OpenAI or Anthropic API key for agent nodes

## Install

Current version is **0.10.1**. Preferred install is from PyPI:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install readyagentsdev
```

Or clone and install editable:

```bash
git clone https://github.com/readyagentsdev/readyagents-core.git
cd readyagents-core
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Builtin-tool workflows need no API keys. LLM and MCP extras:

```bash
pip install "readyagentsdev[openai]"
pip install "readyagentsdev[anthropic]"
pip install "readyagentsdev[mcp]"
pip install "readyagentsdev[all]"
```

Agent nodes need a key in `.env` after you install the matching extra.

## First run (no keys)

```bash
readyagents run examples/calc_pipeline.yaml
```

This uses `calc`, `now`, `json_get`, a transform, and a condition. It writes a run record under `.readyagents/runs/` (gitignored) after each node.

```bash
readyagents runs list
readyagents runs show <run_id>
readyagents eval examples/eval/pass.yaml
readyagents run examples/list_dir.yaml
```

Scaffold a local starter (workflow + README + `.env.example`):

```bash
readyagents new my-flow
readyagents run my-flow/workflow.yaml
readyagents new demo --template gated
readyagents run demo/workflow.yaml --approve gate
```

Human-in-the-loop (no keys):

```bash
readyagents run examples/approval_gate.yaml --approve gate
# or pause, then:
readyagents run examples/approval_gate.yaml
readyagents resume <run_id> --approve gate
# optional Unreleased localhost page (not a hosted dashboard):
# readyagents approvals serve --host 127.0.0.1 --port 8766
readyagents run examples/fanout_gate.yaml --approve gate
readyagents run examples/include_demo.yaml
readyagents run examples/multi_gate.yaml --approve first --approve second
```

## Configure BYOK

```bash
cp .env.example .env
```

Edit `.env` and set the key you actually have:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`

You do not have to pick OpenAI. If the agent node has no `model:` and you set only one key, the engine uses that provider.

Optionally pin `READYAGENTS_DEFAULT_MODEL` (`openai:gpt-4o-mini` or `anthropic:claude-sonnet-4-5`).

ReadyAgents never ships with vendor keys. You bring your own.

## LLM examples

```bash
readyagents run examples/research_brief.yaml --input topic="retrieval augmented generation"
readyagents run examples/support_triage.yaml --input message="I cannot log in"
readyagents run examples/code_review.yaml --input path=examples/sample_code.py
```

`research_brief` is LLM-only by default. To fetch a URL before writing the brief:

1. Set `allow_http: true` in the workflow (or `READYAGENTS_ALLOW_HTTP=1`)
2. Pass `--input url=https://example.com/article`

If HTTP is off, the writer still produces a plan + brief from the model alone.

## Dry-run

```bash
readyagents run examples/research_brief.yaml --dry-run --input topic=test
```

Dry-run interpolates templates and walks the graph. It does not call an LLM or `http_get`.

## Validate without running

```bash
readyagents validate examples/code_review.yaml
```

## Next

- [Workflow syntax](workflows.md)
- [Configuration](configuration.md)
- [MCP toolkit](mcp.md)
- [Localhost approval UI](browser-approval-ui.md)
- [Packs](packs.md) / [Continuous pack](continuous-pack.md) (optional, separate distribution)
- [CLI](cli.md)
