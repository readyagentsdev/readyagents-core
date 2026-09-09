"""Adversarial read: product claims must not overclaim compliance or security."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_DOC_PATHS = [
    ROOT / "docs" / "compliance.md",
    ROOT / "docs" / "observability.md",
    ROOT / "SECURITY.md",
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
]

# Longest first so "compliant with the ai act" wins over "certified".
_FORBIDDEN = (
    ("compliant with the ai act", re.compile(r"compliant\s+with\s+the\s+ai\s+act", re.I)),
    ("makes you compliant", re.compile(r"makes\s+you\s+compliant", re.I)),
    ("guarantees compliance", re.compile(r"guarantees\s+compliance", re.I)),
    ("encryption at rest", re.compile(r"encryption\s+at\s+rest", re.I)),
    ("tamper-proof", re.compile(r"tamper[\s-]*proof", re.I)),
    ("certification", re.compile(r"\bcertification\b", re.I)),
    ("certified", re.compile(r"\bcertified\b", re.I)),
)

_NEAR_NEGATION = re.compile(
    r"\b(not|never|no|without|nor|neither|cannot|can't|don't|doesn't|"
    r"isn't|won't|do not|does not|is not|are not|was not|were not)\b",
    re.I,
)
_SECTION_NEGATION = re.compile(
    r"does not prove|do not (claim|provide|make|encrypt|ship)|"
    r"never (claim|compliance|certif)|not legal compliance|"
    r"not a certificate",
    re.I,
)


def _plain(text: str) -> str:
    text = re.sub(r"[*_`]+", "", text)
    return text.replace("\u2014", "-").replace("\u2013", "-")


def _is_negated(plain: str, start: int, end: int) -> bool:
    before = plain[max(0, start - 500) : start]
    after = plain[end : min(len(plain), end + 48)]
    near = before[-180:] + " " + after[:32]
    if _NEAR_NEGATION.search(near):
        return True
    if _SECTION_NEGATION.search(before):
        return True
    return False


def _iter_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for path in _DOC_PATHS:
        assert path.is_file(), f"missing required doc {path}"
        sources.append((path.name, path.read_text(encoding="utf-8")))
    try:
        from readyagents.compliance.evidence import PACK_README

        sources.append(("PACK_README", PACK_README))
    except ImportError:
        evidence = ROOT / "src" / "readyagents" / "compliance" / "evidence.py"
        if evidence.is_file():
            sources.append((evidence.name, evidence.read_text(encoding="utf-8")))
    return sources


def test_docs_do_not_overclaim() -> None:
    hits: list[str] = []
    for name, raw in _iter_sources():
        plain = _plain(raw)
        for label, pattern in _FORBIDDEN:
            for match in pattern.finditer(plain):
                if _is_negated(plain, match.start(), match.end()):
                    continue
                lo = max(0, match.start() - 60)
                hi = min(len(plain), match.end() + 40)
                snippet = re.sub(r"\s+", " ", plain[lo:hi]).strip()
                hits.append(f"{name}: {label!r} in …{snippet}…")
    assert not hits, "overclaim:\n" + "\n".join(hits)


def test_compliance_doc_exists_and_disclaims() -> None:
    text = (ROOT / "docs" / "compliance.md").read_text(encoding="utf-8")
    paragraphs = [
        _plain(part).strip().lower()
        for part in text.split("\n\n")
        if part.strip() and not part.strip().startswith("#")
    ]
    assert paragraphs, "docs/compliance.md has no body"
    first = paragraphs[0]
    assert "evidence" in first
    assert "not legal compliance" in first
    assert "not certification" in first
    assert "not legal advice" in first
    body = _plain(text).lower()
    assert "article 12" in body
    assert "article 13" in body
    assert "article 14" in body
    assert "operator" in body
