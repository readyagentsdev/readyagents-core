from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.errors import ConfigError, PathError, ToolError
from readyagents.mcp.builtin import tool_read_file, tool_write_file
from readyagents.packs.loader import confine_pack_path
from readyagents.paths import (
    filesystem_case_sensitive,
    force_case_sensitive,
    resolve_within,
)
from readyagents.workflow.runner import confine_under


def test_in_workspace_unicode_and_nested(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "dir" / "café 日本語.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("ok", encoding="utf-8")
    got = resolve_within("deep/dir/café 日本語.txt", tmp_path)
    assert got == nested.resolve()
    assert confine_under("deep/dir/café 日本語.txt", tmp_path, what="path") == got


def test_parent_escape_rejected(tmp_path: Path) -> None:
    secret = tmp_path.parent / f"secret-{tmp_path.name}"
    secret.write_text("nope", encoding="utf-8")
    with pytest.raises(PathError, match="outside"):
        resolve_within("../" + secret.name, tmp_path)


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"out-{tmp_path.name}.txt"
    outside.write_text("x", encoding="utf-8")
    link = tmp_path / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("skip-reason: symlink privilege missing")
    with pytest.raises(PathError, match="outside"):
        resolve_within("leak.txt", tmp_path)


def test_case_probe_both_branches(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("in", encoding="utf-8")
    with force_case_sensitive(tmp_path, True):
        assert filesystem_case_sensitive(tmp_path) is True
        with pytest.raises(PathError, match="outside"):
            resolve_within("../" + tmp_path.name + "/../../etc/passwd", tmp_path)
    with force_case_sensitive(tmp_path, False):
        assert filesystem_case_sensitive(tmp_path) is False
        inside = resolve_within("file.txt", tmp_path)
        assert inside.is_relative_to(tmp_path.resolve()) or inside.exists()
        with pytest.raises(PathError, match="outside"):
            resolve_within(tmp_path / ".." / tmp_path.name.swapcase() / ".." / "nope", tmp_path)


def test_path_length_typed_error(tmp_path: Path) -> None:
    huge = "a" * 5000
    with pytest.raises(PathError, match="path-length"):
        resolve_within(huge, tmp_path)


def test_null_and_empty_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathError):
        resolve_within("", tmp_path)
    with pytest.raises(PathError):
        resolve_within("foo\x00bar", tmp_path)


def test_migrated_call_sites_share_resolve_within(tmp_path: Path) -> None:
    """confine_under / tools / pack loader all refuse the same escape."""
    secret = tmp_path.parent / f"escape-{tmp_path.name}.txt"
    secret.write_text("x", encoding="utf-8")
    rel = f"../{secret.name}"
    with pytest.raises((PathError, ConfigError), match="outside"):
        confine_under(rel, tmp_path, what="path")
    with pytest.raises(ToolError, match="outside"):
        tool_read_file(rel, workspace=tmp_path)
    with pytest.raises(ToolError, match="outside"):
        tool_write_file(rel, "nope", workspace=tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises((PathError, ConfigError), match="outside"):
        confine_pack_path(f"../{secret.name}", workspace)
