# decompose-plan [<plan-file>]

Break a plan into numbered `/goal` chunks. The plan source can come from:

1. **A file path argument** — `/goal-flight decompose-plan docs-private/refactor-plan.md`
2. **An in-context conversation** — no arg; the plan was discussed in this session (the user reviewed the plan with you and it's in the chat log)
3. **A file Read'd earlier in this session** — no arg; you find the most relevant from session context

If the source is ambiguous, ask the user which plan they want decomposed before proceeding.

## Steps

### 0. Read the goal statement (load-bearing anchor)

Read `<repo-root>/docs-private/<topic>-goal-statement-*.md` (most recent).

- **If absent**: bail with: "No goal statement found. Run `/goal-flight init <topic>` first to pin the high-level goal."
- **If status is `DRAFT`**: refuse to proceed. Return: "Goal statement at `<resolved-file-path>` is still DRAFT (`<reason from file>`). Sharpen before continuing — pick one: (1) re-run `/goal-flight init <topic>` and accept the gstack `/office-hours` interrogation; or (2) edit `<resolved-file-path>` directly, replace the `Status: DRAFT — <reason>` line with `Status: CONCRETE`, then re-run `/goal-flight decompose-plan`." Cite the resolved absolute path so the user can open or `sed -i` it without re-deriving the slug.
- **If concrete**: keep its content in mind for steps 4.5 and 6 (coherence check and summary).

### 1. Establish plan source

- File arg present: read it (delegate to Explore subagent if >300 lines).
- No arg: scan the recent conversation for plan-shaped content (numbered phases, "step 1 / step 2", architecture sections, refactor discussions). Summarize what plan you think they mean (1-2 sentences) and confirm with the user before proceeding.
- If multiple candidates: list them and ask which.

### 2. Spawn drafter + analyst (sequential — analyst depends on drafter)

**Drafter** (general-purpose Agent): produce the numbered `/goal` decomposition.

> "Read the plan below. Decompose it into N self-contained `/goal` chunks. Each chunk must have SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN sections (skeleton at the bottom of this prompt). Smallest-first; imperative voice. Number them 1..N. Surface anything in the plan that resists decomposition or requires controller-side judgement. Plan: <paste plan text or path>.
>
> Per-chunk skeleton:
> ```
> ## <N>. /goal <SLUG>
> STATUS (optional — pin observed reality if goal is touched post-write)
> SCOPE
> <1-3 sentence problem + boundary; what module(s), what contract>
>
> PRECONDITION (optional)
> - <upstream goal slug + commit, or env requirement>
>
> REFERENCE
> - AGENTS.md (hard invariants)
> - docs-private/<topic>-binding-spec.md §<N>
>
> CHECKLIST
> 1. <smallest-first imperative>
>    [Reviewer note: <margin annotation when checklist amended mid-flight>]
> 2. <...>
>
> ACCEPTANCE
> - <testable criterion>
> - All passing tests stay green
>
> FORBIDDEN
> - <explicit anti-scope: paths or patterns not to touch>
> ```"

When drafter completes, **analyst** (Explore): identify parallel-safe chunks AND trivial chunks the controller can handle inline.

> "Given these N drafted chunks, two tagging passes:
>
> (1) **`[parallel-safe:<group-id>]`** — chunks that touch disjoint files/modules and could safely run in parallel worktrees. Chunks in the same group can run together; different groups must be sequential relative to each other if they share dependencies. Conservative bias: when unsure, do not tag.
>
> (2) **`[controller-direct]`** — chunks where dispatching a subagent would cost more than just doing the work inline. Two distinct triggers:
>
>     **A. Trivially small work.** Single-file change, < ~30 LoC delta, no cross-module coupling, no new public surface, no test-harness changes. Dispatch overhead exceeds the work. Examples: typo fixes, version bumps, single-constant renames, single-line bug fixes confirmed against an existing failing test.
>
>     **B. Too much context to explain.** The controller has already loaded substantial relevant state (read files, traced data flow, accumulated reasoning across prior chunks in this session) that a fresh subagent would have to re-discover via dispatch wrapper. When the cost of EXPLAINING the context (wrapper rendering + executor re-load) exceeds the cost of just doing the work, controller-direct wins on the same overhead-arbitrage logic as case A, but for a different reason. Heuristic: if a clean dispatch wrapper for this chunk would exceed ~5 KB (the verification-first target size per `prompts/dispatch-wrapper.md`) primarily because the chunk depends on session-loaded controller state, prefer inline. Common shapes: mid-debug chunks where the controller has just diagnosed the bug; chunks that resolve a P0 from a milestone review the controller just consumed; chunks that depend on rolling decisions made in the last 10 turns that haven't been promoted to `docs-private/rag/decisions.md` yet.
>
> Conservative bias: when unsure, do NOT tag — let `execute.md` dispatch a subagent. The default subagent path is safer for ambiguous cases (clean context, transcript record, parallel-safe candidates).
>
> Report which file paths each chunk touches (audit trail for parallel safety + controller-direct triviality). Drafter output: <paste>."

### 3. Write to goal-queue

Write to `<repo-root>/docs-private/<topic>-goal-queue-<today>.md` with this shape:

```
# <TOPIC> Goal Queue
Date: <today>
Working directory: <repo-root>
Skill-loaded: <LOADED_LINE from SKILL.md §Session pre-flight probe 1, captured at decomposition time>

Each goal is a self-contained `/goal` dispatch directive. Read together with AGENTS.md, the binding-spec, and the plan of record.

## Progress (as of <today>, main @ <head>)
| Goal | Status | Commit |
|------|--------|--------|
| 1. `<slug>` | TODO | — |
| 2. `<slug>` [parallel-safe:A] | TODO | — |
...

Status: ✅ DONE — `<hash>` · 🟡 IN-FLIGHT — `<executor-id>` · TODO · BLOCKED — `<reason>` · PARTIAL — `<reason; see #N>`

Tags (see SKILL.md for full definitions):
- [parallel-safe:<group>] — chunks in the same group can run via `--parallel N`
- [milestone] — trigger gstack review sweep after this chunk lands
- [controller-direct] — controller handles inline (trivial OR too much session-loaded context to explain)
- [goal-mode] — chunk warrants the iteration loop primitive (codex /goal native or external Opus/Grok loop)
- [max-iterations:<N>] — cap for [goal-mode] external loops
- [mixed-executor] — iterations cross executor types for model-diversity stuck-loop recovery

## Universal preconditions
- All passing tests stay green; new tests added per goal
- No silent fallback between providers on unit failure
- <other AGENTS.md-derived invariants>

## <N>. `/goal <SLUG>` (per-chunk skeleton from the drafter above)
```

If a same-day file exists, append new chunks numbered after the last existing entry (do not duplicate). Tags `[parallel-safe:<group>]` come from the analyst (step 2 above); other tags applied by analyst or controller as judgment dictates.

### 4. Review the decomposition itself (parallel reviewers)

**Two reviewers in parallel.** The decomposition is the artifact under review. Both reviewers use the same gstack `/plan-eng-review` framing (when installed) for consistent severity ranking; they produce independent findings since they're different models.

**Claude challenger** — preferred Claude-side path:

- If gstack registered on Claude side (`~/.claude/skills/gstack/` exists): invoke `Skill(skill: "plan-eng-review", args: "<path to docs-private/<topic>-goal-queue-<today>.md>. Reference: docs-private/<topic>-goal-statement-*.md, AGENTS.md.")` directly, OR dispatch a subagent that invokes it (use subagent if you want to isolate context).
- If gstack absent on Claude side: spawn a subagent (Agent tool, general-purpose) with `prompts/decomposition-review.md` prompt + same plan + decomposition + the goal-statement pasted in.

**Codex challenger** (background, parallel second opinion):

- If gstack registered on codex side (`~/.codex/skills/gstack/` exists):
  ```bash
  timeout --kill-after=10 300 codex exec '/plan-eng-review <path to docs-private/<topic>-goal-queue-<today>.md>. Reference: docs-private/<topic>-goal-statement-*.md, AGENTS.md.' > /tmp/goal-flight-decomp-codex-<topic>.txt 2>&1 &
  ```
- If gstack absent on codex side — point codex at the prompt file on disk, don't paste its contents into the exec arg:
  ```bash
  timeout --kill-after=10 300 codex exec \
    "Read ~/.claude/skills/goal-flight/prompts/decomposition-review.md in full and execute it. Plan: <path-to-plan-file>. Drafted decomposition: docs-private/<topic>-goal-queue-<today>.md. Goal-statement: docs-private/<topic>-goal-statement-*.md. If your context compacts mid-review, re-read the prompts file — the file is the unparaphrased source of truth." \
    > /tmp/goal-flight-decomp-codex-<topic>.txt 2>&1 &
  ```
  Avoids spamming the controller's tokens with pre-pasted prompt + plan + decomposition; survives codex session compaction; bypasses any CLI argument length limit. Same principle as `SKILL.md` §Codex reliability "keep the prompt short — pass pointers."

Capture the PID. The output goes to a temp file.

Wait for both. (Codex: poll the temp file or `wait $PID`. Claude reviewer: returns when done.)

If codex stalls (the `timeout(1)` wrapper fires after 300 s, or the optional watchdog kills on zero-output ≥90 s / no-progress ≥180 s — see `SKILL.md` §Codex reliability): proceed with the Claude reviewer's findings only. Note in RESUME-NOTES' "In-flight" section.

### 4.5. Verify the decomposition serves the goal

Spawn a quick coherence subagent (Explore):

> "Read goal-statement: `<paste file content>`. Read drafted decomposition: `<paste numbered chunks>`. Does this decomposition, if executed end-to-end, plausibly achieve the goal-statement's success criteria? List any chunks that don't trace to a goal element. List any goal elements that no chunk addresses. Score: aligned / partial / divergent."

If `partial` or `divergent`: surface to user; do NOT proceed to ask-questions until the gap is resolved (revise either the decomposition or the goal-statement). The user shouldn't have to remind the session what the goal is mid-execute — catch divergence here.

### 5. Consolidate findings; offer revision

Dedupe findings across the two reviewers. Severity-rank P0/P1/P2/P3. Surface to user:

> "Decomposition reviewed by codex + claude. Found N P0 (must-fix), M P1 (should-fix), K P2 (nice-to-have). [Brief summary of each P0/P1.] Want me to revise the decomposition before continuing, or proceed to ask-questions?"

If user says revise: re-spawn the drafter with the findings as input; loop back to step 3.

### 6. Confirm and summarize

Before declaring decompose-plan done, print to the user:

- **Total chunks**: N
- **Parallel-safe groups**: K (with chunk counts per group)
- **Estimated commit count**: N (one per chunk) + ~N/5 for milestone fix-clusters
- **Files the user should review before launching execute**:
  - `docs-private/<topic>-goal-queue-<today>.md`
  - `docs-private/RESUME-NOTES-<today>.md` (if updated)
  - Any binding-spec or plan-of-record referenced

> "Plan looks right? Want me to launch ask-questions, or does anything need revision first?"

### 7. Auto-chain to ask-questions

Unless the user explicitly said no in step 6, immediately invoke `commands/ask-questions.md` with `--scope decomposition`. (Do this without re-prompting; the auto-chain is part of decompose-plan's contract.)

### 8. Update RESUME-NOTES

Refresh `docs-private/RESUME-NOTES-<today>.md` (bump rev) with: decomposition complete, N chunks, K parallel-safe groups, queue file path, next step `/goal-flight execute`. Delegate the writing to a subagent so the controller doesn't burn its own context.
