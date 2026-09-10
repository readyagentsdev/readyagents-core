"""Onboarding docs and extra-missing errors must match the shipped package."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents import __version__
from readyagents.cli import app
from readyagents.errors import LLMError
from readyagents.llm.anthropic_provider import AnthropicProvider
from readyagents.llm.base import Message, missing_extra_message
from readyagents.llm.openai_provider import OpenAIProvider

ROOT = Path(__file__).resolve().parents[1]


def test_missing_extra_message_names_distribution() -> None:
    text = missing_extra_message("OpenAI", "openai")
    assert "readyagentsdev[openai]" in text
    assert "readyagents[" not in text.replace("readyagentsdev[", "")


def test_openai_missing_extra_names_readyagentsdev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    provider = OpenAIProvider(api_key="sk-test")
    with pytest.raises(LLMError, match=r"readyagentsdev\[openai\]"):
        provider.complete([Message(role="user", content="hi")], model="gpt-4o-mini")


def test_anthropic_missing_extra_names_readyagentsdev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    provider = AnthropicProvider(api_key="sk-test")
    with pytest.raises(LLMError, match=r"readyagentsdev\[anthropic\]"):
        provider.complete([Message(role="user", content="hi")], model="claude-3-haiku")


def test_first_ten_minutes_matches_package() -> None:
    text = (ROOT / "docs" / "first-ten-minutes.md").read_text(encoding="utf-8")
    assert "not on PyPI" not in text
    assert "readyagentsdev" in text
    assert f"**{__version__}**" in text
    assert "0.8.0" not in text


_NEW_CMD = re.compile(r"readyagents new ([A-Za-z0-9_-]+)(?: --template ([A-Za-z0-9_-]+))?")


def test_first_ten_minutes_pypi_new_dests_are_distinct(tmp_path: Path, monkeypatch) -> None:
    """A linear follow of the PyPI walkthrough must not hit overwrite."""
    text = (ROOT / "docs" / "first-ten-minutes.md").read_text(encoding="utf-8")
    found = _NEW_CMD.findall(text)
    assert found, "first-ten-minutes must show readyagents new"
    names = [name for name, _template in found]
    gated = [name for name, template in found if template == "gated"]
    assert gated, "PyPI HITL path must scaffold --template gated"
    assert "my-flow" not in gated
    assert len(names) == len(set(names)), names
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    for name, template in found:
        args = ["new", name]
        if template:
            args.extend(["--template", template])
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / name / "workflow.yaml").is_file()


def test_getting_started_matches_package() -> None:
    text = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert f"**{__version__}**" in text
    assert "Current version is **1.0.0**" not in text
    assert "optional Unreleased localhost page" not in text
    assert "does **not** ship `examples/`" in text
    assert "readyagents new my-flow" in text


def test_why_readyagents_product_version_matches_package() -> None:
    text = (ROOT / "docs" / "why-readyagents.md").read_text(encoding="utf-8")
    assert f"ReadyAgents {__version__}" in text
    assert "ReadyAgents 0.10.0" not in text
    assert "PARTIAL" in text


def test_a2a_docs_card_is_protocol_0_3() -> None:
    text = (ROOT / "docs" / "a2a.md").read_text(encoding="utf-8")
    assert "protocolVersion" in text
    assert "0.3.0" in text
    assert "agent-card.json` (A2A v1.0)" not in text


def test_readme_does_not_sell_unreleased_as_tag() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Unreleased" in text
    assert "not on the 1.9.0 tag" in text or "not on the 1.9.0" in text
    assert "Release notes 0.8.0" not in text
