# Local models

ReadyAgents talks to **OpenAI-compatible** HTTP endpoints. It does not bundle
weights, start Ollama/vLLM/llama.cpp, or download models.

## Loopback needs no API key

When the resolved base URL is loopback (or an allowlisted private host), a
placeholder key is not required. Remote compat URLs still need a key.

| Provider prefix | Default base | Key |
| --- | --- | --- |
| `ollama:<model>` | `http://127.0.0.1:11434/v1` | not required |
| `openai-compat:<model>` with `OPENAI_COMPAT_BASE_URL` on loopback | that URL | not required |
| `groq:<model>` / remote `OPENAI_COMPAT_BASE_URL` | `https://api.groq.com/openai/v1` | required |

```bash
# Ollama already running on this host
readyagents run flow.yaml --sovereign
# flow.yaml agent node: model: ollama:llama3.2
```

Keyless example (no live model): `examples/ollama_local.yaml`.

## Honest limits

- **Tool calls** depend on the server. Ollama and llama.cpp vary by model;
  many local servers omit or partially implement tools. ReadyAgents will not
  invent tool-call support the endpoint does not offer.
- **Context length** is the model's, not Core's. Set node `timeout_seconds`
  and workflow `runaway` guards; do not assume 128k.
- **vLLM** typically serves `/v1` on a private host. Allowlist it under
  sovereign: `--sovereign-allow 10.0.0.8` (must resolve to a private address).
- **llama.cpp** server (`--host 127.0.0.1`) is loopback; same keyless path.

See [sovereign.md](sovereign.md).
