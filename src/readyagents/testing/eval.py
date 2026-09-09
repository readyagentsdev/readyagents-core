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

_DETERMINISM_BUCKETS = frozenset({"sealed", "recomputed", "unsealable", "misses"})
_USAGE_METRICS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens", "cost_micros"})


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
    expect_determinism: dict[str, list[str]] | None = None
    expect_nodes: list[str] | None = None
    expect_tools: list[dict[str, Any]] | None = None
    expect_usage: dict[str, dict[str, int]] | None = None


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
        expect_determinism=_optional_determinism_field(raw, name),
        expect_nodes=_optional_str_list_field(raw, "expect_nodes", name),
        expect_tools=_optional_tools_field(raw, name),
        expect_usage=_optional_usage_field(raw, name),
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


def _optional_determinism_field(raw: Mapping[str, Any], name: str) -> dict[str, list[str]] | None:
    data = _optional_mapping_field(raw, "expect_determinism", name)
    if data is None:
        return None
    out: dict[str, list[str]] = {}
    for bucket, value in data.items():
        if bucket not in _DETERMINISM_BUCKETS:
            raise ConfigError(
                f"Eval case {name!r} expect_determinism has unknown bucket {bucket!r}"
            )
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ConfigError(
                f"Eval case {name!r} expect_determinism.{bucket} must be a list of strings"
            )
        out[bucket] = [str(item) for item in value]
    return out


def _optional_str_list_field(raw: Mapping[str, Any], key: str, name: str) -> list[str] | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"Eval case {name!r} field '{key}' must be a list of strings")
    return [str(item) for item in value]


