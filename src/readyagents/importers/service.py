"""Import orchestration: parse → map → emit → validate → dry-run → graph."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from readyagents.atomic import atomic_write_text
from readyagents.compliance.graph import render_mermaid
from readyagents.config import Settings, get_settings
from readyagents.errors import ImportRefused, ReadyAgentsError
from readyagents.importers.crewai import parse_crewai
from readyagents.importers.emit import emit
from readyagents.importers.ir import SOURCES, IntermediateGraph
from readyagents.importers.langgraph import parse_langgraph
from readyagents.importers.n8n import parse_n8n
from readyagents.importers.report import FidelityReport
from readyagents.importers.table import MappingTable, load_table
from readyagents.importers.trigger import parse_trigger
from readyagents.workflow.runner import confine_under, load_workflow, run_workflow_file

_PARSERS: dict[str, Callable[..., IntermediateGraph]] = {
    "n8n": parse_n8n,
    "langgraph": parse_langgraph,
    "crewai": parse_crewai,
    "trigger": parse_trigger,
}


@dataclass
class ImportResult:
    workflow_path: Path
    report_path: Path
    graph_path: Path
    report: FidelityReport
    mermaid: str
    dry_run_ok: bool
    workflow: dict[str, Any]


def explain_source(source: str) -> MappingTable:
    return load_table(source)


def import_workflow(
    source: str,
    path: Path | str,
    *,
    out: Path | str,
    report_path: Path | str | None = None,
    force: bool = False,
    settings: Settings | None = None,
    dry_run: bool = True,
) -> ImportResult:
    settings = settings or get_settings()
    token = str(source or "").strip().lower()
    if token not in SOURCES:
        raise ImportRefused(
            f"unknown import source {source!r}; expected {', '.join(SOURCES)}",
            reason="unknown_source",
        )
    src = Path(path)
    if not src.is_file():
        raise ImportRefused(f"import source not found: {src}", reason="missing")
    text = src.read_text(encoding="utf-8")
    graph = _PARSERS[token](text, filename=src.name)
    table = load_table(token)
    workflow, report = emit(graph, table)
    dest_dir = confine_under(out, settings.workspace_path(), what="import out")
    dest_dir.mkdir(parents=True, exist_ok=True)
    wf_path = dest_dir / "workflow.yaml"
    if wf_path.exists() and not force:
        raise ImportRefused(
            f"refusing to overwrite {wf_path}; pass --force",
            reason="exists",
        )
    report_dest = Path(report_path) if report_path else dest_dir / "fidelity.md"
    if report_path:
        report_dest = confine_under(report_dest, settings.workspace_path(), what="import report")
    graph_path = dest_dir / "workflow.mmd"
    yaml_text = yaml.safe_dump(workflow, sort_keys=False, allow_unicode=True)
    spec = load_workflow(wf_path, source=yaml_text)
    mermaid = render_mermaid(spec)
    atomic_write_text(wf_path, yaml_text, encoding="utf-8", newline="\n")
    atomic_write_text(report_dest, report.to_markdown(), encoding="utf-8", newline="\n")
    atomic_write_text(graph_path, mermaid, encoding="utf-8", newline="\n")
    dry_ok = False
    if dry_run:
        try:
            state = run_workflow_file(wf_path, dry_run=True, persist=False, settings=settings)
            dry_ok = state.status == "succeeded"
        except ReadyAgentsError:
            dry_ok = False
    return ImportResult(
        workflow_path=wf_path,
        report_path=report_dest,
        graph_path=graph_path,
        report=report,
        mermaid=mermaid,
        dry_run_ok=dry_ok,
        workflow=workflow,
    )
