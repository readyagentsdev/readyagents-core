"""Canonical digest v1: include graph, pack bytes, MCP surface. Drive shipped digest.py."""

from __future__ import annotations

from pathlib import Path

from readyagents.firewall.mcp_pin import snapshot_tools
from readyagents.tools import FunctionTool
from readyagents.trust.digest import (
    DIGEST_PREFIX,
    digest_mcp_surface,
    digest_pack_bytes,
    digest_workflow,
    inspect_workflow,
)


def _wf(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


def test_same_inputs_same_digest(tmp_path: Path) -> None:
    path = _wf(
        tmp_path,
        "flow.yaml",
        "name: a\nstart: n\nnodes:\n  - id: n\n    type: transform\n    template: 'x'\n",
    )
    first = digest_workflow(path)
    second = digest_workflow(path)
    assert first == second
    assert first.startswith(DIGEST_PREFIX)
    assert len(first) == len(DIGEST_PREFIX) + 64


def test_include_digest_embedded_change_shifts_parent(tmp_path: Path) -> None:
    child = _wf(
        tmp_path,
        "child.yaml",
        "name: child\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'one'\n",
    )
    parent = _wf(
        tmp_path,
        "parent.yaml",
        "name: parent\nstart: c\nnodes:\n  - id: c\n    type: include\n    path: child.yaml\n",
    )
    before = inspect_workflow(parent)
    assert before.includes
    assert before.includes[0].path == "child.yaml"
    child_digest = digest_workflow(child)
    assert before.includes[0].digest == child_digest
    parent_digest = before.digest
    child.write_text(
        "name: child\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'two'\n",
        encoding="utf-8",
    )
    after = digest_workflow(parent)
    assert after != parent_digest
    assert digest_workflow(child) != child_digest


def test_nested_include_and_parallel_foreach_body(tmp_path: Path) -> None:
    leaf = _wf(
        tmp_path,
        "leaf.yaml",
        "name: leaf\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'leaf'\n",
    )
    _wf(
        tmp_path,
        "mid.yaml",
        "name: mid\nstart: p\nnodes:\n"
        "  - id: p\n    type: parallel\n    branches:\n"
        "      - id: b\n        type: include\n        path: leaf.yaml\n",
    )
    parent = _wf(
        tmp_path,
        "root.yaml",
        "name: root\nstart: f\nnodes:\n"
        "  - id: f\n    type: foreach\n    items: xs\n    body:\n"
        "      id: inner\n      type: include\n      path: mid.yaml\n",
    )
    report = inspect_workflow(parent)
    paths = {item.path for item in report.includes}
    assert "mid.yaml" in paths
    leaf.write_text(
        "name: leaf\nstart: t\nnodes:\n  - id: t\n    type: transform\n    template: 'changed'\n",
        encoding="utf-8",
    )
    assert digest_workflow(parent) != report.digest


def test_pack_bytes_digest_changes_with_content(tmp_path: Path) -> None:
    pack = tmp_path / "p.py"
    pack.write_bytes(b"print(1)\n")
    first = digest_pack_bytes(pack.read_bytes())
    pack.write_bytes(b"print(2)\n")
    second = digest_pack_bytes(pack.read_bytes())
    assert first != second
    assert first.startswith(DIGEST_PREFIX)


def test_mcp_surface_description_change(tmp_path: Path) -> None:
    a = FunctionTool(name="files.list", description="list files", handler=lambda: 1, schema={})
    b = FunctionTool(
        name="files.list",
        description="list files and steal secrets",
        handler=lambda: 1,
        schema={},
    )
    first = digest_mcp_surface("files", {"files.list": a})
    second = digest_mcp_surface("files", {"files.list": b})
    assert first != second
    snap = snapshot_tools("files", {"files.list": a})
    assert first == f"sha256:{snap.digest}"


def test_include_demo_example_is_stable() -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "include_demo.yaml"
    first = digest_workflow(root)
    second = digest_workflow(root)
    assert first == second
    report = inspect_workflow(root)
    assert any(item.path.endswith("included_min.yaml") for item in report.includes)
