import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from setupctl import cli
from setupctl import replay as rp
from setupctl.corpus import CorpusItem
from setupctl.snapshot import HookReg


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", "ubuntu:24.04"], capture_output=True, check=False)
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(not _docker_works(), reason="needs docker with the ubuntu:24.04 image")


def _item(tool: str = "Bash", *, response: dict | None = None, failed: bool = False, uid: str = "i1",
          cwd: str = "/") -> CorpusItem:
    return CorpusItem(uid, tool, {"command": "ls"}, cwd, "s", "/nowhere/t.jsonl", "toolu_1", response, failed)


def _reg(script: Path, event: str = "PreToolUse", matcher: str = "Bash") -> HookReg:
    return HookReg("/s.json", event, matcher, f"bash {script}", str(script), None)


def _script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    return p


def _sb(tmp_path: Path, timeout: float = 5) -> rp.SandboxConfig:
    return rp.SandboxConfig(timeout, 4, "bypassPermissions", str(tmp_path))


def test_matcher_rules_follow_the_hooks_reference() -> None:
    assert rp.matcher_matches("Bash|Edit", "Edit", "PreToolUse")
    assert not rp.matcher_matches("Bash|Edit", "Write", "PreToolUse")
    assert rp.matcher_matches("Edit, Write", "Write", "PostToolUse")
    assert rp.matcher_matches("", "Anything", "PreToolUse") and rp.matcher_matches("*", "X", "PostToolUse")
    assert rp.matcher_matches("^Notebook", "NotebookEdit", "PreToolUse")
    assert not rp.matcher_matches("Edit", "NotebookEdit", "PreToolUse")
    assert rp.matcher_matches("Bash", "Monitor", "PreToolUse")
    assert not rp.matcher_matches("Bash", "Monitor", "PostToolUse")


def test_payloads_only_where_the_item_can_feed_the_event() -> None:
    assert rp.payload(_item(), "PostToolUse", "default") is None
    assert rp.payload(_item(response={"stdout": ""}), "PostToolUse", "default")["tool_response"] == {"stdout": ""}
    failed = _item(response={"content": "boom"}, failed=True)
    assert rp.payload(failed, "PostToolUseFailure", "default")["error"] == "boom"
    assert rp.payload(_item(), "PostToolUseFailure", "default") is None


def test_decisions_rewrites_timeouts_and_skips_are_recorded(tmp_path: Path) -> None:
    deny = _script(tmp_path, "deny.sh", """cat >/dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}'""")
    rewrite = _script(tmp_path, "rewrite.sh", """cat >/dev/null
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{"command":"ls -k"}}}'""")
    slow = _script(tmp_path, "slow.sh", "sleep 5")
    post = _script(tmp_path, "post.sh", "cat >/dev/null; exit 2")
    regs = [_reg(deny), _reg(rewrite), _reg(slow), _reg(post, "PostToolUse")]
    # cwd "" occurs in real transcripts; it must run from "/" rather than crash the replay
    out = {o.hook_id.split("|")[-1]: o for o in rp.replay(regs, [_item(cwd="")], _sb(tmp_path, timeout=1), {})}
    assert out["deny.sh"].decision == "deny"
    assert out["rewrite.sh"].updated_input == {"command": "ls -k"}
    assert out["slow.sh"].timed_out
    assert "post.sh" not in out  # the item has no tool_response, so PostToolUse cannot be fed


@needs_docker
def test_container_keeps_writes_off_the_real_filesystem(tmp_path: Path) -> None:
    state, calls, ro, out = tmp_path / "state", tmp_path / "calls", tmp_path / "ro", tmp_path / "out"
    for d in (state, calls, ro):
        d.mkdir()
    writer = _script(tmp_path, "w.sh", f"cat >/dev/null; echo x > {state}/leak; echo y > {ro}/leak; echo done")
    settings = tmp_path / "settings.json"
    hook = {"type": "command", "command": f"bash {writer}"}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}))
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "items.jsonl").write_text(json.dumps(asdict(_item())) + "\n")
    conf = (Path(cli.REPLAY_DEFAULTS_FILE).read_text()
            .replace('writable_tmpfs = ["/tmp", "~/.local/state", "~/.cache"]',
                     f'writable_tmpfs = ["{state}", "{calls}"]')
            .replace('call_root = "/tmp"', f'call_root = "{calls}"'))
    conf_file = tmp_path / "replay.toml"
    conf_file.write_text(conf)
    rc = cli.main(["replay", "--settings", str(settings), "--corpus", str(corpus_dir), "--out", str(out / "r.jsonl"),
                   "--config", str(conf_file), "--repeat", "1"])
    assert rc == 0
    [o] = rp.read_outcomes(out / "r.jsonl")
    assert not (state / "leak").exists()
    assert not (ro / "leak").exists()
    assert o.env_limited  # the write to the read-only path was seen and flagged


def test_runs_that_disagree_are_flagged_nondeterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = iter([0, 2, 0, 0])

    def fake(reg, item, data, env, sb, root):  # noqa: ANN001, ANN202
        return rp.Outcome(rp.hook_id(reg), item.item_id, next(calls), None, None, None, "", False, False)

    monkeypatch.setattr(rp, "_run_one", fake)
    regs = [_reg(tmp_path / "a.sh"), _reg(tmp_path / "b.sh")]
    out = rp.replay(regs, [_item()], rp.SandboxConfig(1, 1, "default", str(tmp_path)), {}, repeat=2)
    assert [o.nondeterministic for o in out] == [False, True]


def test_hook_identity_survives_a_symlinked_move(tmp_path: Path) -> None:
    real = _script(tmp_path, "guard.sh", "true")
    link = tmp_path / "elsewhere" / "guard.sh"
    link.parent.mkdir()
    link.symlink_to(real)
    a = HookReg("/s", "PreToolUse", "Bash", f"bash {real}", str(real), None)
    b = HookReg("/s", "PreToolUse", "Bash", f"bash {link}", str(link.resolve()), None)
    assert rp.hook_id(a) == rp.hook_id(b)
