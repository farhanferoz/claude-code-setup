"""Build a replay corpus from Claude Code transcripts: real tool calls, deduplicated, sampled per tool.

Transcripts hold every tool call's full input, unlike guard logs (which truncate). The corpus is private
data (tool inputs can hold secrets), so it is written with mode 0600 and never committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Fact: the substrings a transcript line must contain to carry a tool call or a tool result.
_TOOL_MARKERS = ('"tool_use"', '"tool_result"')


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    tool_name: str
    tool_input: dict[str, Any]
    cwd: str
    session_id: str
    transcript_path: str
    tool_use_id: str
    tool_response: dict[str, Any] | None
    failed: bool


@dataclass
class CorpusStats:
    files: int = 0
    lines_bad: int = 0
    calls_seen: int = 0
    calls_unique: int = 0
    per_tool: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SamplePlan:
    per_tool_default: int
    per_tool: Mapping[str, int]
    seed: int

    def size_for(self, tool: str) -> int:
        return self.per_tool.get(tool, self.per_tool_default)


def _calls_in(path: Path, stats: CorpusStats) -> list[CorpusItem]:
    """Every tool call in one transcript, paired with its result when the transcript has it."""
    uses: dict[str, tuple[str, dict[str, Any], str, str]] = {}
    results: dict[str, tuple[dict[str, Any] | None, bool]] = {}
    with path.open(errors="replace") as f:
        for line in f:
            if not any(m in line for m in _TOOL_MARKERS):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                stats.lines_bad += 1
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and isinstance(c.get("input"), dict) and c.get("id"):
                    uses[c["id"]] =(c.get("name", ""), c["input"], d.get("cwd", ""), d.get("sessionId", ""))
                elif c.get("type") == "tool_result":
                    structured = d.get("toolUseResult")
                    resp = structured if isinstance(structured, dict) else {"content": c.get("content")}
                    results[c.get("tool_use_id", "")] = (resp, bool(c.get("is_error")))
    items = []
    for use_id, (name, tool_input, cwd, sid) in uses.items():
        resp, failed = results.get(use_id, (None, False))
        key = hashlib.sha256((name + "\0" + json.dumps(tool_input, sort_keys=True)).encode()).hexdigest()[:16]
        items.append(CorpusItem(key, name, tool_input, cwd, sid, str(path), use_id, resp, failed))
    return items


def build_corpus(transcripts: Iterable[Path], plan: SamplePlan) -> tuple[list[CorpusItem], CorpusStats]:
    """Deduplicate calls by (tool, input) across all transcripts; reservoir-sample up to the plan's size per tool.

    Deterministic for one seed: files are read in sorted order.
    """
    rng = random.Random(plan.seed)
    stats = CorpusStats()
    seen: set[str] = set()
    pools: dict[str, list[CorpusItem]] = {}
    counts: dict[str, int] = {}
    for path in sorted(set(transcripts)):
        stats.files += 1
        for item in _calls_in(path, stats):
            stats.calls_seen += 1
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            n = counts[item.tool_name] = counts.get(item.tool_name, 0) + 1
            pool = pools.setdefault(item.tool_name, [])
            k = plan.size_for(item.tool_name)
            if len(pool) < k:
                pool.append(item)
            elif (j := rng.randrange(n)) < k:
                pool[j] = item
    stats.calls_unique = len(seen)
    stats.per_tool = {t: len(p) for t, p in sorted(pools.items())}
    return [i for t in sorted(pools) for i in pools[t]], stats


def write_corpus(items: list[CorpusItem], stats: CorpusStats, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = out_dir / "items.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for item in items:
            f.write(json.dumps(asdict(item), sort_keys=True) + "\n")
    (out_dir / "stats.json").write_text(json.dumps(asdict(stats), indent=2, sort_keys=True) + "\n")


def read_corpus(out_dir: Path) -> list[CorpusItem]:
    with (out_dir / "items.jsonl").open() as f:
        return [CorpusItem(**json.loads(line)) for line in f]
