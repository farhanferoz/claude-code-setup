# claude-code-setup

This repository maps one working Claude Code setup onto the public repositories that provide each part of it, and
installs any or all of those parts with one command. Each part lives in its own repository and installs on its own.
This repository holds the map, a version-pinned manifest (`manifest.toml`), the install command and a `doctor` that
checks the result.

## Reproduce everything

```sh
git clone https://github.com/farhanferoz/claude-code-setup
cd claude-code-setup
./install --dry-run        # print every command, run nothing
./install                  # the core pieces
./install --all            # the core pieces and the optional ones
python3 -m setupctl doctor # check each installed piece and name the install line of any that failed
```

Install these first: Python 3.11 or newer, `git`, [`uv`](https://docs.astral.sh/uv/), `jq` (ccage's installer
stops without it), and a C/C++ compiler (corpus-search builds `llama-cpp-python`, which PyPI ships only as source).
On Debian or Ubuntu: `sudo apt install git python3 jq build-essential`; on macOS: `xcode-select --install` and
`brew install jq`. `./install` reuses a clone under `~/dev/<name>` when the clone's `origin` matches the
manifest (change the folder with `--dev-root`), and otherwise clones the pinned version under
`~/.local/share/claude-code-setup/src` (`--src-root`). `--only NAME` and `--skip NAME` select pieces. The command
never edits a shell start-up file itself. ccage's own installer appends one sourcing loop to `~/.bashrc` or
`~/.zshrc` (see the ccage README).

