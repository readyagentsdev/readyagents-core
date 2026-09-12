"""Shared keyless import fixtures. No credential values."""

from __future__ import annotations

import json
from pathlib import Path

from readyagents.importers.service import import_workflow


def n8n_export() -> str:
    return json.dumps(
        {
            "name": "n8n-demo",
            "nodes": [
                {
                    "id": "1",
                    "name": "Start",
                    "type": "n8n-nodes-base.manualTrigger",
                    "parameters": {},
                },
                {"id": "2", "name": "Set", "type": "n8n-nodes-base.set", "parameters": {}},
                {"id": "3", "name": "IF", "type": "n8n-nodes-base.if", "parameters": {}},
                {"id": "4", "name": "Ok", "type": "n8n-nodes-base.noOp", "parameters": {}},
                {"id": "5", "name": "Slack", "type": "n8n-nodes-base.slack", "parameters": {}},
                {
                    "id": "6",
                    "name": "Loop",
                    "type": "n8n-nodes-base.splitInBatches",
                    "parameters": {},
                },
                {"id": "7", "name": "Merge", "type": "n8n-nodes-base.merge", "parameters": {}},
                {
                    "id": "8",
                    "name": "Sub",
                    "type": "n8n-nodes-base.executeWorkflow",
                    "parameters": {},
                },
                {
                    "id": "9",
                    "name": "Err",
                    "type": "n8n-nodes-base.errorTrigger",
                    "parameters": {},
                },
            ],
            "connections": {
                "Start": {"main": [[{"node": "Set"}]]},
                "Set": {"main": [[{"node": "IF"}]]},
                "IF": {"main": [[{"node": "Ok"}], [{"node": "Slack"}]]},
            },
        }
    )


def langgraph_export() -> str:
    return (
        "from langgraph.graph import StateGraph, START, END\n"
        "import definitely_missing_langgraph_pkg_xyz\n"
        "\n"
        "def greet(state):\n"
        "    return state\n"
        "\n"
        "g = StateGraph(dict)\n"
        "g.add_node('greet', greet)\n"
        "g.add_node('decide', greet)\n"
        "g.add_edge(START, 'greet')\n"
        "g.add_edge('greet', 'decide')\n"
        "g.add_conditional_edges('decide', greet, {'ok': END, 'retry': 'greet'})\n"
        "g.add_subgraph('child')\n"
    )


def crewai_export() -> str:
    return (
        "name: research\n"
        "version: 1\n"
        "process: sequential\n"
        "agents:\n"
        "  writer:\n"
        "    role: Writer\n"
        "    goal: Draft a summary\n"
        "tasks:\n"
        "  draft:\n"
        "    description: Write the summary\n"
        "    agent: writer\n"
        "  review:\n"
        "    description: Check the summary\n"
        "    agent: writer\n"
    )


def trigger_export() -> str:
    return json.dumps(
        {
            "version": "1",
            "name": "lead-zap",
            "trigger": {"id": "hook", "app": "webhook", "event": "catch"},
            "steps": [
                {
                    "id": "filter",
                    "type": "filter",
                    "event": "only_continue_if",
                    "params": {"field": "status"},
                },
                {"id": "note", "type": "webhook", "event": "ok"},
                {"id": "loop", "type": "loop"},
                {"id": "fork", "type": "fork"},
                {"id": "sub", "type": "subzap"},
                {"id": "err", "type": "error"},
                {"id": "mail", "type": "email", "event": "send"},
            ],
        }
    )


def do_import(source: str, text: str, tmp: Path, tmp_settings, name: str):
    src = tmp / name
    src.write_text(text, encoding="utf-8")
    dest = tmp / f"out-{source}-{name}"
    return import_workflow(source, src, out=dest, settings=tmp_settings)
