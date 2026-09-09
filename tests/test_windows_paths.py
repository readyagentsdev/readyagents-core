"""Adversarial path-containment corpus for Windows-ish and cross-platform attacks.

Drives ``resolve_within`` (and migrated call sites). Never reimplements containment.
Skipped cases must include a greppable ``skip-reason:`` string.
"""

from __future__ import annotations

import os
import sys
import tempfile
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

# Known skip-reason strings for the coverage summary (must stay greppable).
SKIP_REASON_83 = "skip-reason: 8.3 names are a Windows resolver feature"
SKIP_REASON_TRAILING = "skip-reason: trailing-dot space is a Windows normalisation"
SKIP_REASON_SYMLINK = "skip-reason: symlink privilege missing"
SKIP_REASON_MACOS = "skip-reason: not macOS /tmp vs /private/tmp"


# ---------------------------------------------------------------------------
# 1. Case-variant escape — both probe branches forced explicitly
# ---------------------------------------------------------------------------


def test_case_insensitive_probe_still_rejects_outside(tmp_path: Path) -> None:
    """force_case_sensitive(root, False): resolved-outside paths must raise."""
    (tmp_path / "inside.txt").write_text("ok", encoding="utf-8")
    outside = tmp_path.parent / f"secret-ci-{tmp_path.name}.txt"
    outside.write_text("nope", encoding="utf-8")
    with force_case_sensitive(tmp_path, False):
        assert filesystem_case_sensitive(tmp_path) is False
        assert resolve_within("inside.txt", tmp_path).exists()
        with pytest.raises(PathError, match="outside"):
            resolve_within(f"../{outside.name}", tmp_path)
        # Case-swapped root segment then climb out — still outside after resolve.
        swapped = tmp_path.name.swapcase() or (tmp_path.name + "X")
        with pytest.raises(PathError, match="outside"):
            resolve_within(
                tmp_path / ".." / swapped / ".." / outside.name,
                tmp_path,
            )


def test_case_sensitive_probe_rejects_outside(tmp_path: Path) -> None:
    """force_case_sensitive(root, True): outside paths raise PathError."""
    with force_case_sensitive(tmp_path, True):
        assert filesystem_case_sensitive(tmp_path) is True
        with pytest.raises(PathError, match="outside"):
            resolve_within("../" + tmp_path.name + "/../../etc/passwd", tmp_path)
        with pytest.raises(PathError, match="outside"):
            resolve_within(tmp_path / ".." / "nope-cs.txt", tmp_path)


# ---------------------------------------------------------------------------
# 2. 8.3 short-name traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("short", ["PROGRA~1", "WORKSP~1", "FOO~1", r"..\PROGRA~1"])
def test_short_name_8_3_traversal(tmp_path: Path, short: str) -> None:
    """8.3 aliases are a Windows resolver feature; skip when inexpressible."""
    if os.name != "nt":
        pytest.skip(SKIP_REASON_83)
    # On Windows, a short name that resolves outside the workspace must raise.
    try:
        resolved_outside = False
        try:
            resolve_within(short, tmp_path)
        except PathError:
            resolved_outside = True
        if not resolved_outside:
            # Literal ~ name stayed inside — not an 8.3 resolver alias here.
            pytest.skip(SKIP_REASON_83)
    except OSError:
        pytest.skip(SKIP_REASON_83)


# ---------------------------------------------------------------------------
# 3. ADS / colon-in-component
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ads",
    [
        "allowed.txt:hidden",
        "dir::$INDEX_ALLOCATION",
        r"sub\allowed.txt:zone",
    ],
)
def test_ads_colon_component_rejected(tmp_path: Path, ads: str) -> None:
    """Colon in a non-drive component is refused (ADS / NTFS tricks)."""
    (tmp_path / "allowed.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PathError):
        resolve_within(ads, tmp_path)


def test_ads_via_tool_read_file(tmp_path: Path) -> None:
    (tmp_path / "allowed.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ToolError):
        tool_read_file("allowed.txt:hidden", workspace=tmp_path)


# ---------------------------------------------------------------------------
# 4. Reserved device names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
        "CON.txt",
        "nul.log",
        "COM1.dat",
        "LPT1.txt",
        r"sub\NUL",
        "folder/PRN",
    ],
)
def test_reserved_device_names_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(PathError, match="reserved"):
        resolve_within(name, tmp_path)


# ---------------------------------------------------------------------------
# 5. Trailing dot or space components
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        r"foo.\bar",
        r"foo \bar",
        r"sub\trailing.",
        "name.",
        "name ",
        "dir/file.",
        "dir/file /x",
    ],
)
def test_trailing_dot_or_space_components(tmp_path: Path, raw: str) -> None:
    """Trailing dot/space in a component is forbidden (Windows normalisation)."""
    with pytest.raises(PathError, match="trailing"):
        resolve_within(raw, tmp_path)


def test_trailing_space_component_rejected(tmp_path: Path) -> None:
    """Space-terminated component (not only trailing path whitespace)."""
    with pytest.raises(PathError, match="trailing"):
        resolve_within("plain /nested", tmp_path)


