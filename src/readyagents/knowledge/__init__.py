"""Knowledge pipelines: ingest, chunk, cite, sync. Plumbing, not retrieval quality."""

from __future__ import annotations

from readyagents.knowledge.cite import citation_from_record, parse_citation, resolve_citation

__all__ = ["citation_from_record", "parse_citation", "resolve_citation"]
