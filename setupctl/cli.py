"""Argument parsing only; dispatches to the modules that do the work."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from setupctl.snapshot import SnapshotConfig, SnapshotError, take_snapshot, write_snapshot

# A fact about the repository layout: the shipped defaults file sits beside the package.
DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "snapshot.toml"
EXIT_USAGE = 2  # a fact: the conventional exit status for "not implemented / bad usage"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="setupctl")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="install the manifest pieces (not implemented yet)")
    snap = sub.add_parser("snapshot", help="record what this machine runs")
    snap.add_argument("--out", type=Path, required=True, help="JSON file to write (the only file written)")
    snap.add_argument("--config", type=Path, default=DEFAULTS_FILE, help="TOML of defaults (default: snapshot.toml)")
    snap.add_argument("--master-dir")
    snap.add_argument("--cage-glob")
    snap.add_argument("--skill-root", action="append", dest="skill_roots")
    snap.add_argument("--agent-root", action="append", dest="agent_roots")
    snap.add_argument("--reference-file", action="append", dest="reference_files")
    snap.add_argument("--reference-pattern")
    snap.add_argument("--tree-ignore", action="append", dest="tree_ignore")
    snap.add_argument("--deny-name", action="append", dest="deny_names")
    snap.add_argument("--tool", action="append", dest="tools", metavar="LABEL=COMMAND")
    return parser


def _config(args: argparse.Namespace) -> SnapshotConfig:
    with args.config.open("rb") as f:
        merged = tomllib.load(f)
    for key in ("master_dir", "cage_glob", "skill_roots", "agent_roots", "reference_files", "reference_pattern",
                "tree_ignore", "deny_names"):
        if getattr(args, key) is not None:
            merged[key] = getattr(args, key)
    tools = dict(merged.get("tools", {}))
    for item in args.tools or ():
        label, sep, command = item.partition("=")
        if not sep or not label or not command:
            raise SnapshotError(f"--tool {item!r}: write it as LABEL=COMMAND, for example --tool git='git --version'.")
        tools[label] = command
    try:
        return SnapshotConfig(
            master_dir=Path(merged["master_dir"]).expanduser(),
            cage_glob=merged["cage_glob"],
            skill_roots=tuple(merged["skill_roots"]),
            agent_roots=tuple(merged["agent_roots"]),
            reference_files=tuple(merged["reference_files"]),
            reference_pattern=merged["reference_pattern"],
            tree_ignore=tuple(merged["tree_ignore"]),
            deny_names=tuple(merged["deny_names"]),
            tools=tuple(sorted(tools.items())),
        )
    except KeyError as exc:
        raise SnapshotError(f"{args.config}: missing key {exc}; add it or pass the matching flag.") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "install":
        print("setupctl install: not implemented yet.", file=sys.stderr)
        return EXIT_USAGE
    try:
        snapshot = take_snapshot(_config(args))
    except SnapshotError as exc:
        print(f"setupctl snapshot: {exc}", file=sys.stderr)
        return 1
    write_snapshot(snapshot, args.out)
    print(
        f"hooks={len(snapshot.hooks)} components={len(snapshot.components)} "
        f"references={len(snapshot.references)} -> {args.out}"
    )
    return 0
