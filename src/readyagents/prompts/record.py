"""Prompt object: id, version, content hash, text. Stored as data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from readyagents.prompts.layout import SCHEMA_REGISTRY, content_hash


@dataclass
class PromptVersion:
    version: int
    content_hash: str
    text: str
    source: str = "literal"
    created_at: str = ""
    parent: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "text": self.text,
            "source": self.source,
            "created_at": self.created_at,
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptVersion:
        text = str(data.get("text") or "")
        digest = str(data.get("content_hash") or content_hash(text))
        parent = data.get("parent")
        return cls(
            version=int(data.get("version") or 0),
            content_hash=digest,
            text=text,
            source=str(data.get("source") or "literal"),
            created_at=str(data.get("created_at") or ""),
            parent=int(parent) if parent is not None else None,
        )


@dataclass
class PromptObject:
    """Addressable prompt: id + versions. Active version is the adopted one."""

    id: str
    node_id: str
    active_version: int = 1
    versions: list[PromptVersion] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node_id": self.node_id,
            "active_version": self.active_version,
            "versions": [row.as_dict() for row in self.versions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptObject:
        rows = data.get("versions") or []
        return cls(
            id=str(data.get("id") or ""),
            node_id=str(data.get("node_id") or data.get("id") or ""),
            active_version=int(data.get("active_version") or 1),
            versions=[PromptVersion.from_dict(row) for row in rows if isinstance(row, dict)],
        )

    def version(self, number: int) -> PromptVersion | None:
        for row in self.versions:
            if row.version == number:
                return row
        return None

    def active(self) -> PromptVersion | None:
        return self.version(self.active_version)


@dataclass
class PromptRegistry:
    schema: str = SCHEMA_REGISTRY
    workflow: str = ""
    prompts: dict[str, PromptObject] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "workflow": self.workflow,
            "prompts": {key: value.as_dict() for key, value in self.prompts.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PromptRegistry:
        raw = data.get("prompts") or {}
        prompts = {
            str(key): PromptObject.from_dict(value)
            for key, value in dict(raw).items()
            if isinstance(value, dict)
        }
        return cls(
            schema=str(data.get("schema") or SCHEMA_REGISTRY),
            workflow=str(data.get("workflow") or ""),
            prompts=prompts,
        )
