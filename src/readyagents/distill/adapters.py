"""Signed, versioned adapter registry. Unsigned adapters refuse to load."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from readyagents.config import Settings, get_settings
from readyagents.distill.schema import AdapterRecord
from readyagents.distill.store import list_adapters, load_adapter, save_adapter
from readyagents.errors import DistillRefused, DistillUnsigned
from readyagents.trust.digest import KIND_ADAPTER, digest_bytes
from readyagents.trust.sign import sign_artifact, verify_artifact


def register_adapter(
    record: AdapterRecord,
    *,
    settings: Settings | None = None,
    sign_key: Path | str | None = None,
    artifact: Path | str | None = None,
) -> AdapterRecord:
    settings = settings or get_settings()
    dest = Path(artifact or record.path)
    if dest.is_file():
        record.digest = digest_bytes(dest.read_bytes())
        record.path = str(dest)
    if sign_key:
        sign_artifact(dest, key=sign_key, kind=KIND_ADAPTER)
        record.signed = True
    save_adapter(record, settings)
    return record


def require_signed(
    adapter_id: str,
    *,
    settings: Settings | None = None,
    keyring: Any = None,
) -> AdapterRecord:
    settings = settings or get_settings()
    record = load_adapter(adapter_id, settings)
    artifact = Path(record.path)
    if not artifact.is_file():
        raise DistillRefused(f"adapter artifact missing: {artifact}", reason="missing")
    sig = artifact.parent / f"{artifact.name}.sig"
    if not sig.is_file() or not record.signed:
        raise DistillUnsigned(f"unsigned adapter {adapter_id}")
    try:
        result = verify_artifact(artifact, kind=KIND_ADAPTER, keyring=keyring)
    except Exception as extra:
        raise DistillUnsigned(f"adapter {adapter_id} failed signature verify") from extra
    if not result.get("ok"):
        raise DistillUnsigned(f"adapter {adapter_id} failed signature verify")
    return record


def show_adapter(adapter_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    return load_adapter(adapter_id, settings).model_dump(mode="python", by_alias=True)


def remove_adapter(adapter_id: str, *, settings: Settings | None = None) -> None:
    from readyagents.distill.store import adapter_folder, load_promoted, save_promoted

    settings = settings or get_settings()
    record = load_adapter(adapter_id, settings)
    folder = adapter_folder(adapter_id, settings)
    for child in folder.glob("*"):
        child.unlink()
    folder.rmdir()
    pins = load_promoted(settings)
    changed = False
    for node, row in list(pins.items()):
        if isinstance(row, dict) and row.get("adapter_id") == record.id:
            pins.pop(node, None)
            changed = True
    if changed:
        save_promoted(pins, settings)


def catalog(settings: Settings | None = None) -> list[dict[str, Any]]:
    return [row.model_dump(mode="python", by_alias=True) for row in list_adapters(settings)]
