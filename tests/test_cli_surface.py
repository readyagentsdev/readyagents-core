"""CLI surface snapshot (H-05).

Walks the live Typer app, collects every command path with its parameters and
defaults, and compares against the committed ``test_cli_surface.json`` snapshot.
This guards against silent CLI surface drift — the thing that let ``cli.py``
grow to 235 KB unnoticed.

To regenerate the snapshot after an intentional CLI change::

    READYAGENTS_UPDATE_SNAPSHOT=1 pytest tests/test_cli_surface.py -q
"""

import json
import os
from pathlib import Path
from typing import Any

import typer

from readyagents.cli import app

SNAPSHOT = Path(__file__).with_name("test_cli_surface.json")


def _type_spec(tp: Any) -> str:
    # str(tp) embeds memory addresses for custom types; build a stable spec.
    parts = [type(tp).__name__]
    name = getattr(tp, "name", None)
    if isinstance(name, str):
        parts.append(name)
    choices = getattr(tp, "choices", None)
    if choices:
        try:
            parts.append("[" + ",".join(sorted(str(c) for c in choices)) + "]")
        except TypeError:
            parts.append("[...]")
    return ":".join(parts)


def _param_spec(param: Any) -> dict:
    is_option = type(param).__name__.endswith("Option")
    envvar = getattr(param, "envvar", None)
    return {
        "kind": "option" if is_option else "argument",
        "name": param.name,
        "opts": sorted(param.opts) if is_option else [],
        "secondary_opts": sorted(param.secondary_opts) if is_option else [],
        "type": _type_spec(param.type),
        "default": repr(param.default),
        "required": bool(param.required),
        "help": getattr(param, "help", None) or "",
        "envvar": sorted(envvar) if envvar else [],
    }


def _record(cmd: Any, path: str, out: dict) -> None:
    out[path] = {
        "help": (cmd.help or "").strip(),
        "params": [_param_spec(p) for p in cmd.params],
    }


def _walk(cmd: Any, path: str, out: dict) -> None:
    if hasattr(cmd, "commands"):
        # Groups with an invocable callback (root options, health) have their
        # own surface in addition to their subcommands. The root lands on "".
        if cmd.callback is not None:
            _record(cmd, path, out)
        for name in sorted(cmd.commands):
            child = name if not path else f"{path} {name}"
            _walk(cmd.commands[name], child, out)
    else:
        _record(cmd, path, out)


def collect_surface() -> dict:
    cmd = typer.main.get_command(app)
    out: dict = {}
    _walk(cmd, "", out)
    return dict(sorted(out.items()))


def test_cli_surface_matches_snapshot() -> None:
    surface = collect_surface()
    assert surface, "collected an empty CLI surface"
    if os.environ.get("READYAGENTS_UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(surface, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert SNAPSHOT.exists(), (
        "missing CLI surface snapshot; generate it with "
        "READYAGENTS_UPDATE_SNAPSHOT=1 pytest tests/test_cli_surface.py -q"
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert surface == expected, "CLI surface drifted from the committed snapshot"


def test_cli_surface_covers_all_commands() -> None:
    surface = collect_surface()
    top_level = {path.split(" ")[0] for path in surface if path}
    # 57 top-level commands/groups; nested paths (e.g. "models adapters list")
    # add depth but no new top-level names.
    assert len(top_level) == 57, f"expected 57 top-level commands, got {len(top_level)}"
