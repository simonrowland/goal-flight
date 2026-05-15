---
name: goal-flight
description: "long-running unattended controller for chunked code work — init repo, decompose plan, anticipate questions, execute with embedded review and milestone gstack sweeps"
trigger: /goal-flight
---

# /goal-flight

Turn a fresh Claude Code session into a **controller** for long-running, decomposed code work — refactors, multi-turn implement-from-architecture-doc, porting, recursive end-to-end testing, finite Ralph/Karpathy loops, scientific convergence against ground truth or first principles.

The controller dispatches `\goal` chunks to executor subagents, embeds adversarial self-review in every dispatch, runs parallel codex+claude review sweeps at milestones (the "gstack" pattern), and writes dated handoff notes before context fills. Designed for ~12-hour unattended runs where you check in periodically rather than babysit.

## Sub-commands

```
/goal-flight                              # print the high-level pattern reference
/goal-flight init <topic>                 # check tooling, audit repo, scaffold AGENTS/docs-private/
/goal-flight decompose-plan [<plan-file>] # break a plan into \goal chunks; review the decomposition
/goal-flight ask-questions [<scope>]      # spawn anticipatory subagents; surface clarifying questions
/goal-flight execute [--parallel <N>]     # run the per-chunk loop; sequential default, parallel-safe opt-in
/goal-flight build-corpus [<flags>]       # extend / rebuild the docs-private/rag/ corpus after init
```

Single-shot helpers:
```
/goal-flight resume                       # rebuild RESUME-NOTES from current git state
/goal-flight goal <SLUG>                  # append one goal to the queue using the skeleton
```

## What you must do when invoked

