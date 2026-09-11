from __future__ import annotations

import faulthandler
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from readyagents.config import Settings, clear_settings_cache
from readyagents.llm.base import CompletionResult, Message
from readyagents.workflow.governor import reset_governor_for_tests


class MockLLM:
    name = "mock"

    def __init__(self, text: str = "mocked") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    def complete(
        self, messages: list[Message], *, model: str, tools=None, **kwargs
    ) -> CompletionResult:
        self.calls.append(messages)
        return CompletionResult(text=self.text, model=model)


@pytest.fixture(scope="session", autouse=True)
def _ci_stack_dumps() -> Iterator[None]:
    """On Windows Actions, dump all threads every 60s so a hang has a stack."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "nt":
        yield
        return
    faulthandler.enable(file=sys.stderr, all_threads=True)
    faulthandler.dump_traceback_later(60, repeat=True, file=sys.stderr)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(autouse=True)
def _reset_governor() -> None:
    reset_governor_for_tests()
    yield
    reset_governor_for_tests()


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        home=tmp_path / ".readyagents",
        workspace=tmp_path,
        allow_http=False,
        default_model="openai:gpt-4o-mini",
        openai_api_key=None,
        anthropic_api_key=None,
        _env_file=(),  # type: ignore[call-arg]
    )
    yield settings
    clear_settings_cache()


@pytest.fixture
def examples_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "examples"
