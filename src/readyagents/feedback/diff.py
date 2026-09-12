"""Structured diffs: original + opcodes with indices, not two stored blobs."""

from __future__ import annotations

import difflib
from typing import Any


def structured_diff(original: Any, edited: Any) -> list[dict[str, Any]]:
    """Store every opcode with line indices so apply_diff round-trips.

    Equal chunks keep indices only (no duplicated text). Insert/replace
    carry ``to``; delete/replace carry ``from``.
    """
    left = str(original if original is not None else "").splitlines(keepends=True)
    right = str(edited if edited is not None else "").splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=left, b=right)
    out: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        row: dict[str, Any] = {"op": tag, "i1": i1, "i2": i2, "j1": j1, "j2": j2}
        if tag in {"replace", "delete"}:
            row["from"] = "".join(left[i1:i2])
        if tag in {"replace", "insert"}:
            row["to"] = "".join(right[j1:j2])
        out.append(row)
    return out


def apply_diff(original: Any, diff: list[dict[str, Any]] | None) -> str:
    """Rebuild the edited text from original + indexed opcodes."""
    if not diff:
        return str(original if original is not None else "")
    left = str(original if original is not None else "").splitlines(keepends=True)
    if any("i1" in row for row in diff if isinstance(row, dict)):
        rebuilt: list[str] = []
        for row in diff:
            if not isinstance(row, dict):
                continue
            op = str(row.get("op") or "")
            i1 = int(row.get("i1") or 0)
            i2 = int(row.get("i2") or 0)
            if op == "equal":
                rebuilt.extend(left[i1:i2])
            elif op == "delete":
                continue
            else:
                rebuilt.append(str(row.get("to") or ""))
        return "".join(rebuilt)
    return _legacy_apply(left, diff)


def _legacy_apply(lines: list[str], diff: list[dict[str, Any]]) -> str:
    src = "".join(lines)
    rebuilt: list[str] = []
    cursor = 0
    new = ""
    for row in diff:
        old = str(row.get("from") or "")
        new = str(row.get("to") or "")
        op = str(row.get("op") or "")
        if old:
            idx = src.find(old, cursor)
            if idx >= 0:
                rebuilt.append(src[cursor:idx])
                if op != "delete":
                    rebuilt.append(new)
                cursor = idx + len(old)
                continue
        if op in {"insert", "replace"}:
            rebuilt.append(new)
    rebuilt.append(src[cursor:])
    result = "".join(rebuilt)
    return result if result else str(new if diff else src)
