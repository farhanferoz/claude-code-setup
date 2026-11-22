"""Record what a machine runs: hook registrations, skills, agents, tool versions, name references.

Never opens credentials or transcripts: any path with a component in ``SnapshotConfig.deny_names`` is skipped.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


class SnapshotError(RuntimeError):
    """A snapshot input is unusable; the message names the file and the fix."""


@dataclass(frozen=True)
class SnapshotConfig:
    master_dir: Path
    cage_glob: str
    skill_roots: tuple[str, ...]
    agent_roots: tuple[str, ...]
    reference_files: tuple[str, ...]  # globs relative to master_dir
    reference_pattern: str  # alternatives, each with one capture group: the bare name
    tree_ignore: tuple[str, ...]  # file or directory names left out of a tree hash
    deny_names: tuple[str, ...]  # path components that are never opened
    tools: tuple[tuple[str, str], ...]  # (label, command line) for the version record


@dataclass(frozen=True)
class HookReg:
    settings_path: str
    event: str
    matcher: str
    command: str
    resolved_script: str | None
    sha256: str | None


@dataclass(frozen=True)
class Component:
    kind: Literal["skill", "agent"]
    name: str
    resolved_path: str
    tree_sha256: str


@dataclass(frozen=True)
class Reference:
    source_file: str
    line: int
    name: str
    resolves_to: tuple[str, ...]  # "<kind>:<resolved path>" per component answering to the name


@dataclass(frozen=True)
class Snapshot:
    host: str
    taken_at: str
    hooks: tuple[HookReg, ...]
    components: tuple[Component, ...]
    tools: dict[str, str]
    references: tuple[Reference, ...]


def _expand(text: str) -> str:
    return os.path.expandvars(os.path.expanduser(text))


def _denied(path: Path, cfg: SnapshotConfig) -> bool:
    return any(part in cfg.deny_names for part in path.parts)


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha(path: Path, cfg: SnapshotConfig) -> str:
    """Hash a file, or a directory's sorted relative paths and file contents. ``path`` is already resolved."""
    if path.is_file():
        return _file_sha(path)
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d not in cfg.tree_ignore and d not in cfg.deny_names)
        for name in sorted(filenames):
            file = Path(dirpath, name)
            if name in cfg.tree_ignore or name in cfg.deny_names or not file.is_file():
                continue
            digest.update(file.relative_to(path).as_posix().encode() + b"\0")
            digest.update(_file_sha(file).encode() + b"\0")
    return digest.hexdigest()


def _resolve_script(command: str, cfg: SnapshotConfig) -> str | None:
    """First token of the hook command that names an existing file, symlinks resolved; else None."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for token in tokens:
        if "/" not in token or token.startswith("-"):
            continue
        candidate = Path(_expand(token))
        if candidate.is_file():
            real = candidate.resolve()
            return None if _denied(real, cfg) else str(real)
    return None


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"{path}: cannot read as JSON ({exc}); fix or remove that settings file.") from exc
    if not isinstance(data, dict):
        raise SnapshotError(f"{path}: top level must be a JSON object; fix that settings file.")
    return data


def hooks_in_settings(path: Path, cfg: SnapshotConfig) -> list[HookReg]:
    """Every command-hook registration in one settings file, scripts resolved through symlinks and hashed."""
    regs: list[HookReg] = []
    hooks = _read_settings(path).get("hooks", {})
    if not isinstance(hooks, dict):
        return regs
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    continue
                script = _resolve_script(command, cfg)
                regs.append(
                    HookReg(
                        settings_path=str(path),
                        event=event,
                        matcher=str(group.get("matcher", "")),
                        command=command,
                        resolved_script=script,
                        sha256=_file_sha(Path(script)) if script else None,
                    )
                )
    return regs


def _collect_hooks(cfg: SnapshotConfig) -> tuple[HookReg, ...]:
    paths = [cfg.master_dir / "settings.json", *(Path(p) for p in sorted(glob.glob(_expand(cfg.cage_glob))))]
    regs: list[HookReg] = []
    for path in paths:
        if path.is_file() and not _denied(path, cfg):
            regs.extend(hooks_in_settings(path, cfg))
    return tuple(regs)


def _collect_components(cfg: SnapshotConfig) -> tuple[Component, ...]:
    found: list[Component] = []
    for root in cfg.skill_roots:
        for entry in sorted(Path(_expand(root)).glob("*")):
            real = entry.resolve()
            if real.is_dir() and (real / "SKILL.md").is_file() and not _denied(real, cfg):
                found.append(Component("skill", entry.name, str(real), _tree_sha(real, cfg)))
    for root in cfg.agent_roots:
        for entry in sorted(Path(_expand(root)).glob("*.md")):
            real = entry.resolve()
            if real.is_file() and not _denied(real, cfg):
                found.append(Component("agent", entry.stem, str(real), _tree_sha(real, cfg)))
    return tuple(found)


def _collect_references(cfg: SnapshotConfig, components: tuple[Component, ...]) -> tuple[Reference, ...]:
    answers: dict[str, list[str]] = {}
    for c in components:
        answers.setdefault(c.name, []).append(f"{c.kind}:{c.resolved_path}")
    pattern = re.compile(cfg.reference_pattern)
    files: set[str] = set()
    for spec in cfg.reference_files:
        files.update(glob.glob(str(cfg.master_dir / spec)))
    refs: list[Reference] = []
    for name in sorted(files):
        path = Path(name)
        if not path.is_file() or _denied(path.resolve(), cfg):
            continue
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            for match in pattern.finditer(line):
                ref = next(g for g in match.groups() if g)
                refs.append(Reference(str(path), lineno, ref, tuple(answers.get(ref, ()))))
    return tuple(refs)


def _tool_version(command: str) -> str:
    try:
        out = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if out.returncode == 0 and text else "unavailable"


def take_snapshot(cfg: SnapshotConfig) -> Snapshot:
    components = _collect_components(cfg)
    return Snapshot(
        host=socket.gethostname(),
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
        hooks=_collect_hooks(cfg),
        components=components,
        tools={label: _tool_version(cmd) for label, cmd in cfg.tools},
        references=_collect_references(cfg, components),
    )


def write_snapshot(s: Snapshot, path: Path) -> None:
    """Write ``s`` as sorted-key JSON to ``path`` (the only file this module writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(s), sort_keys=True, indent=2) + "\n")