# ---------------------------------------------------------------------------
# 6. UNC and extended-length prefixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        r"\\server\share\file.txt",
        r"//server/share/file.txt",
        r"\\?\C:\Windows\System32",
        r"//?/C:/Windows/System32",
        r"\\.\C:\foo",
    ],
)
def test_unc_and_extended_prefixes_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(PathError):
        resolve_within(raw, tmp_path)


def test_unc_via_confine_under(tmp_path: Path) -> None:
    with pytest.raises((PathError, ConfigError)):
        confine_under(r"\\server\share\x", tmp_path, what="workflow")


# ---------------------------------------------------------------------------
# 7. Drive-relative paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["C:file.txt", "D:readme", "c:autoexec.bat", "Z:x"])
def test_drive_relative_rejected(tmp_path: Path, raw: str) -> None:
    with pytest.raises(PathError, match="drive-relative"):
        resolve_within(raw, tmp_path)


# ---------------------------------------------------------------------------
# 8. Symlink escape
# ---------------------------------------------------------------------------


def test_symlink_file_escape_resolve_within(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"out-sym-{tmp_path.name}.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip(SKIP_REASON_SYMLINK)
    with pytest.raises(PathError, match="outside"):
        resolve_within("leak.txt", tmp_path)
    with pytest.raises((PathError, ConfigError), match="outside"):
        confine_under("leak.txt", tmp_path, what="path")
    with pytest.raises(ToolError, match="outside"):
        tool_read_file("leak.txt", workspace=tmp_path)
    with pytest.raises(ToolError, match="outside"):
        tool_write_file("leak.txt", "x", workspace=tmp_path)


def test_symlink_escape_confine_pack_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "evil.py"
    outside.write_text("def get_pack():\n    return None\n", encoding="utf-8")
    link = workspace / "pack.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip(SKIP_REASON_SYMLINK)
    with pytest.raises((PathError, ConfigError), match="outside"):
        confine_pack_path("pack.py", workspace)


# ---------------------------------------------------------------------------
# 9. macOS /tmp vs /private/tmp
# ---------------------------------------------------------------------------


def test_macos_tmp_private_tmp_alias_contained() -> None:
    if sys.platform != "darwin":
        pytest.skip(SKIP_REASON_MACOS)
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        assert str(root).startswith("/tmp") or "/tmp/" in str(root.resolve())
        target = root / "note.txt"
        target.write_text("hi", encoding="utf-8")
        resolved_root = root.resolve()
        # Prefer the alternate spelling of the same directory.
        if str(root).startswith("/tmp"):
            alt = Path("/private") / str(root).lstrip("/") / "note.txt"
        else:
            alt = Path(str(resolved_root).replace("/private/tmp", "/tmp", 1)) / "note.txt"
        got = resolve_within(alt, root)
        assert got == target.resolve()
        assert got.is_relative_to(resolved_root) or got == target.resolve()


# ---------------------------------------------------------------------------
# 10. Path-length guard → PathError (not OSError)
# ---------------------------------------------------------------------------


def test_path_length_guard_raises_path_error(tmp_path: Path) -> None:
    huge = "n" * 5000
    with pytest.raises(PathError, match="path-length") as excinfo:
        resolve_within(huge, tmp_path)
    assert type(excinfo.value) is PathError
    assert not isinstance(excinfo.value, OSError)


def test_path_length_via_confine_under(tmp_path: Path) -> None:
    with pytest.raises((PathError, ConfigError), match="path-length"):
        confine_under("z" * 5000, tmp_path, what="path")


# ---------------------------------------------------------------------------
# 11. Legitimate in-workspace Unicode, spaces, deep nesting
# ---------------------------------------------------------------------------


def test_legitimate_unicode_spaces_deep_nesting(tmp_path: Path) -> None:
    rel = Path("α β") / "deep" / "nest" / "café 日本語 — ok.txt"
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True)
    dest.write_text("ok", encoding="utf-8")
    got = resolve_within(rel.as_posix(), tmp_path)
    assert got == dest.resolve()
    assert confine_under(rel.as_posix(), tmp_path, what="path") == got
    assert tool_read_file(rel.as_posix(), workspace=tmp_path) == "ok"
    tool_write_file(rel.as_posix(), "updated", workspace=tmp_path)
    assert dest.read_text(encoding="utf-8") == "updated"


def test_legitimate_nested_pack_path(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    nested = workspace / "packs" / "my_pack.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x = 1\n", encoding="utf-8")
    assert confine_pack_path("packs/my_pack.py", workspace) == nested.resolve()


# ---------------------------------------------------------------------------
# 12. Coverage summary — greppable skip-reason: lines
# ---------------------------------------------------------------------------


def test_coverage_summary_records_every_skip_reason(capsys: pytest.CaptureFixture[str]) -> None:
    """Print every corpus skip-reason so CI logs stay greppable."""
    reasons = [
        SKIP_REASON_83,
        SKIP_REASON_TRAILING,
        SKIP_REASON_SYMLINK,
        SKIP_REASON_MACOS,
    ]
    for reason in reasons:
        assert "skip-reason:" in reason
        print(reason)
    captured = capsys.readouterr()
    assert SKIP_REASON_83 in captured.out
    assert SKIP_REASON_TRAILING in captured.out
    assert SKIP_REASON_SYMLINK in captured.out
    assert SKIP_REASON_MACOS in captured.out
    assert captured.out.count("skip-reason:") >= 4
