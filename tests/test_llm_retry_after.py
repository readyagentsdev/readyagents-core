"""429 Retry-After from shipped provider clients must back-pressure the governor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from readyagents.errors import GovernorBackpressure, LLMError
from readyagents.llm.base import Message
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
