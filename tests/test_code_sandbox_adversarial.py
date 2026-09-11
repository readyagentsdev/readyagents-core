"""Adversarial escape-corpus for type: code subprocess sandbox.

Drive shipped APIs only; hostile attempts must fail closed with typed errors.
Note: child wraps builtins.open and a few os/subprocess spawn hooks; os.open /
pathlib.io.open are not separately wrapped in this checkout.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from readyagents.code.runner import spawn_sandboxed
from readyagents.errors import (
    ApprovalRequired,
    CodeContainerUnavailable,
    CodeCpuLimitExceeded,
    CodeFilesystemDenied,
    CodeImportDenied,
    CodeNetworkDenied,
    CodeWallLimitExceeded,
)
from readyagents.firewall.policy_file import NodeRule, Policy
from readyagents.testing.helpers import run_workflow_spec
from readyagents.workflow.runner import run_workflow_file

_SECRET = "sk-adversarial-parent-secret-do-not-leak"


def _wf(tmp: Path, body: str, name: str = "adv.yaml") -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


def _run(path: Path, tmp_settings, **kwargs):
    return run_workflow_file(path, settings=tmp_settings, persist=False, **kwargs)


def _spawn(
    tmp_path: Path,
    source: str,
    *,
    allow_imports: list[str] | None = None,
    network: bool = False,
    limits: dict | None = None,
    node_id: str = "escape",
):
    return spawn_sandboxed(
        node_id=node_id,
        source=source,
        inputs={},
        isolation="subprocess",
        require_isolation=None,
        limits=limits or {"cpu_seconds": 2, "wall_seconds": 2, "output_bytes": 65536},
        allow_imports=allow_imports,
        network=network,
        read_roots=[],
        write_roots=[],
        workspace=tmp_path,
    )


# --- 1. Parent secret must not reach the child ---


def test_import_os_denied_blocks_environ_secret_read(
    tmp_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    assert os.environ.get("OPENAI_API_KEY") == _SECRET
    path = _wf(
        tmp_path,
        """
name: secret_import
nodes:
  - id: steal
    type: code
    source: |
      import os
      result = {"key": os.environ.get("OPENAI_API_KEY")}
""",
    )
    with pytest.raises(CodeImportDenied):
        _run(path, tmp_settings)


def test_allow_imports_os_still_scrubs_openai_api_key(
    tmp_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    assert os.environ.get("OPENAI_API_KEY") == _SECRET
    path = _wf(
        tmp_path,
        """
name: secret_scrub
nodes:
  - id: steal
    type: code
    allow_imports: [json, os]
    source: |
      import os
      result = {
          "key": os.environ.get("OPENAI_API_KEY"),
          "has_key": "OPENAI_API_KEY" in os.environ,
          "env": dict(os.environ),
      }
""",
    )
    state = _run(path, tmp_settings)
    assert state.status == "succeeded"
    out = state.node_outputs["steal"]
    assert out["key"] is None
    assert out["has_key"] is False
    assert _SECRET not in str(out)
    assert "OPENAI_API_KEY" not in out["env"]


def test_spawn_sandboxed_minimal_env_omits_openai_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    recorded = _spawn(
        tmp_path,
        'import os\nresult = {"key": os.environ.get("OPENAI_API_KEY"), '
        '"keys": sorted(os.environ)}\n',
        allow_imports=["json", "os"],
    )
    assert recorded["output"]["key"] is None
    assert "OPENAI_API_KEY" not in recorded["output"]["keys"]
    assert _SECRET not in recorded["stdout"]
    assert _SECRET not in recorded["stderr"]


# --- 2. /proc and fd discovery must not leak parent secrets ---


def test_proc_self_environ_denied_when_os_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    with pytest.raises(CodeFilesystemDenied):
        _spawn(
            tmp_path,
            "\n".join(
                [
                    "import os",
                    "blob = open('/proc/self/environ', 'rb').read()",
                    "result = {'blob': blob.decode('utf-8', 'replace')}",
                ]
            ),
            allow_imports=["json", "os"],
        )


def test_proc_self_environ_via_workflow_fails_closed(
    tmp_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    path = _wf(
        tmp_path,
        """
name: proc_env
nodes:
  - id: steal
    type: code
    allow_imports: [json, os]
    source: |
      data = open("/proc/self/environ", "rb").read()
      text = data.decode("utf-8", "replace")
      result = {"leaked": "OPENAI_API_KEY" in text or "sk-adversarial" in text, "raw": text}
""",
    )
    with pytest.raises(CodeFilesystemDenied):
        _run(path, tmp_settings)


def test_dev_fd_and_proc_fd_paths_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", _SECRET)
    for target in ("/proc/self/fd/0", "/dev/fd/0", "/proc/1/environ"):
        with pytest.raises(CodeFilesystemDenied):
            _spawn(
                tmp_path,
                f"result = {{'data': open({target!r}, 'rb').read().decode('utf-8', 'replace')}}\n",
                allow_imports=["json", "os"],
                node_id="fdleak",
            )


# --- 3. Writes outside the sandbox via traversal and symlink ---


def test_path_traversal_write_denied(tmp_settings, tmp_path: Path) -> None:
    marker = tmp_path / "escape_outside.txt"
    assert not marker.exists()
    path = _wf(
        tmp_path,
        """
name: traverse
nodes:
  - id: pwn
    type: code
    source: |
      open("../escape_outside.txt", "w").write("pwned")
      result = {"ok": True}
