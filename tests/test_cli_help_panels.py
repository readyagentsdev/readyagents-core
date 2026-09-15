"""H-04: ``readyagents --help`` panel structure.

The top-level help must group commands into a leading ``Core`` panel plus
themed ``Extras: <theme>`` panels (one taxonomy shared with
``docs/extras.md``). Nothing may be hidden: every registered top-level
command must appear in exactly one panel.

Uses ``typer.testing.CliRunner`` against the live ``readyagents.cli:app``.
Only ``CORE_COMMANDS`` and the extras theme headings are pinned; the full
command set is derived from the live app, so newly added commands are
covered by the anti-hidden guard automatically.
"""

import re
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from readyagents.cli import app

runner = CliRunner()

# Pinned by H-04: the exact contents of the Core panel.
CORE_COMMANDS = frozenset(
    {
        "run",
        "resume",
        "runs",
        "new",
        "validate",
        "eval",
        "decide",
        "approvals",
        "policy",
        "spend",
        "mcp",
        "doctor",
        "init",
        "version",
    }
)

# Pinned by H-04: the eight ``docs/extras.md`` section headings. Every
# non-core command panel must be exactly ``Extras: <heading>``.
EXTRAS_HEADINGS = (
    "Governance and audit",
    "Connectivity",
    "Data and knowledge",
    "Agent capability",
    "Operations",
    "Authoring",
    "Model and prompt",
    "Packaging and distribution",
)
EXPECTED_EXTRAS_PANELS = frozenset(f"Extras: {h}" for h in EXTRAS_HEADINGS)

# Total top-level commands at the time H-04 was written. The names are
# derived from the live app, but the count is asserted so a command that
# silently disappears from --help (e.g. via hidden=True) fails loudly.
EXPECTED_COMMAND_COUNT = 57

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PANEL_HEADER_RE = re.compile(r"\s*╭─\s*(.+?)\s*─")
# Command rows start the name in column 1 (``│ name ...``); wrapped help
# continuation rows are indented further, so they do not match.
COMMAND_ROW_RE = re.compile(r" (\S+)")


def get_help_text() -> str:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, f"--help failed: {result.output}"
    return ANSI_RE.sub("", result.output)


def parse_panels(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split --help into ``(panel_order, {panel_name: [command, ...]})``."""
    order: list[str] = []
    panels: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        header = PANEL_HEADER_RE.match(line)
        if header:
            current = header.group(1)
            panels[current] = []
            order.append(current)
            continue
        if current is None or not line.startswith("│"):
            continue
        parts = line.split("│")
        if len(parts) < 2:
            continue
        row = COMMAND_ROW_RE.match(parts[1])
        if row and not row.group(1).startswith("-"):
            panels[current].append(row.group(1))
    return order, panels


def registered_command_names() -> set[str]:
    """Top-level command names from the live app (not from --help text)."""
    return set(get_command(app).commands.keys())


def command_panels(order: list[str]) -> list[str]:
    """Panel names in order, excluding the options panel."""
    return [name for name in order if name != "Options"]


def test_core_panel_exists_first_with_exact_contents():
    order, panels = parse_panels(get_help_text())
    assert "Core" in panels, (
        f"No Core panel in --help; panels found: {order}"
    )
    panels_in_order = command_panels(order)
    assert panels_in_order[0] == "Core", (
        f"Core panel must appear first, got order: {panels_in_order}"
    )
    assert set(panels["Core"]) == set(CORE_COMMANDS), (
        f"Core panel must contain exactly the 14 core commands; "
        f"missing={sorted(set(CORE_COMMANDS) - set(panels['Core']))} "
        f"extra={sorted(set(panels['Core']) - set(CORE_COMMANDS))}"
    )


def test_every_command_appears_in_some_panel():
    """Anti-hidden guard: no registered command may vanish from --help."""
    order, panels = parse_panels(get_help_text())
    expected = registered_command_names()
    assert len(expected) == EXPECTED_COMMAND_COUNT, (
        f"Expected {EXPECTED_COMMAND_COUNT} registered commands, "
        f"found {len(expected)}: {sorted(expected)}"
    )
    shown = {cmd for name in order for cmd in panels[name]}
    missing = expected - shown
    assert not missing, (
        f"{len(missing)} command(s) hidden from --help: {sorted(missing)}"
    )
    phantom = shown - expected
    assert not phantom, (
        f"--help lists unknown command(s): {sorted(phantom)}"
    )


def test_no_command_appears_in_two_panels():
    order, panels = parse_panels(get_help_text())
    seen: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    for name in order:
        for cmd in panels[name]:
            if cmd in seen:
                duplicates.setdefault(cmd, [seen[cmd]]).append(name)
            else:
                seen[cmd] = name
    assert not duplicates, (
        f"Command(s) listed in multiple panels: {duplicates}"
    )


def test_extras_panels_match_docs_taxonomy():
    order, panels = parse_panels(get_help_text())
    for name in command_panels(order):
        if name == "Core":
            continue
        assert name in EXPECTED_EXTRAS_PANELS, (
            f"Unexpected command panel {name!r}; expected only 'Core' and "
            f"{sorted(EXPECTED_EXTRAS_PANELS)}"
        )


def test_no_hidden_commands_in_cli_source():
    """hidden=True deletes a command from --help; H-04 forbids it outright."""
    cli_source = Path(__file__).resolve().parent.parent / "src" / "readyagents" / "cli.py"
    text = cli_source.read_text()
    assert "hidden=True" not in text, "cli.py must not use hidden=True"
    assert "hidden =" not in text, "cli.py must not set hidden on commands"
