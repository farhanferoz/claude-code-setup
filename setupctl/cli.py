"""Argument parsing only; dispatches to the modules that do the work."""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from setupctl import corpus, diffing, isolation, replay
from setupctl.snapshot import SnapshotConfig, SnapshotError, hooks_in_settings, take_snapshot, write_snapshot

# A fact about the repository layout: the shipped defaults file sits beside the package.
DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "snapshot.toml"
REPLAY_DEFAULTS_FILE = Path(__file__).resolve().parent.parent / "replay.toml"
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
    cb = sub.add_parser("corpus", help="build the replay corpus from transcripts").add_subparsers(
        dest="corpus_command", required=True).add_parser("build")
    cb.add_argument("--out", type=Path, required=True)
    cb.add_argument("--config", type=Path, default=REPLAY_DEFAULTS_FILE)
    cb.add_argument("--transcripts")
    cb.add_argument("--per-tool-default", type=int)
    cb.add_argument("--per-tool", action="append", metavar="TOOL=N", default=[])
    cb.add_argument("--seed", type=int)
    rp = sub.add_parser("replay", help="replay the corpus through one settings file's tool hooks")
    rp.add_argument("--settings", type=Path, required=True)
    rp.add_argument("--corpus", type=Path, required=True)
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--config", type=Path, default=REPLAY_DEFAULTS_FILE)
    rp.add_argument("--snapshot-config", type=Path, default=DEFAULTS_FILE)
    rp.add_argument("--repeat", type=int, default=2)
    rp.add_argument("--workers", type=int)
    rp.add_argument("--timeout", type=float)
    rp.add_argument("--isolation", choices=("container", "none"),
                    help="container (default from config): re-run inside a read-only view of the host")
    df = sub.add_parser("diff", help="compare two replays (.jsonl) or two snapshots (.json)")
    df.add_argument("--old", type=Path, required=True)
    df.add_argument("--new", type=Path, required=True)
    df.add_argument("--allow", type=Path)
    df.add_argument("--config", type=Path, default=REPLAY_DEFAULTS_FILE)
    return parser


def _expand(text: str) -> str:
    return os.path.expandvars(os.path.expanduser(text))


def _corpus_build(args: argparse.Namespace) -> int:
    conf = tomllib.loads(args.config.read_text())
    per_tool = dict(conf.get("per_tool", {}))
    for item in args.per_tool:
        tool, sep, n = item.partition("=")
        if not sep or not n.isdigit():
            raise SnapshotError(f"--per-tool {item!r}: write it as TOOL=N, for example --per-tool Bash=3000.")
        per_tool[tool] = int(n)
    plan = corpus.SamplePlan(
        per_tool_default=args.per_tool_default if args.per_tool_default is not None else conf["per_tool_default"],
        per_tool=per_tool,
        seed=args.seed if args.seed is not None else conf["seed"],
    )
    paths = [Path(p) for p in glob.glob(_expand(args.transcripts or conf["transcripts"]), recursive=True)]
    items, stats = corpus.build_corpus(paths, plan)
    corpus.write_corpus(items, stats, args.out)
    print(f"files={stats.files} calls={stats.calls_seen} unique={stats.calls_unique} sampled={len(items)} "
          f"bad_lines={stats.lines_bad} -> {args.out}")
    return 0


def _replay(args: argparse.Namespace, argv: Sequence[str]) -> int:
    full = tomllib.loads(args.config.read_text())
    conf = full["sandbox"]
    if (args.isolation or conf["isolation"]) == "container":
        c = full["container"]
        cfg = isolation.ContainerConfig(c["docker"], c["image"], tuple(_expand(p) for p in conf["writable_tmpfs"]),
                                        tuple(c["pass_env"]))
        repo = Path(__file__).resolve().parent.parent
        inner = ["/usr/bin/env", f"PYTHONPATH={repo}", c["python"], "-m", "setupctl", *argv, "--isolation", "none"]
        out_dir = args.out.resolve().parent
        out_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run(isolation.container_command(inner, cfg, [out_dir]), check=False).returncode
    sandbox = replay.SandboxConfig(
        per_call_timeout_s=args.timeout if args.timeout is not None else conf["per_call_timeout_s"],
        workers=args.workers if args.workers is not None else conf["workers"],
        permission_mode=conf["permission_mode"],
        call_root=_expand(conf["call_root"]),
    )
    snap_args = argparse.Namespace(config=args.snapshot_config, tools=None, **{k: None for k in _SNAPSHOT_KEYS})
    regs = hooks_in_settings(args.settings, _config(snap_args))
    settings_env = json.loads(args.settings.read_text()).get("env", {})
    outcomes = replay.replay(regs, corpus.read_corpus(args.corpus), sandbox, settings_env, args.repeat)
    replay.write_outcomes(outcomes, args.out)
    flaky = len({o.hook_id for o in outcomes if o.nondeterministic})
    print(f"runs={len(outcomes)} hooks={len({o.hook_id for o in outcomes})} nondeterministic_hooks={flaky} "
          f"timed_out={sum(o.timed_out for o in outcomes)} env_limited={sum(o.env_limited for o in outcomes)} "
          f"-> {args.out}")
    return 0


def _diff(args: argparse.Namespace) -> int:
    normalise = tomllib.loads(args.config.read_text()).get("diff", {}).get("normalise", [])
    allowed = diffing.load_allowed(args.allow, [tuple(p) for p in normalise])
    if args.old.suffix == ".jsonl":
        report = diffing.diff_outcomes(replay.read_outcomes(args.old), replay.read_outcomes(args.new), allowed)
    else:
        report = diffing.diff_snapshots(json.loads(args.old.read_text()), json.loads(args.new.read_text()), allowed)
    for line in report.notes:
        print(f"note: {line}")
    for line in report.failures:
        print(f"FAIL: {line}")
    print("OK: no differences beyond the allowed ones" if report.ok else f"{len(report.failures)} difference(s)")
    return 0 if report.ok else 1


_SNAPSHOT_KEYS = ("master_dir", "cage_glob", "skill_roots", "agent_roots", "reference_files", "reference_pattern",
                  "tree_ignore", "deny_names")


def _config(args: argparse.Namespace) -> SnapshotConfig:
    with args.config.open("rb") as f:
        merged = tomllib.load(f)
    for key in _SNAPSHOT_KEYS:
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
    if args.command in ("corpus", "replay", "diff"):
        try:
            if args.command == "replay":
                return _replay(args, [str(a) for a in (argv if argv is not None else sys.argv[1:])])
            return _corpus_build(args) if args.command == "corpus" else _diff(args)
        except SnapshotError as exc:
            print(f"setupctl {args.command}: {exc}", file=sys.stderr)
            return 1
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
