"""Load and validate manifest.toml into frozen records."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ManifestError(ValueError):
    """The manifest is invalid; the message names the piece, the key and the fix."""


class PieceKind(StrEnum):
    CCAGE = "ccage"  # ./install.sh in the clone
    UV_TOOL = "uv-tool"  # uv tool install [--editable] <src>
    PIP_EDITABLE = "pip-editable"
    MARKETPLACE = "marketplace"  # claude plugin marketplace add <repo>
    LINK_ONLY = "link-only"  # README link, nothing installed


@dataclass(frozen=True)
class Piece:
    name: str
    repo: str
    version: str
    kind: PieceKind
    clone_dir: str
    install: tuple[str, ...]
    check: tuple[str, ...]
    optional: bool


def _text(raw: dict, key: str, where: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ManifestError(f"{where}: '{key}' must be a string; write it as {key} = \"...\".")
    if required and not value:
        raise ManifestError(f"{where}: missing '{key}'; add {key} = \"...\" to this [[piece]].")
    return value


def _commands(raw: dict, key: str, where: str, *, required: bool) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ManifestError(f"{where}: '{key}' must be a list of non-empty command strings.")
    if required and not value:
        raise ManifestError(
            f"{where}: missing '{key}'; a piece that installs something needs {key} = [\"<command>\"]."
        )
    return tuple(value)


def _piece(raw: dict, index: int) -> Piece:
    name = raw.get("name")
    where = f"piece '{name}'" if isinstance(name, str) and name else f"piece #{index + 1}"
    name = _text(raw, "name", where)
    kind_text = _text(raw, "kind", where)
    try:
        kind = PieceKind(kind_text)
    except ValueError:
        allowed = ", ".join(k.value for k in PieceKind)
        raise ManifestError(f"{where}: unknown kind '{kind_text}'; use one of: {allowed}.") from None
    link_only = kind is PieceKind.LINK_ONLY
    optional = raw.get("optional", False)
    if not isinstance(optional, bool):
        raise ManifestError(f"{where}: 'optional' must be true or false.")
    return Piece(
        name=name,
        repo=_text(raw, "repo", where),
        version=_text(raw, "version", where, required=not link_only),
        kind=kind,
        clone_dir=_text(raw, "clone_dir", where),
        install=_commands(raw, "install", where, required=not link_only),
        check=_commands(raw, "check", where, required=not link_only),
        optional=optional,
    )


def load_manifest(path: Path) -> tuple[Piece, ...]:
    """Read ``path`` and return its pieces in file order; raise ManifestError on any invalid entry."""
    with path.open("rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"{path}: not valid TOML ({exc}); fix the syntax.") from exc
    raw_pieces = data.get("piece")
    if not isinstance(raw_pieces, list) or not raw_pieces:
        raise ManifestError(f"{path}: no [[piece]] entries; add at least one.")
    pieces = tuple(_piece(raw, i) for i, raw in enumerate(raw_pieces))
    seen: set[str] = set()
    for piece in pieces:
        if piece.name in seen:
            raise ManifestError(f"{path}: duplicate piece name '{piece.name}'; names must be unique.")
        seen.add(piece.name)
    return pieces
