"""Shipped Agent Skills path: install, type: skill, export, agents-md."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from readyagents.cli import app
from readyagents.errors import SkillDrift, SkillPathDenied, SkillRefused, TrustError
from readyagents.firewall.policy_file import load_policy
from readyagents.firewall.taint import provenance_of
from readyagents.skills.agents_md import generate_agents_md
from readyagents.skills.catalog import disclose_all, get_record
from readyagents.skills.export import export_workflow
from readyagents.skills.install import add_skill
from readyagents.skills.parse import parse_skill_md
from readyagents.tools import ToolRegistry
from readyagents.workflow.engine import run_workflow
from readyagents.workflow.nodes import ExecutionContext
from readyagents.workflow.schema import WorkflowSpec

runner = CliRunner()


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _skill_md(**extra) -> str:
    name = extra.pop("name", "house-writing-style")
    desc = extra.pop(
        "description",
        "Apply house style to a draft. Use when rewriting ReadyAgents prose.",
    )
    tools = extra.pop("allowed-tools", None)
    lines = ["---", f"name: {name}", f"description: {desc}"]
    if tools is not None:
        lines.append(f"allowed-tools: {tools}")
    lines.extend(["---", "", "Do not call tools. Rewrite in short sentences."])
    return "\n".join(lines)


def _write_skill(tmp: Path, *, name: str = "house-writing-style", script: bool = False) -> Path:
    folder = tmp / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(_skill_md(name=name), encoding="utf-8")
    if script:
        (folder / "scripts").mkdir()
        (folder / "scripts" / "apply.py").write_text(
            'result = {"styled": (inputs.get("draft") or "") + " [styled]"}\n',
            encoding="utf-8",
        )
    return folder


def test_frontmatter_constraints_and_directory_match(tmp_path: Path) -> None:
    with pytest.raises(SkillRefused, match="frontmatter"):
        parse_skill_md("no frontmatter")
    with pytest.raises(SkillRefused, match="name"):
        parse_skill_md(_skill_md(name="BAD_NAME"))
    with pytest.raises(SkillRefused, match="match directory"):
        parse_skill_md(_skill_md(name="house-writing-style"), directory_name="other")
    huge = "x" * 2000
    with pytest.raises(SkillRefused, match="description"):
        parse_skill_md(_skill_md(description=huge))
    rec = parse_skill_md(_skill_md(), directory_name="house-writing-style")
    assert rec.name == "house-writing-style"
    assert rec.disclose() == {"name": rec.name, "description": rec.description}


def test_install_indexes_and_progressive_disclosure(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src", script=True)
    home = tmp_settings.home_path()
    row = add_skill(src, home=home)
    assert row["name"] == "house-writing-style"
    assert row["digest"].startswith("sha256:")
    assert "scripts/apply.py" in row["scripts"]
    disclosed = disclose_all(home)
    assert disclosed == [{"name": row["name"], "description": row["description"]}]
    assert "Do not call" not in json.dumps(disclosed)
    listed = get_record(home, "house-writing-style")
    assert listed["signature_status"] == "unsigned"
    assert "instructions" not in listed


def test_skill_node_injects_untrusted_body_and_runs_sandbox_script(
    tmp_path: Path, tmp_settings
) -> None:
    src = _write_skill(tmp_path / "src", script=True)
    home = tmp_settings.home_path()
    add_skill(src, home=home)
    spec = {
        "name": "use-skill",
        "nodes": [
            {
                "id": "apply",
                "type": "skill",
                "skill": "house-writing-style",
                "inputs": {"draft": "{{ draft }}"},
                "output_key": "styled",
            }
        ],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(wf, ToolRegistry(), pin_home=home, default_model="mock:test")
    done = run_workflow(wf, {"draft": "hello"}, ctx)
    assert done.status == "succeeded"
    out = done.output_keys["styled"]
    assert "Do not call" in out["instructions"]
    assert out["disclosed"]["name"] == "house-writing-style"
    assert "Do not call" not in json.dumps(out["disclosed"])
    assert out["script_output"]["styled"] == "hello [styled]"
    assert provenance_of(done, "styled").trust == "untrusted"
    assert provenance_of(done, "apply").trust == "untrusted"


def test_allowed_tools_filtered_by_policy(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src")
    (src / "SKILL.md").write_text(_skill_md(**{"allowed-tools": "calc now"}), encoding="utf-8")
    home = tmp_settings.home_path()
    add_skill(src, home=home)
    policy_path = tmp_path / "p.yaml"
    policy_path.write_text("version: 1\ndefault: deny\ntools:\n  now: {}\n", encoding="utf-8")
    spec = {
        "name": "tools",
        "nodes": [
            {"id": "apply", "type": "skill", "skill": "house-writing-style", "output_key": "s"}
        ],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(
        wf,
        ToolRegistry(),
        pin_home=home,
        policy=load_policy(policy_path),
        default_model="mock:test",
    )
    done = run_workflow(wf, {}, ctx)
    tools = done.output_keys["s"]["tools"]
    assert "now" in tools
    assert "calc" not in tools


def test_install_refuses_zip_slip_symlink_and_oversize(tmp_path: Path, tmp_settings) -> None:
    home = tmp_settings.home_path()
    zpath = tmp_path / "slip.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../escape/SKILL.md", _skill_md())
    with pytest.raises(SkillPathDenied):
        add_skill(zpath, home=home)
    src = _write_skill(tmp_path / "ok")
    link = tmp_path / "link-skill"
    try:
        link.symlink_to(src)
        linked = True
    except OSError:
        linked = False
    if linked:
        with pytest.raises(SkillPathDenied):
            add_skill(link, home=home)
    big = tmp_path / "big.zip"
    payload = b"x" * (1_048_576 + 10)
    with zipfile.ZipFile(big, "w") as zf:
        zf.writestr("house-writing-style/SKILL.md", _skill_md())
        zf.writestr("house-writing-style/blob.bin", payload)
    # size cap is on archive file size
    if big.stat().st_size <= 1_048_576:
        big.write_bytes(big.read_bytes() + payload)
    with pytest.raises(SkillRefused, match="size"):
        add_skill(big, home=home)


def test_unsigned_required_and_drift_detected(tmp_path: Path, tmp_settings) -> None:
    src = _write_skill(tmp_path / "src")
    home = tmp_settings.home_path()
    with pytest.raises(SkillRefused, match="unsigned"):
        add_skill(src, home=home, require_signature=True)
    add_skill(src, home=home)
    installed = Path(get_record(home, "house-writing-style")["path"])
    (installed / "SKILL.md").write_text(
        _skill_md(description="changed description after install for drift."),
        encoding="utf-8",
    )
    spec = {
        "name": "drift",
        "nodes": [{"id": "apply", "type": "skill", "skill": "house-writing-style"}],
    }
    wf = WorkflowSpec.model_validate(spec)
    ctx = ExecutionContext(wf, ToolRegistry(), pin_home=home, default_model="mock:test")
    with pytest.raises(SkillDrift):
        run_workflow(wf, {}, ctx)


def test_export_valid_skill_and_runs_workflow(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    folder = export_workflow(_root() / "examples" / "calc_pipeline.yaml", dest)
    assert folder.name == "calc-pipeline"
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    rec = parse_skill_md(text, directory_name=folder.name)
    assert rec.name == folder.name
    run_sh = (folder / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert "readyagents run" in run_sh
    assert "$DIR/workflow.yaml" in run_sh
    blob = ""
    for path in folder.rglob("*"):
        if path.is_file():
            blob += path.read_text(encoding="utf-8", errors="replace")
    assert "sk-" not in blob
    assert "/Users/" not in blob
    from readyagents.workflow.runner import run_workflow_file

    done = run_workflow_file(folder / "workflow.yaml", persist=False)
    assert done.status == "succeeded"


def test_agents_md_names_run_validate_test() -> None:
    text = generate_agents_md()
    assert "readyagents run" in text
    assert "readyagents validate" in text
    assert "pytest" in text


def test_cli_skills_help_twice_and_agents_md() -> None:
    first = runner.invoke(app, ["skills", "--help"])
    second = runner.invoke(app, ["skills", "--help"])
    assert first.exit_code == 0, first.stdout + first.stderr
    assert second.exit_code == 0
    a = runner.invoke(app, ["agents-md", "--json"])
    b = runner.invoke(app, ["agents-md", "--json"])
    assert a.exit_code == 0, a.stdout + a.stderr
    assert b.exit_code == 0
    payload = json.loads(a.stdout[a.stdout.find("{") :])
    assert "readyagents run" in payload["text"]


def test_skill_node_required_at_schema() -> None:
    with pytest.raises(ValidationError, match="skill"):
        WorkflowSpec.model_validate({"name": "bad", "nodes": [{"id": "s", "type": "skill"}]})


def test_skill_digest_excludes_sig(tmp_path: Path) -> None:
    from readyagents.skills.digest import digest_skill_dir

    src = _write_skill(tmp_path)
    before = digest_skill_dir(src)
    (src / "SKILL.md.sig").write_text("not-a-real-signature\n", encoding="utf-8")
    assert digest_skill_dir(src) == before


def _ed25519_pem(tmp: Path, name: str = "key") -> tuple[Path, Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    priv_path = tmp / f"{name}.pem"
    pub_path = tmp / f"{name}.pub.pem"
    priv_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


def test_ed25519_skill_sign_verify_and_tamper(tmp_path: Path, tmp_settings) -> None:
    pytest.importorskip("cryptography")
    from readyagents.skills.digest import digest_skill_dir
    from readyagents.trust.keyring import add_key, load_keyring
    from readyagents.trust.sign import sign_artifact, signature_path, verify_artifact

    priv, pub = _ed25519_pem(tmp_path)
    home = tmp_settings.home_path()
    add_key(pub, name="ops", home=home)
    src = _write_skill(tmp_path / "src", name="signed-skill")
    payload = sign_artifact(src / "SKILL.md", key=priv)
    assert payload["kind"] == "skill"
    assert payload["algorithm"] == "ed25519"
    assert digest_skill_dir(src) == payload["digest"]
    verified = verify_artifact(src / "SKILL.md", keyring=load_keyring(home=home))
    assert verified["ok"] is True
    assert verified["kind"] == "skill"
    assert verified["digest"] == payload["digest"]
    row = add_skill(src, home=home, require_signature=True)
    assert row["signature_status"] == "signed"
    assert row["digest"] == payload["digest"]

    tampered = _write_skill(tmp_path / "tamper", name="tampered-skill")
    sign_artifact(tampered / "SKILL.md", key=priv)
    body = (tampered / "SKILL.md").read_text(encoding="utf-8")
    (tampered / "SKILL.md").write_text(body.replace("Rewrite", "PWNED"), encoding="utf-8")
    with pytest.raises(SkillRefused, match="signature") as caught:
        add_skill(tampered, home=home)
    assert caught.value.reason == "forged"

    forged_sig = _write_skill(tmp_path / "forged", name="forged-sig-skill")
    sign_artifact(forged_sig / "SKILL.md", key=priv)
    sig = signature_path(forged_sig / "SKILL.md")
    data = json.loads(sig.read_text(encoding="utf-8"))
    data["signature"] = data["signature"][:-4] + "AAAA"
    sig.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SkillRefused, match="signature") as caught_sig:
        add_skill(forged_sig, home=home)
    assert caught_sig.value.reason == "forged"


def test_digest_only_skill_sig_is_forged(tmp_path: Path, tmp_settings) -> None:
    from readyagents.skills.digest import digest_skill_dir

    src = _write_skill(tmp_path / "src", name="digest-only-skill")
    digest = digest_skill_dir(src)
    (src / "SKILL.md.sig").write_text(digest + "\n", encoding="utf-8")
    with pytest.raises(SkillRefused, match="signature") as caught:
        add_skill(src, home=tmp_settings.home_path())
    assert caught.value.reason == "forged"


def _skill_flow(root: Path, name: str = "house-writing-style") -> Path:
    flow = root / "skill-flow.yaml"
    flow.write_text(
        "name: use-skill\nstart: apply\nnodes:\n"
        f"  - id: apply\n    type: skill\n    skill: {name}\n    output_key: styled\n",
        encoding="utf-8",
    )
    return flow


def test_lockfile_pins_skill_and_refuses_drift(tmp_path: Path, tmp_settings) -> None:
    from readyagents.skills.digest import digest_skill_dir
    from readyagents.trust.enforce import evaluate_workflow
    from readyagents.trust.lock import (
        build_lockfile,
        diff_lockfile,
        load_lockfile,
        write_lockfile,
    )
    from readyagents.workflow.runner import run_workflow_file

    src = _write_skill(tmp_path / "src")
    home = tmp_settings.home_path()
    add_skill(src, home=home)
    flow = _skill_flow(tmp_settings.workspace_path())
    with pytest.raises(TrustError, match="skill not installed"):
        build_lockfile(flow, skill_home=tmp_path / "empty-home")
    lock = build_lockfile(flow, skill_home=home)
    skill_art = next(item for item in lock.artifacts if item.kind == "skill")
    assert skill_art.name == "house-writing-style"
    assert skill_art.path == "skills/house-writing-style"
    installed = Path(get_record(home, "house-writing-style")["path"])
    assert skill_art.digest == digest_skill_dir(installed)
    dest = flow.parent / "readyagents.lock"
    write_lockfile(lock, dest)
    done = run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    assert done.status == "succeeded"

    (installed / "SKILL.md").write_text(
        _skill_md(description="changed description after lock pin for drift."),
        encoding="utf-8",
    )
    drifted = build_lockfile(flow, skill_home=home)
    mismatches = diff_lockfile(load_lockfile(dest), drifted)
    assert any(
        row["reason"] == "digest_mismatch" and row["artifact"] == "skill:skills/house-writing-style"
        for row in mismatches
    )
    with pytest.raises(TrustError, match="lockfile mismatch") as eval_caught:
        evaluate_workflow(
            flow,
            workspace=tmp_settings.workspace_path(),
            frozen=True,
            skill_home=home,
        )
    assert eval_caught.value.reason == "digest_mismatch"
    with pytest.raises(TrustError, match="lockfile mismatch") as caught:
        run_workflow_file(flow, settings=tmp_settings, persist=False, frozen=True)
    assert caught.value.reason == "digest_mismatch"


def test_cli_lock_pins_installed_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from readyagents.config import clear_settings_cache

    home = tmp_path / ".readyagents"
    monkeypatch.setenv("READYAGENTS_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    src = _write_skill(tmp_path / "src")
    add_skill(src, home=home)
    flow = _skill_flow(tmp_path)
    locked = runner.invoke(app, ["lock", str(flow), "--json"])
    assert locked.exit_code == 0, locked.stdout + locked.stderr
    payload = json.loads(locked.stdout[locked.stdout.find("{") :])
    kinds = {row["kind"] for row in payload["artifacts"]}
    assert "skill" in kinds
    skill_row = next(row for row in payload["artifacts"] if row["kind"] == "skill")
    assert skill_row["name"] == "house-writing-style"
    assert skill_row["path"] == "skills/house-writing-style"
    assert skill_row["digest"].startswith("sha256:")
    frozen = runner.invoke(app, ["run", str(flow), "--frozen", "--no-persist"])
    assert frozen.exit_code == 0, frozen.stdout + frozen.stderr
    installed = Path(get_record(home, "house-writing-style")["path"])
    (installed / "SKILL.md").write_text(
        _skill_md(description="cli lock pin drifted after the lock was written."),
        encoding="utf-8",
    )
    bad = runner.invoke(app, ["run", str(flow), "--frozen", "--no-persist"])
    assert bad.exit_code == 1
    assert "lockfile mismatch" in bad.stdout + bad.stderr
    clear_settings_cache()


def test_export_planted_secret_refused(tmp_path: Path) -> None:
    flow = tmp_path / "leaky.yaml"
    flow.write_text(
        "name: leaky\nstart: t\nnodes:\n"
        "  - id: t\n    type: transform\n"
        "    template: 'api_key: sk-planted-secret-value'\n    output_key: out\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillRefused, match="secret") as caught:
        export_workflow(flow, tmp_path / "out")
    assert caught.value.reason == "secret"


def test_export_planted_absolute_path_refused(tmp_path: Path) -> None:
    flow = tmp_path / "paths.yaml"
    flow.write_text(
        "name: paths\nstart: t\nnodes:\n"
        "  - id: t\n    type: transform\n"
        "    template: 'wrote /Users/someone/secret.txt'\n    output_key: out\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillRefused, match="absolute") as caught:
        export_workflow(flow, tmp_path / "out")
    assert caught.value.reason == "path"
