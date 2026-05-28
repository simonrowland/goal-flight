# goal-flight

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Last commit](https://img.shields.io/github/last-commit/simonrowland/goal-flight)
![Stars](https://img.shields.io/github/stars/simonrowland/goal-flight)

goal-flight is a multi-agent controller, which delegates coding /goal and parallel-review work to additional agent sessions. It lets you hand a frontier model a large software task to break down into closed chunks, and keep moving after the first context window would normally fall apart. It turns the work into durable project files: a plan, queue, environment caveats, worker status, review evidence, and resume notes that survive compaction, restarts, and overnight runs. Multi-hour runs can land as a clean stack of one-commit-per-chunk on main, with integrated self-reviews leveraging Gstack.

**Controller hosts.** [Claude Code](https://claude.ai/code) is the reference controller. Goal Flight also ships controller ports for [Codex](https://github.com/openai/codex), [Cursor](https://cursor.com), and [OpenCode](https://opencode.ai) — same `SKILL.md`, file-backed queue, and dispatch machinery, with host-specific install wrappers below. Workers include codex, cursor, grok, claude-cli, and other ACP or bash-tail adapters.

**What the controller is for**: high-level management, not execution. The controller holds enough context about your project's goal, scenery (constraints, architecture, prior decisions, failure modes), and intent to exercise discretion and recommend the next move — then dispatches actual work to workers to run in an iterative code-review goal loop. This workflow allows lightly-supervised coding: you check in, ratify suggested moves, redirect when needed, and trust the controller to keep the project anchored across compactions and unattended hours. The dispatch / review / handoff machinery below is what frees the controller to do that job.

[Features](#features) • [Quickstart](#quickstart) • [Architecture](docs/architecture.md) • [Commands](#commands)

```bash
# Claude Code (reference controller):
git clone https://github.com/simonrowland/goal-flight.git ~/.claude/skills/goal-flight

# Codex / Cursor / OpenCode — clone once, then one command per host (global + project):
git clone https://github.com/simonrowland/goal-flight.git ~/.goal-flight && cd ~/.goal-flight
./install.sh cursor /path/to/your/project
./install.sh opencode /path/to/your/project
./install.sh codex
```

Restart the host, then run doctor: `python3 scripts/goalflight_doctor.py --project-root /path/to/your/project`.

Same flags via `setup.sh`: `--cursor-install`, `--opencode-install`, and `--codex-install` (each implies `--apply --yes`). Dry-run, link-to-Claude, and agents-standard paths are in [docs/hosts/cursor.md](docs/hosts/cursor.md) and [docs/hosts/opencode.md](docs/hosts/opencode.md).

## Features

- Multi-hour unattended runs with light supervision
- Verification-first dispatch (live files only)
- Parallel cross-agent reviews (Claude + another model via gstack)
- Two-axis routing (iteration pattern × comms shape)
- Provider-aware rate-pressure walkback
- Procedural runtime state + doctor checks
- Self-delegation `/fork` pattern (opt-in)
  
## What it gets you

- **Multi-hour unattended runs.** Check in periodically or respond to decision notifications. The controller's context primarily holds architecture, plan, and metadata (queue state, recent commits, in-flight dispatch headers); real work happens in subagent context windows.
- **Verification-first dispatch.** Wrappers point at files for the agent to investigate, not pre-pasted "facts" that go stale on the timescale of minutes. Frontier models trust controller-text uncritically; pointers force them to re-verify against live disk and surface drift.
- **Parallel cross-agent reviews at milestone cadence.** Two independent reviewers (Claude + codex) address bugs and completion before pestering you. Via [gstack](https://github.com/garrytan/gstack)'s `/review` skill when installed.
- **/goal native.** the controller picks from per chunk. **Iteration pattern** (one-shot for most chunks, goal-mode loop for chunks that need plan/act/test/review-to-convergence, controller-direct for trivial work)
- **Token Management.** Throw tokens at your problem for better code and less babysitting, but divide the usage and rate-limits between multiple agent vendors, task-by-task.
- **Fancy Monitoring.** ACP for structured events from claude-cli, cursor, codex, grok. Uses bash-tail as fallback and to support generic agents. Controller handles worker notifications, and can escalate messages to the user.
- **Rate-limit walkback.** `goalflight_rate_pressure.py` watches the dispatch ledger for provider-level rate-limit signatures and surfaces a STATUS marker + recommended fallback when pressure crosses threshold. It tracks how many workers are on your machine to be mindful of capacity limits.
- **Procedural runtime state.** Capacity, dispatch ledgers, compact status, log watching, doctor checks, ACP runs, rate-pressure detection, and file-backed review jobs live under `scripts/goalflight_*.py`. The doctor checks for cli-agent updates, so you can run the worker update command.

## How it differs from the alternatives

- vs. **running one host controller naively** — the controller doesn't itself do the work. It dispatches and verifies, which means it stays small and runs longer before compaction.
- vs. **cloud agent swarms or editor agents** — runs on your machine, in your controller host, with your existing local tools and adapters. It brings the benefits of a Claw team to your desktop.
- vs. **writing prompts manually** — make the plan, not the code. The skill asks a frontier model to decompose your plan into chunks, flagging what can run in parallel and what can have a /goal pattern. Every `/goal` reviews to convergence by default; the 7-category adversarial self-review runs inside the loop until reviews pass, so the controller never sees a non-converged result.

## Quickstart

```bash
# In your project repo, in a controller session (Claude Code wrapper shown):
/goal-flight init <topic>         # audit repo, scaffold AGENTS.md/SKILL.md + docs-private/,
                                  # build optional RAG corpus, register codex-trust,
                                  # probe box capacity + ACP-worker availability →
                                  # docs-private/env-caveats.md
/goal-flight decompose-plan       # break the plan into /goal chunks (SCOPE / CHECKLIST
                                  # / ACCEPTANCE / FORBIDDEN), parallel reviewer pass
/goal-flight execute              # per-chunk dispatch loop (ACP when available, else
                                  # Bash-&-tail-file), embedded self-review,
                                  # milestone reviewer examples every K commits
/goal-flight doctor               # validate wrapper/package health, companion tools,
                                  # codex trust, context-mode, gstack, autoreview, ACP +
                                  # surface model currency + rate-pressure
/goal-flight update               # pull latest goal-flight from origin + run
                                  # each worker CLI's self-update (codex /
                                  # grok / cursor-agent / claude / -cli-acp)
/goal-flight resume               # rebuild RESUME-NOTES from current git state
                                  # (use when picking up across sessions)
```

> **Working signal, not rigid gates**: the skill pins a `goal-<topic>-<date>.md` file at init for compaction-survival, but it's an anchor — not a contract. `decompose-plan` proceeds on whatever signal exists (the goal-statement when present, or the plan source, architecture doc, and in-session conversation), surfacing any inferred assumptions as inline-office-hours backlog items the user can validate during the run. Show up with "here's my architecture doc plus ten minutes of context-setting chat" and the skill takes it from there. The premises file accumulates validated answers as the run progresses. **DRAFT goal-statement is fine** — `decompose-plan` proceeds anyway; sharpen any time by editing `docs-private/goal-<topic>-<date>.md` directly.

`/goal-flight` with no args prints `SKILL.md` — the full pattern reference.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight init <topic>` | Tool check, repo audit, scaffold, codex-trust registration |
| `/goal-flight decompose-plan [<plan>]` | Break a plan into `/goal` chunks with parallel reviewer pass |
| `/goal-flight ask-questions [<scope>]` | Anticipatory subagents; surface clarifying questions |
| `/goal-flight execute [--parallel <N>]` | Per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight doctor` | Read-only health check for plugin/package/runtime readiness, model currency, rate-pressure |
| `/goal-flight update` | Pull latest goal-flight from origin + run each worker CLI's self-update |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the optional RAG corpus |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue |
| `/goal-flight register-codex [<path>]` | Register a project as codex-trusted |
| `/goal-flight validate-dispatch [<slug>]` | Render a chunk's dispatch wrapper without dispatching |
| `/goal-flight validate-queue [<path>]` | Schema-check the goal-queue |

Plus an opt-in self-delegation pattern via `/fork` — controller writes a marker contract; forked session detects via env var and follows the contract; controller monitors compact status. See `protocols/self-delegation.md`; fork instructions are not always-loaded.

Detailed operating procedures are split into load-on-demand files under
`protocols/`. The always-loaded `SKILL.md` is intentionally small.

## Dispatch routing (two orthogonal axes)

**Iteration pattern** — how many turns the chunk needs:

| Pattern | When | Cost |
|---|---|---|
| **`controller-direct`** | Trivially small (single-file, < ~30 LoC), OR controller already has the session-loaded context a fresh subagent would have to re-discover | Inline; no subagent |
| **one-shot subagent** | Default for most chunks. Frontier model picks the executor target based on chunk shape | One subagent dispatch per chunk |
| **goal-mode loop** | Multi-step refactor, code migration, prototype implementation, converge code to ground-truth — anything that benefits from plan/act/test/review-to-convergence | Multi-hour autonomous session (codex `/goal` natively, or controller-driven iteration loop) |

**Comms shape** (orthogonal axis) — how the controller observes the worker. Goal-flight uses the [Agent Client Protocol](https://agentclientprotocol.com) wherever the worker has an adapter (codex / cursor / claude / grok all do today); bash-tail with a `tail -f`-style marker-grep watcher is the cold-storage fallback. ACP composes with `goal-mode` for any worker; `goal-mode + bash-tail` composes only with codex `/goal` today (codex emits a Final-response marker the watcher detects; other workers' headless modes don't).

The controller picks executor + comms per chunk based on chunk shape, available adapters, and the rate-pressure walkback's recent observations. The shipped routing defaults lean toward sub-billed workers (codex / cursor / grok) for code-writing — calibrated against the maintainer's current vendor plans, not a project-wide prescription. Adjust to your environment by editing the routing table in `SKILL.md` "Worker Routing"; the walkback adapts dynamically when any one provider gets pressured.

## Multi-node fleet (1.0)

For remote workers over SSH, bootstrap a fleet store and use `goalflight_fleet.py`
dispatch / watch / reconcile. OpenCode, Cursor, Codex, and Claude ACP workers
can run on registered nodes while the controller stays local. See
[docs/fleet.md](docs/fleet.md) for the operator guide; live smoke:
`GOALFLIGHT_LIVE_SSH=1 ./tests/manual/test_fleet_live_smoke.sh`.

Unified CLI: `bin/goalflight <domain> <resource> <verb>` (action router over
`config/actions/`).

## When NOT to use this

- **One-off scripts or quick fixes.** Overhead unjustified for <8 chunks or <2000 LoC delta. Pre-flight gates auto-skip the RAG corpus for projects this small.
- **Pair-programming sessions.** Designed for unattended runs. If you're steering every turn, the wrapper overhead just slows you down.
- **Sensitive operations the controller shouldn't autonomously trigger.** Production deploys, prod data writes, credential rotations — human-in-the-loop is the point.
- **Projects without test signal.** Self-review and milestone-review depend on tests / grep invariants / verification commands existing.

## Companion tools (strongly recommended)

- **[gstack](https://github.com/garrytan/gstack)** — Garry Tan's skill pack provides `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate` for both Claude Code and codex. Goal-flight invokes `/review` as the **default independent reviewer** for chunk-level pre-commit review (`protocols/chunk-review.md`) and for milestone reviews (`protocols/milestone-review.md`, gstack + concern-diverse sweep); `/office-hours` covers fuzzy-goal interrogation at init. **Optional** — without gstack, goal-flight falls back to local prompts at `prompts/gstack-claude-review.md` + `prompts/gstack-codex-challenge.md` (and embedded executor self-review still catches most issues). With gstack installed, you get consistent severity-ranking framing across both review lenses, which is meaningfully higher quality on long runs.
- **autoreview** — Complementary diff-local pre-commit pass (`protocols/chunk-review.md`, `./scripts/autoreview.sh`). Runs in parallel with gstack at chunk level when the controller chooses; does **not** replace gstack as the default review path. Catches diff-local issues (API footguns, missing tests on touched paths, regression invariants) that a structural reviewer may not prioritize. Requires upstream autoreview (typically the Cursor autoreview skill or `AUTOREVIEW_HELPER`); doctor reports WARN when absent.
- **[context-mode](https://github.com/simonrowland/context-mode)** — MCP plugin that offloads large command outputs (diffs, integration test runs, codex tail files, large greps) to an FTS5 sandbox queried by pattern. The multiplier that makes 12-hour unattended runs feasible — without it, tool-output fills the controller's context fast and you hit compaction early.

## Maintainer test tiers

Default `./tests/run.sh` stays hermetic and cheap. Set
`GOALFLIGHT_AUTOREVIEW=1` to include `tests/bash/test-autoreview-smoke.sh`,
which runs `scripts/autoreview.sh --engine claude` against a known-good fixture
commit through `scripts/autoreview_claude_acp`. Each invocation consumes one
Claude ACP-sub-billed autoreview pass.


## Adapting

This skill ships tuned for high-accuracy scientific programming but the patterns generalize. Workflow: clone the repo, open it in your controller host (Claude Code wrapper today), and ask the host to adapt the skill for a domain project with a north star, verification command, invariants, and any domain-specific self-review categories. A strong subagent can read the whole thing, propose a diff, and apply it in one pass.

Main tuning knobs:

- **North star + asking discipline + token bias** — `SKILL.md` hard-conventions section.
- **Self-review categories** — `prompts/executor-self-review.md` lines 14–35. Seven abstract categories; add domain-specific ones (e.g. SCHEMA GAP for ETL, A11Y GAP for frontend).
- **Review cadence K** — `commands/execute.md` step 4 ("Every K commits, default K=5"). Change the K literal or pass `--review-every <K>` per run.
- **RAG corpus slice mix + word budgets** — `templates/rag-corpus-schema.md.tpl`.
- **`/goal` mode prompt shape** — `templates/codex-goal-prompt.md.tpl` (Objective / Workspace / Rules / Acceptance / Test gates / Final response schema).

## License

MIT — see [LICENSE](LICENSE).
