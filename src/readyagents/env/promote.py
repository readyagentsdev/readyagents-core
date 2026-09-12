"""Promotion gates: eval, fixtures, benchmark, health, signed approval. Distinct reasons."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from readyagents.env.release import diff_releases
from readyagents.env.schema import EnvironmentSpec
from readyagents.env.store import EnvStore
from readyagents.errors import (
    ApprovalRequired,
    EnvGateBenchmark,
    EnvGateEval,
    EnvGateFixtures,
    EnvGateHealth,
    EnvRefused,
)


def promote(
    workflow: Path | str,
    *,
    source: str,
    target: str,
    spec: EnvironmentSpec,
    settings: Any = None,
    actor: str | None = None,
    decisions: dict[str, str] | None = None,
    hooks: dict[str, Callable[..., Any]] | None = None,
    sign_key: Path | str | None = None,
) -> dict[str, Any]:
    """Copy source env current onto target if all gates pass. Does not re-pin."""
    del sign_key
    store = EnvStore(settings)
    candidate = store.current(source)
    if candidate is None:
        raise EnvRefused(f"environment {source!r} has no current release", reason="undeployed")
    from readyagents.env.release import verify_release

    verify_release(candidate, settings=settings)
    evidence = evaluate_gates(
        spec,
        workflow=Path(workflow),
        candidate=candidate,
        current=store.current(target),
        settings=settings,
        actor=actor,
        decisions=decisions,
        hooks=hooks,
    )
    pointer = dict(candidate)
    pointer["from"] = source
    pointer["evidence"] = evidence
    pointer["actor"] = actor
    store.set_current(target, pointer)
    store.append_history(target, {"event": "promote", **pointer})
    return pointer


def rollback_env(
    env: str,
    *,
    spec: EnvironmentSpec,
    settings: Any = None,
    actor: str | None = None,
    decisions: dict[str, str] | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    """Manual rollback. Authorised as strongly as promotion when approval is declared."""
    store = EnvStore(settings)
    if spec.gates and spec.gates.approval:
        _approval_gate(
            spec.gates.approval,
            candidate=store.previous(env) or {},
            current=store.current(env),
            actor=actor,
            decisions=decisions or {},
            node_id="rollback",
        )
    return store.rollback(env, actor=actor, reason=reason)


def evaluate_gates(
    spec: EnvironmentSpec,
    *,
    workflow: Path,
    candidate: dict[str, Any],
    current: dict[str, Any] | None,
    settings: Any = None,
    actor: str | None = None,
    decisions: dict[str, str] | None = None,
    hooks: dict[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    gates = spec.gates
    evidence: dict[str, Any] = {}
    if gates is None:
        return evidence
    hook = hooks or {}
    if gates.eval:
        evidence["eval"] = _eval_gate(gates.eval, workflow, hook.get("eval"), settings)
    if gates.fixtures:
        evidence["fixtures"] = _fixtures_gate(
            gates.fixtures, workflow, hook.get("fixtures"), settings
        )
    if gates.benchmark:
        evidence["benchmark"] = _bench_gate(gates.benchmark, hook.get("benchmark"), settings)
    if gates.health:
        evidence["health"] = _health_gate(gates.health, hook.get("health"), settings)
    if gates.approval:
        _approval_gate(
            gates.approval,
            candidate=candidate,
            current=current,
            actor=actor,
            decisions=decisions or {},
            node_id="promote",
        )
        evidence["approval"] = {"actor": actor, "ok": True}
    return evidence


def _eval_gate(spec: dict[str, Any], workflow: Path, hook: Any, settings: Any) -> dict[str, Any]:
    if hook is not None:
        ok = bool(hook())
        if not ok:
            raise EnvGateEval("promotion eval gate failed")
        return {"ok": True, "hook": True}
    if not spec.get("must_pass", True):
        return {"ok": True, "skipped": True}
    suite = spec.get("suite")
    if not suite:
        raise EnvGateEval("promotion eval gate needs a suite")
    from readyagents.testing.eval import load_eval_suite, run_eval

    path = Path(suite)
    if not path.is_absolute():
        path = workflow.parent / path
    report = run_eval(load_eval_suite(path), settings=settings)
    if not report.ok:
        raise EnvGateEval(f"promotion eval gate failed: {report.failed} failed")
    return {"ok": True, "passed": report.passed}


def _fixtures_gate(
    spec: dict[str, Any], workflow: Path, hook: Any, settings: Any
) -> dict[str, Any]:
    if hook is not None:
        ok = bool(hook())
        if not ok:
            raise EnvGateFixtures("promotion fixture regression")
        return {"ok": True, "hook": True}
    if not spec.get("no_regression", True):
        return {"ok": True, "skipped": True}
    suite = spec.get("suite")
    if suite:
        try:
            return _eval_gate({"suite": suite, "must_pass": True}, workflow, None, settings)
        except EnvGateEval as extra:
            raise EnvGateFixtures(str(extra) or "promotion fixture regression") from extra
    return {"ok": True, "no_suite": True}


def _bench_gate(spec: dict[str, Any], hook: Any, settings: Any) -> dict[str, Any]:
    del settings
    if hook is not None:
        ok = bool(hook())
        if not ok:
            raise EnvGateBenchmark("promotion benchmark gate failed")
        return {"ok": True, "hook": True}
    current = spec.get("current")
    baseline = spec.get("baseline")
    if not current or not baseline:
        raise EnvGateBenchmark("promotion benchmark gate needs current and baseline")
    from readyagents.bench.compare import compare_results

    cur = json_load(Path(current))
    base = json_load(Path(baseline))
    tolerance = spec.get("tolerance")
    parsed: dict[str, float] | None = None
    if isinstance(tolerance, str) and tolerance.endswith("%"):
        parsed = {"wall_ms_pct": float(tolerance[:-1]) / 100.0}
    elif isinstance(tolerance, dict):
        parsed = {str(k): float(v) for k, v in tolerance.items()}
    report = compare_results(cur, base, tolerance=parsed)
    if not report.ok:
        raise EnvGateBenchmark("promotion benchmark gate failed: " + "; ".join(report.regressions))
    return {"ok": True}


def _health_gate(spec: dict[str, Any], hook: Any, settings: Any) -> dict[str, Any]:
    if hook is not None:
        score = float(hook())
        minimum = float(spec.get("min_score") or 0)
        if score < minimum:
            raise EnvGateHealth(f"health score {score} < {minimum}")
        return {"ok": True, "score": score, "hook": True}
    from readyagents.health.query import query_health
    from readyagents.run_store import open_run_store

    store = open_run_store(settings)
    window = int(spec.get("window") or 100)
    report = query_health(store, window=window)
    rate = 1.0
    if report.workflows:
        rate = float(report.workflows[0].success_rate)
    minimum = float(spec.get("min_score") or 0)
    if rate < minimum:
        raise EnvGateHealth(f"health score {rate} < {minimum}")
    return {"ok": True, "score": rate}


def _approval_gate(
    spec: dict[str, Any],
    *,
    candidate: dict[str, Any],
    current: dict[str, Any] | None,
    actor: str | None,
    decisions: dict[str, str],
    node_id: str = "promote",
) -> None:
    roles = list(spec.get("roles") or [])
    if not roles:
        return
    # Only the gate's own node id counts. --approve rollback must not promote,
    # and --approve promote must not authorise a manual rollback.
    token = str(decisions.get(node_id) or "").strip().lower()
    if token in {"approve", "approved", "yes"}:
        return
    diff = diff_releases(current, candidate)
    verb = "promoting" if node_id == "promote" else "rolling back"
    raise ApprovalRequired(
        node_id,
        "env",
        f"UNTRUSTED RELEASE DIFF (review the complete pin set before {verb})\n"
        + _format_diff(diff),
        pause={"untrusted": True, "diff": diff, "roles": roles, "actor": actor},
    )


def _format_diff(diff: dict[str, Any]) -> str:
    lines = [f"current: {diff.get('from') or '(none)'}", f"candidate: {diff.get('to') or '(none)'}"]
    pins = diff.get("pins")
    if isinstance(pins, dict):
        for key, value in sorted(pins.items()):
            if isinstance(value, dict):
                lines.append(f"  {key}: {value.get('from')} -> {value.get('to')}")
            else:
                lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def json_load(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
