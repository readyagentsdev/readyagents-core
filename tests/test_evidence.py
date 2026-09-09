"""Evidence pack from shipped run_workflow_file + CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.compliance.evidence import write_evidence_pack
from readyagents.config import clear_settings_cache
from readyagents.decisions.signing import sign_body, verify_signed_body
from readyagents.errors import ConfigError
from readyagents.policy import Redactor
from readyagents.workflow.runner import load_workflow, run_workflow_file

_runner = CliRunner()


def _cli_env(monkeypatch, tmp_path: Path, tmp_settings) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_settings.home_path()))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    clear_settings_cache()


def test_evidence_pack_files_and_hashes(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'hello'\n    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    dest = tmp_path / "evidence-out"
    pack = write_evidence_pack(
        dest,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
    )
    expected = {
        "manifest.json",
        "run.json",
        "decisions.json",
        "audit.jsonl",
        "workflow.yaml",
        "graph.mmd",
        "evidence.html",
        "README.md",
    }
    names = {p.name for p in pack.iterdir()}
    assert expected <= names
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        blob = (pack / name).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == digest
    html = (pack / "evidence.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower() or "src=" not in html.lower()
    readme = (pack / "README.md").read_text(encoding="utf-8")
    assert "not" in readme.lower() and "certif" in readme.lower()
    dest2 = tmp_path / "evidence-out-2"
    pack2 = write_evidence_pack(
        dest2,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
    )
    m2 = json.loads((pack2 / "manifest.json").read_text(encoding="utf-8"))
    assert m2["files"] == manifest["files"]


def test_evidence_refuses_overwrite_without_force(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    dest = tmp_path / "pack"
    write_evidence_pack(
        dest,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
    )
    try:
        write_evidence_pack(
            dest,
            state=state,
            workflow=load_workflow(wf),
            workflow_text=wf.read_text(encoding="utf-8"),
            audit_dir=tmp_settings.audit_dir(),
        )
    except ConfigError as exc:
        assert "overwrite" in str(exc).lower() or "force" in str(exc).lower()
    else:
        raise AssertionError("expected ConfigError on overwrite")
    write_evidence_pack(
        dest,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
        force=True,
    )


def test_pack_time_redaction_reverified(tmp_path: Path, tmp_settings) -> None:
    secret = "sk-evidencepacksecret99"
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n"
        f"    template: 'contact me@example.com key={secret}'\n    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    dest = tmp_path / "pack"
    write_evidence_pack(
        dest,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
        redactor=Redactor(),
    )
    blob = (dest / "run.json").read_text(encoding="utf-8")
    assert secret not in blob
    assert "me@example.com" not in blob
    assert "[redacted]" in blob


def test_sign_verifies(tmp_path: Path, tmp_settings) -> None:
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    dest = tmp_path / "pack"
    secret = "decision-secret-for-evidence"
    write_evidence_pack(
        dest,
        state=state,
        workflow=load_workflow(wf),
        workflow_text=wf.read_text(encoding="utf-8"),
        audit_dir=tmp_settings.audit_dir(),
        sign_secret=secret,
    )
    body = (dest / "manifest.json").read_bytes()
    sig = (dest / "manifest.json.sig").read_text(encoding="utf-8").strip()
    verify_signed_body(secret, body, sig)
    assert sign_body(secret, body) == sig


def test_evidence_cli(tmp_path: Path, tmp_settings, monkeypatch) -> None:
    _cli_env(monkeypatch, tmp_path, tmp_settings)
    wf = tmp_path / "w.yaml"
    wf.write_text(
        "name: w\nnodes:\n  - id: a\n    type: transform\n    template: 'x'\n    output_key: summary\n",
        encoding="utf-8",
    )
    state = run_workflow_file(wf, settings=tmp_settings, persist=True)
    first = _runner.invoke(app, ["evidence", state.run_id, "--out", "pack-a", "--json"])
    assert first.exit_code == 0, first.stdout + first.stderr
    again = _runner.invoke(app, ["evidence", state.run_id, "--out", "pack-b", "--json"])
    assert again.exit_code == 0, again.stdout + again.stderr
    m1 = json.loads((tmp_path / "pack-a" / "manifest.json").read_text(encoding="utf-8"))
    m2 = json.loads((tmp_path / "pack-b" / "manifest.json").read_text(encoding="utf-8"))
    assert m1["files"] == m2["files"]
    denied = _runner.invoke(app, ["evidence", state.run_id, "--out", "pack-a", "--json"])
    assert denied.exit_code == 1
