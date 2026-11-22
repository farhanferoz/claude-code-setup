"""Install manifest pieces: reuse an existing clone when its origin matches, else clone the pinned version, then run
the piece's own install and check commands. Never edits a shell rc file."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setupctl.manifest import Piece, PieceKind

GITHUB_URL = "https://github.com/{repo}"  # fact: the public clone URL form for an owner/name repository
_ORIGIN = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$")  # fact: https and ssh remote forms


class InstallError(RuntimeError):
    """A piece cannot be installed as asked; the message names the remedy."""


class Outcome(StrEnum):
    INSTALLED = "installed"
    LINK = "link"
    FAILED = "failed"
    PLANNED = "planned"  # dry run


@dataclass(frozen=True)
class Options:
    dev_root: Path  # where existing clones are looked for (and reused)
    src_root: Path  # where missing pieces are cloned
    dry_run: bool


@dataclass(frozen=True)
class Result:
    piece: str
    outcome: Outcome
    detail: str


Runner = Callable[[str, Path | None], int]


def shell_runner(command: str, cwd: Path | None) -> int:
    return subprocess.run(["bash", "-c", command], cwd=cwd, check=False).returncode


def select(pieces: Sequence[Piece], only: Sequence[str], skip: Sequence[str], include_optional: bool) -> list[Piece]:
    names = {p.name for p in pieces}
    unknown = sorted((set(only) | set(skip)) - names)
    if unknown:
        raise InstallError(f"unknown piece(s): {', '.join(unknown)}; choose from: {', '.join(sorted(names))}.")
    if only:
        chosen = [p for p in pieces if p.name in only]
    else:
        chosen = [p for p in pieces if include_optional or not p.optional]
    return [p for p in chosen if p.name not in skip]


def _origin_repo(clone: Path) -> str | None:
    out = subprocess.run(["git", "-C", str(clone), "remote", "get-url", "origin"], capture_output=True, text=True,
                         check=False)
    m = _ORIGIN.search(out.stdout.strip()) if out.returncode == 0 else None
    return m.group("repo") if m else None


def resolve_clone(piece: Piece, opts: Options, run: Runner) -> tuple[Path, str]:
    """The clone to install from, and a note: reused (with a warning if not at the pinned version) or cloned."""
    existing = opts.dev_root / piece.clone_dir
    if existing.exists():
        origin = _origin_repo(existing)
        if origin != piece.repo:
            raise InstallError(f"{existing} exists but its origin is {origin or 'not a GitHub git remote'}, not "
                               f"{piece.repo}; move it aside or pass --dev-root to a folder without it.")
        head = subprocess.run(["git", "-C", str(existing), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=False).stdout.strip()
        note = "reused" if head == piece.version else f"reused (HEAD {head[:7]}, tested version {piece.version[:7]})"
        return existing, note
    target = opts.src_root / piece.clone_dir
    if not opts.dry_run:
        opts.src_root.mkdir(parents=True, exist_ok=True)
        if run(f"git clone --quiet {GITHUB_URL.format(repo=piece.repo)} '{target}'", None) != 0:
            raise InstallError(f"cloning {piece.repo} failed; check the network and that the repository is public.")
        if run(f"git -C '{target}' checkout --quiet {piece.version}", None) != 0:
            raise InstallError(f"{piece.repo} has no commit {piece.version}; update manifest.toml.")
    return target, f"cloned at {piece.version[:7]}"


def install(pieces: Sequence[Piece], opts: Options, run: Runner = shell_runner, say: Callable[[str], None] = print
            ) -> list[Result]:
    results: list[Result] = []
    for piece in pieces:
        if piece.kind is PieceKind.LINK_ONLY:
            say(f"{piece.name}: see https://github.com/{piece.repo} (installed by its own instructions)")
            results.append(Result(piece.name, Outcome.LINK, piece.repo))
            continue
        try:
            clone, note = resolve_clone(piece, opts, run)
        except InstallError as exc:
            say(f"{piece.name}: FAILED: {exc}")
            results.append(Result(piece.name, Outcome.FAILED, str(exc)))
            continue
        cwd = None if piece.kind is PieceKind.MARKETPLACE else clone
        if opts.dry_run:
            for command in (*piece.install, *piece.check):
                say(f"{piece.name}: would run in {cwd or '.'}: {command}")
            results.append(Result(piece.name, Outcome.PLANNED, note))
            continue
        failed = next((c for c in (*piece.install, *piece.check) if run(c, cwd) != 0), None)
        outcome = Outcome.FAILED if failed else Outcome.INSTALLED
        detail = f"{note}; failed: {failed}" if failed else note
        say(f"{piece.name}: {outcome.value} ({detail})")
        results.append(Result(piece.name, outcome, detail))
    return results


def doctor(pieces: Sequence[Piece], run: Runner = shell_runner, say: Callable[[str], None] = print) -> list[Result]:
    """Run each installed piece's check commands; a failing one names the piece and its install line."""
    results: list[Result] = []
    for piece in pieces:
        if piece.kind is PieceKind.LINK_ONLY:
            results.append(Result(piece.name, Outcome.LINK, piece.repo))
            continue
        failed = next((c for c in piece.check if run(c, None) != 0), None)
        if failed:
            say(f"{piece.name}: check failed ({failed}); install it with: ./install --only {piece.name}")
            results.append(Result(piece.name, Outcome.FAILED, failed))
        else:
            say(f"{piece.name}: ok")
            results.append(Result(piece.name, Outcome.INSTALLED, "checks pass"))
    return results
