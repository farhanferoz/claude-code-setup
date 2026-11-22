from pathlib import Path

import pytest

from setupctl.manifest import ManifestError, PieceKind, load_manifest

PIECE = """
[[piece]]
name = "{name}"
repo = "o/{name}"
{version}
kind = "{kind}"
clone_dir = "{name}"
{commands}
optional = false
"""
CMDS = 'install = ["./install.sh"]\ncheck = ["tool --help"]'


def _write(tmp_path: Path, *pieces: str) -> Path:
    path = tmp_path / "manifest.toml"
    path.write_text("".join(pieces))
    return path


def _piece(name: str, kind: str = "ccage", version: str = 'version = "abc"', commands: str = CMDS) -> str:
    return PIECE.format(name=name, kind=kind, version=version, commands=commands)


def test_valid_two_piece_manifest_loads(tmp_path: Path) -> None:
    pieces = load_manifest(_write(tmp_path, _piece("a"), _piece("b", kind="uv-tool")))
    assert [p.name for p in pieces] == ["a", "b"]
    assert pieces[0].kind is PieceKind.CCAGE
    assert pieces[1].install == ("./install.sh",) and pieces[1].check == ("tool --help",)


def test_unknown_kind_names_piece_and_allowed_kinds(tmp_path: Path) -> None:
    with pytest.raises(ManifestError) as err:
        load_manifest(_write(tmp_path, _piece("bad", kind="docker")))
    assert "piece 'bad'" in str(err.value)
    assert all(k.value in str(err.value) for k in PieceKind)


def test_missing_version_on_installable_piece_raises(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="missing 'version'"):
        load_manifest(_write(tmp_path, _piece("a", version="")))


def test_duplicate_name_raises(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="duplicate piece name 'a'"):
        load_manifest(_write(tmp_path, _piece("a"), _piece("a")))


def test_link_only_needs_no_install_check_or_version(tmp_path: Path) -> None:
    (piece,) = load_manifest(_write(tmp_path, _piece("p", kind="link-only", version="", commands="")))
    assert piece.install == () and piece.check == () and piece.version == ""


def test_shipped_manifest_is_valid() -> None:
    pieces = load_manifest(Path(__file__).resolve().parent.parent / "manifest.toml")
    assert len(pieces) == 11
