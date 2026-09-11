"""Shipped type: code path: JSON I/O, limits, allowlist, replay, container fail-closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import (
    ApprovalRequired,
    CodeContainerUnavailable,
    CodeCpuLimitExceeded,
    CodeError,
    CodeFileSizeLimitExceeded,
    CodeFilesystemDenied,
    CodeImportDenied,
    CodeMemoryLimitExceeded,
    CodeNetworkDenied,
    CodeOutputLimitExceeded,
    CodeProcessLimitExceeded,
    CodeSchemaError,
    CodeWallLimitExceeded,
)
from readyagents.firewall.policy_file import NodeRule, Policy
from readyagents.replay.cassette import Cassette
from readyagents.testing.helpers import run_workflow_spec
from readyagents.workflow.runner import run_workflow_file

runner = CliRunner()


def _wf(tmp: Path, body: str) -> Path:
    path = tmp / "code.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _run(path: Path, tmp_settings, **kwargs):
    return run_workflow_file(path, settings=tmp_settings, persist=False, **kwargs)


def test_code_json_in_out(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: sum_code
nodes:
  - id: reshape
    type: code
    source: |
      result = {"total": sum(inputs["rows"])}
    inputs:
      rows: "{{ rows }}"
    output_schema:
      type: object
      properties:
        total: {type: number}
      required: [total]
    output_key: summary
""",
    )
    state = _run(path, tmp_settings, inputs={"rows": [1, 2, 3]})
    assert state.status == "succeeded"
    assert state.output_keys["summary"]["total"] == 6


def test_schema_mismatch_is_typed(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: bad_schema
nodes:
  - id: reshape
    type: code
    source: |
      result = {"total": "nope"}
    output_schema:
      type: object
      properties:
        total: {type: number}
      required: [total]
""",
    )
    with pytest.raises(CodeSchemaError):
        _run(path, tmp_settings)


def test_import_os_denied(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: deny_os
nodes:
  - id: reshape
    type: code
    source: |
      import os
      result = {"home": os.environ.get("OPENAI_API_KEY")}
""",
    )
    with pytest.raises(CodeImportDenied):
        _run(path, tmp_settings)


def test_network_denied(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: deny_net
nodes:
  - id: reshape
    type: code
    source: |
      import socket
      result = {}
    network: false
""",
    )
    with pytest.raises(CodeNetworkDenied):
        _run(path, tmp_settings)


def test_wall_limit(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: wall
nodes:
  - id: reshape
    type: code
    source: |
      import time
      time.sleep(30)
      result = {"ok": true}
    limits:
      cpu_seconds: 30
      wall_seconds: 1
      output_bytes: 65536
""",
    )
    with pytest.raises(CodeWallLimitExceeded):
        _run(path, tmp_settings)


def test_cpu_limit(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: cpu
nodes:
  - id: reshape
    type: code
    source: |
      while True:
          pass
      result = {"ok": True}
    limits:
      cpu_seconds: 1
      wall_seconds: 15
      output_bytes: 65536
""",
    )
    with pytest.raises(CodeCpuLimitExceeded):
        _run(path, tmp_settings)


def test_sleep_over_cpu_under_wall_succeeds(tmp_settings, tmp_path: Path) -> None:
    """Wall-clock sleep is not CPU time; communicate timeout is wall_seconds."""
    path = _wf(
        tmp_path,
        """
name: nap
nodes:
  - id: reshape
    type: code
    source: |
      import time
      time.sleep(6)
      result = {"ok": True}
    limits:
      cpu_seconds: 5
      wall_seconds: 15
      output_bytes: 65536
""",
    )
    state = _run(path, tmp_settings)
    assert state.status == "succeeded"
    assert state.node_outputs["reshape"]["ok"] is True


def test_file_size_limit(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: fatfile
nodes:
  - id: reshape
    type: code
    source: |
      handle = open("blob.bin", "wb")
      handle.write(b"x" * 10000)
      handle.close()
      result = {"ok": True}
    limits:
      cpu_seconds: 5
      wall_seconds: 10
      file_size_bytes: 100
      output_bytes: 65536
""",
    )
    with pytest.raises(CodeFileSizeLimitExceeded):
        _run(path, tmp_settings)


def test_memory_limit(tmp_settings, tmp_path: Path) -> None:
    """Non-zero pages over memory_mb. Zero-fill can stay compressed on Darwin."""
    path = _wf(
        tmp_path,
        """
name: hog
nodes:
  - id: reshape
    type: code
    source: |
      blocks = []
      unit = bytes(range(256))
      while True:
          block = bytearray(unit * 8192)
          block[0] = len(blocks) & 255
          blocks.append(block)
      result = {"n": len(blocks)}
    limits:
      cpu_seconds: 30
      wall_seconds: 20
      memory_mb: 64
      output_bytes: 65536
""",
    )
    with pytest.raises(CodeMemoryLimitExceeded):
        _run(path, tmp_settings)


