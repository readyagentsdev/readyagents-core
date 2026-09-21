"""Onboarding docs and extra-missing errors must match the shipped package."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents import __version__
from readyagents.cli import app
from readyagents.errors import A2AError, LLMError, MCPError, missing_extra_message
from readyagents.examples import list_examples
from readyagents.llm.anthropic_provider import AnthropicProvider
from readyagents.llm.base import Message
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


_NEW_CMD = re.compile(
    r"readyagents new ([A-Za-z0-9_][A-Za-z0-9_-]*)"
    r"(?: (--template|--from-example) ([A-Za-z0-9_./-]+))?"
)


def test_first_ten_minutes_new_dests_are_distinct(tmp_path: Path, monkeypatch) -> None:
    """A linear follow of the walkthrough must not hit overwrite (H-06: one road)."""
    text = (ROOT / "docs" / "first-ten-minutes.md").read_text(encoding="utf-8")
    found = _NEW_CMD.findall(text)
    assert found, "first-ten-minutes must show readyagents new"
    names = [name for name, _flag, _value in found]
    hitl = [
        name for name, flag, value in found if flag == "--from-example" and value == "approval_gate"
    ]
    assert hitl, "HITL path must materialize --from-example approval_gate"
    assert "my-flow" not in hitl
    assert len(names) == len(set(names)), names
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    for name, flag, value in found:
        args = ["new", name]
        if flag:
            args.extend([flag, value])
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert (tmp_path / name / "workflow.yaml").is_file()


def test_first_ten_compose_tour_is_pip_honest() -> None:
    text = (ROOT / "docs" / "first-ten-minutes.md").read_text(encoding="utf-8")
    assert "## 4. Compose" in text
    section4 = text.split("## 4. Compose", 1)[1].split("## Optional", 1)[0]
    assert "tour-each" in section4 and "--from-example foreach_calc" in section4
    assert "tour-fan" in section4 and "--from-example fanout_gate" in section4
    assert "tour-all" in section4 and "--from-example graph_complex" in section4
    assert "tour-inc" in section4 and "--from-example include_demo" in section4
    assert "{'results': [2, 4]}" in section4
    assert "fanout_gate ok: 42" in section4 or "fanout_gate ok:" in section4
    assert "include_demo ok: 15" in section4
    assert "readyagents run examples/include_demo.yaml" not in section4
    assert "copies one file" not in section4 and "copies a single file" not in section4
    workflows = (ROOT / "docs" / "workflows.md").read_text(encoding="utf-8")
    assert "--from-example" in workflows
    assert "recursively copies" in workflows
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "include_demo.yaml` | Sub-workflow `include` (`new --from-example`)" in readme
    assert "composed_gate.yaml` | Include + parallel + approval (`new --from-example`)" in readme


def test_getting_started_matches_package() -> None:
    text = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert f"**{__version__}**" in text
    assert "Current version is **1.0.0**" not in text
    assert "optional Unreleased localhost page" not in text
    assert "does **not** ship `examples/`" not in text
    assert "ships `examples/` as package data" in text
    assert "--list-examples" in text
    assert "--from-example" in text
    assert len(list_examples()) > 0
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


def test_construct_server_missing_mcp_extra_names_readyagentsdev(monkeypatch) -> None:
    import readyagents.mcp.server as srv

    monkeypatch.setattr(srv, "mcp_available", lambda: False)
    with pytest.raises(MCPError, match=r"readyagentsdev\[mcp\]"):
        srv.construct_server()


def test_mcp_client_missing_extra_names_readyagentsdev(tmp_path: Path, monkeypatch) -> None:
    from readyagents.mcp.client import MCPClient
    from readyagents.workflow.schema import MCPServerSpec

    monkeypatch.setattr("readyagents.mcp.client.mcp_available", lambda: False)
    client = MCPClient({"demo": MCPServerSpec(command="true")}, tmp_path)
    with pytest.raises(MCPError, match=r"readyagentsdev\[mcp\]"):
        client.tools()


def test_a2a_serve_missing_uvicorn_names_readyagentsdev(
    tmp_path: Path, tmp_settings, monkeypatch
) -> None:
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        "name: t\nstart: a\nnodes:\n  - id: a\n    type: transform\n    template: 'ok'\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    from readyagents.a2a.server import serve_a2a

    with pytest.raises(A2AError, match=r"readyagentsdev\[mcp\]"):
        serve_a2a(wf)


def test_mcp_docs_name_readyagentsdev_extra() -> None:
    text = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    assert "readyagentsdev[mcp]" in text


def test_readme_current_version_matches_package() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"**{__version__}**" in text
    assert "not on the 1.9.0 tag" not in text
    assert "Release notes 0.8.0" not in text


def test_authoring_covers_decide_criteria_and_routes() -> None:
    text = (ROOT / "docs" / "authoring.md").read_text(encoding="utf-8")
    assert "mapping" in text and "list" in text
    assert "choice" in text and "score" in text
    assert "readyagents validate" in text
    assert "routes" in text
    assert "then" in text and "else" in text
    assert "noul" in text
    assert "not directly routable" in text
    assert "decisions.md" in text
    decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
    assert "examples/eval/decide_triage/" in decisions
    assert "Write 20–50 labelled cases" in decisions
    assert "several thresholds" in decisions
    assert "raising `min_confidence` sends more" in decisions
    assert "reversible" in decisions and "irreversible" in decisions
    assert "not P(correct)" in decisions
    assert "not a security control" in decisions


def test_readme_version_line_matches_package_and_pyproject() -> None:
    """The README version sentence, ``__version__``, and pyproject are one value."""
    import tomllib

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"Current version is \*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*", text)
    assert match, "README version line not found"
    with (ROOT / "pyproject.toml").open("rb") as handle:
        package = tomllib.load(handle)["project"]["version"]
    assert match.group(1) == __version__ == package


def test_fail_preserves_mcp_brackets_in_rich_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rich markup must not strip [mcp] from install hints printed via _fail."""
    from io import StringIO

    import typer
    from rich.console import Console

    import readyagents.cli._common as common

    buf = StringIO()
    monkeypatch.setattr(
        common,
        "err_console",
        Console(file=buf, force_terminal=False, no_color=True),
    )
    with pytest.raises(typer.Exit) as exit_info:
        common._fail(MCPError(missing_extra_message("MCP", "mcp")))
    assert exit_info.value.exit_code == 1
    assert "readyagentsdev[mcp]" in buf.getvalue()
