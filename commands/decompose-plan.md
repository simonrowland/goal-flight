# decompose-plan [<plan-file>]

Break a plan into numbered `\goal` chunks. The plan source can come from:

1. **A file path argument** — `/goal-flight decompose-plan docs-private/refactor-plan.md`
2. **An in-context conversation** — no arg; the plan was discussed in this session (the user reviewed the plan with you and it's in the chat log)
3. **A file Read'd earlier in this session** — no arg; you find the most relevant from session context

If the source is ambiguous, ask the user which plan they want decomposed before proceeding.

## Steps

### 0. Read the goal statement (load-bearing anchor)

Read `<repo-root>/docs-private/<topic>-goal-statement-*.md` (most recent).

- **If absent**: bail with: "No goal statement found. Run `/goal-flight init <topic>` first to pin the high-level goal."
- **If status is `DRAFT`**: refuse to proceed. Return: "Goal statement is still DRAFT (`<reason from file>`). Sharpen it first — re-run `/goal-flight init <topic>` and accept the office-hours interrogation, or edit the file directly and remove the DRAFT marker."
- **If concrete**: keep its content in mind for steps 4.5 and 6 (coherence check and summary).

### 1. Establish plan source

- File arg present: read it (delegate to Explore subagent if >300 lines).
- No arg: scan the recent conversation for plan-shaped content (numbered phases, "step 1 / step 2", architecture sections, refactor discussions). Summarize what plan you think they mean (1-2 sentences) and confirm with the user before proceeding.
- If multiple candidates: list them and ask which.

### 2. Spawn drafter + analyst (sequential — analyst depends on drafter)

**Drafter** (general-purpose Agent): produce the numbered `\goal` decomposition.

> "Read the plan below. Decompose it into N self-contained `\goal` chunks. Each chunk must have SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN sections per the goal-queue template at `~/.claude/skills/goal-flight/templates/goal-queue.tpl` (read it for the exact skeleton). Smallest-first; imperative voice. Number them 1..N. Surface anything in the plan that resists decomposition or requires controller-side judgement. Plan: <paste plan text or path>."

When drafter completes, **analyst** (Explore): identify parallel-safe chunks.

> "Given these N drafted chunks, identify which touch disjoint files/modules and could safely run in parallel worktrees. Tag each parallel-safe chunk with `[parallel-safe:<group-id>]` (chunks in the same group can run together; different groups must be sequential relative to each other if they share dependencies). Conservative bias: when unsure, do not tag. Report which file paths each chunk touches; this becomes the audit trail for parallel safety. Drafter output: <paste>."

### 3. Write to goal-queue

Render `templates/goal-queue.tpl` with the drafted chunks and `[parallel-safe:<group>]` tags. Write to `<repo-root>/docs-private/<topic>-goal-queue-<today>.md`. If a same-day file exists, append the new chunks numbered after the last existing entry (do not duplicate).

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
- If gstack absent on codex side:
  ```bash
  timeout --kill-after=10 300 codex exec '<contents of prompts/decomposition-review.md, with the plan + drafted decomposition pasted in>' > /tmp/goal-flight-decomp-codex-<topic>.txt 2>&1 &
  ```

Capture the PID. The output goes to a temp file.

Wait for both. (Codex: poll the temp file or `wait $PID`. Claude reviewer: returns when done.)

If codex stalls (the `timeout(1)` wrapper fires after 300 s, or the optional watchdog kills on zero-output ≥90 s / no-progress ≥180 s — see `reference/pattern.md` §Codex reliability): proceed with the Claude reviewer's findings only. Note in RESUME-NOTES' "In-flight" section.

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
