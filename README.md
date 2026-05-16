# goal-flight

A [Claude Code](https://claude.ai/code) skill for long-running unattended controller-pattern work — refactors, multi-turn implementation from architecture docs, ports, recursive end-to-end testing, finite Karpathy / Ralph loops, scientific convergence against ground truth or first principles.

## What it does

Turn a fresh Claude Code session into a **controller** that:

- Decomposes a plan into numbered `/goal` chunks with structured SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN
- Dispatches each chunk to an **executor** — Claude Agent subagent (default), `codex exec` (`/goal` mode for multi-hour loops; short-prompt for reviews), or Grok via `grok -p`
- Embeds adversarial self-review inside every dispatch (executor self-fixes P0/P1/P2 before reporting done)
- Runs **parallel codex + claude review sweeps** at milestone cadence via [gstack](https://github.com/garrytan/gstack)'s `/review` skill
- Builds a **RAG corpus** of curated dispatch-time context — read as starting hypotheses the executor verifies, not as authoritative facts
- Writes dated **handoff notes** before context fills so the next controller wakes up cleanly

Designed for ~12-hour unattended runs where you check in periodically or respond to decision notifications rather than babysit.

The philosophy is to apply more tokens to improve software quality, and apply more tokens to reduce unnecessary user prompts (including Netflix-style "are you still there?" prompts).

## Install

```bash
git clone https://github.com/simonrowland/goal-flight.git ~/.claude/skills/goal-flight
```

Then in a Claude Code session: `/goal-flight init <topic>` to start, or `/goal-flight` (no args) to print `SKILL.md`.

## Quickstart

```bash
# 1. In your project repo, in a Claude Code session:
/goal-flight init <topic>            # audits the repo, scaffolds AGENTS.md +
                                     # docs-private/ + (optional) RAG corpus,
                                     # registers the project as codex-trusted
                                     # if codex is installed

/goal-flight decompose-plan          # breaks a plan into numbered /goal chunks
                                     # with SCOPE / CHECKLIST / ACCEPTANCE /
                                     # FORBIDDEN; parallel reviewer pass

/goal-flight execute                 # runs the per-chunk dispatch loop with
                                     # embedded self-review; milestone codex+
                                     # claude reviews every K commits

# 2. Resume after a break / new session:
/goal-flight resume                  # rebuilds RESUME-NOTES from current git state

# 3. Maintenance / debugging
/goal-flight register-codex          # registers cwd project as codex-trusted
/goal-flight validate-dispatch <slug>   # dry-run the wrapper for review
/goal-flight validate-queue          # schema-check the goal-queue

# 4. Self-delegation via /fork (optional — when you want to branch the
#    controller into a sub-session that does a focused task and reports
#    back via filesystem markers):
bash scripts/self-fork-detect.sh write '<task>'    # before /fork
# (user types /fork or `claude --resume <sid> --fork-session`)
bash scripts/self-fork-detect.sh detect            # in the fork
bash scripts/self-fork-detect.sh find-fork         # back in controller
bash scripts/self-fork-detect.sh monitor <jsonl>   # watch fork's progress
```

`/goal-flight` with no args prints `SKILL.md` — the full gist.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight` | Print `SKILL.md` |
| `/goal-flight init <topic>` | Check tooling, audit repo via subagent, scaffold AGENTS / docs-private / RAG corpus |
| `/goal-flight decompose-plan [<plan-file>]` | Break a plan into `/goal` chunks; parallel reviewer pass for the decomposition |
| `/goal-flight ask-questions [<scope>]` | Spawn anticipatory subagents; surface clarifying questions for the user |
| `/goal-flight execute [--parallel <N>]` | Run the per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the `docs-private/rag/` corpus after init |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue using the skeleton |
| `/goal-flight register-codex [<path>]` | Register a project (or worktree path) as codex-trusted; bypass MCP approval-gate stalls |
| `/goal-flight validate-dispatch [<slug>]` | Render the dispatch wrapper for a goal **without dispatching** — dry-run for wrapper-composition bugs |
| `/goal-flight validate-queue [<path>]` | Schema-check the goal-queue: chunk structure, numbering, parallel-safe tags, slug uniqueness |

## Why this pattern works (the gist)

The naive way to run a long unattended session is "Claude does everything; user comes back to check." This fails because the controlling agent's context fills with file reads, tool output, and re-derivation, and quality drops as it accumulates noise. The naive solution is summarization, which is lossy.

The controller pattern instead **delegates concrete work to bounded subagents**, each operating in their own context window. The controller stays small — it dispatches, verifies the result, commits, and dispatches again. Each subagent dispatch is a verification-first briefing: the wrapper scaffolds what the executor should investigate, not what it should mirror verbatim. Pre-pasted "facts" go stale on the timescale of minutes; pointers stay correct.

Adversarial self-review is embedded inside every executor prompt — the executor treats its own work as if a different agent submitted it, runs through a P0/P1/P2/P3 checklist, self-fixes before reporting done. This catches roughly the same surface a separate reviewer subagent would, at a fraction of the controller-context cost. Full multi-agent review (codex + claude, concern-split) is reserved for milestone checkpoints.

The result: the user can step away in bunny slippers. The controller makes forward motion through routine chunks, holding state in files (RESUME-NOTES, goal queue, RAG corpus) instead of conversation history. When a real blocker arrives, the controller surfaces a well-prepared question with subagent-vetted options. "Step 1 done. Continue?" is not a blocker; the controller just continues.

## Adapting this to your project

This skill ships tuned for **high-accuracy scientific programming**, but the patterns generalize. The intended workflow: clone this repo, open it in Claude Code, point at it and say "Adapt this skill for a [domain] project; my north star is [X]; my self-review categories should add [Y]; here's our verification command and our invariants." The skill is ~200 KB total (down from ~230 KB after the May 2026 strip — the wholesale rewrite of `commands/init.md` and `commands/execute.md` to remove SKILL.md-redundant guidance is the next-largest prune target if you want to push further). A single Opus subagent can still read the whole thing, propose a diff, and apply it in one pass. Then commit your fork.

The main knobs are in `SKILL.md` (north star, asking discipline, token-bias dial), `prompts/executor-self-review.md` (the 7 abstract categories — add domain-specific ones), `commands/execute.md` (review cadence `K`, parallel mode), and `templates/rag-corpus-schema.md.tpl` (slice mix + word budgets).

## Self-delegation via `/fork`

Optional pattern, useful when the controller has substantial session-loaded state that a fresh Agent-tool subagent would have to re-discover, AND you want a `/rewind`-able savepoint before doing something risky/exploratory.

Claude Code's `/fork` (renamed `/branch` in v2.1.77 but `/fork` still works) creates a new session with all conversation history inherited; the new session gets a fresh `CLAUDE_CODE_SESSION_ID`. The skill ships `scripts/self-fork-detect.sh` to formalize the contract — controller writes a marker before forking; the fork reads the marker post-fork and learns what task to execute + which keyword markers to emit so the controller (polling the fork's JSONL) can extract status, results, completion, blockers, or intervention-required signals.

**Marker vocabulary** (only return channel; forks lack the task-notification callback that Agent-tool subagents have):

| Marker | Semantics | Monitor exit |
|---|---|---|
| `FORK-STATUS: <update>` | Intermediate progress | (keeps polling) |
| `FORK-RESULT: <key>=<value>` | Structured output to extract | (keeps polling) |
| `FORK-NEED: <question>` | Controller/user intervention required | **2** |
| `FORK-COMPLETE: <summary>` | Done | **0** |
| `FORK-BLOCKED: <reason>` | Unrecoverable, won't continue | **1** |

See [`SKILL.md` §Self-delegation via /fork](SKILL.md) for the full pattern. The empirical identity surface (controller / subagent / fork — env var + JSONL path per role) is documented there, verified May 2026 on Claude Code v2.1.142.

## When NOT to use this

- **One-off scripts or quick fixes.** Overhead unjustified for <8 chunks or <2000 LoC delta. Pre-flight gates auto-skip the corpus build for projects this small.
- **Pair-programming sessions.** Designed for unattended runs. If you're steering every turn, the wrapper overhead slows you down.
- **Sensitive operations the controller shouldn't autonomously trigger.** Production deploys, prod data writes, credential rotations — human-in-the-loop is the point. `goal-flight` is for code-writing autonomy, not ops automation.
- **Projects without test signal.** Self-review and milestone-review depend on tests / grep invariants / verification commands existing.

## Companion tools

- **[gstack](https://github.com/garrytan/gstack)** — Gary Tan's skill pack works for both Claude Code and codex; provides `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate`. `goal-flight` leans on it for milestone reviews.
- **context-mode** — MCP plugin that offloads large command outputs to an FTS5 sandbox. Strongly recommended for the controller pattern.

## Directory layout

```
goal-flight/
├── SKILL.md                          # The gist — canonical reference. /goal-flight no-args prints this.
├── README.md                         # This file.
├── CHANGELOG.md
├── VERSION
├── commands/                         # Sub-command implementations
│   ├── init.md
│   ├── decompose-plan.md
│   ├── ask-questions.md
│   ├── execute.md
│   ├── build-corpus.md
│   ├── register-codex.md
│   ├── validate-dispatch.md
│   └── validate-queue.md
├── prompts/                          # Subagent prompt templates
│   ├── dispatch-wrapper.md           # Verification-first principle + Layer 0 spec
│   ├── executor-self-review.md       # The 7 abstract self-review categories
│   ├── decomposition-review.md
│   ├── ask-anticipatory.md
│   ├── repo-audit.md
│   ├── gstack-claude-review.md
│   ├── gstack-codex-challenge.md
│   └── dual-plan-adversarial.md
├── templates/                        # Load-bearing shapes (init-time templates inlined)
│   ├── codex-goal-prompt.md.tpl      # /goal mode prompt shape
│   └── rag-corpus-schema.md.tpl      # Corpus directory shape + verified-at convention
├── scripts/
│   ├── install-codex-overrides.sh    # Codex trust registration
│   └── self-fork-detect.sh           # /fork self-delegation contract + monitor
├── tests/
│   ├── run.sh
│   ├── test-install-codex-overrides.sh
│   ├── test-self-fork-detect.sh
│   └── README.md
```

## License

MIT — see [LICENSE](LICENSE).
