"""Replay corpus tool calls through hook registrations; record each decision.

Isolation comes from the caller: `setupctl replay` re-runs itself inside `isolation.container_command`, so every
hook here sees a read-only host with empty writable scratch directories. Each call also gets a fresh
CLAUDE_CONFIG_DIR, so a hook's own log and state never carry from one call to the next.

Payload fields and matcher rules follow https://code.claude.com/docs/en/hooks.md (fetched 2026-11-22):
"Common input fields" (session_id, transcript_path, cwd, permission_mode, hook_event_name), the PreToolUse
example (tool_name, tool_input, tool_use_id), PostToolUse (tool_response) and PostToolUseFailure (error,
is_interrupt), and the "Matcher patterns" table (empty or "*" = all; only letters, digits, _, -, spaces, ","
and "|" = exact names; anything else = unanchored regular expression; a "Bash" matcher on PreToolUse also
matches Monitor commands).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from setupctl.corpus import CorpusItem
from setupctl.snapshot import HookReg

# Fact: the events whose input is a tool call, so a recorded call can be replayed into them.
REPLAYABLE_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
_EXACT_MATCHER = re.compile(r"[A-Za-z0-9_\- ,|]*")  # fact: hooks.md "Matcher patterns" exact-string set
_READ_ONLY_MARK = "Read-only file system"  # fact: the strerror text for EROFS
_CAGE_PART = re.compile(r"^(.*/\.claude[^/]*)/projects/")  # fact: transcripts live under <config dir>/projects/
_HEAD_CHARS = 400  # how much stdout/stderr an outcome keeps; enough to compare refusal text


@dataclass(frozen=True)
class SandboxConfig:
    per_call_timeout_s: float
    workers: int
    permission_mode: str
    call_root: str  # where each call's fresh CLAUDE_CONFIG_DIR is made; must be writable


@dataclass(frozen=True)
class Outcome:
    hook_id: str
    item_id: str
    exit_code: int
    decision: str | None
    updated_input: dict[str, Any] | None
    additional_context: str | None
    stderr_head: str
    timed_out: bool
    env_limited: bool
    nondeterministic: bool = False


def hook_id(reg: HookReg) -> str:
    """Identity that survives a file moving behind a symlink: event, matcher, script basename."""
    script = reg.resolved_script or reg.command
    return f"{reg.event}|{reg.matcher}|{Path(script.split()[-1]).name}"


def matcher_matches(matcher: str, tool_name: str, event: str) -> bool:
    if matcher in ("", "*"):
        return True
    if _EXACT_MATCHER.fullmatch(matcher):
        names = {n.strip() for n in re.split(r"[|,]", matcher) if n.strip()}
        return tool_name in names or (event == "PreToolUse" and tool_name == "Monitor" and "Bash" in names)
    return re.search(matcher, tool_name) is not None


def payload(item: CorpusItem, event: str, permission_mode: str) -> dict[str, Any] | None:
    """The stdin JSON Claude Code would send for ``item`` on ``event``; None when the item cannot feed it."""
    base: dict[str, Any] = {
        "session_id": item.session_id,
        "transcript_path": item.transcript_path,
        "cwd": item.cwd,
        "permission_mode": permission_mode,
        "hook_event_name": event,
        "tool_name": item.tool_name,
        "tool_input": item.tool_input,
        "tool_use_id": item.tool_use_id,
    }
    if event == "PreToolUse":
        return base
    if event == "PostToolUse":
        return None if item.failed or item.tool_response is None else {**base, "tool_response": item.tool_response}
    if event == "PostToolUseFailure":
        if not item.failed:
            return None
        error = (item.tool_response or {}).get("content", "")
        return {**base, "error": error if isinstance(error, str) else json.dumps(error), "is_interrupt": False}
    return None


def _parse_stdout(stdout: str) -> tuple[str | None, dict[str, Any] | None, str | None]:
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(out, dict):
        return None, None, None
    specific = out.get("hookSpecificOutput") or {}
    decision = specific.get("permissionDecision") or out.get("decision")
    return decision, specific.get("updatedInput"), specific.get("additionalContext")


def _call_dir(root: Path, item: CorpusItem) -> Path:
    """A fresh CLAUDE_CONFIG_DIR holding a copy of the transcript's cage settings.json."""
    d = Path(tempfile.mkdtemp(dir=root))
    m = _CAGE_PART.match(item.transcript_path)
    if m and Path(m.group(1), "settings.json").is_file():
        shutil.copy2(Path(m.group(1), "settings.json"), d / "settings.json")
    return d


def _run_one(
    reg: HookReg, item: CorpusItem, data: dict[str, Any], env: Mapping[str, str], sb: SandboxConfig, root: Path
) -> Outcome:
    call_dir = _call_dir(root, item)
    call_env = {**env, "CLAUDE_CONFIG_DIR": str(call_dir), "CLAUDE_PROJECT_DIR": item.cwd or "/"}
    try:
        p = subprocess.run(["bash", "-c", reg.command], input=json.dumps(data), capture_output=True, text=True,
                           timeout=sb.per_call_timeout_s, env=call_env, check=False,
                           cwd=item.cwd if item.cwd and Path(item.cwd).is_dir() else "/")
    except subprocess.TimeoutExpired:
        return Outcome(hook_id(reg), item.item_id, -1, None, None, None, "", True, False)
    finally:
        shutil.rmtree(call_dir, ignore_errors=True)
    decision, updated, context = _parse_stdout(p.stdout)
    return Outcome(hook_id(reg), item.item_id, p.returncode, decision, updated, context,
                   p.stderr[:_HEAD_CHARS], False, _READ_ONLY_MARK in p.stderr)


def replay(
    regs: Sequence[HookReg],
    items: Sequence[CorpusItem],
    sandbox: SandboxConfig,
    settings_env: Mapping[str, str],
    repeat: int = 1,
) -> list[Outcome]:
    """Run every (registration, matching item) pair ``repeat`` times; flag pairs whose runs disagree."""
    jobs = []
    for reg in regs:
        if reg.event not in REPLAYABLE_EVENTS:
            continue
        for item in items:
            if matcher_matches(reg.matcher, item.tool_name, reg.event):
                data = payload(item, reg.event, sandbox.permission_mode)
                if data is not None:
                    jobs.append((reg, item, data))
    env = {**os.environ, **settings_env}
    runs: list[list[Outcome]] = []
    with (
        tempfile.TemporaryDirectory(prefix="setupctl-replay-", dir=sandbox.call_root) as tmp,
        ThreadPoolExecutor(sandbox.workers) as pool,
    ):
        for _ in range(max(1, repeat)):
            runs.append(list(pool.map(lambda j: _run_one(*j, env, sandbox, Path(tmp)), jobs)))
    first = runs[0]
    return [
        replace(o, nondeterministic=any(_verdict(r[i]) != _verdict(o) for r in runs[1:]))
        for i, o in enumerate(first)
    ]


def _verdict(o: Outcome) -> tuple[Any, ...]:
    return (o.exit_code, o.decision, json.dumps(o.updated_input, sort_keys=True), o.timed_out)


def write_outcomes(outcomes: Sequence[Outcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for o in outcomes:
            f.write(json.dumps(asdict(o), sort_keys=True) + "\n")


def read_outcomes(path: Path) -> list[Outcome]:
    with path.open() as f:
        return [Outcome(**json.loads(line)) for line in f]
