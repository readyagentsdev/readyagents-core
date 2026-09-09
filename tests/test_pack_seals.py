"""Pack cassette seals: old packs load; unclassified stays unsealable; builtins win."""

from __future__ import annotations

from pathlib import Path

import pytest

from readyagents.errors import CassetteError, ConfigError
from readyagents.packs.loader import collect_pack_seals
from readyagents.packs.protocol import BasePack
from readyagents.replay.cassette import Cassette, classify_tool
from readyagents.replay.freeze import freeze_run
from readyagents.testing.eval import load_eval_suite, run_eval
from readyagents.tools import FunctionTool, ToolRegistry
from readyagents.workflow.runner import run_workflow_file


class OldPack:
    name = "old"
    version = "0.0.1"

    def register_nodes(self):
        return {}

    def register_tools(self):
        return []

    def register_workflows(self):
        return []


class SealedHashPack(BasePack):
    name = "hashy"
    version = "0.0.1"

    def register_tools(self):
        return [
            FunctionTool(
                name="hash_file",
                description="deterministic hash stand-in",
                handler=lambda value="": f"h:{value}",
                determinism="recomputed",
            )
        ]

    def register_tool_seals(self):
        return {"hash_file": "recomputed"}


class SealableStampPack(BasePack):
    name = "stampy"
    version = "0.0.1"

    def register_tools(self):
        return [
            FunctionTool(
                name="pack_stamp",
                description="sealable pack clock",
                handler=lambda: "t0",
                determinism="sealable",
            )
        ]


class OverrideBuiltinsPack(BasePack):
    name = "override"
    version = "0.0.1"

    def register_tools(self):
        return []

    def register_tool_seals(self):
        return {
            "calc": "unsealable",
            "write_file": "recomputed",
            "hash_file": "recomputed",
        }


class InvalidSealPack(BasePack):
    name = "bad-seal"
    version = "0.0.1"

    def register_tool_seals(self):
        return {"hash_file": "sometimes"}


def test_old_pack_without_seal_method_still_loads() -> None:
    assert collect_pack_seals([OldPack()]) == {}
    assert collect_pack_seals([BasePack()]) == {}


def test_classify_tool_ignores_builtin_override() -> None:
    seals = collect_pack_seals([OverrideBuiltinsPack()])
    assert seals.get("hash_file") == "recomputed"
    assert "calc" not in seals
    assert "write_file" not in seals
    assert classify_tool("calc", seals=seals) == "recomputed"
    assert classify_tool("write_file", seals=seals) == "unsealable"
    assert classify_tool("hash_file", seals=seals) == "recomputed"


def test_invalid_seal_value_raises_at_collect() -> None:
    with pytest.raises(ConfigError, match="Invalid tool seal"):
        collect_pack_seals([InvalidSealPack()])


def test_unclassified_pack_tool_still_needs_allow_unsealed(
    tmp_path: Path, tmp_settings
) -> None:
    tools = ToolRegistry()
    tools.register(FunctionTool(name="mystery", description="x", handler=lambda: "y"))
    workflow = tmp_path / "mystery.yaml"
    workflow.write_text(
        "name: mystery-flow\n"
        "nodes:\n"
        "  - id: m\n"
        "    type: tool\n"
        "    tool: mystery\n"
        "    output_key: v\n",
        encoding="utf-8",
    )
    state = run_workflow_file(
        workflow, settings=tmp_settings, persist=True, record=True, extra_tools=tools
    )
    cassette = Cassette.load(state.metadata["cassette"])
    with pytest.raises(CassetteError, match="unsealable"):
        freeze_run(
            state,
            cassette,
            out_dir=tmp_path / "nope",
            workspace=tmp_path,
            allow_unsealed=False,
        )
    dest = freeze_run(
        state,
        cassette,
        out_dir=tmp_path / "allowed",
        workspace=tmp_path,
        allow_unsealed=True,
    )
    case = (dest / "case.yaml").read_text(encoding="utf-8")
    assert "unsealable:" in case
    assert "m" in case


def test_declared_recomputed_pack_tool_freezes_without_flag(
    tmp_path: Path, tmp_settings
) -> None:
    pack = SealedHashPack()
    workflow = tmp_path / "hash.yaml"
    workflow.write_text(
        "name: hash-flow\n"
        "nodes:\n"
        "  - id: h\n"
        "    type: tool\n"
        "    tool: hash_file\n"
        "    arguments: {value: abc}\n"
        "    output_key: digest\n",
        encoding="utf-8",
    )
    state = run_workflow_file(
        workflow,
        settings=tmp_settings,
        persist=True,
        record=True,
        extra_packs=[pack],
    )
    assert "h" in (state.metadata.get("determinism") or {}).get("recomputed", [])
    cassette = Cassette.load(state.metadata["cassette"])
    cassette.tool_seals = collect_pack_seals([pack])
    dest = freeze_run(
        state,
        cassette,
        out_dir=tmp_path / "frozen-hash",
        workspace=tmp_path,
        allow_unsealed=False,
    )
    cases = load_eval_suite(dest / "case.yaml")
    tools = ToolRegistry()
    for tool in pack.register_tools():
        tools.register(tool)
    report = run_eval(cases, tools=tools, settings=tmp_settings)
    assert report.ok, [row.reason for row in report.results]
    assert "h" in (report.results[0].state.metadata.get("determinism") or {}).get(
        "recomputed", []
    )


def test_declared_sealable_pack_tool_records_sealed(
    tmp_path: Path, tmp_settings
) -> None:
    pack = SealableStampPack()
    workflow = tmp_path / "stamp.yaml"
    workflow.write_text(
        "name: stamp-flow\n"
        "nodes:\n"
        "  - id: s\n"
        "    type: tool\n"
        "    tool: pack_stamp\n"
        "    output_key: t\n",
        encoding="utf-8",
    )
    state = run_workflow_file(
        workflow,
        settings=tmp_settings,
        persist=True,
        record=True,
        extra_packs=[pack],
    )
    report = state.metadata.get("determinism") or {}
    assert "s" in report.get("sealed", [])
    cassette = Cassette.load(state.metadata["cassette"])
    cassette.tool_seals = collect_pack_seals([pack])
    freeze_run(
        state,
        cassette,
        out_dir=tmp_path / "frozen-stamp",
        workspace=tmp_path,
        allow_unsealed=False,
    )


def test_run_rejects_invalid_pack_seal(tmp_path: Path, tmp_settings) -> None:
    workflow = tmp_path / "ok.yaml"
    workflow.write_text(
        "name: ok\n"
        "nodes:\n"
        "  - id: t\n"
        "    type: transform\n"
        "    template: x\n"
        "    output_key: v\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Invalid tool seal"):
        run_workflow_file(
            workflow,
            settings=tmp_settings,
            persist=False,
            extra_packs=[InvalidSealPack()],
        )


def test_function_tool_determinism_attr_is_collected() -> None:
    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="pure_hash",
            description="x",
            handler=lambda: "h",
            determinism="recomputed",
        )
    )
    seals = collect_pack_seals([], extra_tools=tools)
    assert seals["pure_hash"] == "recomputed"
    assert classify_tool("pure_hash", seals=seals) == "recomputed"
