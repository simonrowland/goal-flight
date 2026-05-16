---
name: goal-flight
version: 0.2.0
description: "long-running unattended controller for chunked code work — init repo, decompose plan, anticipate questions, execute with embedded review and milestone gstack sweeps"
allowed-tools:
  - Bash
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Agent
  - Skill
  - TodoWrite
  - AskUserQuestion
triggers:
  - /goal-flight
  - start a long refactor
  - begin chunked work
  - set up controller for unattended run
  - decompose this plan into goal chunks
---

# /goal-flight

Turn a fresh Claude Code session into a **controller** for long-running, decomposed code work — refactors, multi-turn implement-from-architecture-doc, porting, recursive end-to-end testing, finite Ralph/Karpathy loops, scientific convergence against ground truth or first principles.

The controller dispatches `/goal` chunks to executor subagents, embeds adversarial self-review in every dispatch, runs parallel codex+claude review sweeps at milestones (via [gstack](https://github.com/garrytan/gstack)'s `/review` skill when installed), and writes dated handoff notes before context fills. Designed for ~12-hour unattended runs where you check in periodically rather than babysit.

## Session pre-flight (silent — surface only what fires)

On `/goal-flight` invocation, the controller orients itself with three fast probes before responding to any sub-command. Silent on a fresh project; surfaces drift before it bites.

1. **Skill version** — try in order: `cat ~/.claude/skills/goal-flight/VERSION`, then any `VERSION` file beside a `SKILL.md` matching this skill (`find ~/.claude/plugins -path '*goal-flight/VERSION'` for plugin-form installs). Skip silently if neither resolves. Quote as `(goal-flight v<X.Y.Z>)` parenthetical in the session's first non-trivial response so RESUME-NOTES forensics can pin behaviour to a version.
2. **In-flight state** — `find docs-private -maxdepth 1 -name 'RESUME-NOTES-*.md' -mtime -7 2>/dev/null | sort | tail -3` (mtime-filtered: only recent files trip the nudge; year-old RESUME-NOTES from a prior topic don't). If any match and the user invoked something other than `resume` / `goal` / `init`, surface a one-line nudge: "RESUME-NOTES from <date> exists — run `/goal-flight resume` first?" User redirects if they meant to start fresh.
3. **Corpus drift** — if `docs-private/rag/` exists, read the oldest `verified-at:` SHA from slice frontmatter (format pinned in `templates/rag-corpus-schema.md.tpl` §Frontmatter — YAML frontmatter, lowercase `verified-at`, full 40-char SHA). If `git rev-list --count <SHA>..HEAD` exceeds 20, surface "corpus is N commits stale — executors will verify aggressively or run `/goal-flight build-corpus --next-wave`."

A fresh project trips none of these. They cost ~50 ms (~200 ms on slow-startup shells) and prevent silent staleness compounding across a 12-hour run.

## Sub-commands

```
/goal-flight                              # print this file
/goal-flight init <topic>                 # check tooling, audit repo, scaffold AGENTS/docs-private/
/goal-flight decompose-plan [<plan-file>] # break a plan into /goal chunks; review the decomposition
/goal-flight ask-questions [<scope>]      # spawn anticipatory subagents; surface clarifying questions
/goal-flight execute [--parallel <N>]     # run the per-chunk loop
/goal-flight build-corpus [<flags>]       # extend / rebuild the docs-private/rag/ corpus
/goal-flight register-codex [<path>]      # register a project as codex-trusted (re-run safely)
/goal-flight validate-dispatch [<slug>]   # dry-run a dispatch wrapper; catch malformed layers
/goal-flight validate-queue [<path>]      # schema-check the goal-queue
/goal-flight resume                       # rebuild RESUME-NOTES from current git state
/goal-flight goal <SLUG>                  # append one goal to the queue
```

Dispatch on the first token by reading the matching `commands/<token>.md`; `resume` and `goal` are handled inline (see bottom of this file).

If no args: print this file.

## Hard conventions

### Dispatch model

- **Default to Claude Agent tool** (`model: "opus"`, highest reasoning) for code-writing chunks. Claude session billing, not API billing. **Never `claude -p`** — that consumes API billing.
- **Codex `exec` is a peer dispatch target**, not just a reviewer. Use `Bash timeout 300 codex exec '<short pointer-shaped prompt>'` — keep the prompt short, pass file pointers (the agent reads what it needs at the time it needs it). The MCP approval-gate stall that otherwise breaks ~2/5 codex dispatches is solved at init by `scripts/install-codex-overrides.sh` (registers the project + its worktrees by path prefix as codex-trusted).
- **Codex `/goal` mode** is a real CLI feature (codex ≥ 0.128.0, `features.goals = true` in `~/.codex/config.toml`). Multi-hour autonomous plan→act→test→iterate loop. Invocation: `codex exec -C <workdir> - < prompt.md`. The prompt shape (Objective + Workspace + Rules + Acceptance + Test gates + Final response schema — see `templates/codex-goal-prompt.md.tpl`) auto-activates the loop when goal-shaped. **`/goal` is the unified chunk marker** — appears literally at the top of each chunk in the goal-queue and at the top of each dispatched prompt. To Claude Opus / Grok executors it's just an in-prompt text marker (read as labelled section heading); to codex executors with `features.goals = true` it activates the in-session goal-mode loop. Opus / Grok iteration loops use the same goal-prompt structure but run as external loops driven by the controller (one dispatch per iteration); only codex has the in-session `/goal` primitive.
- **Three dispatch paths** (frontier models pick; the skill doesn't prescribe between Opus / codex / Grok):
  1. **`[controller-direct]`** — controller does the work inline. Two triggers: (a) trivially small (single-file, < ~30 LoC, no cross-module coupling); (b) controller already has the session-loaded state a fresh subagent would have to re-discover (mid-debug, just-consumed milestone-review P0, rolling decisions not yet in `decisions.md`). Heuristic for (b): a clean dispatch wrapper would exceed ~5 KB primarily because of context the controller already holds.
  2. **Single-shot subagent** — Agent tool (Claude) OR codex `exec` short-prompt OR `grok -p` short-prompt. One dispatch, executor reports done. The default path for most chunks. Frontier model picks the executor target based on chunk shape.
  3. **Goal-mode loop** — codex `/goal` (in-session, codex ≥ 0.128 + features.goals) OR external loop driven by the controller (one Agent or Grok dispatch per iteration; controller parses Final response block, re-dispatches with updated `Iteration N of MAX, Prior progress: ...` preamble). Iteration cap: 5–8 default, configurable via `[max-iterations:<N>]` chunk tag.
- **Token bias defaults UP** — largest model + highest reasoning + parallel reviewers + corpus eagerly built — because subagent / codex / grok tokens are cheap relative to engineering quality. Override DOWN per-chunk via `[controller-direct]` or a smaller `model:` field for trivially-small work.
- **Channel routing.** The three-paths section above doesn't prescribe between Opus / codex / grok within a path. When the user states a routing preference at session start, apply it consistently through the run. Typical override: route coding-heavy chunks to codex when Claude session-limits bite from concurrent runs; reserve Claude for orchestration and milestone reviews (gstack `/review` Claude-side); use codex `/review` (via gstack) for the parallel second-opinion review channel. Grok remains an executor (single-shot `grok -p` or `/goal`-loop iteration) — wiring grok-p as a review-channel target is forward work.

### Verification-first dispatch wrapper

The wrapper **scaffolds what the executor should investigate**; it does NOT substitute for investigation. Controller-pasted "facts" (file:line refs, function signatures, invariant restatements) go stale on the timescale of minutes; frontier models trust pasted text because the controller is upstream in the trust hierarchy. Pointers force the agent to re-verify against live disk and surface drift.

Full spec: `prompts/dispatch-wrapper.md`. Target dispatch size: 3–5 KB, not 6–11 KB (the size reduction is the empirical regression test). Layer 0 (base-verification pre-flight) is mandatory for worktree-isolated dispatches.

### Codex reliability

- **Register every project as codex-trusted** at init time via `scripts/install-codex-overrides.sh`. Without it, non-interactive `codex exec` blocks indefinitely on the first MCP tool call when context-mode (or any MCP server with `approval_mode = "approve"`) is registered — codex surfaces this as `request_user_input is not supported in exec mode for thread <id>` followed by silent retry loops. Trust registration auto-approves the call, dissolving the loop. Trust is prefix-based; worktrees inherit automatically. See `commands/register-codex.md`.
- **Register context-mode on codex side** at init time via `scripts/register-context-mode-codex.py`. The script handles the plugin-form vs explicit-form detection on Claude side and writes the canonical `npx -y context-mode@latest` codex registration. Without it, `codex exec` works but loses the context-mode multiplier for that dispatch.
- **Codex CLI ≥ 0.128.0** for `/goal` mode (older versions still work for reviews / consolidation / short-prompt dispatches — they just don't have the in-session loop primitive). Init step 1 checks the version and recommends `codex update` if older.
- **Pre-install external dependencies before `/goal` mode dispatches.** Multi-hour autonomous loops + mid-iteration `pip install` (or `npm install`, `uv sync`, `cargo build`) introduce a real friction class: codex tries to surface-install dependencies on demand, which can wedge on network or leave half-installed venvs that the next iteration trips over. Resolve the dependency surface in the workspace BEFORE composing the dispatch (e.g., `pip install -r requirements.txt`, `uv sync`, `npm install`). Surface known-missing packages in the dispatch Rules so codex doesn't try to surface-install. Same principle for short-prompt codex dispatches when the chunk imports something not in the base env.
- **Wrap `codex exec` in `timeout --kill-after=10 300`** for non-`/goal` dispatches. Codex v0.130.0 has no `--timeout` flag. 300 s is 10× observed p95 (~25 s healthy). **Do NOT** wrap `/goal` mode dispatches in `timeout 300` — they're multi-hour by design; the controller monitors the tail file for the Final response block to detect completion.
- **Backstop watchdog** for residual stalls (network wedge, codex-internal hang): zero-output ≥ 90 s OR no-progress ≥ 180 s ⇒ kill + retry as Claude general-purpose subagent. Applies to short-prompt dispatches only; for `/goal` mode, long pauses during plan/act/test/iterate cycles are expected and the watchdog would false-positive.
- **Fallback when trust can't be registered** (`--ignore-user-config`): shared machine, one-off invocation, or `~/.codex/config.toml` shouldn't be mutated. Run `codex exec --ignore-user-config '...'`. Loses MCP tool access in the dispatched session; trust-registration is strictly preferable when feasible.

### Asking discipline — north star

The controller's value is forward motion until a real blocker. **Interrupt the user only when a decision genuinely affects the project's north star** (default: code quality + first-principles scientific integrity; configurable per project).

- **Don't ask trivia.** Worktree labels, file naming nits, paint colors — never ask. Just decide.
- **Don't do Netflix "are-you-still-watching" check-ins.** "Step 1 done. Continue?" when nothing is blocked is the antipattern. Running commentary as work progresses IS welcome — short status lines giving the user a window into the work. The thing to cut is the implicit-stop pattern ("Hold or go?", "Proceed?") on routine forward motion.
- **Prepare the question with subagents first.** While waiting on a long-running subagent or codex review, dispatch anticipatory reviewer-loop subagents to pre-resolve choices. A well-prepared ask with subagent-vetted options is worth roughly 5 raw asks.
- **Do ask when** a chemistry/physics/security/correctness assumption needs user values, when a destructive operation is about to fire, or when a decision would lock in a wrong invariant.

### State — three layers, different scopes

- **`docs-private/RESUME-NOTES-<date>-(rev N).md`** — cross-session prose handoff. Survives compaction, session boundaries, machine reboots. Bump rev at: end of init, after decompose-plan, after each milestone review, before any anticipated compaction, on queue completion. Append-only within a day — never overwrite a prior rev.
- **`docs-private/<topic>-goal-queue-<date>.md` Progress table** — cross-session chunk-level state. One row per chunk; status = TODO / IN-FLIGHT / DONE / BLOCKED. Updated immediately after every commit. What `/goal-flight resume` reconstructs from.
- **TodoWrite (harness state)** — in-session tactical sub-step tracking. "Read the file. Edit the function. Run pytest." Survives in-session compaction but NOT cross-session. Optional. If state needs to survive `/goal-flight resume`, it goes in a file; if it's "next 3 tool calls," TodoWrite is fine.

### Handoff before compact

When context is ~80% full or compaction is imminent, write fresh `docs-private/RESUME-NOTES-<date>.md`. 80% is the default; the right threshold is a function of (remaining work in the queue) × (cost of waking afresh with summaries). Conserve harder when multiple subagent dispatches are in flight whose notifications carry mid-decision state; run hotter (90%+) when the queue is 1–3 trivial chunks from done and the most recent RESUME-NOTES rev already captures in-flight state. Subagents + `\goal` mode do most of the heavy lifting for extending session life — the controller's own context primarily holds metadata, not the bottleneck (as long as the controller doesn't pull large outputs into its own context).

### Context-mode multiplier

[context-mode](https://github.com/simonrowland/context-mode) installs MCP tools (`ctx_execute`, `ctx_batch_execute`, `ctx_search`, `ctx_fetch_and_index`) that offload large command outputs to an FTS5 sandbox and query them by pattern instead of stuffing context. Real multiplier for the controller pattern — diff verification, pytest output, goal-queue searches, codex tail monitoring all benefit. Init checks both Claude Code and codex MCP registrations and recommends install if missing.

### gstack integration

[gstack](https://github.com/garrytan/gstack) works on both Claude Code and codex. Init checks both install locations (`~/.claude/skills/gstack/`, `~/.codex/skills/gstack/`, plus project-level `.agents/skills/gstack/`) and recommends install if either side is missing. Prefer Claude-direct invocation (`Skill(skill: "review", ...)`) when available; use `timeout 300 codex exec '/review <range>'` for the parallel second-opinion perspective at milestones.

### Read discipline

- **Controller delegates *bulk* Read-heavy work to Explore subagents.** Don't read whole READMEs, architecture docs, or files >200 lines directly during init or execute — spawn an Explore agent, consume its summary. Short verification reads (< 200 lines, specific `file:line` ranges, targeted `grep` / `tail -n` / `sed -n`) are fine inline. The ban is on bulk context consumption, not on the controller using its eyes.
- **`AGENTS.md` is never overwritten.** If it exists, init proposes additions/edits as a diff for the user to apply.

### Self-delegation via `/fork`

Claude Code's `/fork` (renamed `/branch` in v2.1.77 but `/fork` still works) creates a new session branched off the current one with full conversation history inherited; the new session gets a new `CLAUDE_CODE_SESSION_ID`. **The forked session can self-detect via env var comparison against a marker the controller wrote before forking.**

Empirical identity surface (verified):

| Role | `CLAUDE_CODE_SESSION_ID` | JSONL path on disk |
|---|---|---|
| Controller | `X` | `<proj>/X.jsonl` (top-level) |
| Subagent (Agent-tool dispatch) | `X` (inherited) | `<proj>/X/subagents/agent-<hash>.jsonl` (nested) |
| Fork | `Y` (new) | `<proj>/Y.jsonl` (top-level sibling) |

Use `scripts/self-fork-detect.sh`:

```bash
# 1. Controller writes the contract BEFORE typing /fork:
bash scripts/self-fork-detect.sh write '<task the fork should execute>'
# Also captures a JSONL snapshot of ~/.claude/projects/ at this moment.

# 2. After /fork (or `claude --resume <sid> --fork-session`), in the new session:
bash scripts/self-fork-detect.sh detect
# Prints one of: ORIGINAL | FORK | SUBAGENT | NO_CONTRACT
# On FORK: also prints the task to execute + completion + abort signals.

# 3. (Optional) Controller locates the fork's JSONL on disk:
bash scripts/self-fork-detect.sh find-fork
# Prints any top-level JSONLs that didn't exist at write time and
# aren't the controller's own — fork candidates. Empty output =
# no fork yet (wait 1-2s after the user types /fork, then retry).

# 4. (Optional) Controller monitors the fork's progress live:
bash scripts/self-fork-detect.sh monitor <fork-jsonl-path>
# Polls the fork's JSONL (default every 5s), prints new assistant
# text as it appears, exits on "FORK-COMPLETE" marker OR if the
# JSONL stops growing for 120s (--idle-stop configurable).

# 5. After the fork's work is committed:
bash scripts/self-fork-detect.sh clear
```

Default contract path: `docs-private/.fork-contract.json` (gitignored by default per the existing `docs-private/` policy).

**Keyword marker vocabulary** (the only "return channel" — fork emits these in its assistant text; controller polls JSONL for them):

| Marker | Semantics | Monitor exit |
|---|---|---|
| `FORK-STATUS: <update>` | Intermediate progress; controller logs but keeps monitoring | (keeps polling) |
| `FORK-RESULT: <key>=<value>` | Structured output the controller should extract | (keeps polling) |
| `FORK-NEED: <question>` | Fork is blocked on a controller/user decision | **exit 2** (intervention required) |
| `FORK-COMPLETE: <summary>` | Fork is done | **exit 0** |
| `FORK-BLOCKED: <reason>` | Fork hit an unrecoverable issue, won't continue | **exit 1** |

`detect` (FORK case) prints this vocabulary to the fork so it knows what to emit; `monitor` greps for these strings and routes accordingly. The fork's work persists via git/RESUME-NOTES; the original controller also sees it on `/rewind` or `resume` if not actively monitoring.

The keyword mechanism is the workaround for forks lacking a task-notification analog. Subagents (Agent tool) get a proper callback on completion; forks don't, so the fork must DELIBERATELY emit grep-able strings the controller polls for.

**Resolving `FORK-NEED` (the fork asked a question; how does the controller reply?)** — forks similarly lack a clean inbound channel; there's no `--reply` flag that appends to a paused fork. Four options in order of cost:

1. **`/rewind` + redo in the controller** (default, recommended). Rewind to before the fork; do the work in the controller with the answer baked in. Zero extra cost. The fork's partial work, if it committed anything, persists on disk and can be cherry-picked or referenced.
2. **User manually replies in the fork window.** Switch to the fork's Claude Code window (or `claude --resume <fork-sid>` interactively) and type the reply. No cost; requires the user is at the keyboard. The fork resumes; the controller re-invokes `monitor` to keep watching.
3. **Sidecar reply file** (cooperative — needs pre-arranged contract). Controller writes `docs-private/.fork-reply.md` with the answer; the fork's contract pre-instructs it to re-read this file on its next turn. Still needs an external trigger for the fork's next turn (either option 2 or option 4) — so doesn't fully eliminate the orchestration step, but lets the controller stage the reply asynchronously.
4. **`scripts/self-fork-detect.sh reply <fork-sid-or-jsonl> '<reply prompt>'`** (API-billed; opt-in). Wraps `claude --resume <fork-sid> --print '<reply>'`. Fully automated. Anthropic's prompt caching makes this cheaper than the "claude -p antipattern" framing implies — the fork's prior conversation is cached prefix at ~10% rate, only the new reply turn + response are full new-token cost. For a short "read this file and answer" reply, typically a few cents per FORK-NEED resolution. The blanket "claude -p antipattern" rule in this skill is about WHOLE-CONVERSATION new dispatches at API rates (where session-billing would be free), not short cached-prefix continuations. Still meaningfully more expensive than option 1 over a long run; pick per context.

The right pick is usually option 1 — by the time FORK-NEED fired, the fork has already paused and the controller has the user's attention; `/rewind` + redo is faster than wrestling with the fork's reply channel.

When to use vs `[controller-direct]` vs Agent-tool subagent:

- **`[controller-direct]`** — controller does the work inline. Best for trivial chunks (single-file, <30 LoC) OR when controller-state matters but the work is short.
- **Agent-tool subagent** — fresh-context dispatch, task-notification on completion, full JSONL transcript. Best when you want the work in an isolated context (clean per-call), don't need controller's loaded state, and want the completion-notification affordance.
- **`/fork`** — branch the controller into a new session with full inherited state. Best when controller has substantial session-loaded context the work needs AND the work is risky/exploratory enough that a `/rewind`-able savepoint helps. Single-thread (no concurrent execution); reporting back is filesystem-only (commits + RESUME-NOTES; no task-notification equivalent for forks).

### Worktree convention

Controller works in the main worktree. Parallel-safe chunks each get `<repo>/.claude/worktrees/<adjective-noun>/` on a `claude/<adjective-noun>` branch. Codex trust prefix-matches the project root, so worktrees inherit automatically — no per-worktree registration. Cherry-pick onto main; do NOT use `git merge --ff-only` (sibling worktrees branched off main don't fast-forward cleanly when other worktrees committed since).

### Per-chunk loop (canonical shape)

1. **Dispatch** — pick one of the three paths above based on chunk shape.
2. **Wait** — do NOT poll the subagent transcript. The harness sends a task-notification on completion. Use the waiting interval for parallel-safe prep (next-chunk wrapper composition, RAG corpus drift, anticipatory questions).
3. **Verify the diff briefly** — scope contained, suite green, no leaked invariants. Executor's self-review already caught issues; you sanity-check.
4. **Commit** (one chunk = one commit). Parallel-mode: cherry-pick onto main.
5. **Update the Progress table** in goal-queue + your visible state.
6. **Look-ahead** — fire-and-forget Explore subagent reading the next 1–2 chunks for hidden dependencies or missing acceptance criteria.

### Progress table — keep this current

```
Chunk                       Status            Commit
1. <SLUG>                   ✅                <hash>
2. <SLUG>                   ✅                <hash>
3. <SLUG> (current)         🟡 in flight      —
#4 / #5 / #6                queued            —
#7                          post-<gate>       —

<branch> @ <head>, <N> green.
```

Status legend: ✅ done · 🟡 in flight · queued · blocked · post-`<gate>`.

### Three subagent types

| Type | Purpose | Source for the dispatched prompt | Reports |
|------|---------|----------------------------------|---------|
| **Executor** | Implement a `/goal` chunk; writes code, runs tests, commits | Controller composes per `prompts/dispatch-wrapper.md` (all 5 layers + optional RAG slices) | `git diff --stat`, P0/P1/P2/P3 findings, tests run, surprises |
| **Reviewer** | Read-only adversarial pass over a commit range or draft | Loaded as-is from `prompts/gstack-claude-review.md`, `prompts/gstack-codex-challenge.md`, or `prompts/decomposition-review.md` — these are reduced-layer shapes (effectively Layers 1+3, no Layer 0/2/5). Or invoked via gstack `/review` when registered (preferred). | Findings list with file:line refs, P0/P1/P2/P3, confidence |
| **Planner** | Write a plan document to a pinned path; "NO code changes" | Loaded as-is from `prompts/dual-plan-adversarial.md` (when dual-lens) or composed inline (Layers 1+3 + pinned deliverable path) | File path, word count, bottom-line recommendation, open questions |

The shapes for reviewer + planner live in their respective prompt files, NOT composed per-dispatch by the controller. The controller's job for these types is to substitute the few placeholders (`<range>`, `<paths>`, `<deliverable path>`) and dispatch; the full wrapper is in the file. Reviewer/planner dispatches that mistakenly receive the executor wrapper (all 5 layers) waste context and encourage drift into code-writing — keep them on the reduced shapes.

### Don't

- Run a separate reviewer subagent per chunk — the embedded self-review is the cheaper substitute. Reserve full multi-agent review for milestones.
- Bundle multiple chunks in one commit.
- Refactor outside the chunk's SCOPE mid-execution. File a follow-up chunk.
- Skip the diff verification because self-review reported clean.
- Use `git merge --ff-only` to integrate parallel-worktree subagent commits. Cherry-pick.
- Hand-write dispatch wrappers when the RAG corpus exists. Source layers 2/3/4 from the corpus with verification framing.
- Poll a background subagent. Wait for `task-notification`.

### Don't (cont.) — date format + notifications + worker-context

- **Date format**: `YYYY-MM-DD` from the conversation's `currentDate`. Same-day RESUME-NOTES bump `(rev N)` in H1.
- **Notifications** via `osascript -e 'display notification "X" with title "goal-flight"'` ONLY on blockers and queue completion. Session scrollback is the primary monitor. Forensics live in the harness session JSONL + per-subagent JSONL + codex tail files.
- **Worker context is optional**. Default: dispatches point executors at `AGENTS.md` directly. Create `docs-private/worker-context.md` only if AGENTS.md is huge (>1000 lines) or the project has distinct worker profiles.
- **High-level goal pinned at init** to `docs-private/<topic>-goal-statement-<today>.md`. Controller cites it in decompose-plan, mid-execute decisions, milestone summaries.

## Resume / goal sub-commands (handled inline)

### `resume`
1. Find the most recent `docs-private/RESUME-NOTES-*.md`. If none, bail: "Run `/goal-flight init <topic>` first."
2. Find the most recent goal-queue: `docs-private/*-goal-queue-*.md`.
3. Read git state via Bash: `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git log --oneline -20`.
4. Spawn a subagent to write `docs-private/RESUME-NOTES-<today>.md`. If today's file exists, increment `(rev N)` in H1 instead of overwriting. RESUME-NOTES contains: TL;DR (one paragraph), Progress table verbatim, in-flight executor IDs/PIDs, reading order on wake (numbered file list), first-5-minutes (exact next steps).
5. Print: file path; placeholders the user still needs to fill in.

### `goal <SLUG>`
1. Find the most recent goal-queue.
2. Find the highest existing `## N.` goal number.
3. Append: `## <N+1>. /goal <SLUG>` with SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN skeleton; tags `[parallel-safe:<group>]` `[milestone]` `[controller-direct]` `[goal-mode]` `[max-iterations:<N>]` `[mixed-executor]` as applicable.
4. Append a row to the Progress table: `| #<N+1> \`<SLUG>\` | TODO |`.
5. Print the appended block.
