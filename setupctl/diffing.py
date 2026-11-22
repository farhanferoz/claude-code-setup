"""Compare two replays, or two snapshots, under an explicit list of allowed differences.

A replay comparison fails on any change in exit code, decision, rewritten input or timeout, and on a changed
refusal text (stderr of an exit-2 block) or added context after path normalisation. A snapshot comparison
fails on a hook, skill or agent that appears, disappears or changes content without being allowed, and on a
name reference that resolved before and no longer does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from setupctl.replay import Outcome

BLOCK_EXIT = 2  # fact: hooks.md, exit code 2 blocks the tool call and feeds stderr to Claude


@dataclass(frozen=True)
class AllowedDiffs:
    changed: frozenset[str] = frozenset()  # names whose content may change (deliberate edits)
    removed: Mapping[str, str] = field(default_factory=dict)  # name or hook_id -> reason it may disappear
    normalise: tuple[tuple[str, str], ...] = ()  # (regex, replacement) applied to texts before comparing


@dataclass
class DiffReport:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def load_allowed(path: Path | None, normalise: Sequence[tuple[str, str]]) -> AllowedDiffs:
    if path is None:
        return AllowedDiffs(normalise=tuple(normalise))
    import tomllib

    data = tomllib.loads(path.read_text())
    return AllowedDiffs(
        changed=frozenset(data.get("changed", [])),
        removed=dict(data.get("removed", {})),
        normalise=tuple(normalise) + tuple(tuple(p) for p in data.get("normalise", [])),
    )


def _norm(text: str | None, allowed: AllowedDiffs) -> str:
    out = text or ""
    for pattern, repl in allowed.normalise:
        out = re.sub(pattern, repl, out)
    return out


def diff_outcomes(old: Sequence[Outcome], new: Sequence[Outcome], allowed: AllowedDiffs) -> DiffReport:
    report = DiffReport()
    a = {(o.hook_id, o.item_id): o for o in old}
    b = {(o.hook_id, o.item_id): o for o in new}
    for hook in sorted({h for h, _ in a} - {h for h, _ in b}):
        if hook in allowed.removed:
            report.notes.append(f"removed as allowed: {hook} ({allowed.removed[hook]})")
        else:
            report.failures.append(f"hook missing in new run: {hook}")
    for hook in sorted({h for h, _ in b} - {h for h, _ in a}):
        report.failures.append(f"hook only in new run: {hook}")
    flaky: set[str] = set()
    for key in sorted(a.keys() & b.keys()):
        x, y = a[key], b[key]
        if x.nondeterministic or y.nondeterministic:
            flaky.add(key[0])
            continue
        where = f"{key[0]} on item {key[1]}"
        if (x.exit_code, x.decision, x.timed_out) != (y.exit_code, y.decision, y.timed_out):
            report.failures.append(f"{where}: exit/decision {x.exit_code}/{x.decision} -> {y.exit_code}/{y.decision}")
        elif json.dumps(x.updated_input, sort_keys=True) != json.dumps(y.updated_input, sort_keys=True):
            report.failures.append(f"{where}: rewritten input differs")
        elif _norm(x.additional_context, allowed) != _norm(y.additional_context, allowed):
            report.failures.append(f"{where}: added context differs")
        elif x.exit_code == BLOCK_EXIT and _norm(x.stderr_head, allowed) != _norm(y.stderr_head, allowed):
            report.failures.append(f"{where}: refusal text differs")
    for hook in sorted(flaky):
        report.notes.append(f"nondeterministic, left to the live-fire: {hook}")
    return report


def _hook_key(h: Mapping[str, Any]) -> tuple[str, str, str, str]:
    script = h.get("resolved_script") or h["command"]
    return (h["settings_path"], h["event"], h["matcher"], Path(script.split()[-1]).name)


def diff_snapshots(old: Mapping[str, Any], new: Mapping[str, Any], allowed: AllowedDiffs) -> DiffReport:
    """Compare two snapshot JSON documents (as written by ``write_snapshot``)."""
    report = DiffReport()
    for label, key_of, sha in (
        ("hook", _hook_key, "sha256"),
        ("component", lambda c: (c["kind"], c["name"]), "tree_sha256"),
    ):
        section = "hooks" if label == "hook" else "components"
        a = {key_of(x): x for x in old[section]}
        b = {key_of(x): x for x in new[section]}
        for k in sorted(a.keys() - b.keys()):
            name = k[-1]
            if name in allowed.removed:
                report.notes.append(f"{label} removed as allowed: {k} ({allowed.removed[name]})")
            else:
                report.failures.append(f"{label} missing after: {k}")
        for k in sorted(b.keys() - a.keys()):
            report.failures.append(f"{label} only after: {k}")
        for k in sorted(a.keys() & b.keys()):
            if a[k][sha] != b[k][sha] and k[-1] not in allowed.changed:
                report.failures.append(f"{label} content changed without being allowed: {k}")
    resolved_before = {r["name"] for r in old["references"] if r["resolves_to"]}
    resolved_after = {r["name"] for r in new["references"] if r["resolves_to"]}
    for name in sorted(resolved_before - resolved_after):
        report.failures.append(f"reference no longer resolves: {name}")
    return report