def _optional_tools_field(raw: Mapping[str, Any], name: str) -> list[dict[str, Any]] | None:
    if "expect_tools" not in raw or raw["expect_tools"] is None:
        return None
    value = raw["expect_tools"]
    if not isinstance(value, list):
        raise ConfigError(f"Eval case {name!r} field 'expect_tools' must be a list")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ConfigError(f"Eval case {name!r} expect_tools[{index}] must be a mapping")
        tool_name = item.get("name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ConfigError(f"Eval case {name!r} expect_tools[{index}] needs a name")
        row: dict[str, Any] = {"name": tool_name.strip()}
        if "arguments" in item and item["arguments"] is not None:
            arguments = item["arguments"]
            if not isinstance(arguments, Mapping):
                raise ConfigError(
                    f"Eval case {name!r} expect_tools[{index}].arguments must be a mapping"
                )
            row["arguments"] = dict(arguments)
        out.append(row)
    return out


def _optional_usage_field(raw: Mapping[str, Any], name: str) -> dict[str, dict[str, int]] | None:
    data = _optional_mapping_field(raw, "expect_usage", name)
    if data is None:
        return None
    out: dict[str, dict[str, int]] = {}
    for metric, spec in data.items():
        if metric not in _USAGE_METRICS:
            raise ConfigError(f"Eval case {name!r} expect_usage has unknown metric {metric!r}")
        if not isinstance(spec, Mapping):
            raise ConfigError(
                f"Eval case {name!r} expect_usage.{metric} must be a mapping with max"
            )
        extra = set(spec) - {"max"}
        if extra:
            raise ConfigError(
                f"Eval case {name!r} expect_usage.{metric} has unknown keys {sorted(extra)}"
            )
        if "max" not in spec:
            raise ConfigError(f"Eval case {name!r} expect_usage.{metric} needs an integer max")
        try:
            ceiling = int(spec["max"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Eval case {name!r} expect_usage.{metric}.max must be an integer"
            ) from exc
        if ceiling < 0:
            raise ConfigError(f"Eval case {name!r} expect_usage.{metric}.max must be >= 0")
        out[metric] = {"max": ceiling}
    return out


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
    failed = _score_determinism(state, case)
    if failed:
        return failed
    failed = _score_nodes(state, case)
    if failed:
        return failed
    failed = _score_tools(state, case)
    if failed:
        return failed
    failed = _score_usage(state, case)
    if failed:
        return failed
    return True, "ok"


def _score_determinism(state: RunState, case: EvalCase) -> tuple[bool, str] | None:
    if case.expect_determinism is None:
        return None
    raw = state.metadata.get("determinism")
    if not isinstance(raw, Mapping):
        return False, "determinism metadata missing"
    for bucket, expected in case.expect_determinism.items():
        actual = _bucket_ids(raw, bucket)
        wanted = {str(item) for item in expected}
        if actual != wanted:
            return False, (f"determinism.{bucket} {sorted(actual)!r} != {sorted(wanted)!r}")
    return None


def _bucket_ids(report: Mapping[str, Any], bucket: str) -> set[str]:
    value = report.get(bucket) or []
    if not isinstance(value, list):
        return set()
    if bucket != "misses":
        return {str(item) for item in value if item}
    ids: set[str] = set()
    for item in value:
        if isinstance(item, Mapping) and item.get("node_id"):
            ids.add(str(item["node_id"]))
        elif isinstance(item, str) and item:
            ids.add(item)
    return ids


def _score_nodes(state: RunState, case: EvalCase) -> tuple[bool, str] | None:
    if case.expect_nodes is None:
        return None
    actual = [row.node_id for row in state.results]
    if actual != list(case.expect_nodes):
        return False, f"nodes {actual!r} != {list(case.expect_nodes)!r}"
    return None


def _score_tools(state: RunState, case: EvalCase) -> tuple[bool, str] | None:
    if case.expect_tools is None:
        return None
    observed, from_cassette = _observed_tools(state, case)
    expected = list(case.expect_tools)
    if len(observed) < len(expected):
        missing = expected[len(observed)]["name"]
        return False, f"tool[{len(observed)}] {missing} missing"
    if len(observed) > len(expected):
        extra = observed[len(expected)]["name"]
        return False, f"tool[{len(expected)}] extra {extra}"
    for index, exp in enumerate(expected):
        obs = observed[index]
        if obs.get("name") != exp["name"]:
            return False, f"tool[{index}] {obs.get('name')!r} != {exp['name']!r}"
        if "arguments" in exp:
            if not from_cassette:
                return False, "expect_tools.arguments require a cassette"
            obs_args = obs.get("arguments")
            if not isinstance(obs_args, Mapping):
                return False, f"tool[{index}] arguments missing"
            for key, value in dict(exp["arguments"]).items():
                if obs_args.get(key) != value:
                    return False, (
                        f"tool[{index}] arguments.{key} {obs_args.get(key)!r} != {value!r}"
                    )
    return None


def _observed_tools(state: RunState, case: EvalCase) -> tuple[list[dict[str, Any]], bool]:
    if case.cassette is not None:
        from readyagents.replay.cassette import Cassette

        cassette = Cassette.load(case.cassette)
        observed: list[dict[str, Any]] = []
        for entry in cassette.entries.values():
            if entry.get("kind") != "tool":
                continue
            row: dict[str, Any] = {"name": str(entry.get("name") or "")}
            if "arguments" in entry and not entry.get("redacted_blocked"):
                row["arguments"] = dict(entry.get("arguments") or {})
            observed.append(row)
        return observed, True
    rounds: list[dict[str, Any]] = []
    for node in state.results:
        for item in node.tool_rounds or []:
            rounds.append({"name": str(item.get("name") or "")})
    return rounds, False


def _score_usage(state: RunState, case: EvalCase) -> tuple[bool, str] | None:
    if case.expect_usage is None:
        return None
    for metric, spec in case.expect_usage.items():
        if metric not in state.usage:
            return False, f"usage.{metric} missing"
        actual = int(state.usage[metric])
        ceiling = int(spec["max"])
        if actual > ceiling:
            return False, f"usage.{metric} {actual} > max {ceiling}"
    return None


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