The Claude Code plugins come from the marketplace in
[claude-skills](https://github.com/farhanferoz/claude-skills). `./install` adds the marketplace. Then pick the
plugins inside Claude Code, for example `/plugin install guards@claude-skills`.

## The map

Each row is one practice from the talk, the repository and file that provide it, and how to install that piece
alone. A **hook** is a script that Claude Code runs on an event (session start, before a tool call, after an edit)
and that can allow, block, rewrite or feed back. A **rule** is written text the model reads and can ignore. A
**skill** is a procedure the model loads on demand.

| Practice | Kind | Where it lives | Install alone |
|---|---|---|---|
| **Context hygiene and running unattended** | | | |
| Checkpoint before `/clear`: state and next steps in `RESUME.md`, rulings in `DECISIONS.md` | skill | [ccage](https://github.com/farhanferoz/ccage) `share/skills/checkpoint` | ccage `./install.sh`, or `/plugin install session-continuity@claude-skills` (ccage `plugin/`) |
| Read `RESUME.md`, `DECISIONS.md` and the plans back after every `/clear`, in parts when large | hook | ccage `share/hooks/resume_autoload.sh`, `share/hooks/session_doc_chunk.sh` | ccage, or `/plugin install session-continuity@claude-skills` (RESUME and DECISIONS only) |
| Keep `RESUME.md` small and refuse a write that drops open next steps | hook | ccage `share/hooks/resume_budget_check.sh` | ccage |
| A plan box ticks only when its check passes | hook | ccage `share/hooks/plan_contract_guard.sh`, `lib/ccage_plan.py` | ccage |
| Automatic checkpoint, clear and resume at a context threshold | tool | ccage `bin/ccage-auto` | ccage |
| **Guardrails** | | | |
| Guards that block with the fix, rewrite, or feed back, each logging every decision | hook | [claude-skills](https://github.com/farhanferoz/claude-skills) `plugins/guards` | `/plugin install guards@claude-skills` |
| One set of policy hooks in every project | tool | ccage, with `CCAGE_SEED_LOCAL_HOOKS=1` (ccage `docs/SEED-LOCAL-HOOKS.md`) | ccage |
| **Organisation** | | | |
| Project layout: `RESUME.md`, `DECISIONS.md`, `CHANGELOG.md`, `plans/` with `archive/` | skill | ccage `share/skills/checkpoint` (creates the files in a new project) | ccage, or `/plugin install session-continuity@claude-skills` |
| Session start reports plans that fell behind the disk, the rulings or `RESUME.md` | hook | ccage `lib/ccage_plan.py` | ccage |
| Scratch folders pruned at session start | hook | claude-skills `plugins/session-hooks/hooks/session_scratch_prune.sh` | `/plugin install session-hooks@claude-skills` |
| Refuse files with junk names in a repository | hook | claude-skills `plugins/guards/hooks/clutter_guard.sh` | `guards` plugin |
| One plan for a long programme with several strands | skill | claude-skills `plugins/programme-plan` | `/plugin install programme-plan@claude-skills` |
| **Delegation** | | | |
| Plans with exact files, tests and non-goals, run in dependency waves | skill | claude-skills `plugins/orchestration` (`writing-plans`, `subagent-driven-development`, `executing-plans`) | `/plugin install orchestration@claude-skills` |
| Workers pinned to a model and effort, and the policy for choosing one | agent | claude-skills `plugins/workers` (`agents/`, `reference/model-routing-policy.md`) | `/plugin install workers@claude-skills` |
| A dispatch needs a stated reason, and fan-outs have a cap | hook | claude-skills `plugins/guards/hooks/agent_dispatch_guard.sh`, `agent_fanout_cap.sh`, `workflow_budget_guard.sh` | `guards` plugin |
| Parallel workers never write the same file | hook | claude-skills `plugins/orchestration/hooks/write_set_guard.sh` | `orchestration` plugin |
| **Cost and context** | | | |
| Spend per session, day and tool | tool | [tokenol](https://github.com/farhanferoz/tokenol) | `uv tool install --editable '.[serve]'` in a clone |
| Cost of each subagent, and of the last fan-out at the next session start | skill, hook | claude-skills `plugins/orchestration/skills/agent-cost`, `plugins/session-hooks/hooks/cost_report.sh` | `orchestration` and `session-hooks` plugins |
| Keep the prompt cache warm through an idle gap | skill | ccage `share/skills/keepwarm` | ccage, or `/plugin install session-continuity@claude-skills` |
| Status line: context used, the 5-hour and 7-day limits, cache state, open plan items | tool | claude-skills `plugins/session-hooks/scripts/statusline-command.sh` | copy it and set `statusLine` (session-hooks README) |
| Find skills nobody uses, so they can be switched off | tool | claude-skills `plugins/session-hooks/scripts/skill_usage_inventory.py` | `session-hooks` plugin |
| **Code quality** | | | |
| One coding standard in every session | hook, rule | claude-skills `plugins/coding-standard` (`reference/coding-standards.md`) | `/plugin install coding-standard@claude-skills` |
| Feedback after each edit: unnamed constants, copied blocks, structure | hook | claude-skills `plugins/guards/hooks/code_hygiene_check.sh`, `scripts/dupcheck.py`, `scripts/structure_ratchet.py` | `guards` plugin |
| Test limits: one broad run per session, tests that must be able to fail | hook | claude-skills `plugins/guards/hooks/test_scope_guard.sh`, `test_growth_check.py` | `guards` plugin |
| Review sized by commit count and the surface touched | skill | claude-skills `plugins/review-suite` (`tiered-review`) | `/plugin install review-suite@claude-skills` |
| Simplify before review | skill | Claude Code's built-in `/simplify` | none |
| **Research quality** | | | |
| Figure workflow, methodology review, checks before a long training run, summaries grounded in notes | skill | claude-skills `plugins/research-method` | `/plugin install research-method@claude-skills` |
| Talks and slide decks: message and audience first, one prototype slide, sourced numbers, render and look, cold read | skill, tool | claude-skills `plugins/talk-deck` | `/plugin install talk-deck@claude-skills` |
| Research report as a self-contained chapter with a cold read | skill | claude-skills `plugins/research-report` | `/plugin install research-report@claude-skills` |
| Search your own notes, PDFs and notebooks | tool | [corpus-search](https://github.com/farhanferoz/corpus-search) | `uv tool install --editable .` in a clone |
| Simulated review panel for your own draft application | skill | claude-skills `plugins/review-panel` | `/plugin install review-panel@claude-skills` |
| Deep research with a cheaper model tier and three-way verification | skill | claude-skills `plugins/deep-research-tiered` | `/plugin install deep-research-tiered@claude-skills` |
| **Long jobs** | | | |
| Submit GPU and large jobs through slurm | tool | [preflight](https://github.com/alan-turing-institute/preflight) | see its README |
| Long commands go to the background, and a `timeout` gets a hard kill | hook | claude-skills `plugins/orchestration/hooks/foreground_long_job_guard.sh`, `poll_loop_guard.sh`, `plugins/guards/hooks/timeout_kill_guard.sh` | `orchestration` and `guards` plugins |
| A watcher that outlives the session and writes the result into `RESUME.md` | tool | ccage `bin/ccage-watch` | ccage |
| An idle session is asked, then checkpointed and resumed, then stopped | tool | ccage `bin/ccage-auto` | ccage |
| Watch a job from the main session | skill | claude-skills `plugins/orchestration/skills/watch-job` | `orchestration` plugin |
| **Local models** | | | |
| One place for each model's load settings, serving and downloads | tool | [localmodels](https://github.com/farhanferoz/localmodels) | `uv tool install --editable .` in a clone |

### Optional pieces

`./install --all` adds these. [ogc](https://github.com/farhanferoz/ogc) lets Claude Code use an OpenCode Go
subscription. [playwright-agents](https://github.com/farhanferoz/playwright-agents) wraps Playwright's test
agents. [training-supervisor-plugin](https://github.com/farhanferoz/training-supervisor-plugin) monitors GPU training jobs.
[devbox-kit](https://github.com/farhanferoz/devbox-kit) holds tools for working on a remote Linux workstation from a
laptop, and [fleet-monitoring](https://github.com/farhanferoz/fleet-monitoring) gives Grafana dashboards and alerts for
one machine or many. `./install` prints the links of these three rather than running their installers: ogc builds
with Go, and devbox-kit and fleet-monitoring need per-machine settings first.

## Add your own private layer

The published pieces hold no personal values. Each per-person value is a plugin setting (`userConfig`), which Claude
Code passes to the plugin's hooks as the environment variable `CLAUDE_PLUGIN_OPTION_<KEY>`. Three files stay
yours:

- `~/.claude/CLAUDE.md` holds your own rules. Some practices in the talk are written rules of this kind (the analysis
  checks before a causal conclusion, the disk ledger, the worker review rule), so they are not published as code.
- `~/.claude/settings.json` holds your permissions and an `env` block, which can set the same
  `CLAUDE_PLUGIN_OPTION_<KEY>` values for hooks you register yourself.
- `~/.claude/skills/` holds skills you keep to yourself. A plain skill and a plugin skill of the same name both load,
  because plugin skills carry the plugin's name as a prefix.

## Third-party skills we use

We link these to their upstream repositories rather than republishing them.

- [obra/superpowers](https://github.com/obra/superpowers): `brainstorming`, `systematic-debugging`,
  `test-driven-development`, `verification-before-completion`, `using-git-worktrees`, `dispatching-parallel-agents`,
  `requesting-code-review`, `finishing-a-development-branch`, `writing-skills`. The `orchestration` plugin carries
  our changed forks of `writing-plans`, `executing-plans` and `subagent-driven-development`, with the upstream MIT
  notice.
- [getsentry/skills](https://github.com/getsentry/skills): `commit`, `pr-writer`, `code-review`, `find-bugs`,
  `security-review`, `create-branch`, `iterate-pr`, `skill-writer` and others.
- [trailofbits/skills](https://github.com/trailofbits/skills): `differential-review`, `fp-check`, `second-opinion`,
  `property-based-testing`, `audit-context-building`, `codeql`.
- [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch): the `autoresearch` loops.
- [microsoft/playwright](https://github.com/microsoft/playwright): the Playwright test agents.

## How the pieces are checked

`setupctl` also checks that a change to the setup changes no behaviour. `setupctl snapshot` records which hooks,
skills and agents a machine runs. `setupctl corpus build` samples real past tool calls from Claude Code transcripts,
and `setupctl replay` feeds them to every hook in a write-blocking container. `setupctl diff` then compares two
snapshots or two replays and exits non-zero on any changed decision.

## Licence

MIT. See `LICENSE`.
