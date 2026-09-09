from __future__ import annotations

from pathlib import Path

import readyagents


def _stability_names() -> list[str]:
    text = Path("docs/stability.md").read_text(encoding="utf-8")
    names: list[str] = []
    capture = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "```python":
            capture = True
            continue
        if capture and stripped == "```":
            break
        if not capture:
            continue
        if stripped.startswith('"') and stripped.endswith('",'):
            names.append(stripped[1:-2])
        elif stripped.startswith('"') and stripped.endswith('"'):
            names.append(stripped[1:-1])
    return names


def test_all_matches_stability_doc() -> None:
    names = _stability_names()
    assert list(readyagents.__all__) == names


def test_version_dunder() -> None:
    assert "__version__" in readyagents.__all__
    assert readyagents.__version__