If no args: read `reference/pattern.md` (in this skill's directory) and print it. Do not ask follow-up questions.

If args, dispatch on the first token by reading the matching detailed instructions:

| First token | Read and follow |
|-------------|-----------------|
| `init` | `commands/init.md` |
| `decompose-plan` | `commands/decompose-plan.md` |
| `ask-questions` | `commands/ask-questions.md` |
| `execute` | `commands/execute.md` |
| `build-corpus` | `commands/build-corpus.md` |
| `resume` | (handle inline; see "Resume / goal" below) |
| `goal` | (handle inline; see "Resume / goal" below) |

The detailed sub-command instructions live in this skill's `commands/` directory so this SKILL.md stays scannable.

## Hard conventions (apply to every sub-command)

- **Antipattern: `claude -p`.** It consumes Anthropic API billing rates instead of your session billing. **Always use the Agent tool** to spawn Claude subagents (their work is part of your session billing). Codex reviewers go through `Bash codex exec`.
- **Default code-writing dispatches to the largest available model + highest reasoning level.** For Agent tool: `model: "opus"` (largest current Claude). For codex: include the highest-reasoning flag (`-c model_reasoning_effort=xhigh` or whatever the current codex CLI exposes; verify with `codex --help`). The trade is latency/cost for perfectionist output — for refactor work where one subtle bug propagates across all subsequent chunks, the trade is worth it. For non-code chunks (planning, review, docs writing) the default model is fine; reserve the escalation for chunks where the executor is writing code that will be cherry-picked into main.
- **Strongly recommend context-mode.** [context-mode](https://github.com/simonrowland/context-mode) installs MCP tools (`ctx_execute`, `ctx_batch_execute`, `ctx_search`, `ctx_fetch_and_index`) that offload large command outputs to an FTS5 sandbox and query them by pattern instead of stuffing context. For the controller pattern this is a real multiplier — diff verification, integration pytest output, goal-queue searches, codex tail monitoring all benefit. Codex side benefits even more during `\goal` loops where shell tooling fills context quickly. Init checks both Claude Code and codex MCP registrations and recommends install if missing.
- **Guiding philosophy: use more tokens to improve quality, especially when parallelisable.** Subagent tokens (Claude session billing) and codex tokens (OpenAI Pro accounts) are largely "free goods" relative to engineering quality. The goal is to think / prepare / iterate / review MORE in service of higher-quality software. Concrete biases this should produce when the user is away from keyboard:
  - When uncertain on a design call, dispatch a planner pair (chemistry-first + engineering-first lenses, or analogous concern-split) rather than waiting to ask.
  - Add a parallel codex reviewer for any non-trivial milestone — codex's second-opinion is essentially free.
  - Default to the largest model + highest reasoning for any code-writing work (already a hard convention above).
  - Build the corpus eagerly at init; loosen the small-project skip threshold rather than tighten it.
  - On the first sign of dispatch drift or inconsistent output across parallel subagents: dispatch a consistency reviewer, don't paper over.
  - Iteration cycles are cheap; serialized "ask the user, then act" cycles are not. Prefer parallel adversarial review over blocking on user input for choices the controller can adjudicate from evidence.
- **Asking discipline — north star: code quality + first-principles scientific integrity.** Interrupt the user only when a decision genuinely affects one of those. Refinements:
  - **Prepare the question with subagents first.** While waiting on codex or any background work, dispatch anticipatory reviewer-loop subagents to pre-resolve choices the controller might otherwise have to ask about. A second pair of eyes resolves most "should I X or Y?" questions; remaining ones become better-formed asks because the subagent has shaped the options.
  - **Don't ask trivia.** Worktree labels, file naming nits, paint colors, "proceed or hold when the next step is obviously consistent with the philosophy" — never ask. These don't affect code quality or scientific integrity. Just decide.
  - **Don't do Netflix "are-you-still-watching" check-ins.** "Step 1 done. Continue?" when nothing is blocked is the antipattern. The user has handed the controller authority precisely so they can step away in bunny slippers; the controller's value is forward motion through routine work until a real blocker arrives. Running commentary as work progresses IS welcome — short status lines showing forward motion give the user a window into what's happening. The thing to cut is the implicit-stop pattern: "X is done. Proceed?" / "Hold or go?" / "Want me to continue?" — these create a hard wait for confirmation when no real decision is needed. Status without an asking-hook is fine; status WITH an asking-hook on non-decision steps is the antipattern.
  - **Do ask when the choice affects** the north star: an unresolved chemistry/physics assumption, a scope-vs-scope trade-off no subagent can adjudicate without user values, a destructive operation (per the harness's confirmation rule), or a decision that would lock in a wrong invariant.
  - The right amplification: a well-prepared ask with subagent-vetted options is worth roughly 5 raw asks. Front-load thinking; back-load interruption. When the controller does interrupt, it should be a thoughtful "here's my very thoughtful recommendation against your three main options for how to square X" — not a status ping.
- **Leverages [gstack](https://github.com/garrytan/gstack)** when installed — Gary Tan's skill pack works for **both Claude Code and codex**, exposing `/review`, `/office-hours`, `/plan-eng-review`, `/plan-ceo-review`, `/cso`, `/investigate`, etc. Init checks both install locations (`~/.claude/skills/gstack/`, `~/.codex/skills/gstack/`, plus project-level `.agents/skills/gstack/`) and recommends install if either side is missing. Commands prefer **Claude-direct invocation** (Skill tool, e.g., `/review <range>`) when available; use `codex exec '/review <range>'` for the parallel second-opinion perspective; fall back to local prompts in `prompts/` only when gstack is absent on both sides.
- **Controller delegates Read-heavy work to Explore subagents.** Never reads README/docs/large source files directly during init or execute. Spawn an Explore agent and consume its summary.
- **Worker context is optional, not load-bearing.** Modern context windows (200k+ for mid-tier models, 1M for Opus 1M-context) make precis curation a false economy — the ~5k tokens you save aren't worth the drift risk between a precis and the canonical AGENTS.md. Default: dispatches point executors at AGENTS.md directly. Create `docs-private/worker-context.md` only if your AGENTS.md is genuinely huge (>1000 lines) or the project has multiple distinct worker profiles with little overlap. When present, it supersedes AGENTS.md as the executor's reading entry point; when absent, AGENTS.md serves both controller and executors fine.
- **The high-level goal is pinned at init time** to `docs-private/<topic>-goal-statement-<today>.md`. Controller cites it in decompose-plan, mid-execute decisions, and milestone summaries. User shouldn't need to remind the session what the point of the task is.
- **Date format**: `YYYY-MM-DD` from the conversation's `currentDate`. Same-day RESUME-NOTES bump `(rev N)` in H1; never overwrite.
- **AGENTS.md is never overwritten.** If it exists, init proposes additions/edits as a diff for the user to apply.
- **Status / progress questions** ("where are we?", "what's the queue?", "what just landed?") work without any skill layer — the controller answers from its visible Progress table and a quick read of the goal-queue. No `/goal-flight status` command needed.
- **Notifications** via `osascript -e 'display notification "X" with title "goal-flight"'` ONLY on blockers and queue completion. Session scrollback is the primary monitor. Forensics: the harness already captures full session JSONL at `~/.claude/projects/<encoded-repo>/<session-id>.jsonl` plus per-subagent JSONL at `<...>/subagents/agent-<id>.jsonl` plus codex tail files at `/tmp/goal-flight-*-<iso>.txt`. RESUME-NOTES carries the prose narrative; goal-queue Progress table carries the per-chunk status. No additional structured log is needed — a separate `controller.log` is net redundant given those three layers.
- **Two-worktree convention** — controller works in main worktree; parallel-safe goals each get a `<repo>/.claude/worktrees/<adjective-noun>/` worktree on a `claude/<name>` branch.

## Resume / goal sub-commands (handled inline)

### `resume`
1. Find the most recent `docs-private/RESUME-NOTES-*.md`. If none, bail: "Run `/goal-flight init <topic>` first."
2. Find the most recent goal-queue: `docs-private/*-goal-queue-*.md`.
3. Read git state via Bash: `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git log --oneline -20`.
4. **Spawn a subagent** to render `templates/RESUME-NOTES.tpl` with the captured state and write to `docs-private/RESUME-NOTES-<today>.md`. If today's file exists, increment `(rev N)` in H1 instead of overwriting.
5. Print: file path; placeholders the user still needs to fill in.

### `goal <SLUG>`
1. Find the most recent goal-queue.
2. Find the highest existing `## N.` goal number.
3. Append a new entry: `## <N+1>. \goal <SLUG>` with SCOPE/CHECKLIST/ACCEPTANCE/FORBIDDEN skeleton (read from `templates/goal-queue.tpl`).
4. Append a row to the Progress table: `| #<N+1> \`<SLUG>\` | TODO |`.
5. Print the appended block.
