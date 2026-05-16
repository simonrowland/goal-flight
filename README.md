# goal-flight

A [Claude Code](https://claude.ai/code) skill that turns a fresh session into a **controller** for long, decomposed code work. Claude Code runs out of context on multi-chunk refactors when one agent does everything; goal-flight delegates concrete work to bounded subagents and keeps the controller small, so multi-hour unattended runs land as a clean stack of one-commit-per-chunk on main without the controller's context window filling up.

```bash
git clone https://github.com/simonrowland/goal-flight.git ~/.claude/skills/goal-flight
```

## What it gets you

- **Multi-hour unattended runs.** Check in periodically or respond to decision notifications. The controller's context primarily holds metadata (queue state, recent commits, in-flight dispatch headers); real work happens in subagent context windows.
- **Verification-first dispatch.** Wrappers point at files for the agent to investigate, not pre-pasted "facts" that go stale on the timescale of minutes. Frontier models trust controller-text uncritically; pointers force them to re-verify against live disk and surface drift.
- **Parallel codex + claude reviews at milestone cadence.** Two independent reviewers (Claude and codex, OR concern-split Claude×Claude) catch what one model misses. Via [gstack](https://github.com/garrytan/gstack)'s `/review` skill when installed.
- **Three dispatch paths** the controller picks from per chunk, not one rigid loop — controller-inline for trivial chunks, single-shot subagent for the common case, multi-hour goal-mode loop (codex `/goal` or controller-driven iteration) for chunks that need it.

## How it differs from the alternatives

- vs. **running Claude Code naively** — the controller doesn't itself do the work. It dispatches and verifies, which means it stays small and runs longer before compaction.
- vs. **cloud agents (Devin / Cursor agent)** — runs on your machine, in your Claude Code session, with your existing skills (gstack, context-mode, codex). No new platform, no separate billing per task.
- vs. **writing prompts manually** — the dispatch wrapper is composed from a verification-first principle, not hand-crafted per goal. The 7-category adversarial self-review is embedded in every executor prompt so the executor catches its own errors before the controller verifies.

## Quickstart

```bash
# In your project repo, in a Claude Code session:
/goal-flight init <topic>         # audit repo, scaffold AGENTS.md + docs-private/,
                                  # build optional RAG corpus, register codex-trust
/goal-flight decompose-plan       # break the plan into /goal chunks (SCOPE / CHECKLIST
                                  # / ACCEPTANCE / FORBIDDEN), parallel reviewer pass
/goal-flight execute              # per-chunk dispatch loop, embedded self-review,
                                  # milestone codex+claude reviews every K commits
/goal-flight resume               # rebuild RESUME-NOTES from current git state
                                  # (use when picking up across sessions)
```

`/goal-flight` with no args prints `SKILL.md` — the full pattern reference.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight init <topic>` | Tool check, repo audit, scaffold, codex-trust registration |
| `/goal-flight decompose-plan [<plan>]` | Break a plan into `/goal` chunks with parallel reviewer pass |
| `/goal-flight ask-questions [<scope>]` | Anticipatory subagents; surface clarifying questions |
| `/goal-flight execute [--parallel <N>]` | Per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the optional RAG corpus |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue |
| `/goal-flight register-codex [<path>]` | Register a project as codex-trusted |
| `/goal-flight validate-dispatch [<slug>]` | Render a chunk's dispatch wrapper without dispatching |
| `/goal-flight validate-queue [<path>]` | Schema-check the goal-queue |

Plus an opt-in self-delegation pattern via `/fork` — controller writes a marker contract; forked session detects via env var and follows the contract; controller monitors the fork's JSONL for keyword markers (`FORK-STATUS`, `FORK-COMPLETE`, `FORK-NEED`, etc.). See `SKILL.md` §Self-delegation via `/fork`.

## Three dispatch paths (the cost/loop trade-off)

| Path | When | Cost |
|---|---|---|
| **`[controller-direct]`** | Trivially small (single-file, < ~30 LoC), OR controller already has the session-loaded context a fresh subagent would have to re-discover | Inline; no subagent |
| **Single-shot subagent** (Claude Agent / `codex exec` / `grok -p`) | Default for most chunks. Frontier model picks the executor target based on chunk shape | One subagent dispatch per chunk |
| **Goal-mode loop** (codex `/goal` in-session, or external iteration loop driven by the controller) | Multi-step refactor, code migration, prototype implementation — anything that benefits from a plan/act/test/iterate loop | Multi-hour autonomous session (codex), or N Agent-tool dispatches (controller-driven loop) |

The skill doesn't prescribe between Opus, codex, and Grok within a path — frontier models pick based on the chunk. Hard convention: code-writing dispatches use the largest available model + highest reasoning by default; non-code dispatches use defaults.

## When NOT to use this

- **One-off scripts or quick fixes.** Overhead unjustified for <8 chunks or <2000 LoC delta. Pre-flight gates auto-skip the RAG corpus for projects this small.
- **Pair-programming sessions.** Designed for unattended runs. If you're steering every turn, the wrapper overhead just slows you down.
- **Sensitive operations the controller shouldn't autonomously trigger.** Production deploys, prod data writes, credential rotations — human-in-the-loop is the point.
- **Projects without test signal.** Self-review and milestone-review depend on tests / grep invariants / verification commands existing.

## Companion tools (strongly recommended)

- **[gstack](https://github.com/garrytan/gstack)** — Gary Tan's skill pack provides `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate` for both Claude Code and codex. Goal-flight invokes `/review` for milestone reviews and `/office-hours` for fuzzy-goal interrogation at init. Without gstack, goal-flight falls back to local prompts; with it, you get the consistent severity-ranking framing across both review lenses.
- **[context-mode](https://github.com/simonrowland/context-mode)** — MCP plugin that offloads large command outputs (diffs, integration test runs, codex tail files, large greps) to an FTS5 sandbox queried by pattern. The multiplier that makes 12-hour unattended runs feasible — without it, tool-output fills the controller's context fast and you hit compaction early.

## Adapting

This skill ships tuned for high-accuracy scientific programming but the patterns generalize. Workflow: clone the repo, open it in Claude Code, ask Claude to "adapt this skill for a [domain] project; my north star is [X]; my self-review categories should add [Y]; here's our verification command and our invariants." A single Opus subagent can read the whole thing, propose a diff, and apply it in one pass.

Main tuning knobs: `SKILL.md` (north star, asking discipline, token-bias), `prompts/executor-self-review.md` (the 7 abstract self-review categories — add domain-specific ones), `commands/execute.md` (review cadence `K`, parallel mode), `templates/rag-corpus-schema.md.tpl` (slice mix + word budgets).

## License

MIT — see [LICENSE](LICENSE).
