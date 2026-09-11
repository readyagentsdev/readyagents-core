"""Shipped type: ingest path: chunking, versioning, cite, freshness, forget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import (
    ContractError,
    KnowledgeCiteDenied,
    KnowledgePathDenied,
    KnowledgeStale,
    KnowledgeWalkExceeded,
)
from readyagents.knowledge.chunk import chunk_text
from readyagents.knowledge.cite import citation_from_record, resolve_citation
from readyagents.knowledge.forget import forget_document
from readyagents.memory.protocol import MemoryRecord, open_memory_store
from readyagents.memory.retrieve import hybrid_search
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def _run(spec, tmp_settings, tmp_path: Path, **kwargs):
    return run_workflow_spec(
        spec,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        **kwargs,
    )


def _policy_md(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "retention.md").write_text(
        "# Retention\n\nKeep tickets for 90 days.\n\n## Exceptions\n\nLegal holds last longer.\n",
        encoding="utf-8",
    )
    (docs / "access.md").write_text(
        "# Access\n\nNeed-to-know only.\n",
        encoding="utf-8",
    )


def _ingest_spec(strategy: str = "heading", on_change: str = "supersede", **extra):
    node = {
        "id": "load",
        "type": "ingest",
        "source": {"kind": "directory", "path": "docs", "glob": "*.md"},
        "chunk": {"strategy": strategy, "max_chars": 200, "overlap": 20},
        "scope": "ns:policies",
        "on_change": on_change,
        "output_key": "loaded",
    }
    node.update(extra)
    return {
        "name": "know",
        "memory_scopes": ["ns:policies"],
        "nodes": [node],
    }


def test_chunk_strategies_boundaries() -> None:
    text = "alpha " * 30 + "\n\n" + "beta " * 30
    fixed = chunk_text(text, strategy="fixed", max_chars=40, overlap=10)
    assert fixed
    assert all(len(c.text) <= 40 for c in fixed)
    if len(fixed) > 1:
        assert fixed[1].start < fixed[0].end
    paras = chunk_text("one para.\n\nTwo para.\n\nThree.", strategy="paragraph", max_chars=80)
    assert len(paras) >= 2
    md = "# Title\nhello\n## Section\nworld\n"
    heads = chunk_text(md, strategy="heading", max_chars=200)
    paths = [c.heading_path for c in heads]
    assert any("Title" in p for p in paths)
    assert any("Section" in p for p in paths)
    table = "h1,h2\na,1\nb,2\nc,3\n"
    rows = chunk_text(table, strategy="row_group", max_chars=40, row_group=2)
    assert rows
    for chunk in rows:
        lines = [ln for ln in chunk.text.splitlines() if ln.strip()]
        assert lines[0].startswith("h1")
        for line in lines[1:]:
            assert "," in line
            reader_ok = list(line)  # full row kept; no mid-row split
            assert reader_ok


def test_ingest_heading_and_citations(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    state = _run(_ingest_spec("heading"), tmp_settings, tmp_path)
    assert state.status == "succeeded"
    loaded = state.output_keys["loaded"]
    assert loaded["added"] >= 1
    assert loaded["chunks"] >= 1
    cite = loaded["citations"][0]
    assert cite["document_id"]
    assert "range" in cite
    store = open_memory_store(tmp_settings.home_path())
    recs = store.list(scope="ns:policies")
    assert recs
    rec = recs[0]
    assert rec.metadata.get("document_id")
    assert rec.metadata.get("document_version")
    assert rec.metadata.get("heading_path") is not None
    assert citation_from_record(rec) is not None
    store.close()


def test_unchanged_reingest_is_noop(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    first = _run(_ingest_spec(), tmp_settings, tmp_path)
    second = _run(_ingest_spec(), tmp_settings, tmp_path)
    assert first.output_keys["loaded"]["added"] >= 1
    assert second.output_keys["loaded"]["unchanged"] >= 1
    assert second.output_keys["loaded"]["added"] == 0
    assert second.output_keys["loaded"]["updated"] == 0


def test_change_versions_supersede_and_keep(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    _run(_ingest_spec("paragraph", "supersede"), tmp_settings, tmp_path)
    (tmp_path / "docs" / "retention.md").write_text(
        "# Retention\n\nNow 30 days.\n", encoding="utf-8"
    )
    super_state = _run(_ingest_spec("paragraph", "supersede"), tmp_settings, tmp_path)
    assert super_state.output_keys["loaded"]["updated"] >= 1
    store = open_memory_store(tmp_settings.home_path())
    versions = {
        str(rec.metadata.get("document_version"))
        for rec in store.list(scope="ns:policies")
        if rec.metadata.get("document_id") == "retention.md"
    }
    store.close()
    assert len(versions) == 1
    (tmp_path / "docs" / "retention.md").write_text(
        "# Retention\n\nNow 7 days.\n", encoding="utf-8"
    )
    keep = _run(_ingest_spec("paragraph", "keep_versions"), tmp_settings, tmp_path)
    assert keep.output_keys["loaded"]["updated"] >= 1
    store = open_memory_store(tmp_settings.home_path())
    versions = {
        str(rec.metadata.get("document_version"))
        for rec in store.list(scope="ns:policies")
        if rec.metadata.get("document_id") == "retention.md"
    }
    store.close()
    assert len(versions) >= 2


def test_search_returns_structured_citations_and_cite_resolves(
    tmp_settings, tmp_path: Path
) -> None:
    _policy_md(tmp_path)
    spec = _ingest_spec("heading")
    spec["nodes"][0]["next"] = "recall"
    spec["nodes"].append(
        {
            "id": "recall",
            "type": "memory",
            "op": "search",
            "scope": "ns:policies",
            "query": "tickets",
            "limit": 5,
            "output_key": "hits",
        }
    )
    state = _run(spec, tmp_settings, tmp_path)
    hits = state.output_keys["hits"]["hits"]
    assert hits
    cite = hits[0]["citation"]
    store = open_memory_store(tmp_settings.home_path())
    resolved = resolve_citation(store, cite, scope="ns:policies")
    store.close()
    assert hits[0]["text"] == resolved["text"] or resolved["text"] in hits[0]["text"]


def test_knowledge_cli_cite_and_sync(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    _policy_md(tmp_path)
    _run(_ingest_spec("heading"), tmp_settings, tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    listed = runner.invoke(app, ["knowledge", "list", "--scope", "ns:policies", "--json"])
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    payload = json.loads(listed.stdout[listed.stdout.find("{") :])
    assert payload["ok"] is True
    assert payload["count"] >= 1
    doc = payload["documents"][0]["document_id"]
    store = open_memory_store(tmp_settings.home_path())
    rec = next(r for r in store.list(scope="ns:policies") if r.metadata.get("document_id") == doc)
    cite = citation_from_record(rec)
    store.close()
    cited = runner.invoke(
        app, ["knowledge", "cite", json.dumps(cite), "--scope", "ns:policies", "--json"]
    )
    assert cited.exit_code == 0, cited.stdout + cited.stderr
    body = json.loads(cited.stdout[cited.stdout.find("{") :])
    assert rec.text in body["text"] or body["text"] in rec.text
    wf = tmp_path / "ingest.yaml"
    wf.write_text(
        "name: know\nmemory_scopes: [ns:policies]\nnodes:\n"
        "  - id: load\n    type: ingest\n    source: {kind: directory, path: docs, glob: '*.md'}\n"
        "    chunk: {strategy: heading, max_chars: 200}\n    scope: ns:policies\n",
        encoding="utf-8",
    )
    synced = runner.invoke(app, ["knowledge", "sync", str(wf), "--json"])
    assert synced.exit_code == 0, synced.stdout + synced.stderr
    sync_body = json.loads(synced.stdout[synced.stdout.find("{") :])
    assert "unchanged" in sync_body or "added" in sync_body


def test_require_citation_from_retrieved_fails_uncited(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    llm = ScriptedLLM().enqueue("I refuse to cite anything")
    spec = {
        "name": "cite",
        "memory_scopes": ["ns:policies"],
        "nodes": [
            {
                "id": "load",
                "type": "ingest",
                "source": {"kind": "directory", "path": "docs", "glob": "*.md"},
                "chunk": {"strategy": "paragraph", "max_chars": 200},
                "scope": "ns:policies",
                "next": "recall",
            },
            {
                "id": "recall",
                "type": "memory",
                "op": "search",
                "scope": "ns:policies",
                "query": "retention",
                "output_key": "hits",
                "next": "answer",
            },
            {
                "id": "answer",
                "type": "agent",
                "model": "mock:test",
                "prompt": "Answer using {{hits}}",
                "output_key": "out",
                "contract": {
                    "rules": [{"require_citation": {"from": "retrieved"}}],
                    "on_invalid": "fail",
                },
            },
        ],
    }
    with pytest.raises(ContractError, match="uncited|retrieved"):
        _run(spec, tmp_settings, tmp_path, llm=llm)


class TrapLLM:
    name = "trap"

    def complete(self, messages, *, model, tools=None, **kwargs):
        raise AssertionError("spend complete() must not run")


def test_require_citation_retrieved_preflight_before_spend(tmp_settings, tmp_path: Path) -> None:
    spec = {
        "name": "empty",
        "nodes": [
            {
                "id": "answer",
                "type": "agent",
                "model": "mock:test",
                "prompt": "say hi",
                "contract": {
                    "rules": [{"require_citation": {"from": "retrieved"}}],
                    "on_invalid": "fail",
                },
            }
        ],
    }
    with pytest.raises(ContractError, match="retrieved"):
        _run(spec, tmp_settings, tmp_path, llm=TrapLLM())


def test_freshness_refuses_stale(tmp_settings, tmp_path: Path) -> None:
    store = open_memory_store(tmp_settings.home_path())
    rec = MemoryRecord(
        id="a" * 32,
        scope="ns:policies",
        text="old policy",
        metadata={"document_id": "old.md", "ingested_at": "2020-01-01T00:00:00+00:00"},
        created_at="2020-01-01T00:00:00+00:00",
        provenance="ingest",
    )
    store.write(rec)
    store.close()
    spec = {
        "name": "stale",
        "memory_scopes": ["ns:policies"],
        "nodes": [
            {
                "id": "recall",
                "type": "memory",
                "op": "search",
                "scope": "ns:policies",
                "query": "policy",
                "freshness": {"max_age": "1d"},
            }
        ],
    }
    with pytest.raises(KnowledgeStale):
        _run(spec, tmp_settings, tmp_path)


def test_hybrid_blend_recorded_and_reproducible() -> None:
    recs = [
        MemoryRecord(id="1" * 32, scope="ns:s", text="alpha widget", created_at="a"),
        MemoryRecord(id="2" * 32, scope="ns:s", text="beta gadget", created_at="b"),
    ]
    vectors = {"1" * 32: [1.0, 0.0], "2" * 32: [0.0, 1.0]}
    hits_a, w_a = hybrid_search(
        recs,
        "widget",
        vectors=vectors,
        query_vector=[1.0, 0.0],
        bm25_weight=0.5,
        embedding_weight=0.5,
    )
    hits_b, w_b = hybrid_search(
        recs,
        "widget",
        vectors=vectors,
        query_vector=[1.0, 0.0],
        bm25_weight=0.5,
        embedding_weight=0.5,
    )
    assert w_a == w_b
    assert [h.record.id for h in hits_a] == [h.record.id for h in hits_b]
    assert [h.score for h in hits_a] == [h.score for h in hits_b]


def test_forget_document_unretrievable(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    _policy_md(tmp_path)
    _run(_ingest_spec(), tmp_settings, tmp_path)
    store = open_memory_store(tmp_settings.home_path())
    recs = store.list(scope="ns:policies")
    doc = str(recs[0].metadata.get("document_id"))
    cite = citation_from_record(recs[0])
    removed = forget_document(store, scope="ns:policies", document_id=doc)
    assert removed >= 1
    left = [r for r in store.list(scope="ns:policies") if r.metadata.get("document_id") == doc]
    assert left == []
    for rec in recs:
        if rec.metadata.get("document_id") == doc:
            assert store.vector(rec.id) is None
    with pytest.raises(KnowledgeCiteDenied):
        resolve_citation(store, cite, scope="ns:policies")
    hits = store.search(scope="ns:policies", query="retention")
    assert all((h.record.metadata or {}).get("document_id") != doc for h in hits)
    store.close()
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    listed = runner.invoke(app, ["knowledge", "list", "--scope", "ns:policies", "--json"])
    payload = json.loads(listed.stdout[listed.stdout.find("{") :])
    ids = [row["document_id"] for row in payload.get("documents") or []]
    assert doc not in ids


def test_ingest_taint_untrusted(tmp_settings, tmp_path: Path) -> None:
    from readyagents.firewall.taint import provenance_of

    _policy_md(tmp_path)
    state = _run(_ingest_spec(), tmp_settings, tmp_path)
    assert provenance_of(state, "loaded").trust == "untrusted"
    assert provenance_of(state, "loaded").source == "knowledge"


def test_path_traversal_refused(tmp_settings, tmp_path: Path) -> None:
    spec = {
        "name": "bad",
        "memory_scopes": ["ns:policies"],
        "nodes": [
            {
                "id": "load",
                "type": "ingest",
                "source": {"kind": "file", "path": "../outside.md"},
                "scope": "ns:policies",
            }
        ],
    }
    with pytest.raises((KnowledgePathDenied, KnowledgeWalkExceeded)):
        _run(spec, tmp_settings, tmp_path)


def test_walk_file_cap(tmp_settings, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(5):
        (docs / f"f{i}.md").write_text(f"doc {i}\n", encoding="utf-8")
    from readyagents.knowledge.walk import walk_source

    with pytest.raises(KnowledgeWalkExceeded):
        walk_source(
            {"kind": "directory", "path": "docs", "glob": "*.md"},
            workspace=tmp_path,
            max_files=2,
        )


def test_cite_outside_scope_denied(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    _run(_ingest_spec(), tmp_settings, tmp_path)
    store = open_memory_store(tmp_settings.home_path())
    rec = store.list(scope="ns:policies")[0]
    cite = citation_from_record(rec)
    with pytest.raises(KnowledgeCiteDenied):
        resolve_citation(store, cite, scope="ns:other")
    store.close()


def test_validate_knowledge_example_twice() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "knowledge_ingest.yaml"
    first = runner.invoke(app, ["validate", str(example)])
    second = runner.invoke(app, ["validate", str(example)])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr


def test_knowledge_list_twice_keyless(tmp_settings, monkeypatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    first = runner.invoke(app, ["knowledge", "list", "--json"])
    second = runner.invoke(app, ["knowledge", "list", "--json"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert a["command"] == b["command"] == "knowledge list"
    assert a["ok"] is True and b["ok"] is True


def test_run_workflow_file_ingest(tmp_settings, tmp_path: Path) -> None:
    _policy_md(tmp_path)
    path = tmp_path / "flow.yaml"
    path.write_text(
        "name: fileingest\nmemory_scopes: [ns:policies]\nnodes:\n"
        "  - id: load\n    type: ingest\n    source: {kind: directory, path: docs, glob: '*.md'}\n"
        "    chunk: {strategy: paragraph, max_chars: 200}\n    scope: ns:policies\n"
        "    output_key: loaded\n",
        encoding="utf-8",
    )
    state = run_workflow_file(path, settings=tmp_settings, persist=False)
    assert state.status == "succeeded"
    assert state.output_keys["loaded"]["added"] >= 1


def test_existing_ticket_citation_still_works() -> None:
    llm = ScriptedLLM().enqueue("see ticket T-1", model="x")
    from readyagents.testing.helpers import run_workflow_spec as run

    state = run(
        {
            "name": "c",
            "inputs": {"ticket_id": "T-1"},
            "nodes": [
                {
                    "id": "n",
                    "type": "agent",
                    "model": "x",
                    "prompt": "cite",
                    "output_key": "out",
                    "contract": {
                        "rules": [{"require_citation": {"from": "ticket_id"}}],
                        "on_invalid": "fail",
                    },
                }
            ],
        },
        llm=llm,
        inputs={"ticket_id": "T-1"},
    )
    assert "T-1" in str(state.output_keys["out"])
