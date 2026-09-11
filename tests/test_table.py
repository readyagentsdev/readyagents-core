"""Shipped type: table and type: classify path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.cost.ledger import read_spend_entries
from readyagents.cost.meter import SpendMeter
from readyagents.errors import (
    NodeError,
    TableCapExceeded,
    TableExtraMissing,
    TablePathDenied,
    TableRowError,
    TableSchemaError,
)
from readyagents.replay.cassette import Cassette
from readyagents.table.frame import apply_op as frame_apply
from readyagents.table.frame import available as frame_available
from readyagents.table.ops import apply_op
from readyagents.table.part import is_table_ref
from readyagents.table.store import TableStore
from readyagents.testing.helpers import ScriptedLLM, run_workflow_spec
from readyagents.tools import default_registry
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def _run(spec, tmp_settings, tmp_path: Path, **kwargs):
    return run_workflow_spec(
        spec,
        pin_home=tmp_settings.home_path(),
        workflow_dir=tmp_path,
        **kwargs,
    )


def _csv(tmp_path: Path) -> Path:
    path = tmp_path / "exports.csv"
    path.write_text(
        "id,email,amount\n1,ada@x.test,5\n2,bob@x.test,20\n2,bob@x.test,20\n3,cy@x.test,15\n",
        encoding="utf-8",
    )
    return path


def _load_spec(**extra):
    node = {
        "id": "load",
        "type": "table",
        "op": "read",
        "source": {"kind": "csv", "path": "exports.csv"},
        "schema": {"id": "int", "email": "str", "amount": "float"},
        "output_key": "rows",
    }
    node.update(extra)
    return {"name": "pipe", "nodes": [node]}


def test_read_stores_hash_not_rows(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    state = _run(_load_spec(), tmp_settings, tmp_path)
    assert state.status == "succeeded"
    ref = state.output_keys["rows"]
    assert is_table_ref(ref)
    assert ref["row_count"] == 4
    assert "columns" in ref
    blob = json.dumps(state.to_record())
    assert "ada@x.test" not in blob
    assert ref["_table"] is True
    store = TableStore(tmp_settings.home_path() / "tables")
    assert store.has(ref["sha256"])
    rows = list(store.iter_rows(ref["sha256"]))
    assert rows[0]["id"] == 1


def test_eight_ops_match_fixtures(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    store = TableStore(tmp_path / "tables")
    loaded = _run(_load_spec(), tmp_settings, tmp_path)
    left = _part(tmp_settings, loaded.output_keys["rows"])
    store = TableStore(tmp_settings.home_path() / "tables")
    selected = apply_op("select", store, left, columns=["id", "amount"])
    assert [c.name for c in selected.columns] == ["id", "amount"]
    filtered = apply_op("filter", store, left, when="amount > 10")
    assert filtered.row_count == 3
    deduped = apply_op("dedupe", store, left, keys=["email"], keep="first")
    assert deduped.row_count == 3
    sorted_p = apply_op("sort", store, left, by=["amount"], descending=True)
    amounts = [r["amount"] for r in store.iter_rows(sorted_p.sha256)]
    assert amounts == sorted(amounts, reverse=True)
    derived = apply_op("derive", store, left, derive={"name": "double", "expr": "amount * 2"})
    first = next(store.iter_rows(derived.sha256))
    assert first["double"] == first["amount"] * 2
    grouped = apply_op(
        "aggregate", store, left, keys=["email"], metrics={"amount": "sum", "id": "count"}
    )
    assert grouped.row_count == 3
    other = apply_op("select", store, left, columns=["id", "email", "amount"])
    united = apply_op("union", store, left, right=other)
    assert united.row_count == 8
    joined = apply_op("join", store, left, right=other, on=["id"], how="inner")
    assert joined.row_count >= 4


def test_stdlib_and_frame_hashes_match(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    state = _run(_load_spec(), tmp_settings, tmp_path)
    store = TableStore(tmp_settings.home_path() / "tables")
    left = _part(tmp_settings, state.output_keys["rows"])
    right = apply_op("select", store, left, columns=["id", "email", "amount"])
    cases = [
        ("select", {"columns": ["email", "amount"]}),
        ("filter", {"when": "amount > 10"}),
        ("sort", {"by": ["id"], "descending": False}),
        ("dedupe", {"keys": ["email"], "keep": "first"}),
        ("derive", {"derive": {"name": "n", "expr": "amount + 1"}}),
        ("aggregate", {"keys": ["email"], "metrics": {"amount": "sum"}}),
        ("union", {"right": right}),
        ("join", {"right": right, "on": ["id"], "how": "inner"}),
    ]
    for op, kwargs in cases:
        std = apply_op(op, store, left, **kwargs)
        if not frame_available():
            with pytest.raises(TableExtraMissing):
                frame_apply(op, store, left, **kwargs)
            continue
        extra = frame_apply(op, store, left, **kwargs)
        assert extra.sha256 == std.sha256, op


def test_schema_mismatch_names_row_column_not_value(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_text("id,email\nnot-an-int,secret-value\n", encoding="utf-8")
    spec = {
        "name": "bad",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "bad.csv"},
                "schema": {"id": "int", "email": "str"},
            }
        ],
    }
    with pytest.raises(TableSchemaError, match="row 0, column id") as exc:
        _run(spec, tmp_settings, tmp_path)
    assert "secret-value" not in str(exc.value)
    assert "not-an-int" not in str(exc.value)


def test_classify_rules_first_remainder_ledger(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    llm = ScriptedLLM().enqueue(
        '[{"index": 1, "label": "review"}, {"index": 2, "label": "review"}, '
        '{"index": 3, "label": "reject"}]',
        model="mock:test",
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    )
    meter = SpendMeter()
    spec = _load_spec()
    spec["nodes"][0]["next"] = "triage"
    spec["nodes"].append(
        {
            "id": "triage",
            "type": "classify",
            "source": "{{ rows }}",
            "rules": [{"when": "amount < 10", "label": "auto_approve"}],
            "model_for_remainder": {
                "model": "mock:test",
                "batch": 25,
                "labels": ["approve", "review", "reject"],
            },
            "output_key": "labelled",
        }
    )
    state = _run(spec, tmp_settings, tmp_path, llm=llm, spend_meter=meter)
    assert state.status == "succeeded"
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["messages"][0].content
    assert "ada@x.test" not in prompt
    stats = state.metadata["classify"]["triage"]
    assert stats["rule_rows"] == 1
    assert stats["model_rows"] == 3
    assert stats["model_calls"] == 1
    assert meter.model_calls == 1
    store = TableStore(tmp_settings.home_path() / "tables")
    ref = state.output_keys["labelled"]
    rows = list(store.iter_rows(ref["sha256"]))
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["label"] == "auto_approve"
    assert by_id[1]["decision"] == "rule"
    assert by_id[2]["decision"] == "model"
    blob = json.dumps(state.to_record())
    assert "ada@x.test" not in blob


class TrapLLM:
    name = "trap"

    def complete(self, messages, *, model, tools=None, **kwargs):
        raise AssertionError("model must not run when rules cover every row")


def test_classify_all_rules_no_spend(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "exports.csv").write_text(
        "id,email,amount\n1,ada@x.test,1\n2,bob@x.test,2\n", encoding="utf-8"
    )
    spec = _load_spec()
    spec["nodes"][0]["next"] = "triage"
    spec["nodes"].append(
        {
            "id": "triage",
            "type": "classify",
            "source": "{{ rows }}",
            "rules": [{"when": "amount < 10", "label": "auto_approve"}],
            "model_for_remainder": {"model": "mock:test", "batch": 25, "labels": ["review"]},
            "output_key": "labelled",
        }
    )
    state = _run(spec, tmp_settings, tmp_path, llm=TrapLLM())
    assert state.status == "succeeded"
    assert state.metadata["classify"]["triage"]["model_calls"] == 0


def test_on_row_error_fail_skip_quarantine(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    spec = _load_spec()
    spec["nodes"][0]["next"] = "triage"
    spec["nodes"].append(
        {
            "id": "triage",
            "type": "classify",
            "source": "{{ rows }}",
            "rules": [],
            "model_for_remainder": {
                "model": "mock:test",
                "batch": 25,
                "labels": ["approve"],
            },
            "on_row_error": "fail",
            "output_key": "labelled",
        }
    )
    llm = ScriptedLLM().enqueue('[{"index": 0, "label": "nope"}]', model="mock:test")
    with pytest.raises(TableRowError, match="row 0"):
        _run(spec, tmp_settings, tmp_path, llm=llm)
    llm = ScriptedLLM().enqueue(
        '[{"index": 0, "label": "nope"}, {"index": 1, "label": "approve"}, '
        '{"index": 2, "label": "approve"}, {"index": 3, "label": "approve"}]',
        model="mock:test",
    )
    spec["nodes"][1]["on_row_error"] = "skip"
    skipped = _run(spec, tmp_settings, tmp_path, llm=llm)
    assert skipped.output_keys["labelled"]["row_count"] == 3
    llm = ScriptedLLM().enqueue(
        '[{"index": 0, "label": "nope"}, {"index": 1, "label": "approve"}, '
        '{"index": 2, "label": "approve"}, {"index": 3, "label": "approve"}]',
        model="mock:test",
    )
    spec["nodes"][1]["on_row_error"] = "quarantine"
    quarantined = _run(spec, tmp_settings, tmp_path, llm=llm)
    ref = quarantined.output_keys["labelled"]
    assert ref["row_count"] == 3
    assert ref["errors_row_count"] >= 1
    store = TableStore(tmp_settings.home_path() / "tables")
    err_ids = {r["row"] for r in store.iter_rows(ref["errors_sha256"])}
    good_ids = {r["id"] for r in store.iter_rows(ref["sha256"])}
    assert 0 in err_ids
    assert 1 not in good_ids
    assert 2 in good_ids


def test_csv_jsonl_roundtrip_and_injection(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "in.csv").write_text(
        "id,note\n1,hello\n2,=cmd|A1\n3,+HYPERLINK\n", encoding="utf-8"
    )
    spec = {
        "name": "io",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "in.csv"},
                "schema": {"id": "int", "note": "str"},
                "output_key": "rows",
                "next": "save",
            },
            {
                "id": "save",
                "type": "table",
                "op": "write",
                "source": "{{ rows }}",
                "path": "out.csv",
                "output_key": "written",
            },
        ],
    }
    written = _run(spec, tmp_settings, tmp_path)
    assert written.status == "succeeded"
    text = (tmp_path / "out.csv").read_text(encoding="utf-8")
    assert "'=cmd|A1" in text or "',=cmd" not in text.splitlines()[2]
    assert any(line.startswith("'=") or ",'=cmd" in line for line in text.splitlines())
    assert any("'+HYPERLINK" in line or ",'+HYPERLINK" in line for line in text.splitlines())
    jsonl_spec = {
        "name": "jsonl",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "in.csv"},
                "output_key": "rows",
                "next": "save",
            },
            {
                "id": "save",
                "type": "table",
                "op": "write",
                "source": "{{ rows }}",
                "path": "out.jsonl",
                "output_key": "written",
            },
        ],
    }
    again = _run(jsonl_spec, tmp_settings, tmp_path)
    assert again.status == "succeeded"
    assert (tmp_path / "out.jsonl").is_file()


def test_parquet_roundtrip_or_typed_missing(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    spec = _load_spec()
    spec["nodes"][0]["next"] = "save"
    spec["nodes"].append(
        {
            "id": "save",
            "type": "table",
            "op": "write",
            "source": "{{ rows }}",
            "path": "out.parquet",
            "output_key": "written",
        }
    )
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        with pytest.raises(TableExtraMissing):
            _run(spec, tmp_settings, tmp_path)
        return
    state = _run(spec, tmp_settings, tmp_path)
    assert state.status == "succeeded"
    assert (tmp_path / "out.parquet").is_file()


def test_path_outside_workspace_refused(tmp_settings, tmp_path: Path) -> None:
    spec = {
        "name": "escape",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "../secret.csv"},
            }
        ],
    }
    with pytest.raises(TablePathDenied):
        _run(spec, tmp_settings, tmp_path)


def test_row_cap_typed(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    spec = _load_spec()
    spec["nodes"][0]["limits"] = {"max_rows": 2}
    with pytest.raises(TableCapExceeded, match="rows"):
        _run(spec, tmp_settings, tmp_path)


def test_cli_schema_head_stats_twice(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    _csv(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    from readyagents.config import clear_settings_cache

    clear_settings_cache()
    first = runner.invoke(app, ["table", "schema", "exports.csv", "--json"])
    second = runner.invoke(app, ["table", "schema", "exports.csv", "--json"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert a["command"] == b["command"] == "table schema"
    assert a["row_count"] == b["row_count"] == 4
    head = runner.invoke(app, ["table", "head", "exports.csv", "--n", "2", "--json"])
    assert head.exit_code == 0, head.stdout + head.stderr
    stats = runner.invoke(app, ["table", "stats", "exports.csv", "--json"])
    assert stats.exit_code == 0, stats.stdout + stats.stderr
    body = json.loads(stats.stdout[stats.stdout.find("{") :])
    assert "nulls" in body
    assert body["row_count"] == 4


def test_replay_uses_content_hash(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    _csv(tmp_path)
    spec = _load_spec()
    spec["nodes"][0]["next"] = "keep"
    spec["nodes"].append(
        {
            "id": "keep",
            "type": "table",
            "op": "select",
            "source": "{{ rows }}",
            "columns": ["id", "email"],
            "output_key": "kept",
        }
    )
    tape = Cassette.new(run_id="t1", workflow="pipe")
    first = _run(spec, tmp_settings, tmp_path, cassette=tape, recording=True)
    sha = first.output_keys["kept"]["sha256"]
    dest = tmp_path / "tape.json"
    tape.save(dest)
    loaded = Cassette.load(dest)

    def boom(*_a, **_k):
        raise AssertionError("offline replay must not recompute table ops")

    monkeypatch.setattr("readyagents.table.node.apply_op", boom)
    monkeypatch.setattr("readyagents.table.node.read_table", boom)
    second = _run(spec, tmp_settings, tmp_path, cassette=loaded, offline=True)
    assert second.output_keys["kept"]["sha256"] == sha


def test_foreach_scale_items_opt_in(tmp_path: Path) -> None:
    tools = default_registry(allow_http=False, workspace=tmp_path)
    items = [f"{i}+0" for i in range(5)]
    spec = {
        "name": "scaled",
        "inputs": {"expressions": items},
        "nodes": [
            {
                "id": "each",
                "type": "foreach",
                "items": "expressions",
                "scale_items": 1000,
                "concurrency": 2,
                "output_key": "results",
                "body": {
                    "id": "math",
                    "type": "tool",
                    "tool": "calc",
                    "arguments": {"expression": "{{item}}"},
                },
            }
        ],
    }
    state = run_workflow_spec(spec, tools=tools)
    assert state.status == "succeeded"
    assert len(state.output_keys["results"]) == 5
    too_many = {
        "name": "default-cap",
        "inputs": {"expressions": [f"{i}+0" for i in range(33)]},
        "nodes": [
            {
                "id": "each",
                "type": "foreach",
                "items": "expressions",
                "body": {
                    "id": "math",
                    "type": "tool",
                    "tool": "calc",
                    "arguments": {"expression": "{{item}}"},
                },
            }
        ],
    }
    with pytest.raises(NodeError, match="max_items=32"):
        run_workflow_spec(too_many, tools=tools)


def test_eight_ops_via_workflow(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    spec = {
        "name": "ops",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "exports.csv"},
                "schema": {"id": "int", "email": "str", "amount": "float"},
                "output_key": "rows",
                "next": "keep",
            },
            {
                "id": "keep",
                "type": "table",
                "op": "select",
                "source": "{{ rows }}",
                "columns": ["id", "email", "amount"],
                "output_key": "selected",
                "next": "high",
            },
            {
                "id": "high",
                "type": "table",
                "op": "filter",
                "source": "{{ selected }}",
                "when": "amount > 10",
                "output_key": "filtered",
                "next": "ordered",
            },
            {
                "id": "ordered",
                "type": "table",
                "op": "sort",
                "source": "{{ filtered }}",
                "by": ["amount"],
                "descending": True,
                "output_key": "sorted",
                "next": "unique",
            },
            {
                "id": "unique",
                "type": "table",
                "op": "dedupe",
                "source": "{{ rows }}",
                "keys": ["email"],
                "keep": "first",
                "output_key": "deduped",
                "next": "doubled",
            },
            {
                "id": "doubled",
                "type": "table",
                "op": "derive",
                "source": "{{ rows }}",
                "derive": {"name": "double", "expr": "amount * 2"},
                "output_key": "derived",
                "next": "grouped",
            },
            {
                "id": "grouped",
                "type": "table",
                "op": "aggregate",
                "source": "{{ rows }}",
                "keys": ["email"],
                "metrics": {"amount": "sum"},
                "output_key": "grouped",
                "next": "joined",
            },
            {
                "id": "joined",
                "type": "table",
                "op": "join",
                "source": "{{ rows }}",
                "right": "{{ selected }}",
                "on": ["id"],
                "how": "inner",
                "output_key": "joined",
                "next": "united",
            },
            {
                "id": "united",
                "type": "table",
                "op": "union",
                "source": "{{ rows }}",
                "right": "{{ selected }}",
                "output_key": "united",
            },
        ],
    }
    state = _run(spec, tmp_settings, tmp_path)
    assert state.status == "succeeded"
    assert state.output_keys["selected"]["row_count"] == 4
    assert state.output_keys["filtered"]["row_count"] == 3
    assert state.output_keys["deduped"]["row_count"] == 3
    assert state.output_keys["grouped"]["row_count"] == 3
    assert state.output_keys["united"]["row_count"] == 8
    assert state.output_keys["joined"]["row_count"] >= 4
    store = TableStore(tmp_settings.home_path() / "tables")
    derived = next(store.iter_rows(state.output_keys["derived"]["sha256"]))
    assert derived["double"] == derived["amount"] * 2
    blob = json.dumps(state.to_record())
    assert "ada@x.test" not in blob


def test_classify_remainder_on_spend_ledger(tmp_settings, tmp_path: Path) -> None:
    _csv(tmp_path)
    wf = tmp_path / "triage.yaml"
    wf.write_text(
        "name: triage\nnodes:\n"
        "  - id: load\n    type: table\n    op: read\n"
        "    source: {kind: csv, path: exports.csv}\n"
        "    schema: {id: int, email: str, amount: float}\n"
        "    output_key: rows\n    next: triage\n"
        "  - id: triage\n    type: classify\n    source: '{{ rows }}'\n"
        "    rules: [{when: 'amount < 10', label: auto_approve}]\n"
        "    model_for_remainder: {model: mock:test, batch: 25, labels: [review, reject]}\n"
        "    output_key: labelled\n",
        encoding="utf-8",
    )
    llm = ScriptedLLM().enqueue(
        '[{"index": 1, "label": "review"}, {"index": 2, "label": "review"}, '
        '{"index": 3, "label": "reject"}]',
        model="mock:test",
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True, llm=llm)
    assert state.status == "succeeded"
    assert state.metadata["classify"]["triage"]["model_rows"] == 3
    assert state.metadata["classify"]["triage"]["rule_rows"] == 1
    spend = state.metadata.get("spend") or {}
    assert spend.get("model_calls") == 1
    assert spend.get("total_tokens") == 15
    entries = read_spend_entries(tmp_settings.ledger_dir())
    assert entries
    assert entries[-1]["total_tokens"] == 15
    assert llm.calls and "ada@x.test" not in llm.calls[0]["messages"][0].content


def test_read_on_row_error_skip_and_quarantine(tmp_settings, tmp_path: Path) -> None:
    (tmp_path / "mixed.csv").write_text(
        "id,email\n1,ok@x.test\nnope,bad@x.test\n3,also@x.test\n", encoding="utf-8"
    )
    skip_spec = {
        "name": "skip",
        "nodes": [
            {
                "id": "load",
                "type": "table",
                "op": "read",
                "source": {"kind": "csv", "path": "mixed.csv"},
                "schema": {"id": "int", "email": "str"},
                "on_row_error": "skip",
                "output_key": "rows",
            }
        ],
    }
    skipped = _run(skip_spec, tmp_settings, tmp_path)
    assert skipped.output_keys["rows"]["row_count"] == 2
    skip_spec["nodes"][0]["on_row_error"] = "quarantine"
    skip_spec["name"] = "q"
    quarantined = _run(skip_spec, tmp_settings, tmp_path)
    ref = quarantined.output_keys["rows"]
    assert ref["row_count"] == 2
    assert ref["errors_row_count"] == 1
    store = TableStore(tmp_settings.home_path() / "tables")
    err = list(store.iter_rows(ref["errors_sha256"]))
    assert err[0]["row"] == 1
    assert err[0]["column"] == "id"
    assert "nope" not in json.dumps(err)
    assert "bad@x.test" not in json.dumps(quarantined.to_record())


def test_validate_table_example_twice() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "table_pipeline.yaml"
    first = runner.invoke(app, ["validate", str(example)])
    second = runner.invoke(app, ["validate", str(example)])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr


def _part(tmp_settings, ref):
    from readyagents.table.part import part_from_mapping
    from readyagents.table.store import TableStore as Store

    part = part_from_mapping(ref)
    store = Store(tmp_settings.home_path() / "tables")
    loaded = store.part_for(part.sha256)
    part.columns = loaded.columns
    part.row_count = loaded.row_count
    return part