def test_output_limit(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: fat
nodes:
  - id: reshape
    type: code
    source: |
      result = {"blob": "x" * 8000}
    limits:
      cpu_seconds: 5
      wall_seconds: 10
      output_bytes: 64
""",
    )
    with pytest.raises(CodeOutputLimitExceeded):
        _run(path, tmp_settings)


def test_nproc_limit(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: forks
nodes:
  - id: reshape
    type: code
    allow_imports: [json, os]
    source: |
      import os
      if hasattr(os, "fork"):
          os.fork()
      else:
          os.system("echo")
      result = {"ok": True}
    limits:
      nproc: 1
      wall_seconds: 5
      cpu_seconds: 5
      output_bytes: 65536
""",
    )
    with pytest.raises((CodeProcessLimitExceeded, CodeImportDenied)):
        _run(path, tmp_settings)


def test_container_required_fails_closed(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: need_box
nodes:
  - id: reshape
    type: code
    isolation: container
    source: |
      result = {"ok": true}
""",
    )
    with pytest.raises(CodeContainerUnavailable):
        _run(path, tmp_settings)


def test_filesystem_grant_refuses_parent(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: fs
nodes:
  - id: reshape
    type: code
    source: |
      open("/etc/passwd").read()
      result = {"ok": true}
""",
    )
    with pytest.raises((CodeFilesystemDenied, CodeImportDenied, CodeError)):
        _run(path, tmp_settings)


def test_record_and_offline_replay_does_not_exec(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    path = _wf(
        tmp_path,
        """
name: rec
nodes:
  - id: reshape
    type: code
    source: |
      result = {"total": 3}
    output_key: summary
""",
    )
    first = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=True,
        record=True,
    )
    assert first.status == "succeeded"
    cassette_path = Path(first.metadata["cassette"])
    tape = Cassette.load(cassette_path)
    code_entries = [row for row in tape.entries.values() if row.get("kind") == "code"]
    assert code_entries
    row = code_entries[0]
    for key in ("source", "inputs", "stdout", "stderr", "exit", "tier"):
        assert key in row

    def boom(*_a, **_k):
        raise AssertionError("child interpreter must not run on offline replay")

    monkeypatch.setattr("readyagents.code.runner.spawn_sandboxed", boom)
    replayed = run_workflow_file(
        path,
        settings=tmp_settings,
        persist=False,
        offline=True,
        cassette_path=cassette_path,
    )
    assert replayed.status == "succeeded"
    assert replayed.output_keys["summary"]["total"] == 3


def test_generated_code_approval_shows_full_source(tmp_settings, tmp_path: Path) -> None:
    spec = {
        "name": "gen",
        "nodes": [
            {
                "id": "draft",
                "type": "transform",
                "template": "result = {'ok': True}",
                "output_key": "draft_code",
            },
            {
                "id": "runit",
                "type": "code",
                "source_from": "draft_code",
                "output_key": "summary",
            },
        ],
    }
    policy = Policy(nodes={"runit": NodeRule(require_approval=True)})
    with pytest.raises(ApprovalRequired) as exc:
        run_workflow_spec(spec, policy=policy)
    prompt = exc.value.prompt
    assert "Untrusted generated code" in prompt
    assert "result = " in prompt
    state = run_workflow_spec(spec, policy=policy, decisions={"runit": "approve"})
    assert state.status == "succeeded"


def test_cli_code_example_twice(tmp_settings, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    example = Path(__file__).resolve().parents[1] / "examples" / "code_reshape.yaml"
    dest = tmp_path / "code_reshape.yaml"
    dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    first = runner.invoke(app, ["run", str(dest), "--json", "--no-persist"])
    second = runner.invoke(app, ["run", str(dest), "--json", "--no-persist"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0, second.stdout + second.stderr
    a = json.loads(first.stdout[first.stdout.find("{") :])
    b = json.loads(second.stdout[second.stdout.find("{") :])
    assert a["ok"] is True and b["ok"] is True
    assert a["output_keys"]["summary"]["total"] == b["output_keys"]["summary"]["total"] == 22


def test_doctor_mentions_code_sandbox() -> None:
    from readyagents.doctor import run_doctor

    report = run_doctor()
    block = report["code_sandbox"]
    assert block["subprocess"] is True
    assert "honesty" in block
    assert "accident" in block["honesty"]
    always = block["always"]
    assert "cpu_process_time" in always
    assert "memory_rss" in always
    assert "file_size_capped_open" in always
