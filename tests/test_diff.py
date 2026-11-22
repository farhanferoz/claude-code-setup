from setupctl.diffing import AllowedDiffs, diff_outcomes, diff_snapshots
from setupctl.replay import Outcome

NORM = AllowedDiffs(normalise=((r"(/home/[^/]+|~)/\.claude/hooks/", "<H>/"),
                               (r"/home/[^/]+/dev/claude-skills/plugins/[^/]+/hooks/", "<H>/")))


def _o(hook: str = "PreToolUse|Bash|g.sh", item: str = "i", code: int = 0, decision: str | None = None,
       stderr: str = "", flaky: bool = False) -> Outcome:
    return Outcome(hook, item, code, decision, None, None, stderr, False, False, flaky)


def test_identical_replays_pass_and_a_changed_decision_fails() -> None:
    assert diff_outcomes([_o()], [_o()], NORM).ok
    r = diff_outcomes([_o()], [_o(decision="deny")], NORM)
    assert not r.ok and "g.sh" in r.failures[0] and "item i" in r.failures[0]


def test_refusal_text_compared_after_path_normalisation() -> None:
    old = _o(code=2, stderr="see /home/u/.claude/hooks/g.sh")
    moved = _o(code=2, stderr="see /home/u/dev/claude-skills/plugins/guards/hooks/g.sh")
    reworded = _o(code=2, stderr="see the docs")
    assert diff_outcomes([old], [moved], NORM).ok
    assert not diff_outcomes([old], [reworded], NORM).ok


def test_missing_hook_fails_unless_allowed_and_flaky_pairs_are_set_aside() -> None:
    assert not diff_outcomes([_o()], [], NORM).ok
    allowed = AllowedDiffs(removed={"PreToolUse|Bash|g.sh": "D13"}, normalise=NORM.normalise)
    assert diff_outcomes([_o()], [], allowed).ok
    r = diff_outcomes([_o(flaky=True)], [_o(decision="deny")], NORM)
    assert r.ok and "nondeterministic" in r.notes[0]


def _snap(sha: str = "h1", path: str = "/a/skills/s", refs: tuple[str, ...] = ("/a/skills/s",)) -> dict:
    return {"hooks": [], "components": [{"kind": "skill", "name": "s", "resolved_path": path, "tree_sha256": sha}],
            "references": [{"source_file": "CLAUDE.md", "line": 1, "name": "s", "resolves_to": list(refs)}]}


def test_snapshot_diff_allows_moves_not_unlisted_changes_or_broken_references() -> None:
    assert diff_snapshots(_snap(), _snap(path="/b/plugins/p/skills/s"), NORM).ok
    assert not diff_snapshots(_snap(), _snap(sha="h2"), NORM).ok
    assert diff_snapshots(_snap(), _snap(sha="h2"), AllowedDiffs(changed=frozenset({"s"}))).ok
    assert not diff_snapshots(_snap(), _snap(refs=()), NORM).ok
