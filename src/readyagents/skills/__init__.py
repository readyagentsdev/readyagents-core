"""Agent Skills interop: consume SKILL.md, export workflows as skills."""

from __future__ import annotations

from readyagents.skills.parse import SkillRecord, parse_skill_md

__all__ = ["SkillRecord", "parse_skill_md"]
