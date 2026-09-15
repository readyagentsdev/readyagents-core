"""H-01: no Settings field may resolve from a bare non-READYAGENTS_ env var.

Regression: ``Settings.home`` used to match ambient ``$HOME`` (field-name
matching with ``case_sensitive=False``), so the run store landed in
``$HOME/runs`` instead of ``./.readyagents/runs``. Same class for
``workspace`` vs ``$WORKSPACE`` on CI runners.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import AliasChoices

from readyagents.config import Settings, clear_settings_cache, load_settings


def _declared_aliases_upper(field_name: str) -> set[str]:
    field = Settings.model_fields[field_name]
    alias = field.validation_alias
    names: set[str] = set()
    if isinstance(alias, AliasChoices):
        names.update(str(choice).upper() for choice in alias.choices)
    elif isinstance(alias, str):
        names.add(alias.upper())
    return names


def _bare_probe_fields() -> list[str]:
    """Fields whose UPPER(field) spelling is NOT a declared alias.

    Those must ignore a bare env var of that name. Fields where the bare
    spelling is deliberate (OPENAI_API_KEY, DEFAULT_MODEL, AWS_REGION, ...)
    are excluded — legacy aliases keep working by design.
    """
    probed: list[str] = []
    for name in Settings.model_fields:
        if name.upper() not in _declared_aliases_upper(name):
            probed.append(name)
    return sorted(probed)


def _sentinel_for(field_name: str, baseline: Any) -> str:
    annotation = Settings.model_fields[field_name].annotation
    origin = str(annotation)
    if "bool" in origin:
        return "0" if baseline is True else "1"
    if "int" in origin and "float" not in origin:
        return "2049" if baseline == 2048 else "2048"
    if "float" in origin:
        return "124.5" if baseline == 123.5 else "123.5"
    if "Path" in origin:
        candidate = "/tmp/env-isolation-sentinel"
        return candidate if str(baseline) != candidate else "/tmp/env-isolation-other"
    candidate = "env-isolation-sentinel"
    return candidate if baseline != candidate else "env-isolation-other"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    for var in [v for v in os.environ if v.upper().startswith("READYAGENTS_")]:
        monkeypatch.delenv(var, raising=False)
    yield
    clear_settings_cache()


def test_home_ignores_bare_HOME(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READYAGENTS_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/not-the-store")
    settings = load_settings(env_file=())
    assert settings.home == Path(".readyagents")
    expected = (Path.cwd() / ".readyagents").resolve() / "runs"
    assert settings.runs_dir() == expected


def test_readyagents_home_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/not-the-store")
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / "custom"))
    settings = load_settings(env_file=())
    assert settings.runs_dir() == (tmp_path / "custom").resolve() / "runs"


def test_workspace_ignores_bare_WORKSPACE(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("READYAGENTS_WORKSPACE", raising=False)
    monkeypatch.setenv("WORKSPACE", "/tmp/jenkins")
    settings = load_settings(env_file=())
    assert settings.workspace is None
    assert settings.workspace_path() == Path.cwd().resolve()


def test_workspace_explicit_prefix_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKSPACE", "/tmp/jenkins")
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path / "ws"))
    settings = load_settings(env_file=())
    assert settings.workspace_path() == (tmp_path / "ws").resolve()


@pytest.mark.parametrize("field_name", _bare_probe_fields())
def test_bare_env_name_never_configures_field(
    field_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    var = field_name.upper()
    monkeypatch.delenv(var, raising=False)
    baseline = getattr(load_settings(env_file=()), field_name)
    monkeypatch.setenv(var, _sentinel_for(field_name, baseline))
    probed = getattr(load_settings(env_file=()), field_name)
    assert probed == baseline, f"${var} must not configure Settings.{field_name}"


def test_legacy_non_prefixed_aliases_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-legacy")
    monkeypatch.setenv("DEFAULT_MODEL", "openai:gpt-4o")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    settings = load_settings(env_file=())
    assert settings.openai_api_key == "sk-legacy"
    assert settings.default_model == "openai:gpt-4o"
    assert settings.aws_region == "eu-west-1"
