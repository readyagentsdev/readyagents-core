"""Adversarial read: docs claim evidence, never certification or compliance."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ROOT / "docs" / "compliance.md",
    ROOT / "docs" / "observability.md",
    ROOT / "SECURITY.md",
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "src" / "readyagents" / "compliance" / "evidence.py",
]
FORBIDDEN = (
    r"\bcertified\b",
    r"\bcertification\b",
    r"makes you compliant",
    r"guarantees compliance",
    r"compliant with the ai act",
    r"tamper-proof",
    r"tamperproof",
    r"encryption at rest",
)
_NEGATION = re.compile(
    r"(not|never|no|without|isn't|is not|does not|don't|do not)\b.{0,40}$",
    re.I,
)


def test_docs_do_not_overclaim() -> None:
    hits: list[str] = []
    for path in TARGETS:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for pattern in FORBIDDEN:
            for match in re.finditer(pattern, lowered):
                start = max(0, match.start() - 80)
                prefix = lowered[start : match.start()]
                if _NEGATION.search(prefix) or "not " + match.group(0) in lowered:
                    continue
                if match.group(0) in {"certification", "certified"} and any(
                    token in prefix[-40:] for token in ("never", "not a", "not legal", "no certif")
                ):
                    continue
                hits.append(f"{path.name}: {match.group(0)!r} …{text[start : match.end() + 20]!r}")
    assert not hits, "overclaim:\n" + "\n".join(hits)


def test_compliance_doc_exists_and_disclaims() -> None:
    text = (ROOT / "docs" / "compliance.md").read_text(encoding="utf-8").lower()
    assert "not" in text and "legal" in text
    assert "evidence" in text
    assert "article 12" in text
    assert "article 13" in text
    assert "article 14" in text
