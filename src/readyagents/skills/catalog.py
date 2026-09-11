"""Local confined skill catalog. No marketplace, no auto-update."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from readyagents.atomic import atomic_write_text
from readyagents.errors import SkillRefused
from readyagents.permissions import restrict_file
from readyagents.skills.digest import digest_skill_dir
from readyagents.skills.parse import SkillRecord, load_skill_dir

INDEX_NAME = "index.json"
CATALOG = "skills"


def catalog_dir(home: Path) -> Path:
    return Path(home) / CATALOG


def skill_dir(home: Path, name: str) -> Path:
    return catalog_dir(home) / name


def load_index(home: Path) -> dict[str, Any]:
    path = catalog_dir(home) / INDEX_NAME
    if not path.is_file():
        return {"skills": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"skills": {}}
    if not isinstance(raw, dict):
        return {"skills": {}}
    skills = raw.get("skills")
    if not isinstance(skills, dict):
        raw["skills"] = {}
    return raw


def save_index(home: Path, index: dict[str, Any]) -> None:
    dest = catalog_dir(home)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / INDEX_NAME
    atomic_write_text(
        path,
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
        restrict=True,
    )
    restrict_file(path)


def list_records(home: Path) -> list[dict[str, Any]]:
    index = load_index(home)
    rows = list((index.get("skills") or {}).values())
    return [row for row in rows if isinstance(row, dict)]


def get_record(home: Path, name: str) -> dict[str, Any]:
    index = load_index(home)
    row = (index.get("skills") or {}).get(name)
    if not isinstance(row, dict):
        raise SkillRefused(f"skill not installed: {name}", reason="missing")
    return row


def load_installed(home: Path, name: str) -> SkillRecord:
    row = get_record(home, name)
    folder = Path(str(row.get("path") or skill_dir(home, name)))
    record = load_skill_dir(folder)
    return record


def disclose_all(home: Path) -> list[dict[str, str]]:
    """Progressive disclosure: name and description only, until a node selects."""
    out: list[dict[str, str]] = []
    for row in list_records(home):
        out.append(
            {
                "name": str(row.get("name") or ""),
                "description": str(row.get("description") or ""),
            }
        )
    return out


def upsert(
    home: Path, record: SkillRecord, *, source: str, signature_status: str
) -> dict[str, Any]:
    digest = digest_skill_dir(Path(record.path))
    row = {
        **record.as_index(),
        "source": source,
        "digest": digest,
        "signature_status": signature_status,
    }
    index = load_index(home)
    skills = index.setdefault("skills", {})
    skills[record.name] = row
    save_index(home, index)
    return row


def remove_name(home: Path, name: str) -> None:
    index = load_index(home)
    skills = index.setdefault("skills", {})
    if name not in skills:
        raise SkillRefused(f"skill not installed: {name}", reason="missing")
    skills.pop(name, None)
    save_index(home, index)
    folder = skill_dir(home, name)
    if folder.is_dir():
        import shutil

        shutil.rmtree(folder)
