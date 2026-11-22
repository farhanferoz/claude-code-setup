import subprocess
from pathlib import Path

import pytest

from setupctl.install import InstallError, Options, Outcome, doctor, install, select
from setupctl.manifest import Piece, PieceKind


def _piece(name: str = "tool", kind: PieceKind = PieceKind.UV_TOOL, *, optional: bool = False, version: str = "v1",
           install_cmds: tuple[str, ...] = ("make install",), check: tuple[str, ...] = ("tool --help",)) -> Piece:
    return Piece(name, f"me/{name}", version, kind, name, install_cmds, check, optional)


def _clone(root: Path, name: str, origin: str) -> str:
    repo = root / name
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


class Recorder:
    def __init__(self, fail: str = "") -> None:
        self.calls: list[tuple[str, Path | None]] = []
        self.fail = fail

    def __call__(self, command: str, cwd: Path | None) -> int:
        self.calls.append((command, cwd))
        return 1 if self.fail and self.fail in command else 0


def test_an_existing_clone_with_a_matching_origin_is_reused_not_recloned(tmp_path: Path) -> None:
    head = _clone(tmp_path, "tool", "git@github.com:me/tool.git")
    run = Recorder()
    [r] = install([_piece(version=head)], Options(tmp_path, tmp_path / "src", False), run, say=lambda _: None)
    assert r.outcome is Outcome.INSTALLED and r.detail == "reused"
    assert not any("git clone" in c for c, _ in run.calls)
    assert run.calls[0] == ("make install", tmp_path / "tool")


def test_a_clone_with_another_origin_is_refused_with_the_remedy(tmp_path: Path) -> None:
    _clone(tmp_path, "tool", "https://github.com/someone-else/tool")
    [r] = install([_piece()], Options(tmp_path, tmp_path / "src", False), Recorder(), say=lambda _: None)
    assert r.outcome is Outcome.FAILED and "move it aside" in r.detail


def test_selection_by_only_skip_and_optional() -> None:
    pieces = [_piece("a"), _piece("b"), _piece("c", optional=True)]
    assert [p.name for p in select(pieces, [], [], False)] == ["a", "b"]
    assert [p.name for p in select(pieces, [], ["a"], True)] == ["b", "c"]
    assert [p.name for p in select(pieces, ["c"], [], False)] == ["c"]
    with pytest.raises(InstallError, match="unknown piece"):
        select(pieces, ["nope"], [], False)


def test_dry_run_runs_nothing_and_a_failure_does_not_stop_the_rest(tmp_path: Path) -> None:
    run = Recorder()
    out: list[str] = []
    results = install([_piece("a"), _piece("b")], Options(tmp_path, tmp_path / "src", True), run, say=out.append)
    assert run.calls == [] and {r.outcome for r in results} == {Outcome.PLANNED}
    assert any("would run" in line and "make install" in line for line in out)
    failing = Recorder(fail="git clone")
    results = install([_piece("a"), _piece("b"), _piece("p", PieceKind.LINK_ONLY)],
                      Options(tmp_path, tmp_path / "src", False), failing, say=lambda _: None)
    assert [r.outcome for r in results] == [Outcome.FAILED, Outcome.FAILED, Outcome.LINK]


def test_doctor_names_the_failing_piece_and_its_install_line() -> None:
    out: list[str] = []
    results = doctor([_piece("a"), _piece("b", check=("b --broken",))], Recorder(fail="--broken"), say=out.append)
    assert [r.outcome for r in results] == [Outcome.INSTALLED, Outcome.FAILED]
    assert "./install --only b" in out[-1]
