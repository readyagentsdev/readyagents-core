"""AST-only Python: never import or exec the source module."""

from __future__ import annotations

import ast

from readyagents.errors import ImportRefused

_FORBIDDEN_CALLS = frozenset(
    {"exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals"}
)


def parse_python(source: str, *, filename: str = "<import>") -> ast.AST:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as extra:
        raise ImportRefused(
            f"Python source is not parseable: {extra}", reason="malformed"
        ) from extra
    refuse_dynamic(tree)
    return tree


def refuse_dynamic(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Expression) and False:
            continue
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _FORBIDDEN_CALLS:
                raise ImportRefused(
                    f"non-statically-analysable call {name}() is refused, not guessed",
                    reason="ast",
                )
            if name in {"import_module", "importlib.import_module"}:
                raise ImportRefused(
                    "dynamic importlib import is refused, not guessed",
                    reason="ast",
                )


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        left = _call_name(func.value)
        return f"{left}.{func.attr}" if left else func.attr
    return ""


def const_str(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in {"START", "END"}:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in {"START", "END"}:
        return node.attr
    return None
