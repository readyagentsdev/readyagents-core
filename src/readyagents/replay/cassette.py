"""Content-addressed cassette. Untrusted input: schema, version, and size checked."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from readyagents import __version__
from readyagents.atomic import atomic_write_text
from readyagents.errors import CassetteError, CassetteMiss
from readyagents.llm.base import CompletionResult, Message
from readyagents.llm.cache import completion_key, tool_call_key
from readyagents.llm.tool_calls import tool_calls_from_json, tool_calls_to_json
from readyagents.paths import resolve_within
from readyagents.workflow.state import utc_now

CASSETTE_VERSION = 1
DEFAULT_MAX_ENTRY_BYTES = 1_048_576
DEFAULT_MAX_CASSETTE_BYTES = 10_485_760
_LLM = "llm"
_TOOL = "tool"
_CODE = "code"
_CONTRACT = "contract"
_DOCUMENT = "document"
_TRANSCRIBE = "transcribe"
_TABLE = "table"
_MEDIA = "media"

DETERMINISTIC_TOOLS = frozenset({"calc", "json_get", "json_set", "json_merge"})
SEALABLE_TOOLS = frozenset({"now", "http_get", "read_file", "list_dir"})
CORE_BUILTIN_TOOLS = DETERMINISTIC_TOOLS | SEALABLE_TOOLS | frozenset({"write_file"})
SEAL_CLASSES = frozenset({"recomputed", "sealable", "unsealable"})


def classify_tool(name: str, *, seals: Mapping[str, str] | None = None) -> str:
    """Return recomputed, sealable, or unsealable for a tool name.

    Builtin names always win. Pack seals apply only to unclassified names.
    """
    if name in DETERMINISTIC_TOOLS:
        return "recomputed"
    if name in SEALABLE_TOOLS:
        return "sealable"
    if name in CORE_BUILTIN_TOOLS:
        return "unsealable"
    try:
        from readyagents.connectors.registry import spec_for

        spec = spec_for(name)
        if spec is not None and spec.determinism in SEAL_CLASSES:
            return spec.determinism
    except Exception:  # noqa: BLE001
        pass
    declared = (seals or {}).get(name)
    if declared in SEAL_CLASSES:
        return declared
    return "unsealable"


def classify_node_type(node_type: str) -> str:
    """Default classification for a node type when no cassette entry applies."""
    kind = str(node_type)
    if kind in {"transform", "condition", "foreach", "parallel", "include"}:
        return "recomputed"
    if kind == "approval":
        return "recomputed"
    if kind == "tool":
        return "unsealable"
    if kind == "agent":
        return "unsealable"
    if kind == "code":
        return "sealed"
    if kind in {"document", "transcribe", "table"}:
        return "sealed"
    if kind == "classify":
        return "unsealable"
    return "unsealable"


@dataclass
class DeterminismReport:
    """Per-node sealed / recomputed / unsealable / misses. Additive --json field."""

    sealed: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    unsealable: list[str] = field(default_factory=list)
    misses: list[dict[str, Any]] = field(default_factory=list)
    positional_fallback: bool = False
    _rank: dict[str, int] = field(default_factory=dict, repr=False)

    def note(self, node_id: str, bucket: str, *, miss: dict[str, Any] | None = None) -> None:
        if not node_id:
            return
        ranks = {"recomputed": 1, "sealed": 2, "unsealable": 3, "miss": 4}
        rank = ranks.get(bucket, 0)
        prev = self._rank.get(node_id, 0)
        if rank < prev:
            return
        for seq in (self.sealed, self.recomputed, self.unsealable):
            if node_id in seq:
                seq.remove(node_id)
        self.misses = [row for row in self.misses if row.get("node_id") != node_id]
        self._rank[node_id] = rank
        if bucket == "sealed":
            self.sealed.append(node_id)
        elif bucket == "recomputed":
            self.recomputed.append(node_id)
        elif bucket == "unsealable":
            self.unsealable.append(node_id)
        elif bucket == "miss" and miss is not None:
            self.misses.append(miss)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sealed": list(self.sealed),
            "recomputed": list(self.recomputed),
            "unsealable": list(self.unsealable),
            "misses": list(self.misses),
            "positional_fallback": self.positional_fallback,
        }


def entry_storage_key(kind: str, digest: str, occurrence: int) -> str:
    return f"{kind}:{digest}#{int(occurrence)}"


class Cassette:
    """Keyed tape of LLM and tool results. Legacy list tapes load as positional fallback."""

    def __init__(
        self,
        *,
        run_id: str = "",
        workflow: str = "",
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_bytes: int = DEFAULT_MAX_CASSETTE_BYTES,
    ) -> None:
        self.cassette_version = CASSETTE_VERSION
        self.readyagents_version = __version__
        self.run_id = run_id
        self.workflow = workflow
        self.recorded_at = utc_now()
        self.redacted = False
        self.positional_fallback = False
        self.entries: dict[str, dict[str, Any]] = {}
        self.max_entry_bytes = int(max_entry_bytes)
        self.max_bytes = int(max_bytes)
        self._legacy: list[dict[str, Any]] = []
        self._legacy_index = 0
        self._consume: dict[str, int] = {}
        self._record: dict[str, int] = {}
        self.blocked_nodes: set[str] = set()
        self.report = DeterminismReport()
        self.tool_seals: dict[str, str] = {}
        self.pending_route: dict[str, Any] | None = None
        self.media_blobs: dict[str, bytes] = {}

    @classmethod
    def new(cls, *, run_id: str, workflow: str, **kwargs: Any) -> Cassette:
        return cls(run_id=run_id, workflow=workflow, **kwargs)

    def llm_digest(
        self,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        return completion_key(model, messages, tools)

    def tool_digest(self, name: str, arguments: Mapping[str, Any] | None) -> str:
        return tool_call_key(name, arguments)

    def record_llm(
        self,
        *,
        node_id: str,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        result: CompletionResult,
        blocked: bool = False,
    ) -> str:
        digest = self.llm_digest(model, messages, tools)
        occ = self._record.get(f"{_LLM}:{digest}", 0)
        self._record[f"{_LLM}:{digest}"] = occ + 1
        key = entry_storage_key(_LLM, digest, occ)
        if blocked:
            entry = {
                "kind": _LLM,
                "node_id": node_id,
                "occurrence": occ,
                "digest": digest,
                "redacted_blocked": True,
                "sealed": False,
            }
            self.blocked_nodes.add(node_id)
            self.report.note(node_id, "unsealable")
        else:
            media_hashes = _media_hashes(messages)
            entry = {
                "kind": _LLM,
                "node_id": node_id,
                "occurrence": occ,
                "digest": digest,
                "text": result.text,
                "model": result.model or model,
                "usage": dict(result.usage or {}),
                "cost_micros": result.usage.get("cost_micros") if result.usage else None,
                "tool_calls": tool_calls_to_json(result.tool_calls),
                "sealed": True,
            }
            if media_hashes:
                entry["media"] = media_hashes
            self.report.note(node_id, "sealed")
        if self.pending_route:
            entry["route"] = dict(self.pending_route)
            self.pending_route = None
        self._put(key, entry)
        return key

    def recorded_route(self, node_id: str) -> dict[str, Any] | None:
        """Return the first sealed LLM route recorded for ``node_id``."""
        if not node_id:
            return None
        for entry in self.entries.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != _LLM:
                continue
            if str(entry.get("node_id") or "") != node_id:
                continue
            if entry.get("redacted_blocked"):
                continue
            route = entry.get("route")
            if isinstance(route, dict) and str(route.get("model") or "").strip():
                return dict(route)
        return None

    def record_tool(
        self,
        *,
        node_id: str,
        name: str,
        arguments: Mapping[str, Any] | None,
        result: Any,
        blocked: bool = False,
    ) -> str:
        digest = self.tool_digest(name, arguments)
        occ = self._record.get(f"{_TOOL}:{digest}", 0)
        self._record[f"{_TOOL}:{digest}"] = occ + 1
        key = entry_storage_key(_TOOL, digest, occ)
        klass = classify_tool(name, seals=self.tool_seals)
        if blocked:
            entry = {
                "kind": _TOOL,
                "node_id": node_id,
                "occurrence": occ,
                "digest": digest,
                "name": name,
                "redacted_blocked": True,
                "sealed": False,
            }
            self.blocked_nodes.add(node_id)
            self.report.note(node_id, "unsealable")
        else:
            entry = {
                "kind": _TOOL,
                "node_id": node_id,
                "occurrence": occ,
                "digest": digest,
                "name": name,
                "arguments": dict(arguments or {}),
                "result": result,
                "sealed": klass == "sealable",
            }
            if klass == "sealable":
                self.report.note(node_id, "sealed")
            elif klass == "recomputed":
                self.report.note(node_id, "recomputed")
            else:
                self.report.note(node_id, "unsealable")
        self._put(key, entry)
        return key

    def record_code(
        self,
        *,
        node_id: str,
        source: str,
        inputs: Mapping[str, Any] | None,
        stdout: str,
        stderr: str,
        exit_status: int,
        tier: str,
        output: Any,
    ) -> str:
        digest = self.tool_digest("code", {"node": node_id, "source": source, "inputs": inputs})
        occ = self._record.get(f"{_CODE}:{digest}", 0)
        self._record[f"{_CODE}:{digest}"] = occ + 1
        key = entry_storage_key(_CODE, digest, occ)
        entry = {
            "kind": _CODE,
            "node_id": node_id,
            "occurrence": occ,
            "digest": digest,
            "source": source,
            "inputs": dict(inputs or {}),
            "stdout": stdout,
            "stderr": stderr,
            "exit": int(exit_status),
            "tier": tier,
            "output": output,
            "sealed": True,
        }
        self.report.note(node_id, "sealed")
        self._put(key, entry)
        return key

    def record_document(self, *, node_id: str, output: Any) -> str:
        digest = self.tool_digest("document", {"node": node_id})
        occ = self._record.get(f"{_DOCUMENT}:{digest}", 0)
        self._record[f"{_DOCUMENT}:{digest}"] = occ + 1
        key = entry_storage_key(_DOCUMENT, digest, occ)
        entry = {
            "kind": _DOCUMENT,
            "node_id": node_id,
            "occurrence": occ,
            "digest": digest,
            "output": output,
            "sealed": True,
            "media": _collect_hashes(output),
        }
        self.report.note(node_id, "sealed")
        self._put(key, entry)
        return key

    def replay_document(self, *, node_id: str) -> Any:
        digest = self.tool_digest("document", {"node": node_id})
        occ = self._consume.get(f"{_DOCUMENT}:{digest}", 0)
        key = entry_storage_key(_DOCUMENT, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_DOCUMENT}:{digest}")
        if entry is None:
            raise CassetteMiss(
                f"Cassette miss at node '{node_id}': document entry missing",
                node_id=node_id,
                reason="missing",
            )
        self._consume[f"{_DOCUMENT}:{digest}"] = occ + 1
        self.report.note(node_id, "sealed")
        return entry.get("output")

    def record_table(self, *, node_id: str, output: Any) -> str:
        digest = self.tool_digest("table", {"node": node_id})
        occ = self._record.get(f"{_TABLE}:{digest}", 0)
        self._record[f"{_TABLE}:{digest}"] = occ + 1
        key = entry_storage_key(_TABLE, digest, occ)
        ref = output if isinstance(output, dict) else output
        entry = {
            "kind": _TABLE,
            "node_id": node_id,
            "occurrence": occ,
            "digest": digest,
            "output": ref,
            "sealed": True,
            "sha256": (ref or {}).get("sha256") if isinstance(ref, dict) else None,
        }
        self.report.note(node_id, "sealed")
        self._put(key, entry)
        return key

    def replay_table(self, *, node_id: str) -> Any:
        digest = self.tool_digest("table", {"node": node_id})
        occ = self._consume.get(f"{_TABLE}:{digest}", 0)
        key = entry_storage_key(_TABLE, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_TABLE}:{digest}")
        if entry is None:
            raise CassetteMiss(
                f"Cassette miss at node '{node_id}': table entry missing",
                node_id=node_id,
                reason="missing",
            )
        self._consume[f"{_TABLE}:{digest}"] = occ + 1
        self.report.note(node_id, "sealed")
        return entry.get("output")

    def record_transcribe(self, *, node_id: str, output: Any) -> str:
        digest = self.tool_digest("transcribe", {"node": node_id})
        occ = self._record.get(f"{_TRANSCRIBE}:{digest}", 0)
        self._record[f"{_TRANSCRIBE}:{digest}"] = occ + 1
        key = entry_storage_key(_TRANSCRIBE, digest, occ)
        entry = {
            "kind": _TRANSCRIBE,
            "node_id": node_id,
            "occurrence": occ,
            "digest": digest,
            "output": output,
            "sealed": True,
        }
        self.report.note(node_id, "sealed")
        self._put(key, entry)
        return key

    def replay_transcribe(self, *, node_id: str) -> Any:
        digest = self.tool_digest("transcribe", {"node": node_id})
        occ = self._consume.get(f"{_TRANSCRIBE}:{digest}", 0)
        key = entry_storage_key(_TRANSCRIBE, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_TRANSCRIBE}:{digest}")
        if entry is None:
            raise CassetteMiss(
                f"Cassette miss at node '{node_id}': transcribe entry missing",
                node_id=node_id,
                reason="missing",
            )
        self._consume[f"{_TRANSCRIBE}:{digest}"] = occ + 1
        self.report.note(node_id, "sealed")
        return entry.get("output")

    def replay_code(
        self,
        *,
        node_id: str,
        source: str,
        inputs: Mapping[str, Any] | None,
    ) -> Any:
        digest = self.tool_digest("code", {"node": node_id, "source": source, "inputs": inputs})
        occ = self._consume.get(f"{_CODE}:{digest}", 0)
        key = entry_storage_key(_CODE, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_CODE}:{digest}")
        if entry is None:
            nearest = self.nearest_key(_CODE, digest)
            miss = {
                "node_id": node_id,
                "reason": "missing",
                "nearest_key": nearest,
                "digest": digest,
            }
            self.report.note(node_id, "miss", miss=miss)
            raise CassetteMiss(
                f"Cassette miss at code node '{node_id}' "
                f"(digest {digest[:12]}…, nearest {nearest or 'none'}). "
                "Offline replay never executes code.",
                node_id=node_id,
                reason="missing",
                nearest_key=nearest,
            )
        self._consume[f"{_CODE}:{digest}"] = occ + 1
        self.report.note(node_id, "sealed")
        return entry.get("output")

    def record_contract(
        self,
        *,
        node_id: str,
        output: Any,
        report: Mapping[str, Any] | None,
    ) -> str:
        digest = self.tool_digest("contract", {"node": node_id})
        occ = self._record.get(f"{_CONTRACT}:{digest}", 0)
        self._record[f"{_CONTRACT}:{digest}"] = occ + 1
        key = entry_storage_key(_CONTRACT, digest, occ)
        entry = {
            "kind": _CONTRACT,
            "node_id": node_id,
            "occurrence": occ,
            "digest": digest,
            "output": output,
            "report": dict(report or {}),
            "sealed": True,
        }
        self.report.note(node_id, "sealed")
        self._put(key, entry)
        return key

    def replay_contract(self, *, node_id: str) -> Any:
        digest = self.tool_digest("contract", {"node": node_id})
        occ = self._consume.get(f"{_CONTRACT}:{digest}", 0)
        key = entry_storage_key(_CONTRACT, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_CONTRACT}:{digest}")
        if entry is None:
            nearest = self.nearest_key(_CONTRACT, digest)
            miss = {
                "node_id": node_id,
                "reason": "missing",
                "nearest_key": nearest,
                "digest": digest,
            }
            self.report.note(node_id, "miss", miss=miss)
            raise CassetteMiss(
                f"Cassette miss at contract node '{node_id}' "
                f"(digest {digest[:12]}…, nearest {nearest or 'none'}). "
                "Offline replay never re-calls the model.",
                node_id=node_id,
                reason="missing",
                nearest_key=nearest,
            )
        self._consume[f"{_CONTRACT}:{digest}"] = occ + 1
        self.report.note(node_id, "sealed")
        return entry.get("output")

    def replay_llm(
        self,
        *,
        node_id: str,
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
    ) -> CompletionResult:
        digest = self.llm_digest(model, messages, tools)
        occ = self._consume.get(f"{_LLM}:{digest}", 0)
        key = entry_storage_key(_LLM, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_LLM}:{digest}")
        if entry is None and self.positional_fallback and self._legacy_index < len(self._legacy):
            row = self._legacy[self._legacy_index]
            self._legacy_index += 1
            self.report.positional_fallback = True
            self.report.note(node_id, "sealed")
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            return CompletionResult(
                text=str(row.get("text") or ""),
                model=str(row.get("model") or model),
                usage=dict(usage),
                tool_calls=tool_calls_from_json(row.get("tool_calls")),
            )
        if entry is None or entry.get("redacted_blocked"):
            nearest = self.nearest_key(_LLM, digest)
            reason = "redacted_blocked" if entry and entry.get("redacted_blocked") else "missing"
            miss = {
                "node_id": node_id,
                "reason": reason,
                "nearest_key": nearest,
                "digest": digest,
            }
            self.report.note(node_id, "miss", miss=miss)
            raise CassetteMiss(
                f"Cassette miss at node '{node_id}': {reason} "
                f"(digest {digest[:12]}…, nearest {nearest or 'none'}). "
                "Offline replay never falls through to a live call.",
                node_id=node_id,
                reason=reason,
                nearest_key=nearest,
            )
        self._consume[f"{_LLM}:{digest}"] = occ + 1
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        self.report.note(node_id, "sealed")
        return CompletionResult(
            text=str(entry.get("text") or ""),
            model=str(entry.get("model") or model),
            usage=dict(usage),
            tool_calls=tool_calls_from_json(entry.get("tool_calls")),
        )

    def replay_tool(
        self,
        *,
        node_id: str,
        name: str,
        arguments: Mapping[str, Any] | None,
    ) -> Any:
        digest = self.tool_digest(name, arguments)
        occ = self._consume.get(f"{_TOOL}:{digest}", 0)
        key = entry_storage_key(_TOOL, digest, occ)
        entry = self.entries.get(key)
        if entry is None and occ == 0:
            entry = self.entries.get(f"{_TOOL}:{digest}")
        if entry is None or entry.get("redacted_blocked"):
            nearest = self.nearest_key(_TOOL, digest)
            reason = "redacted_blocked" if entry and entry.get("redacted_blocked") else "missing"
            miss = {
                "node_id": node_id,
                "reason": reason,
                "nearest_key": nearest,
                "digest": digest,
                "tool": name,
            }
            self.report.note(node_id, "miss", miss=miss)
            raise CassetteMiss(
                f"Cassette miss at node '{node_id}' tool '{name}': {reason} "
                f"(nearest {nearest or 'none'}). "
                "Offline replay never falls through to a live call.",
                node_id=node_id,
                reason=reason,
                nearest_key=nearest,
            )
        self._consume[f"{_TOOL}:{digest}"] = occ + 1
        klass = classify_tool(name, seals=self.tool_seals)
        if klass == "recomputed":
            self.report.note(node_id, "recomputed")
        elif klass == "sealable":
            self.report.note(node_id, "sealed")
        else:
            self.report.note(node_id, "unsealable")
        return entry.get("result")

    def nearest_key(self, kind: str, digest: str) -> str | None:
        prefix = f"{kind}:{digest}"
        for key in self.entries:
            if key.startswith(prefix):
                return key
        for key in self.entries:
            if key.startswith(f"{kind}:"):
                return key
        return None

    def to_document(self) -> dict[str, Any]:
        return {
            "cassette_version": self.cassette_version,
            "readyagents_version": self.readyagents_version,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "recorded_at": self.recorded_at,
            "redacted": self.redacted,
            "positional_fallback": self.positional_fallback,
            "entries": dict(self.entries),
            "determinism": self.report.as_dict(),
            "blocked_nodes": sorted(self.blocked_nodes),
        }

    def save(self, path: Path, *, root: Path | None = None) -> Path:
        dest = Path(path)
        if root is not None:
            dest = resolve_within(dest if dest.is_absolute() else dest.name, root, what="cassette")
        else:
            dest = dest.expanduser()
        blob = _dump(self.to_document())
        if len(blob.encode("utf-8")) > self.max_bytes:
            raise CassetteError(
                f"Cassette exceeds the {self.max_bytes} byte cap. "
                "Cassettes are not a log archive; raise "
                "READYAGENTS_CASSETTE_MAX_BYTES or split runs."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(dest, blob, encoding="utf-8", newline="\n", restrict=True)
        if self.media_blobs:
            from readyagents.media.store import MediaStore

            sidecar = dest.parent / f"{dest.stem}.media"
            store = MediaStore(sidecar)
            for digest, data in self.media_blobs.items():
                store.put(data, sha256=digest)
        return dest

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_bytes: int = DEFAULT_MAX_CASSETTE_BYTES,
        root: Path | None = None,
    ) -> Cassette:
        file = Path(path)
        if root is not None:
            file = resolve_within(file if file.is_absolute() else file, root, what="cassette")
        if not file.is_file():
            raise CassetteError(f"Cassette not found: {file}. Record one with --record.")
        raw = file.read_bytes()
        if len(raw) > max_bytes:
            raise CassetteError(f"Cassette {file} exceeds the {max_bytes} byte cap")
        try:
            loaded = _parse(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CassetteError(f"Cassette {file} is not valid JSON: {exc}") from exc
        tape = cls(max_entry_bytes=max_entry_bytes, max_bytes=max_bytes)
        if isinstance(loaded, list):
            tape.positional_fallback = True
            tape.report.positional_fallback = True
            tape._legacy = [row for row in loaded if isinstance(row, dict)]
            tape.workflow = ""
            return tape
        if not isinstance(loaded, dict):
            raise CassetteError(f"Cassette {file} must be an object or a legacy list")
        version = loaded.get("cassette_version", CASSETTE_VERSION)
        try:
            version_i = int(version)
        except (TypeError, ValueError) as exc:
            raise CassetteError(f"Cassette {file} has an invalid cassette_version") from exc
        if version_i != CASSETTE_VERSION:
            raise CassetteError(
                f"Cassette {file} cassette_version {version_i} is not supported "
                f"(expected {CASSETTE_VERSION})"
            )
        tape.cassette_version = version_i
        tape.readyagents_version = str(loaded.get("readyagents_version") or "")
        tape.run_id = str(loaded.get("run_id") or "")
        tape.workflow = str(loaded.get("workflow") or "")
        tape.recorded_at = str(loaded.get("recorded_at") or "")
        tape.redacted = bool(loaded.get("redacted"))
        tape.positional_fallback = bool(loaded.get("positional_fallback"))
        entries = loaded.get("entries")
        if entries is None:
            entries = {}
        if not isinstance(entries, dict):
            raise CassetteError(f"Cassette {file} field 'entries' must be a mapping")
        for key, row in entries.items():
            if not isinstance(key, str) or not isinstance(row, dict):
                raise CassetteError(f"Cassette {file} has an invalid entry")
            kind = row.get("kind")
            if kind not in {
                _LLM,
                _TOOL,
                _CODE,
                _CONTRACT,
                _DOCUMENT,
                _TRANSCRIBE,
                _TABLE,
                _MEDIA,
            }:
                raise CassetteError(f"Cassette {file} entry {key!r} has invalid kind")
            tape.entries[key] = dict(row)
        det = loaded.get("determinism")
        if isinstance(det, dict):
            tape.report.sealed = [str(x) for x in det.get("sealed") or []]
            tape.report.recomputed = [str(x) for x in det.get("recomputed") or []]
            tape.report.unsealable = [str(x) for x in det.get("unsealable") or []]
            tape.report.misses = [dict(x) for x in det.get("misses") or [] if isinstance(x, dict)]
            tape.report.positional_fallback = bool(det.get("positional_fallback"))
        blocked = loaded.get("blocked_nodes")
        if isinstance(blocked, list):
            tape.blocked_nodes = {str(x) for x in blocked}
        sidecar = file.parent / f"{file.stem}.media"
        if sidecar.is_dir():
            for blob in sidecar.rglob("*"):
                if blob.is_file() and len(blob.name) == 64:
                    tape.media_blobs[blob.name] = blob.read_bytes()
        return tape

    def _put(self, key: str, entry: dict[str, Any]) -> None:
        blob = _dump(entry)
        size = len(blob.encode("utf-8"))
        if size > self.max_entry_bytes:
            raise CassetteError(
                f"Cassette entry {key} is {size} bytes; cap is {self.max_entry_bytes}"
            )
        self.entries[key] = entry
        total = sum(len(_dump(v).encode("utf-8")) for v in self.entries.values())
        if total > self.max_bytes:
            self.entries.pop(key, None)
            raise CassetteError(f"Cassette exceeds the {self.max_bytes} byte cap after entry {key}")


def _dump(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"


def _parse(text: str) -> Any:
    import json

    return json.loads(text)


def _media_hashes(messages: list[Message]) -> list[str]:
    hashes: list[str] = []
    for message in messages:
        for item in getattr(message, "media", None) or []:
            if isinstance(item, dict) and item.get("sha256"):
                hashes.append(str(item["sha256"]))
    return hashes


def _collect_hashes(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("sha256") and value.get("_media") is True:
            found.append(str(value["sha256"]))
        for item in value.values():
            found.extend(_collect_hashes(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_collect_hashes(item))
    return found
