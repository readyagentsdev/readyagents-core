"""Restricted row expressions. No eval(), no attribute access, no imports."""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

from readyagents.errors import TableError, WorkflowError
from readyagents.workflow.conditions import evaluate_condition

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_CALLS = {
    "abs": abs,
    "round": round,
    "len": len,
    "min": min,
    "max": max,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}


def eval_predicate(expr: str, row: Mapping[str, Any]) -> bool:
    text = (expr or "").strip()
    if not text:
        return False
    try:
        return bool(evaluate_condition(text, row))
    except (WorkflowError, TableError):
        return bool(_eval_ast(text, row))


def eval_value(expr: str, row: Mapping[str, Any]) -> Any:
    text = (expr or "").strip()
    if not text:
        return None
    if text.isidentifier():
        return row.get(text)
    return _eval_ast(text, row)


def _eval_ast(expr: str, row: Mapping[str, Any]) -> Any:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise TableError("invalid table expression") from exc
    return _node(tree.body, row)


def _node(node: ast.AST, row: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id not in row:
            raise TableError(f"unknown column {node.id!r}")
        return row.get(node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_node(node.left, row), _node(node.right, row))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_node(node.operand, row))
    if isinstance(node, ast.BoolOp):
        values = [_node(v, row) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.Compare):
        left = _node(node.left, row)
        for op, raw in zip(node.ops, node.comparators, strict=True):
            fn = _CMP.get(type(op))
            if fn is None:
                raise TableError("unsupported comparison")
            right = _node(raw, row)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALLS:
            raise TableError("unsupported call in table expression")
        args = [_node(a, row) for a in node.args]
        return _CALLS[node.func.id](*args)
    raise TableError("unsupported table expression")
