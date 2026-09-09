"""Tiny eval harness: score fixture workflows (pass and fail cases)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from readyagents.errors import ConfigError
from readyagents.testing.helpers import run_workflow_file_test, run_workflow_spec
from readyagents.tools import ToolRegistry
from readyagents.workflow.schema import WorkflowSpec
from readyagents.workflow.state import RunState


@dataclass
class EvalCase:
    name: str
    workflow: Mapping[str, Any] | WorkflowSpec | Path | str
    inputs: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    expect_status: str = "succeeded"
    expect_outputs: dict[str, Any] | None = None
    expect_contains: dict[str, str] | None = None
    cassette: Path | None = None


@dataclass
class EvalResult:
    name: str
    passed: bool
    reason: str = ""
    state: RunState | None = None


@dataclass
class EvalReport:
    results: list[EvalResult]

    @property
    def passed(self) -> int:
        return sum(1 for row in self.results if row.passed)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.results if not row.passed)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def assert_passing(self) -> None:
        if self.ok:
            return
        lines = [f"{row.name}: {row.reason}" for row in self.results if not row.passed]
        raise AssertionError("eval failures:\n" + "\n".join(lines))


def load_eval_suite(path: Path | str) -> list[EvalCase]:
    """Load a YAML/JSON mapping with a ``cases:`` list of :class:`EvalCase` fields."""
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Eval suite file not found: {file}")
    text = file.read_text(encoding="utf-8")
    try:
        if file.suffix.lower() in {".json"}:
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not parse eval suite {file}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError(f"Eval suite {file} must be a mapping with a 'cases' list")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list):
        raise ConfigError(f"Eval suite {file} must be a mapping with a 'cases' list")
    if not raw_cases:
        raise ConfigError(f"Eval suite {file} has no cases")
    return [
        _case_from_mapping(row, index=i, suite=file) for i, row in enumerate(raw_cases, start=1)
    ]


def _case_from_mapping(raw: object, *, index: int, suite: Path) -> EvalCase:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"Eval suite {suite} case #{index} must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"Eval suite {suite} case #{index} needs a name")
    name = name.strip()
    workflow_field = raw.get("workflow")
    if isinstance(workflow_field, str):
        rel = workflow_field.strip()
        if not rel:
            raise ConfigError(f"Eval case {name!r} has an empty workflow path")
        # File workflows are resolved next to the suite, not the process cwd.
        workflow: Mapping[str, Any] | WorkflowSpec | Path | str = Path(suite).parent / rel
    elif isinstance(workflow_field, Mapping):
        workflow = dict(workflow_field)
    else:
        raise ConfigError(f"Eval case {name!r} needs a workflow path or inline mapping")
    expect_status = raw.get("expect_status", "succeeded")
    if not isinstance(expect_status, str) or not expect_status.strip():
        raise ConfigError(f"Eval case {name!r} field 'expect_status' must be a string")
    cassette_field = raw.get("cassette")
    cassette_path: Path | None = None
    if isinstance(cassette_field, str) and cassette_field.strip():
        cassette_path = Path(suite).parent / cassette_field.strip()
    return EvalCase(
        name=name,
        workflow=workflow,
        inputs=_mapping_field(raw, "inputs", name, default={}),
        decisions=_str_mapping_field(raw, "decisions", name, default={}),
        expect_status=expect_status.strip(),
        expect_outputs=_optional_mapping_field(raw, "expect_outputs", name),
        expect_contains=_optional_str_mapping_field(raw, "expect_contains", name),
        cassette=cassette_path,
    )


def _mapping_field(
    raw: Mapping[str, Any],
    key: str,
    name: str,
    *,
    default: dict[str, Any],
) -> dict[str, Any]:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, Mapping):
        raise ConfigError(f"Eval case {name!r} field '{key}' must be a mapping")
    return dict(value)


def _str_mapping_field(
    raw: Mapping[str, Any],
    key: str,
    name: str,
    *,
    default: dict[str, str],
) -> dict[str, str]:
    data = _mapping_field(raw, key, name, default=default)
    return {str(k): str(v) for k, v in data.items()}


def _optional_mapping_field(
    raw: Mapping[str, Any],
    key: str,
    name: str,
) -> dict[str, Any] | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if not isinstance(value, Mapping):
        raise ConfigError(f"Eval case {name!r} field '{key}' must be a mapping")
    return dict(value)


def _optional_str_mapping_field(
    raw: Mapping[str, Any],
    key: str,
    name: str,
) -> dict[str, str] | None:
    data = _optional_mapping_field(raw, key, name)
    if data is None:
        return None
    return {str(k): str(v) for k, v in data.items()}


def _score(state: RunState, case: EvalCase) -> tuple[bool, str]:
    if state.status != case.expect_status:
        return False, f"status {state.status!r} != {case.expect_status!r}"
    outputs = state.output_keys or state.node_outputs
    if case.expect_outputs:
        for key, expected in case.expect_outputs.items():
            actual = outputs.get(key)
            if actual != expected:
                return False, f"output {key!r}={actual!r} != {expected!r}"
    if case.expect_contains:
        for key, needle in case.expect_contains.items():
            hay = outputs.get(key)
            if needle not in str(hay):
                return False, f"output {key!r}={hay!r} does not contain {needle!r}"
    return True, "ok"


def run_eval(
    cases: Sequence[EvalCase],
    *,
    llm: Any = None,
    tools: ToolRegistry | None = None,
    settings: Any | None = None,
) -> EvalReport:
    results: list[EvalResult] = []
    for case in cases:
        try:
            extra: dict[str, Any] = {}
            if case.cassette is not None:
                extra["offline"] = True
                extra["cassette_path"] = case.cassette
                extra["record"] = False
            if isinstance(case.workflow, (Path, str)):
                state = run_workflow_file_test(
                    case.workflow,
                    inputs=case.inputs,
                    llm=llm,
                    settings=settings,
                    persist=False,
                    decisions=case.decisions,
                    extra_tools=tools,
                    **extra,
                )
            else:
                state = run_workflow_spec(
                    case.workflow,
                    inputs=case.inputs,
                    llm=llm,
                    tools=tools,
                    decisions=case.decisions,
                )
        except Exception as exc:  # noqa: BLE001
            results.append(EvalResult(name=case.name, passed=False, reason=str(exc)))
            continue
        ok, reason = _score(state, case)
        results.append(EvalResult(name=case.name, passed=ok, reason=reason, state=state))
    return EvalReport(results)