""",
    )
    with pytest.raises(CodeFilesystemDenied):
        _run(path, tmp_settings)
    assert not marker.exists()


def test_symlink_escape_write_denied(tmp_path: Path) -> None:
    outside = tmp_path / "outside_target.txt"
    outside.write_text("safe", encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "safe"
    with pytest.raises(CodeFilesystemDenied):
        _spawn(
            tmp_path,
            "\n".join(
                [
                    "import os",
                    f"os.symlink({str(outside)!r}, 'escape_link')",
                    "open('escape_link', 'w').write('pwned-via-symlink')",
                    "result = {'ok': True}",
                ]
            ),
            allow_imports=["json", "os"],
            node_id="symlink",
        )
    assert outside.read_text(encoding="utf-8") == "safe"


def test_symlink_to_parent_dir_write_denied(tmp_settings, tmp_path: Path) -> None:
    marker = tmp_path / "via_symlink_parent.txt"
    assert not marker.exists()
    path = _wf(
        tmp_path,
        """
name: symlink_parent
nodes:
  - id: pwn
    type: code
    allow_imports: [json, os]
    source: |
      import os
      os.symlink("..", "up")
      open("up/via_symlink_parent.txt", "w").write("pwned")
      result = {"ok": True}
""",
    )
    with pytest.raises(CodeFilesystemDenied):
        _run(path, tmp_settings)
    assert not marker.exists()


# --- 4. Network egress denied ---


def test_socket_import_denied_when_network_false(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: egress
nodes:
  - id: net
    type: code
    network: false
    source: |
      import socket
      s = socket.socket()
      result = {"ok": True}
""",
    )
    with pytest.raises(CodeNetworkDenied):
        _run(path, tmp_settings)


def test_spawn_socket_denied_network_false(tmp_path: Path) -> None:
    with pytest.raises(CodeNetworkDenied):
        _spawn(
            tmp_path,
            "import socket\nresult = {'ok': True}\n",
            allow_imports=["json", "socket"],
            network=False,
        )


# --- 5. Infinite loop contained by cpu/wall limits ---


def test_infinite_loop_fails_closed_cpu_or_wall(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: spin
nodes:
  - id: loop
    type: code
    source: |
      while True:
          pass
      result = {"ok": True}
    limits:
      cpu_seconds: 1
      wall_seconds: 2
      output_bytes: 65536
""",
    )
    with pytest.raises((CodeCpuLimitExceeded, CodeWallLimitExceeded)):
        _run(path, tmp_settings)


def test_sleep_exceeds_wall_seconds(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: sleeper
nodes:
  - id: nap
    type: code
    source: |
      import time
      time.sleep(30)
      result = {"ok": True}
    limits:
      cpu_seconds: 30
      wall_seconds: 1
      output_bytes: 65536
""",
    )
    with pytest.raises(CodeWallLimitExceeded):
        _run(path, tmp_settings)


# --- 6. Generated-code approval shows full untrusted source ---


def test_generated_code_approval_pause_shows_full_untrusted_source(tmp_settings) -> None:
    full_source = (
        "# adversarial generated payload\n"
        "result = {'marker': 'FULL_SOURCE_VISIBLE_ABC123', 'n': 42}\n"
        "unused = 'tail-sentinel-XYZ'\n"
    )
    spec = {
        "name": "gen_approval",
        "nodes": [
            {
                "id": "draft",
                "type": "transform",
                "template": full_source,
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
    with pytest.raises(ApprovalRequired) as raised:
        run_workflow_spec(spec, policy=policy)
    prompt = raised.value.prompt
    assert "untrusted" in prompt.lower()
    assert "not an instruction" in prompt.lower()
    assert full_source in prompt
    assert "FULL_SOURCE_VISIBLE_ABC123" in prompt
    assert "tail-sentinel-XYZ" in prompt
    assert raised.value.node_id == "runit"


# --- 7. Container isolation fails closed ---


def test_isolation_container_fails_closed(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: box
nodes:
  - id: reshape
    type: code
    isolation: container
    source: |
      result = {"ok": True}
""",
        name="iso_container.yaml",
    )
    with pytest.raises(CodeContainerUnavailable):
        _run(path, tmp_settings)


def test_require_isolation_container_fails_closed(tmp_settings, tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        """
name: need_box
nodes:
  - id: reshape
    type: code
    require_isolation: container
    source: |
      result = {"ok": True}
""",
        name="req_container.yaml",
    )
    with pytest.raises(CodeContainerUnavailable):
        _run(path, tmp_settings)


def test_spawn_sandboxed_container_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(CodeContainerUnavailable):
        spawn_sandboxed(
            node_id="box",
            source="result = {'ok': True}\n",
            inputs={},
            isolation="container",
            require_isolation=None,
            limits={"cpu_seconds": 1, "wall_seconds": 1},
            allow_imports=None,
            network=False,
            read_roots=[],
            write_roots=[],
            workspace=tmp_path,
        )
    with pytest.raises(CodeContainerUnavailable):
        spawn_sandboxed(
            node_id="box2",
            source="result = {'ok': True}\n",
            inputs={},
            isolation="subprocess",
            require_isolation="container",
            limits={"cpu_seconds": 1, "wall_seconds": 1},
            allow_imports=None,
            network=False,
            read_roots=[],
            write_roots=[],
            workspace=tmp_path,
        )
