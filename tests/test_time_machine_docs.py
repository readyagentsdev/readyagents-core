"""The time-machine docs fence is the freeze contract — execute it, do not describe it."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.config import clear_settings_cache

runner = CliRunner()
_DOCS = Path(__file__).resolve().parents[1] / "docs" / "time-machine.md"


def _bash_fence(markdown: str) -> list[str]:
    match = re.search(r"```bash\n(.*?)```", markdown, flags=re.S)
    assert match, "docs/time-machine.md has no bash fence"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def _argv(line: str) -> list[str]:
    assert line.startswith("readyagents ")
    return line.split()[1:]


def _json_from_cli(text: str) -> dict:
    for i, ch in enumerate(text):
        if ch in "{[":
            data = json.loads(text[i:])
            assert isinstance(data, dict)
            return data
    raise AssertionError(f"no JSON in:\n{text}")


def test_time_machine_docs_fence_runs_keyless(tmp_path: Path, monkeypatch) -> None:
    lines = _bash_fence(_DOCS.read_text(encoding="utf-8"))
    assert lines[0].startswith("readyagents run ")
    assert "--record" in lines[0] and "--json" in lines[0]
    freeze = next(line for line in lines if " freeze " in f" {line} ")
    freeze_argv = _argv(freeze.replace("RUN_ID", "placeholder"))
    assert "--allow-unsealed" not in freeze_argv
    assert not any(part.startswith("/tmp") for part in freeze_argv)
    out = freeze_argv[freeze_argv.index("--out") + 1]
    assert not Path(out).is_absolute()
    assert "/" not in out.replace("\\", "/") or not out.startswith("/")

    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "examples").mkdir()
    shutil.copy(
        repo / "examples" / "calc_pipeline.yaml", tmp_path / "examples" / "calc_pipeline.yaml"
    )

    clear_settings_cache()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READYAGENTS_HOME", str(tmp_path / ".readyagents"))
    monkeypatch.setenv("READYAGENTS_WORKSPACE", str(tmp_path))
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "READYAGENTS_OPENAI_API_KEY",
        "READYAGENTS_ANTHROPIC_API_KEY",
        "READYAGENTS_OPENAI_COMPAT_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    ran = runner.invoke(app, _argv(lines[0]))
    assert ran.exit_code == 0, ran.stdout + ran.stderr
    payload = _json_from_cli(ran.stdout)
    run_id = str(payload["run_id"])
    assert payload["status"] == "succeeded"

    replay_line = next(line for line in lines if " replay " in f" {line} ")
    replayed = runner.invoke(app, _argv(replay_line.replace("RUN_ID", run_id)))
    assert replayed.exit_code == 0, replayed.stdout + replayed.stderr
    replay_json = _json_from_cli(replayed.stdout)
    report = (
        replay_json.get("determinism") or replay_json.get("metadata", {}).get("determinism") or {}
    )
    assert "pick" in (report.get("recomputed") or [])
    assert "stamp" in (report.get("sealed") or [])
    assert not report.get("unsealable")

    frozen = runner.invoke(app, _argv(freeze.replace("RUN_ID", run_id)))
    assert frozen.exit_code == 0, frozen.stdout + frozen.stderr

    eval_line = next(line for line in lines if line.startswith("readyagents eval "))
    scored = runner.invoke(app, _argv(eval_line))
    assert scored.exit_code == 0, scored.stdout + scored.stderr
    clear_settings_cache()
