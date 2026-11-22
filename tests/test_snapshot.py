import json
import os
from pathlib import Path

import pytest

from setupctl.snapshot import SnapshotConfig, SnapshotError, take_snapshot, write_snapshot

PATTERN = r"`([a-z][\w:-]*)` skill|Skill\(([\w:-]+)\)"


def _hook(command: str) -> dict:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}}


def _skill(root: Path, name: str, body: str = "x") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body)
    (d / "helper.sh").write_text("echo hi")
    return d


def _cfg(home: Path, **over) -> SnapshotConfig:
    base = {
        "master_dir": home / ".claude",
        "cage_glob": str(home / ".claude-*" / "settings.json"),
        "skill_roots": (str(home / ".claude" / "skills"),),
        "agent_roots": (str(home / ".claude" / "agents"),),
        "reference_files": ("CLAUDE.md",),
        "reference_pattern": PATTERN,
        "tree_ignore": ("__pycache__", ".git"),
        "deny_names": (".credentials.json", "projects"),
        "tools": (),
    }
    return SnapshotConfig(**{**base, **over})


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    return tmp_path


def test_hooks_from_master_and_cages_resolve_through_symlinks(home: Path) -> None:
    real = home / "repo" / "guard.sh"
    real.parent.mkdir()
    real.write_text("#!/bin/bash\n")
    link = home / ".claude" / "hooks" / "guard.sh"
    link.parent.mkdir()
    link.symlink_to(real)
    (home / ".claude" / "settings.json").write_text(json.dumps(_hook(f"bash {link}")))
    for cage in ("a", "b"):
        (home / f".claude-{cage}").mkdir()
        (home / f".claude-{cage}" / "settings.json").write_text(json.dumps(_hook(f"bash {link} --x")))
    hooks = take_snapshot(_cfg(home)).hooks
    assert len(hooks) == 3
    assert {h.resolved_script for h in hooks} == {str(real.resolve())}
    assert all(h.sha256 and h.event == "PreToolUse" and h.matcher == "Bash" for h in hooks)


def test_missing_script_records_none_without_crashing(home: Path) -> None:
    (home / ".claude" / "settings.json").write_text(json.dumps(_hook(f"bash {home}/nowhere/gone.sh")))
    (hook,) = take_snapshot(_cfg(home)).hooks
    assert hook.resolved_script is None and hook.sha256 is None


def test_broken_settings_file_raises_naming_the_file(home: Path) -> None:
    (home / ".claude" / "settings.json").write_text("{not json")
    with pytest.raises(SnapshotError, match="settings.json"):
        take_snapshot(_cfg(home))


def test_symlinked_skill_resolves_and_hashes_like_a_real_copy(home: Path) -> None:
    store = home / "store"
    _skill(store, "alpha", "same body")
    _skill(home / ".claude" / "skills", "beta", "same body")
    (home / ".claude" / "skills" / "alpha").symlink_to(store / "alpha")
    comps = {c.name: c for c in take_snapshot(_cfg(home)).components}
    assert comps["alpha"].resolved_path == str((store / "alpha").resolve())
    assert comps["alpha"].tree_sha256 == comps["beta"].tree_sha256


def test_references_resolve_and_report_zero_or_two_answers(home: Path) -> None:
    skills = home / ".claude" / "skills"
    _skill(skills, "watch-job")
    _skill(skills, "checkpoint")
    agents = home / ".claude" / "agents"
    agents.mkdir()
    (agents / "checkpoint.md").write_text("agent")
    (home / ".claude" / "CLAUDE.md").write_text("use the `watch-job` skill\nthen Skill(checkpoint)\nand Skill(ghost)\n")
    refs = {r.name: r for r in take_snapshot(_cfg(home)).references}
    assert len(refs["watch-job"].resolves_to) == 1 and refs["watch-job"].line == 1
    assert len(refs["checkpoint"].resolves_to) == 2
    assert refs["ghost"].resolves_to == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_credentials_and_transcripts_are_never_opened(home: Path) -> None:
    cred = home / ".claude" / ".credentials.json"
    cred.write_text("secret")
    cred.chmod(0)
    transcript = home / ".claude" / "projects" / "p" / "t.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    transcript.chmod(0)
    skill = _skill(home / ".claude" / "skills", "s")
    (skill / ".credentials.json").write_text("secret")
    (skill / ".credentials.json").chmod(0)
    (home / ".claude" / "settings.json").write_text(json.dumps(_hook(f"bash {cred}")))
    (home / ".claude" / "CLAUDE.md").write_text("`s` skill")
    try:
        snap = take_snapshot(_cfg(home, reference_files=("CLAUDE.md", ".credentials.json", "projects/*/*")))
    finally:
        cred.chmod(0o600)
        transcript.chmod(0o600)
        (skill / ".credentials.json").chmod(0o600)
    assert snap.hooks[0].resolved_script is None
    assert snap.components[0].name == "s"


def test_write_snapshot_writes_only_the_out_path(home: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "snap.json"
    before = {p for p in home.rglob("*")}
    write_snapshot(take_snapshot(_cfg(home, tools=(("sh", "sh -c 'echo v1'"), ("nope", "no-such-tool-xyz")))), out)
    data = json.loads(out.read_text())
    assert data["tools"] == {"sh": "v1", "nope": "unavailable"}
    assert {p for p in home.rglob("*")} - before == {out.parent, out}
