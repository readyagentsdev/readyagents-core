"""Structured diffs: original + opcodes, not two stored blobs."""

from __future__ import annotations

import difflib
from typing import Any


def structured_diff(original: Any, edited: Any) -> list[dict[str, str]]:
    left = str(original if original is not None else "").splitlines(keepends=True)
    right = str(edited if edited is not None else "").splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=left, b=right)
    out: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out.append(
            {
                "op": tag,
                "from": "".join(left[i1:i2]),
                "to": "".join(right[j1:j2]),
            }
        )
    return out


def apply_diff(original: Any, diff: list[dict[str, str]] | None) -> str:
    """Rebuild the edited text from original + structured diff."""
    if not diff:
        return str(original if original is not None else "")
    left = str(original if original is not None else "")
    rebuilt: list[str] = []
    cursor = 0
    src = left
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
    return result if result else str(new if diff else left)
