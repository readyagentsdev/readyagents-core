"""Parse open-format SKILL.md. Frontmatter only until the skill is selected."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from readyagents.errors import SkillRefused

MAX_SKILL_MD_BYTES = 256_000
MAX_FRONTMATTER_BYTES = 8_192
MAX_DESCRIPTION = 1024
MAX_NAME = 64
MAX_COMPAT = 500
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class SkillRecord:
    name: str
    description: str
    body: str = ""
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    path: str = ""
    scripts: list[str] = field(default_factory=list)

    def disclose(self) -> dict[str, str]:
        """Progressive disclosure: name and description only."""
        return {"name": self.name, "description": self.description}

    def as_index(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "license": self.license,
            "compatibility": self.compatibility,
            "metadata": dict(self.metadata),
            "allowed_tools": list(self.allowed_tools),
            "scripts": list(self.scripts),
            "path": self.path,
        }


def parse_skill_md(text: str, *, directory_name: str | None = None) -> SkillRecord:
    if len(text.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise SkillRefused("SKILL.md exceeds size cap", reason="too_large")
    front, body = _split_frontmatter(text)
    if len(front.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise SkillRefused("SKILL.md frontmatter exceeds size cap", reason="too_large")
    try:
        raw = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError as extra:
        raise SkillRefused(
            f"SKILL.md frontmatter is not YAML: {extra}", reason="frontmatter"
        ) from extra
    if not isinstance(raw, dict):
        raise SkillRefused("SKILL.md frontmatter must be a mapping", reason="frontmatter")
    _reject_hostile(raw)
    name = _name(raw.get("name"), directory_name=directory_name)
    description = _description(raw.get("description"))
    license_ = _opt_str(raw.get("license"), field="license")
    compatibility = _opt_str(raw.get("compatibility"), field="compatibility", max_len=MAX_COMPAT)
    metadata = _metadata(raw.get("metadata"))
    tools = _allowed_tools(raw.get("allowed-tools"))
    return SkillRecord(
        name=name,
        description=description,
        body=body,
        license=license_,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=tools,
    )


def load_skill_dir(root: Path) -> SkillRecord:
    folder = Path(root)
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        raise SkillRefused(f"SKILL.md missing in {folder}", reason="missing")
    if skill_md.is_symlink() or folder.is_symlink():
        raise SkillRefused("skill path is a symlink", reason="symlink")
    text = skill_md.read_text(encoding="utf-8")
    record = parse_skill_md(text, directory_name=folder.name)
    record.path = str(folder)
    record.scripts = _list_scripts(folder)
    return record


def _split_frontmatter(text: str) -> tuple[str, str]:
    blob = text.lstrip("\ufeff")
    if not blob.startswith("---"):
        raise SkillRefused("SKILL.md must start with YAML frontmatter (---)", reason="frontmatter")
    rest = blob[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        raise SkillRefused("SKILL.md frontmatter is not closed", reason="frontmatter")
    front = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    return front, body


def _name(value: Any, *, directory_name: str | None) -> str:
    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME:
        raise SkillRefused("SKILL.md name must be 1–64 characters", reason="name")
    if not _NAME.fullmatch(name):
        raise SkillRefused(
            "SKILL.md name must be lowercase a-z/0-9 with single hyphens",
            reason="name",
        )
    if directory_name is not None and name != directory_name:
        raise SkillRefused(
            f"SKILL.md name {name!r} must match directory {directory_name!r}",
            reason="name",
        )
    return name


def _description(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_DESCRIPTION:
        raise SkillRefused("SKILL.md description must be 1–1024 characters", reason="description")
    return text


def _opt_str(value: Any, *, field: str, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if max_len is not None and len(text) > max_len:
        raise SkillRefused(f"SKILL.md {field} exceeds {max_len} characters", reason=field)
    return text


def _metadata(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillRefused("SKILL.md metadata must be a string map", reason="metadata")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise SkillRefused(
                "SKILL.md metadata keys and values must be strings", reason="metadata"
            )
        out[key] = item
    return out


def _allowed_tools(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part for part in text.split() if part]


def _reject_hostile(raw: dict[str, Any]) -> None:
    blob = yaml.safe_dump(raw)
    if "\x00" in blob:
        raise SkillRefused("SKILL.md frontmatter contains NUL", reason="hostile")
    if len(blob) > MAX_FRONTMATTER_BYTES:
        raise SkillRefused("SKILL.md frontmatter exceeds size cap", reason="too_large")


def _list_scripts(folder: Path) -> list[str]:
    scripts = folder / "scripts"
    if not scripts.is_dir():
        return []
    out: list[str] = []
    for path in sorted(scripts.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file():
            out.append(str(path.relative_to(folder)).replace("\\", "/"))
    return out
