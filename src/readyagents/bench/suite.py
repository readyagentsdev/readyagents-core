"""Load the shipped scenario suite. Paths resolve next to suite.yaml."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from readyagents.bench.layout import SCHEMA_SUITE, SHAPES
from readyagents.errors import BenchError, ConfigError


@dataclass
class BenchScenario:
    name: str
    shape: str
    workflow: Path
    cassette: Path
    inputs: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    expect_status: str = "succeeded"


def default_suite_path() -> Path:
    here = Path.cwd() / "examples" / "bench" / "suite.yaml"
    if here.is_file():
        return here
    root = Path(__file__).resolve().parents[3]
    cand = root / "examples" / "bench" / "suite.yaml"
    if cand.is_file():
        return cand
    raise BenchError("benchmark suite not found (examples/bench/suite.yaml)")


def load_suite(path: Path | str | None = None) -> list[BenchScenario]:
    file = Path(path) if path is not None else default_suite_path()
    if not file.is_file():
        raise ConfigError(f"Bench suite file not found: {file}")
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BenchError("bench suite must be a mapping")
    schema = str(raw.get("schema") or "")
    if schema and schema != SCHEMA_SUITE:
        raise BenchError(f"unknown bench suite schema {schema!r}")
    rows = raw.get("scenarios")
    if not isinstance(rows, list) or not rows:
        raise BenchError("bench suite needs a non-empty scenarios list")
    out: list[BenchScenario] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise BenchError(f"scenario #{index} must be a mapping")
        name = str(row.get("name") or "").strip()
        shape = str(row.get("shape") or name).strip()
        if not name or name in seen:
            raise BenchError(f"scenario #{index} needs a unique name")
        if shape not in SHAPES:
            raise BenchError(f"scenario {name!r} shape {shape!r} is not a declared shape")
        seen.add(name)
        workflow = _beside(file, row.get("workflow"), "workflow")
        cassette = _beside(file, row.get("cassette"), "cassette")
        inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else {}
        decisions = row.get("decisions") if isinstance(row.get("decisions"), dict) else {}
        out.append(
            BenchScenario(
                name=name,
                shape=shape,
                workflow=workflow,
                cassette=cassette,
                inputs={str(k): v for k, v in dict(inputs).items()},
                decisions={str(k): str(v) for k, v in dict(decisions).items()},
                expect_status=str(row.get("expect_status") or "succeeded"),
            )
        )
    missing = [shape for shape in SHAPES if shape not in {row.shape for row in out}]
    if missing:
        raise BenchError(f"suite missing declared shapes: {', '.join(missing)}")
    return out


def cassette_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_synthetic_cassette(path: Path) -> None:
    from readyagents.bench.layout import SECRET_NEEDLES

    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()
    for needle in SECRET_NEEDLES:
        if needle.lower() in lowered:
            raise BenchError(f"shipped cassette {path} is not synthetic-only ({needle})")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise BenchError(f"cassette {path} must be an object")


def _beside(suite: Path, raw: object, what: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise BenchError(f"scenario {what} path is required")
    path = (suite.parent / raw.strip()).resolve()
    if not path.is_file():
        raise BenchError(f"scenario {what} not found: {path}")
    return path
