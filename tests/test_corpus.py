import json
from pathlib import Path

from setupctl import cli
from setupctl.corpus import SamplePlan, build_corpus, read_corpus


def _use(uid: str, name: str, inp: dict) -> str:
    return json.dumps({"type": "assistant", "cwd": "/w", "sessionId": "s",
                       "message": {"content": [{"type": "tool_use", "id": uid, "name": name, "input": inp}]}})


def _result(uid: str, *, error: bool = False) -> str:
    return json.dumps({"type": "user", "toolUseResult": {"stdout": "ok"},
                       "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "is_error": error}]}})


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


PLAN = SamplePlan(per_tool_default=100, per_tool={}, seed=0)


def test_same_call_in_two_transcripts_is_one_item(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.jsonl", [_use("u1", "Bash", {"command": "ls"}), _result("u1")])
    b = _write(tmp_path / "b.jsonl", [_use("u9", "Bash", {"command": "ls"}), _result("u9")])
    items, stats = build_corpus([a, b], PLAN)
    assert len(items) == 1
    assert stats.calls_seen == 2


def test_sampling_is_capped_per_tool_and_deterministic(tmp_path: Path) -> None:
    lines = [_use(f"b{i}", "Bash", {"command": f"echo {i}"}) for i in range(50)]
    lines += [_use(f"e{i}", "Edit", {"file_path": f"/f{i}"}) for i in range(5)]
    t = _write(tmp_path / "t.jsonl", lines)
    plan = SamplePlan(per_tool_default=100, per_tool={"Bash": 10}, seed=3)
    first, stats = build_corpus([t], plan)
    again, _ = build_corpus([t], plan)
    assert stats.per_tool == {"Bash": 10, "Edit": 5}
    assert [i.item_id for i in first] == [i.item_id for i in again]
    other, _ = build_corpus([t], SamplePlan(100, {"Bash": 10}, seed=4))
    assert {i.item_id for i in other} != {i.item_id for i in first}


def test_bad_lines_counted_results_paired_and_subagents_globbed(tmp_path: Path) -> None:
    root = tmp_path / ".claude-x" / "projects" / "p"
    _write(root / "main.jsonl", ['{"tool_use" broken', _use("u1", "Bash", {"command": "a"}), _result("u1", error=True),
                                 _use("u2", "Read", {"file_path": "/x"})])
    _write(root / "sid" / "subagents" / "agent-1.jsonl", [_use("u3", "Grep", {"pattern": "q"})])
    out = tmp_path / "corpus"
    pattern = str(tmp_path / ".claude-*/projects/**/*.jsonl")
    rc = cli.main(["corpus", "build", "--out", str(out), "--transcripts", pattern])
    assert rc == 0
    items = {i.tool_name: i for i in read_corpus(out)}
    assert set(items) == {"Bash", "Read", "Grep"}
    assert items["Bash"].failed and items["Bash"].tool_response == {"stdout": "ok"}
    assert items["Read"].tool_response is None and not items["Read"].failed
    assert json.loads((out / "stats.json").read_text())["lines_bad"] == 1
    assert (out / "items.jsonl").stat().st_mode & 0o777 == 0o600
