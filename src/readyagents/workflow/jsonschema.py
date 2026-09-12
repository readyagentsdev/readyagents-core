"""Generate a JSON Schema 2020-12 document from ``WorkflowSpec``.

The schema describes the **file** format (``else`` / ``from`` / ``inputs``),
not Python attribute names. Pydantic remains the execution source of truth.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Final

from readyagents import __version__
from readyagents.workflow.schema import NodeType, WorkflowSpec

SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID: Final = "https://readyagents.dev/schema/workflow/v1.json"
SCHEMA_TITLE: Final = "ReadyAgents workflow"
SCHEMA_DESCRIPTION: Final = (
    "YAML/JSON workflow file format for ReadyAgents Core. The v1 path tracks "
    "the file format, not the package version. Cross-field rules (cycles, route "
    "targets, include graphs) are enforced by Pydantic at load time, not by this "
    "schema. Packs may add node types and fields; node objects stay open."
)

# File-format field names (aliases), keyed by the source enum — never hand-copied
# into the schema as a closed type enum.
NODE_TYPE_FIELDS: dict[NodeType, tuple[str, ...]] = {
    NodeType.agent: (
        "prompt",
        "prompt_id",
        "prompt_version",
        "system",
        "model",
        "fallback_models",
        "output_schema",
        "cache",
        "tools",
        "max_tool_rounds",
        "rationale_key",
        "contract",
        "media",
        "media_redact",
    ),
    NodeType.tool: ("tool", "arguments", "contract"),
    NodeType.condition: ("when", "then", "else"),
    NodeType.transform: (
        "template",
        "source",
        "json_path",
        "parse_json",
        "rationale_key",
        "contract",
    ),
    NodeType.approval: (
        "prompt",
        "prompt_id",
        "prompt_version",
        "then",
        "else",
        "approvals_required",
        "distinct_actors",
        "deny_actor",
        "approver_roles",
        "require",
        "expires_in",
        "on_expire",
        "escalate_to",
        "require_reason",
        "reject_short_circuit",
        "recommendation",
        "notify",
        "feedback",
    ),
    NodeType.parallel: ("branches",),
    NodeType.include: ("path", "inputs"),
    NodeType.foreach: ("items", "max_items", "body", "scale_items", "concurrency"),
    NodeType.a2a: ("agent_url", "message", "on_input_required", "token", "contract"),
    NodeType.memory: (
        "op",
        "scope",
        "scope_pattern",
        "query",
        "text",
        "ttl",
        "limit",
        "embed",
        "context",
        "contract",
        "blend",
        "freshness",
    ),
    NodeType.code: (
        "source",
        "source_from",
        "inputs",
        "isolation",
        "allow_imports",
        "network",
        "filesystem",
        "limits",
        "require_isolation",
        "output_schema",
        "contract",
    ),
    NodeType.team: (
        "strategy",
        "supervisor",
        "members",
        "scratchpad",
        "terminate",
        "model",
    ),
    NodeType.document: ("source", "render", "media_redact", "contract"),
    NodeType.transcribe: ("source", "model", "media_redact", "contract"),
    NodeType.ingest: (
        "source",
        "scope",
        "scope_pattern",
        "chunk",
        "on_change",
        "freshness",
        "embed",
        "contract",
    ),
    NodeType.table: (
        "op",
        "source",
        "schema",
        "columns",
        "keys",
        "keep",
        "how",
        "on",
        "right",
        "metrics",
        "by",
        "descending",
        "derive",
        "when",
        "path",
        "limits",
        "on_row_error",
        "contract",
    ),
    NodeType.classify: (
        "source",
        "rules",
        "model_for_remainder",
        "on_row_error",
        "model",
        "limits",
        "contract",
    ),
    NodeType.wait: (
        "until",
        "for_event",
        "for_file",
        "for_run",
        "whichever",
        "on_deadline",
        "default",
        "escalate_to",
        "then",
        "else",
        "next",
    ),
    NodeType.skill: ("skill", "inputs", "script", "limits", "contract"),
    NodeType.browser: ("allow", "credentials", "session", "limits", "actions"),
}

NODE_TYPE_REQUIRED: dict[NodeType, tuple[str, ...]] = {
    NodeType.agent: ("prompt",),
    NodeType.tool: ("tool",),
    NodeType.condition: ("when",),
    NodeType.approval: ("prompt",),
    NodeType.parallel: ("branches",),
    NodeType.include: ("path",),
    NodeType.foreach: ("items", "body"),
    NodeType.a2a: ("agent_url",),
    NodeType.memory: ("op", "scope"),
    NodeType.team: ("strategy", "supervisor", "members"),
    NodeType.document: ("source",),
    NodeType.transcribe: ("source",),
    NodeType.ingest: ("source", "scope"),
    NodeType.table: ("op",),
    NodeType.classify: ("source",),
    NodeType.wait: ("until",),
    NodeType.skill: ("skill",),
    NodeType.browser: ("allow", "actions"),
}

_PYTHON_ONLY_ALIASES: Final[tuple[str, ...]] = ("else_", "from_", "call_inputs")


def workflow_json_schema() -> dict[str, Any]:
    """Return a deterministic JSON Schema 2020-12 dict for the workflow file format."""
    raw = WorkflowSpec.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
    return _post_process(raw)


def workflow_json_schema_text() -> str:
    """UTF-8 JSON text: indent 2, sorted keys, exactly one trailing newline."""
    return json.dumps(workflow_json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _post_process(raw: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(raw)
    defs = schema.setdefault("$defs", {})
    node = defs.get("NodeSpec")
    if not isinstance(node, dict):
        raise RuntimeError("WorkflowSpec JSON Schema is missing $defs.NodeSpec")
    properties = node.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("NodeSpec schema is missing properties")

    missing = [nt.value for nt in NodeType if nt not in NODE_TYPE_FIELDS]
    if missing:
        raise RuntimeError(f"NODE_TYPE_FIELDS missing NodeType members: {missing}")

    type_specific: set[str] = set()
    for names in NODE_TYPE_FIELDS.values():
        type_specific.update(names)

    clauses: list[dict[str, Any]] = []
    for nt in NodeType:
        then_props: dict[str, Any] = {}
        for name in NODE_TYPE_FIELDS[nt]:
            if name in properties:
                then_props[name] = copy.deepcopy(properties[name])
        then: dict[str, Any] = {"type": "object", "properties": then_props}
        required = [name for name in NODE_TYPE_REQUIRED.get(nt, ()) if name in then_props]
        if required:
            then["required"] = required
        clauses.append(
            {
                "if": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {"type": {"const": nt.value}},
                },
                "then": then,
            }
        )

    for name in type_specific:
        properties.pop(name, None)

    node["additionalProperties"] = True
    existing_all_of = node.get("allOf")
    if isinstance(existing_all_of, list):
        node["allOf"] = existing_all_of + clauses
    else:
        node["allOf"] = clauses

    type_field = properties.get("type")
    if isinstance(type_field, dict):
        values = [nt.value for nt in NodeType]
        built_ins = ", ".join(values)
        type_field["description"] = (
            f"Node kind. Built-in values: {built_ins}. Packs may add additional types."
        )
        # Surface source enums for editor completion without closing the field:
        # a pack type is still a string. A top-level `enum` would red-squiggle packs.
        type_field.pop("enum", None)
        type_field["anyOf"] = [{"enum": values}, {"type": "string"}]
        type_field.pop("type", None)

    root_props = schema.get("properties")
    if isinstance(root_props, dict) and "$schema" not in root_props:
        root_props["$schema"] = {
            "description": (
                "Optional JSON Schema identifier or relative path for editors. "
                "Ignored by ReadyAgents at load time; the CLI never fetches this URL."
            ),
            "type": "string",
        }

    dumped = json.dumps(schema)
    for leaked in _PYTHON_ONLY_ALIASES:
        if leaked in dumped:
            raise RuntimeError(f"Python-only name {leaked!r} leaked into the JSON Schema")

    return {
        "$schema": SCHEMA_DIALECT,
        "$id": SCHEMA_ID,
        "title": SCHEMA_TITLE,
        "description": SCHEMA_DESCRIPTION,
        "x-readyagents-version": __version__,
        **{k: v for k, v in schema.items() if k not in {"title", "description", "$schema", "$id"}},
    }
