"""429 Retry-After from shipped provider clients must back-pressure the governor."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import GovernorBackpressure, LLMError
from readyagents.llm.base import Message
from readyagents.workflow.batch import run_batch
from readyagents.workflow.governor import get_governor, reset_governor_for_tests


class _RateLimit(Exception):
    status_code = 429

    def __init__(self, retry_after: str) -> None:
        super().__init__("rate limited")
        self.response = SimpleNamespace(headers={"Retry-After": retry_after})


def test_openai_complete_429_notes_governor(monkeypatch) -> None:
    import openai

    from readyagents.llm.openai_provider import OpenAIProvider

    reset_governor_for_tests()

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003
            raise _RateLimit("30")

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    provider = OpenAIProvider(api_key="sk-test")
    with pytest.raises(LLMError, match="rate limited"):
        provider.complete([Message(role="user", content="hi")], model="gpt-4o-mini")
    gov = get_governor()
    assert gov.blocked_until("openai") > 0
    with pytest.raises(GovernorBackpressure):
        with gov.acquire(workflow="w", provider="openai", timeout=0.0):
            pass


def test_anthropic_complete_429_notes_governor(monkeypatch) -> None:
    import anthropic

    from readyagents.llm.anthropic_provider import AnthropicProvider

    reset_governor_for_tests()

    class FakeMessages:
        def create(self, **kwargs):  # noqa: ANN003
            raise _RateLimit("30")

    class FakeAnthropic:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    provider = AnthropicProvider(api_key="sk-test")
    with pytest.raises(LLMError, match="rate limited"):
        provider.complete([Message(role="user", content="hi")], model="claude-3-haiku")
    gov = get_governor()
    assert gov.blocked_until("anthropic") > 0
    with pytest.raises(GovernorBackpressure):
        with gov.acquire(workflow="w", provider="anthropic", timeout=0.0):
            pass


def _agent_wf(tmp: Path) -> Path:
    path = tmp / "agent.yaml"
    path.write_text(
        "name: agent_row\ndefault_model: openai:gpt-4o-mini\nnodes:\n"
        "  - id: a\n    type: agent\n    prompt: 'say hi'\n    output_key: text\n",
        encoding="utf-8",
    )
    return path


def test_run_batch_without_llm_kwarg_waits_on_openai_429(tmp_path: Path, monkeypatch) -> None:
    """CLI-shaped batch (no llm=) must pass default_model provider into acquire."""
    import openai

    from readyagents.config import Settings

    reset_governor_for_tests()
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("READYAGENTS_DEFAULT_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    hits: list[float] = []

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003
            hits.append(time.monotonic())
            raise _RateLimit("0.5")

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    settings = Settings(
        home=tmp_path / ".readyagents",
        workspace=tmp_path,
        openai_api_key="sk-test",
        default_model="openai:gpt-4o-mini",
        _env_file=(),  # type: ignore[call-arg]
    )
    wf = _agent_wf(tmp_path)
    report = run_batch(
        wf,
        [{}, {}],
        concurrency=1,
        persist=False,
        settings=settings,
        run_kwargs={"no_cache": True, "override_budget": True},
    )
    assert report.failed == 2
    assert len(hits) == 2
    assert hits[1] - hits[0] >= 0.4
    assert get_governor().blocked_until("openai") > 0
    clear_settings_cache()


def test_cli_batch_429_blocks_later_row(tmp_path: Path, monkeypatch) -> None:
    import openai

    reset_governor_for_tests()
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("READYAGENTS_DEFAULT_MODEL", "openai:gpt-4o-mini")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    hits: list[float] = []

    class FakeCompletions:
        def create(self, **kwargs):  # noqa: ANN003
            hits.append(time.monotonic())
            raise _RateLimit("0.5")

    class FakeOpenAI:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    wf = _agent_wf(tmp_path)
    rows = tmp_path / "rows.jsonl"
    rows.write_text("{}\n{}\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "batch",
            str(wf),
            "--input-file",
            str(rows),
            "--concurrency",
            "1",
            "--no-persist",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["command"] == "batch"
    assert data["failed"] == 2
    assert len(hits) == 2
    assert hits[1] - hits[0] >= 0.4
    clear_settings_cache()
