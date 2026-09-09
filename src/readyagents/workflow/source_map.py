"""Map Pydantic validation errors to YAML/JSON source positions.

The compose tree is built only on the error path. Success-path load cost stays
zero. Unmappable or ambiguous (anchor/alias) locations yield no position rather
than a wrong caret.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from readyagents.errors import SourceMapBoundError
from readyagents.policy import Redactor, redactor_from_settings
from readyagents.workflow.schema import NodeType

_MAX_NODES: Final = 20_000
_MAX_DEPTH: Final = 80
_MAX_EXCERPT: Final = 160
_TABSIZE: Final = 8
_ANSI: Final = re.compile(
    r"\x1b(?:[@-Z\\-_]|\][^\x07]*(?:\x07|\x1b\\)|[\[\]()#][0-9;?]*[ -/]*[@-~])"
)
_KEY_ERROR_TYPES: Final = frozenset({"extra_forbidden"})
_DID_YOU_MEAN_CUTOFF: Final = 0.75
_UNKNOWN_NODE: Final = re.compile(r"(?:unknown node(?: id)?|start node) '(?P<id>[^']+)'")
_FREE_TEXT_FIELDS: Final = frozenset(
    {
        "prompt",
        "system",
        "template",
        "source",
        "when",
        "description",
        "path",
        "name",
        "items",
        "model",
        "arguments",
    }
)
_ALIAS_FIELDS: Final = {"else_": "else", "from_": "from", "call_inputs": "inputs"}
_ARTIFACT_LOCS: Final = frozenset(
    {
        "NodeSpec",
        "RetrySpec",
        "BudgetSpec",
        "CircuitSpec",
        "EdgeSpec",
        "MCPServerSpec",
        "WorkflowSpec",
    }
)


@dataclass(frozen=True)
class SourcePosition:
    path: Path
    line: int  # 1-based
    column: int  # 1-based
    excerpt: str
    pointer: str


@dataclass(frozen=True)
class LocatedError:
    loc: tuple[str | int, ...]
    message: str
    position: SourcePosition | None
    include_chain: tuple[SourcePosition, ...] = ()


@dataclass
class _Indexed:
    key_mark: Any | None = None
    value_mark: Any | None = None
    scalar: Any = None


@dataclass
class _Index:
    by_path: dict[tuple[Any, ...], _Indexed]
    ambiguous: set[tuple[Any, ...]]
    lines: list[str]
    source: str


def locate_errors(
    exc: BaseException,
    path: Path | str,
    *,
    source: str | None = None,
    display_path: str | None = None,
) -> list[LocatedError]:
    """Resolve ``exc`` into located problems for ``path``.

    ``path`` is the user-given path (not a leaked resolve()). ``source`` is the
    already-read file text when the caller has it; otherwise the file is read.
    """
    given = Path(path)
    shown = display_path if display_path is not None else str(path)
    shown_path = Path(shown)
    text = source
    if text is None:
        try:
            text = given.read_text(encoding="utf-8")
        except OSError:
            text = ""

    if isinstance(exc, json.JSONDecodeError):
        pos = _position_from_json(exc, shown_path, text)
        return [LocatedError(loc=(), message=str(exc), position=pos)]
    if isinstance(exc, yaml.YAMLError):
        pos = _position_from_yaml(exc, shown_path, text)
        return [LocatedError(loc=(), message=str(exc), position=pos)]
    if not isinstance(exc, ValidationError):
        cause = exc.__cause__
        if isinstance(cause, (ValidationError, json.JSONDecodeError, yaml.YAMLError)):
            return locate_errors(cause, path, source=text, display_path=shown)
        return [LocatedError(loc=(), message=str(exc), position=None)]

    try:
        index = _build_index(text)
    except SourceMapBoundError:
        raise
    problems: list[LocatedError] = []
    for err in exc.errors():
        raw_loc = tuple(err.get("loc", ()))
        loc = _translate_loc(raw_loc)
        message = _with_suggestion(str(err.get("msg") or "invalid"), loc, index, err)
        prefer_key = str(err.get("type") or "") in _KEY_ERROR_TYPES
        pos = _resolve_position(index, loc, shown_path, prefer_key=prefer_key)
        problems.append(LocatedError(loc=loc, message=message, position=pos))
    return problems


def with_include_site(
    problems: list[LocatedError], site: SourcePosition | None
) -> list[LocatedError]:
    if site is None:
        return list(problems)
    return [replace(item, include_chain=item.include_chain + (site,)) for item in problems]


def locate_node_field(
    path: Path | str,
    node_id: str,
    field: str = "path",
    *,
    source: str | None = None,
    display_path: str | None = None,
) -> SourcePosition | None:
    """Position of ``field`` on the node whose ``id`` is ``node_id``."""
    given = Path(path)
    shown = Path(display_path if display_path is not None else str(path))
    text = source
    if text is None:
        try:
            text = given.read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        index = _build_index(text)
    except SourceMapBoundError:
        return None
    for loc, item in index.by_path.items():
        if len(loc) == 3 and loc[0] == "nodes" and loc[2] == "id" and item.scalar == node_id:
            field_loc = (loc[0], loc[1], field)
            return _resolve_position(index, field_loc, shown, prefer_key=False)
    return None


def problem_to_json(item: LocatedError) -> dict[str, Any]:
    pos = item.position
    file_name = str(pos.path) if pos is not None else ""
    return {
        "loc": ".".join(str(part) for part in item.loc),
        "message": item.message,
        "file": file_name,
        "line": pos.line if pos is not None else None,
        "column": pos.column if pos is not None else None,
    }


def render_located_problems(problems: list[LocatedError]) -> str:
    if not problems:
        return ""
    blocks = [_render_one(item) for item in problems]
    n = len(problems)
    suffix = f"{n} problem{'s' if n != 1 else ''} found."
    return "\n\n".join(blocks) + "\n\n" + suffix


def sanitize_excerpt(line: str, column: int) -> tuple[str, int]:
    """Strip ANSI/controls, expand tabs, cap length, and return (excerpt, 1-based col)."""
    raw = _ANSI.sub("", line.replace("\r", ""))
    cleaned: list[str] = []
    for ch in raw:
        o = ord(ch)
        if ch == "\t" or o >= 32:
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    expanded = "".join(cleaned).expandtabs(_TABSIZE)
    prefix = "".join(cleaned)[: max(0, column - 1)].expandtabs(_TABSIZE)
    display_col = len(prefix) + 1
    redacted = _redact(expanded)
    excerpt, display_col = _cap(redacted, display_col)
    return excerpt, max(1, display_col)


def _redact(text: str) -> str:
    """Mask default secret patterns plus configured settings literals/patterns."""
    try:
        from readyagents.config import get_settings

        settings = get_settings()
        configured = redactor_from_settings(
            enabled=True,
            patterns=settings.redact_pattern_list(),
            literals=settings.redact_literal_list(),
        )
    except Exception:  # noqa: BLE001 — excerpts must still redact if settings fail
        configured = None
    active = configured if configured is not None else Redactor()
    return active.redact_text(text)


def _cap(line: str, column: int) -> tuple[str, int]:
    if len(line) <= _MAX_EXCERPT:
        return line, column
    window = _MAX_EXCERPT
    start = max(0, column - 1 - window // 3)
    end = min(len(line), start + window)
    start = max(0, end - window)
    piece = line[start:end]
    if start > 0:
        piece = "…" + piece[1:]
    if end < len(line):
        piece = piece[:-1] + "…"
    new_col = column - start
    return piece, max(1, min(new_col, len(piece)))


def _build_index(source: str) -> _Index:
    if source.startswith("\ufeff"):
        source = source[1:]
    lines = source.splitlines()
    try:
        root = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return _Index(by_path={}, ambiguous=set(), lines=lines, source=source)
    index = _Index(by_path={}, ambiguous=set(), lines=lines, source=source)
    if root is None:
        return index
    seen: set[int] = set()
    _walk(root, (), 0, index, seen, count=[0])
    return index


def _walk(
    node: Node,
    path: tuple[Any, ...],
    depth: int,
    index: _Index,
    seen: set[int],
    count: list[int],
) -> None:
    if depth > _MAX_DEPTH:
        raise SourceMapBoundError(
            f"Workflow source exceeds nesting depth {_MAX_DEPTH} (possible YAML bomb)."
        )
    ident = id(node)
    if ident in seen:
        index.ambiguous.add(path)
        return
    seen.add(ident)
    count[0] += 1
    if count[0] > _MAX_NODES:
        raise SourceMapBoundError(
            f"Workflow source exceeds {_MAX_NODES} nodes (possible YAML bomb)."
        )

    slot = index.by_path.setdefault(path, _Indexed())
    slot.value_mark = getattr(node, "start_mark", None)
    if isinstance(node, ScalarNode):
        slot.scalar = _scalar_python(node)

    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            key = _scalar_python(key_node)
            if key is None:
                key = getattr(key_node, "value", None)
            child = path + (key,)
            child_slot = index.by_path.setdefault(child, _Indexed())
            child_slot.key_mark = getattr(key_node, "start_mark", None)
            _walk(value_node, child, depth + 1, index, seen, count)
    elif isinstance(node, SequenceNode):
        for i, item in enumerate(node.value):
            _walk(item, path + (i,), depth + 1, index, seen, count)


def _scalar_python(node: Node) -> Any:
    if not isinstance(node, ScalarNode):
        return None
    tag = node.tag or ""
    raw = node.value
    if tag == "tag:yaml.org,2002:int":
        try:
            return int(raw, 10)
        except ValueError:
            return raw
    if tag == "tag:yaml.org,2002:bool":
        return str(raw).lower() == "true"
    if tag == "tag:yaml.org,2002:null":
        return None
    if tag == "tag:yaml.org,2002:float":
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _node_ids(index: _Index) -> list[str]:
    found: list[str] = []
    for loc, item in index.by_path.items():
        if len(loc) == 3 and loc[0] == "nodes" and loc[2] == "id" and isinstance(item.scalar, str):
            found.append(item.scalar)
    return found


def _close_match(value: str, candidates: list[str]) -> str | None:
    if not value or not candidates:
        return None
    hits = difflib.get_close_matches(value, candidates, n=1, cutoff=_DID_YOU_MEAN_CUTOFF)
    if hits and hits[0] != value:
        return hits[0]
    return None


def _with_suggestion(message: str, loc: tuple[Any, ...], index: _Index, err: dict[str, Any]) -> str:
    """Append a conservative did-you-mean for node ids / enums. Never for free-text."""
    match = _UNKNOWN_NODE.search(message)
    if match:
        suggestion = _close_match(match.group("id"), _node_ids(index))
        if suggestion:
            return f"{message} (did you mean '{suggestion}'?)"
    field = loc[-1] if loc else None
    if field in _FREE_TEXT_FIELDS:
        return message
    if field == "type":
        raw = err.get("input")
        if isinstance(raw, str):
            suggestion = _close_match(raw, [member.value for member in NodeType])
            if suggestion:
                return f"{message} (did you mean '{suggestion}'?)"
    return message


def _translate_loc(loc: tuple[Any, ...]) -> tuple[str | int, ...]:
    out: list[str | int] = []
    for part in loc:
        if part in _ARTIFACT_LOCS:
            continue
        if part in _ALIAS_FIELDS:
            out.append(_ALIAS_FIELDS[part])
        elif isinstance(part, (str, int)):
            out.append(part)
        else:
            out.append(str(part))
    return tuple(out)


def _resolve_position(
    index: _Index,
    loc: tuple[Any, ...],
    path: Path,
    *,
    prefer_key: bool,
) -> SourcePosition | None:
    if not loc:
        mark = index.by_path.get((), _Indexed()).value_mark
        return _from_mark(path, index, mark)
    for prefix in _prefixes(loc):
        if prefix in index.ambiguous:
            return None
    current = loc
    while True:
        if current in index.ambiguous:
            return None
        slot = index.by_path.get(current)
        if slot is not None:
            mark = slot.key_mark if prefer_key and slot.key_mark is not None else slot.value_mark
            if prefer_key and slot.key_mark is None:
                mark = slot.value_mark
            if not prefer_key:
                mark = slot.value_mark or slot.key_mark
            return _from_mark(path, index, mark)
        if not current:
            return None
        current = current[:-1]
        prefer_key = False


def _prefixes(loc: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    return [loc[:i] for i in range(len(loc), 0, -1)]


def _from_mark(path: Path, index: _Index, mark: Any | None) -> SourcePosition | None:
    if mark is None:
        return None
    line_no = int(getattr(mark, "line", -1)) + 1
    col_no = int(getattr(mark, "column", 0)) + 1
    if line_no < 1:
        return None
    raw = index.lines[line_no - 1] if line_no <= len(index.lines) else ""
    excerpt, column = sanitize_excerpt(raw, col_no)
    pointer = " " * (column - 1) + "^"
    return SourcePosition(path=path, line=line_no, column=column, excerpt=excerpt, pointer=pointer)


def _position_from_yaml(exc: yaml.YAMLError, path: Path, source: str) -> SourcePosition | None:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    lines = source.splitlines()
    return _from_mark(path, _Index(by_path={}, ambiguous=set(), lines=lines, source=source), mark)


def _position_from_json(
    exc: json.JSONDecodeError, path: Path, source: str
) -> SourcePosition | None:
    lines = source.splitlines()
    line_no = int(getattr(exc, "lineno", 1) or 1)
    col_no = int(getattr(exc, "colno", 1) or 1)
    raw = lines[line_no - 1] if 1 <= line_no <= len(lines) else ""
    excerpt, column = sanitize_excerpt(raw, col_no)
    pointer = " " * (column - 1) + "^"
    return SourcePosition(path=path, line=line_no, column=column, excerpt=excerpt, pointer=pointer)


def _render_one(item: LocatedError) -> str:
    loc = ".".join(str(part) for part in item.loc)
    pos = item.position
    if pos is None:
        label = f"{loc}: {item.message}" if loc else item.message
        return f"      = {label}"
    gutter = str(pos.line).rjust(4)
    lines = [
        f"  {pos.path}:{pos.line}:{pos.column}",
        f" {gutter} | {pos.excerpt}",
        f"      | {pos.pointer}",
        f"      = {loc}: {item.message}" if loc else f"      = {item.message}",
    ]
    for site in item.include_chain:
        lines.append(f"      included from {site.path}:{site.line}:{site.column}")
    return "\n".join(lines)
