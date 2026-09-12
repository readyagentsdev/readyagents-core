"""Mapping tables as data. Adding coverage is a table edit plus a test."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from readyagents.errors import ImportRefused
from readyagents.importers.ir import SOURCES


@dataclass(frozen=True)
class MappingEntry:
    kind: str
    target: str | None
    fidelity: str
    reason: str
    nearest: str | None = None
    params: dict[str, Any] | None = None
    structural: str | None = None


@dataclass(frozen=True)
class MappingTable:
    source: str
    schema_versions: tuple[str, ...]
    entries: tuple[MappingEntry, ...]
    default: MappingEntry

    def lookup(self, kind: str) -> MappingEntry:
        exact = {item.kind: item for item in self.entries}
        if kind in exact:
            return exact[kind]
        lowered = kind.lower()
        for item in self.entries:
            token = item.kind.lower()
            if lowered.endswith("." + token) or lowered.endswith("/" + token):
                return item
        return self.default


def load_table(source: str) -> MappingTable:
    token = str(source or "").strip().lower()
    if token not in SOURCES:
        raise ImportRefused(
            f"unknown import source {source!r}; expected {', '.join(SOURCES)}",
            reason="unknown_source",
        )
    path = Path(__file__).resolve().parent / "tables" / f"{token}.json"
    if not path.is_file():
        raise ImportRefused(f"mapping table missing for {token}", reason="missing")
    blob = path.read_text(encoding="utf-8")
    data = json.loads(blob)
    entries = tuple(
        _entry(row, kind=str(row.get("kind") or "*")) for row in data.get("entries") or []
    )
    default = _entry(data.get("default") or {}, kind="*")
    versions = tuple(str(v) for v in (data.get("schema_versions") or []))
    return MappingTable(
        source=token,
        schema_versions=versions,
        entries=entries,
        default=default,
    )


def _entry(row: dict[str, Any], *, kind: str) -> MappingEntry:
    fidelity = str(row.get("fidelity") or "unsupported")
    if fidelity not in {"translated", "approximated", "unsupported"}:
        fidelity = "unsupported"
    target = row.get("target")
    return MappingEntry(
        kind=kind,
        target=str(target) if target else None,
        fidelity=fidelity,
        reason=str(row.get("reason") or "no mapping for this node kind"),
        nearest=str(row["nearest"]) if row.get("nearest") else None,
        params=dict(row["params"]) if isinstance(row.get("params"), dict) else None,
        structural=str(row["structural"]) if row.get("structural") else None,
    )
