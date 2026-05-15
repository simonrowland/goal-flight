# goal-flight

A [Claude Code](https://claude.ai/code) skill for long-running unattended controller-pattern work — refactors, multi-turn implementation from architecture docs, ports, recursive end-to-end testing, finite Karpathy / Ralph loops, scientific convergence against ground truth or first principles.

## What it does

Turn a fresh Claude Code session into a **controller** that:

- Decomposes a plan into numbered `\goal` chunks with structured SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN
- Dispatches each chunk to an **executor subagent** with a 5-layer wrapper (situational frame, template-provider pointer, file-anchors, environment caveats, goal-specific self-review specialization)
- Embeds adversarial self-review inside every dispatch (executor self-fixes P0/P1/P2 before reporting done)
- Runs **parallel codex + claude review sweeps** at milestone cadence (the "gstack" pattern)
- Builds a **RAG corpus** of curated dispatch-time context at init time so the controller stops re-pasting AGENTS.md hard invariants per dispatch
- Writes dated **handoff notes** (RESUME-NOTES-YYYY-MM-DD.md) before context fills so the next controller wakes up cleanly

Designed for ~12-hour unattended runs where you check in periodically rather than babysit.

## Install

```bash
git clone https://github.com/<you>/goal-flight.git ~/.claude/skills/goal-flight
```

Then in a Claude Code session: `/goal-flight init <topic>` to start, or `/goal-flight` (no args) to print the high-level pattern reference.

## Sub-commands

| Command | What it does |
|---------|--------------|
| `/goal-flight` | Print the high-level pattern reference (`reference/pattern.md`) |
| `/goal-flight init <topic>` | Check tooling, audit repo via subagent, scaffold AGENTS / docs-private / RAG corpus |
| `/goal-flight decompose-plan [<plan-file>]` | Break a plan into `\goal` chunks; parallel reviewer pass for the decomposition |
| `/goal-flight ask-questions [<scope>]` | Spawn anticipatory subagents; surface clarifying questions for the user |
| `/goal-flight execute [--parallel <N>]` | Run the per-chunk loop; sequential default, parallel-safe opt-in |
| `/goal-flight build-corpus [<flags>]` | Extend / rebuild the `docs-private/rag/` corpus after init |
| `/goal-flight resume` | Rebuild RESUME-NOTES from current git state |
| `/goal-flight goal <SLUG>` | Append one goal to the queue using the skeleton |

## Guiding philosophy

- **Use more tokens to improve quality, especially when parallelisable.** Subagent tokens and codex tokens are free goods relative to engineering quality.
- **North star: code quality + first-principles scientific integrity.** Interrupt the user only when a decision genuinely affects one of those.
- **Inline the landscape.** With 1M-context models, every Read the executor would have to do can be replaced by content pasted into the dispatch prompt (or sourced from the RAG corpus).
- **Per-intent self-review is cheaper than per-intent reviewer subagents.** Embed adversarial self-review inside the executor prompt; reserve full multi-agent review for milestones.
- **Cherry-pick over merge --ff-only** when integrating parallel-worktree subagent commits onto main.
- **Don't poll background subagents.** Wait for the harness's task-notification.

## Companion tools

- **[gstack](https://github.com/garrytan/gstack)** — Gary Tan's skill pack works for both Claude Code and codex; provides `/review`, `/office-hours`, `/plan-eng-review`, `/cso`, `/investigate`. `goal-flight` leans on it for milestone reviews.
- **context-mode** — MCP plugin that offloads large command outputs (diffs, test runs, codex tails) to an FTS5 sandbox. Strongly recommended for the controller pattern.

## Directory layout

```
goal-flight/
├── SKILL.md                    # Top-level skill definition (slash command entry)
├── commands/                   # Sub-command implementations
│   ├── init.md
│   ├── decompose-plan.md
│   ├── ask-questions.md
│   ├── execute.md
│   └── build-corpus.md
├── prompts/                    # Subagent prompt templates
│   ├── dispatch-wrapper.md     # The 5-layer briefing pattern
│   ├── executor-self-review.md
│   ├── gstack-claude-review.md
│   ├── gstack-codex-challenge.md
│   ├── rag-slice-builder.md    # RAG corpus pipeline
│   ├── rag-slice-review.md
│   ├── rag-cross-slice-consolidation.md
│   ├── rag-final-assessment.md
│   ├── dual-plan-adversarial.md
│   ├── decomposition-review.md
│   ├── ask-anticipatory.md
│   └── repo-audit.md
├── templates/                  # Scaffolding templates rendered at init
│   ├── AGENTS.md.tpl
│   ├── RESUME-NOTES.tpl
│   ├── goal-statement.md.tpl
│   ├── goal-queue.tpl
│   ├── worker-context.md.tpl
│   ├── rag-corpus-schema.md.tpl
│   ├── rag-slice-invariants.md.tpl
│   ├── rag-slice-file-map.md.tpl
│   ├── rag-slice-binding-spec.md.tpl
│   ├── rag-slice-pattern.md.tpl
│   ├── rag-slice-decisions.md.tpl
│   └── rag-slice-verification.md.tpl
└── reference/
    └── pattern.md              # High-level controller pattern reference
```

## Provenance

Distilled from a sustained refactor session on a regolith pyrolysis simulator where the controller-pattern ran for ~12 hours across multiple chunked goals. The pattern proven there is what this skill formalizes — including the post-session audit that found the controller dispatched ~6-11 KB wrappers around 600-1200 char goal text, leading to the dispatch-wrapper 5-layer codification.

## License

MIT — see [LICENSE](LICENSE).
