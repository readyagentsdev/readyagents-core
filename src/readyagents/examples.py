"""Shipped example workflows (H-06).

The wheel carries ``examples/`` as package data (``readyagents/examples/``);
a source checkout resolves to the repo-root ``examples/`` instead, so the same
code serves ``pip install`` users, editable installs, and the test suite.
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import readyagents
from readyagents.errors import ConfigError

# Preferred suffix when a stem matches several files (calc_pipeline.yaml/.json).
_SUFFIX_PREFERENCE = (".yaml", ".yml", ".json")

_SKIP_NAMES = {"__pycache__", ".DS_Store"}


def examples_root() -> Traversable | Path:
    """Directory holding the shipped examples, packaged or repo-root."""
    packaged = resources.files("readyagents") / "examples"
    try:
        if packaged.is_dir():
            return packaged
    except (FileNotFoundError, NotADirectoryError, OSError):
        pass
    fallback = Path(readyagents.__file__).resolve().parent.parent.parent / "examples"
    if fallback.is_dir():
        return fallback
    raise ConfigError("shipped examples are not available in this install")


def _walk_files(root: Traversable | Path) -> list[str]:
    """Sorted posix-style relative paths of every example file."""
    found: list[str] = []

    def visit(node: Traversable | Path, prefix: str) -> None:
        for entry in sorted(node.iterdir(), key=lambda e: e.name):
            if entry.name in _SKIP_NAMES or entry.name.endswith((".pyc", ".pyo")):
                continue
            rel = f"{prefix}{entry.name}"
            if entry.is_dir():
                visit(entry, rel + "/")
            elif entry.is_file():
                found.append(rel)

    visit(root, "")
    return found


def list_examples() -> list[str]:
    """Relative paths of all shipped examples, sorted."""
    return _walk_files(examples_root())


def resolve_example(name: str) -> tuple[str, bytes]:
    """Resolve a name to (relative path, content).

    Accepts an exact relative path (``bench/suite.yaml``) or a bare stem
    (``calc_pipeline``). A stem matching several suffixes prefers YAML, then
    JSON. Anything else is an error listing the candidates.
    """
    relpaths = list_examples()
    if name in relpaths:
        return name, _read(name)
    stem_matches = sorted({p for p in relpaths if Path(p).stem == name or Path(p).name == name})
    if not stem_matches:
        raise ConfigError(
            f"Unknown example '{name}'. Run `readyagents new --list-examples` to list them."
        )
    if len(stem_matches) == 1:
        return stem_matches[0], _read(stem_matches[0])
    for suffix in _SUFFIX_PREFERENCE:
        for candidate in stem_matches:
            if candidate.endswith(suffix):
                return candidate, _read(candidate)
    raise ConfigError(f"Ambiguous example '{name}': {', '.join(stem_matches)}. Give the full path.")


def _read(relpath: str) -> bytes:
    node: Traversable | Path = examples_root()
    for part in relpath.split("/"):
        node = node.joinpath(part)  # type: ignore[assignment]
    with resources.as_file(node) as path:
        return Path(path).read_bytes()


def materialize_example(name: str, dest: Path) -> list[Path]:
    """Copy an example into ``dest`` as ``workflow.<suffix>`` plus a schema file.

    Mirrors ``create_project``: refuses to overwrite existing files.
    """
    from readyagents.workflow.jsonschema import workflow_json_schema_text

    relpath, content = resolve_example(name)
    suffix = Path(relpath).suffix or ".yaml"
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    workflow = dest / f"workflow{suffix}"
    schema_file = dest / "workflow.schema.json"
    for path in (workflow, schema_file):
        if path.exists():
            raise ConfigError(f"Refusing to overwrite existing file: {path}")
    workflow.write_bytes(content)
    schema_file.write_text(workflow_json_schema_text(), encoding="utf-8", newline="\n")
    return [workflow, schema_file]
