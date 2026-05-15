# execute [--parallel <N>]

Run the per-chunk loop. **Sequential by default.** With `--parallel N`: spawn up to N parallel-safe chunks simultaneously in separate worktrees.

## Steps

### 1. Pre-flight

- Find most recent `docs-private/RESUME-NOTES-*.md`. **Delegate the read to an Explore subagent** if it's large; consume the subagent's summary.
- Find most recent goal-queue: `docs-private/<topic>-goal-queue-*.md`.
- Verify reality via Bash: `git log --oneline -10`, `git status`, `git rev-parse HEAD`. Confirm matches RESUME-NOTES' Code-state section.
- Quick test smoke (if known): e.g. `pytest --collect-only` or `npm test -- --listTests`. Confirm baseline.
- If any drift between RESUME-NOTES and reality: pause, surface to user, do not dispatch.

### 2. Per-chunk loop — sequential mode

For each non-DONE goal in the queue (in order):

**a. Build dispatch prompt — render the 5-layer wrapper from `prompts/dispatch-wrapper.md`.**

Raw goal text from the queue is NOT enough. Field-validated practice (55 dispatches sampled): real dispatches that complete cleanly are 6–11 KB; goal text alone is 600–1200 chars. The delta is the 5-layer wrapper the controller composes per dispatch.

Render the dispatch prompt as:

```
\goal <SLUG>

[Layer 1: situational frame — main HEAD, what just landed, what this dispatch's role is]

[Goal text pasted from queue — SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN]

[Layer 2: template-provider pointer — name the canonical mirror + the differences]

[Layer 3: file-path-and-line anchors — flat list of paths, commit hashes, class names]

[Layer 4: environment caveats — optional deps, skip patterns, install state]

[Layer 5: goal-specific self-review — §7 categories from prompts/executor-self-review.md
 SPECIALIZED to this goal's grep patterns + nouns + line numbers. Do not paste raw.]

Report format: see prompts/executor-self-review.md.
Read <repo-root>/docs-private/worker-context.md (if exists) or <repo-root>/AGENTS.md.
```

For **trivial single-file goals** (likely LoC delta < 50, no new public surface, no cross-module coupling): layers 1 + 5 alone suffice. Skip 2/3/4.

**If `docs-private/rag/` corpus exists** (built by init step 3.5): use the slice-to-layer mapping at the bottom of `prompts/dispatch-wrapper.md`. Quick reference:
- Layer 2 ← one `patterns/<pattern>.md` (the canonical example this chunk mirrors)
- Layer 3 ← `file-map.md` (or `file-map/*.md` set if split) + relevant `binding-spec/<intent>.md` slices
- Layer 4 ← `verification.md` (or `verification/tests.md` + `verification/grep-invariants.md` if split) + topic-filtered entries from `decisions.md` (or relevant epoch slice if `decisions.md` was epoch-split)
- Tail ← `invariants.md` (always)
- Layers 1 + 5 always hand-composed per dispatch

For split slices: paste the entire split set, not a subset. Splits manage word budget; relevance filtering happens at the family level (paste this slice or not), not within a family.

If the corpus exists and the dispatch doesn't paste from it for layers 2/3/4, the controller has regressed to hand-composition; self-check before sending.

Read `prompts/dispatch-wrapper.md` for the full per-layer worked examples (sourced from goals #8/#9/#10 dispatches in the goal-flight reference project) and the complete slice-to-layer mapping.

**b. Dispatch as Claude subagent** via the Agent tool (general-purpose). Pass:
- `description`: short phrase like "Execute \\goal <SLUG>"
- `prompt`: the full dispatch prompt from (a)
- `model`: `"opus"` for code-writing chunks (default for execute). Use the agent definition's default model for non-code chunks (planning, review writeups, docs prose).
- The subagent works in the main worktree.

If using codex instead of an Agent-tool dispatch, include the highest-reasoning flag in the `codex exec` invocation (verify the current codex CLI flag — at time of writing `-c model_reasoning_effort=xhigh` is the pattern). Same principle: perfectionist output for code-writing chunks; default reasoning for non-code work.

**c. Wait** for the executor to report done.

**d. Verify diff briefly.** Run in parallel:
- `git diff --stat` — scope contained?
- `git diff` (read first 200 lines) — quick scan for FORBIDDEN actions; grep for the mutator patterns the goal forbade.
- Run the test command (or relevant subset) per worker-context.md.

If any check fails: surface findings to user OR re-dispatch the executor with the failing finding as input. Do not commit on failure.

**e. Commit** (one chunk per commit). Message: short imperative + `(chunk N/M)` suffix. Use HEREDOC.

**f. Update Progress table** in goal-queue: `| #N <slug> | DONE — <hash> |` (replace previous TODO/IN-FLIGHT entry).

**g. Spawn look-ahead subagent** (read-only Explore, fire-and-forget — don't block on its output):
> "Read the goal-queue. The next 1-2 chunks (#<N+1>, #<N+2>) are about to dispatch. Scan the binding-spec, AGENTS.md, and current code state for: hidden dependencies, ambiguities the executor will hit, missing acceptance criteria. Report any anticipatory questions in the format from `prompts/ask-anticipatory.md`. If nothing material, say so."

If the look-ahead returns material questions, run `commands/ask-questions.md --scope next-chunks` non-blockingly (queue for next pause).

### 3. Per-chunk loop — parallel mode (`--parallel N`)

For each batch of up to N consecutive `[parallel-safe:<group>]` chunks:

**a. Spawn worktrees.** For each chunk in the batch:
```bash
git worktree add <repo-root>/.claude/worktrees/<adjective-noun-N>/ -b claude/<adjective-noun-N>
```

**b. Dispatch each as a Claude subagent in its own worktree.** Pass the worktree path in the agent description so the subagent knows where to work.

**c. Controller monitors all N.** As each reports done:
- Verify diff in that worktree (same checks as sequential step 2d).
- Commit in the worktree (the subagent typically does this since it lives inside the isolated worktree's git context).
- Cherry-pick onto main: from main worktree, `git cherry-pick <subagent-commit-hash>`. **Cherry-pick is the default; do NOT use `git merge --ff-only`** — isolated worktrees branched off main do not fast-forward cleanly when sibling worktrees committed since the shared base, so merge --ff-only fails or pulls in unrelated history. Cherry-pick is safer and produces a clean linear stack of one-commit-per-chunk on main, which is also what the Progress table assumes.
- After cherry-pick: run integration pytest from main to catch cross-cluster regressions (each subagent ran tests in isolation; this is the first time all the changes coexist).
- Update Progress table; log.

**d. After all N land**: leave the agent worktrees in place if locked (system manages cleanup); otherwise collapse worktrees (`git worktree remove`); delete branches (`git branch -d`); update Progress table for the batch; log.

**e. If any one chunk fails**: do not block the others. Isolate the failure (mark chunk BLOCKED in queue), notify user (`osascript -e 'display notification "Chunk N blocked: <reason>" with title "goal-flight" sound name "Funk"'`), continue with the rest.

### 4. Periodic gstack review

Every K commits (default K=5; configurable via `--review-every <K>`), or at user-flagged milestones (chunks tagged `[milestone]` in the queue):

**a. Capture the commit range:** `<last-review-head>..<current-head>`.

**b. Two reviewers in parallel — choose the diversity axis.**

Reviewers should be independent on a load-bearing axis. Two axes available:

- **Model diversity** (Claude + codex). Good for numerical/algorithmic chunks where the two models' blind spots are different. The default historically.
- **Concern diversity** (two Claude subagents with disjoint lenses, e.g. "chemistry/accounting correctness" vs "code-quality + architecture consistency"). **More load-bearing for 12-hour refactor runs** — one reviewer can't cover both axes deeply enough; concern-splitting produces 2× the surface coverage.

For big refactor milestones (multi-file, multi-commit), prefer concern diversity. For small numerical / API chunks where you want a second model's blind spots, prefer model diversity. Mix: one Claude reviewer (concern: chemistry) + one codex reviewer (concern: code quality) splits BOTH axes.

Both reviewers use the same gstack `/review` framing (when installed) for consistent severity ranking; they produce independent findings since they're different models OR different concerns.

**Claude challenger** — preferred Claude-side path:

- If gstack registered on Claude side: invoke `Skill(skill: "review", args: "<start-hash>..<end-hash>. Reference: AGENTS.md, docs-private/<topic>-goal-statement-*.md, docs-private/<topic>-goal-queue-*.md. Output findings as P0/P1/P2/P3.")` directly, OR dispatch a subagent that invokes it (use subagent if you want to isolate context — recommended for 12-hour runs).
- If gstack absent on Claude side: spawn a subagent (Agent tool, general-purpose) with `prompts/gstack-claude-review.md` prompt + commit range + paths.

**Codex challenger** (background, parallel second opinion):

- If gstack registered on codex side:
  ```bash
  codex exec '/review <start-hash>..<end-hash>. Reference: AGENTS.md, docs-private/<topic>-goal-statement-*.md, docs-private/<topic>-goal-queue-*.md. Output findings as P0/P1/P2/P3.' > /tmp/goal-flight-gstack-codex-<topic>-<iso>.txt 2>&1 &
  ```
- If gstack absent on codex side:
  ```bash
  codex exec '<contents of prompts/gstack-codex-challenge.md, with commit range and goal-queue path pasted in>' > /tmp/goal-flight-gstack-codex-<topic>-<iso>.txt 2>&1 &
  ```

Capture PID; poll temp file for completion.

**Optional third pass** for security-relevant changes (touches auth, sessions, input handling, SQL, deserialization) — gstack `/cso`:

- Claude-side: `Skill(skill: "cso", args: "<start-hash>..<end-hash>")` if registered, else dispatch subagent.
- Codex-side: `codex exec '/cso <start-hash>..<end-hash>' > /tmp/goal-flight-gstack-cso-<topic>-<iso>.txt 2>&1 &` if registered.

**Fourth pass — RAG corpus drift review** (when `docs-private/rag/` exists from init step 3.5):

The corpus is load-bearing for every dispatch's wrapper layers 2-4. As goals land, decisions change, helpers get lifted, file paths move — slices drift away from current reality. Catch drift at milestone cadence so the next batch of dispatches uses fresh context.

**Sequencing matters**: drift review must run AFTER the milestone's fix-cluster commits have been cherry-picked onto main. If drift review captures its commit range at milestone START, fix-cluster commits land BEFORE drift consumes them — and drift may incorrectly pass on a corpus the fix-clusters have just invalidated. Capture the drift-review range as `<milestone-start>..<HEAD-after-fix-clusters-landed>`, not `<milestone-start>..<milestone-review-start>`.

- Dispatch a Claude subagent (after fix-clusters have landed) with this prompt:
  > "RAG corpus drift review. Read every file in `docs-private/rag/`. For each slice, verify against current code state: (a) do file:line refs still exist? (b) do grep patterns still match? (c) has any decision in `decisions.md` been amended/reversed by a commit in `<milestone-start>..<HEAD-after-fix-clusters>` without the slice being updated? (d) do `patterns/*.md` files still describe the canonical implementation, or has it moved/been lifted? Report per-slice P0/P1/P2/P3 findings."
- Apply fixes inline (controller-direct) for small drift; re-dispatch a slice-builder for major drift (>30% of slice needs rewriting).

If codex isn't available or stalls: proceed with Claude only; note in summary.

**c. Wait for both**; consolidate findings (dedupe; severity-rank P0/P1/P2/P3).

**d. For findings: spawn fix-cluster subagents in parallel.** Group findings by ownership (which file/module they affect); one Claude subagent per ownership-clean cluster. Each subagent:
- Receives its cluster of findings.
- Implements fixes.
- Runs the executor self-review pass before reporting done.

**Each cluster prompt MUST explicitly forbid touching files owned by other clusters.** Without this, parallel fix clusters race on shared files. Wording template:

> "Your scope is the `engines/builtin/` tree + new `tests/chemistry/test_kernel_control_audit.py`. **DO NOT touch `simulator/*` — that's Cluster B's slice. DO NOT touch `docs/` or `docs-private/` — that's Cluster C's slice.** If you find an issue requiring changes outside your scope, surface it in your report rather than implementing — the controller will route to the right cluster or queue as a follow-up."

This is what makes the 3-cluster parallel fix pattern from goal #7's milestone settlement race-free in practice. The forbid list is as load-bearing as the SCOPE.

Cherry-pick each cluster's commits onto main (NOT `git merge --ff-only` — see parallel-mode step c).

**e. After convergence:**
- Bump RESUME-NOTES rev (delegate to subagent) including a one-line milestone-review summary (commit range, findings count, cluster count).
- Update Progress table with milestone-review entries.

### 5. Handoff before compact

When the controller's context is approaching limit (heuristic: turn count above threshold, or user signals):

- **Spawn a subagent** to write fresh `docs-private/RESUME-NOTES-<today>.md`. Bump `(rev N)` if same-day file exists. Subagent reads current state (git log, in-flight chunk, queue progress), renders `templates/RESUME-NOTES.tpl`, writes the file.
- Controller's NEXT turn can be the one that gets compacted; the resume notes ensure a fresh controller can wake up cleanly.

### 6. Notifications

Only fire on:

- **Blocker**: `osascript -e 'display notification "Blocker: <reason>" with title "goal-flight" sound name "Funk"'`
- **Queue completion**: `osascript -e 'display notification "Queue complete: N chunks done" with title "goal-flight" sound name "Glass"'`

Never notify on routine commit success. Never notify on milestone-review completion (it's expected).

### 7. Mid-execute ask-questions

If the controller hits an ambiguity that the look-ahead subagent flagged but couldn't resolve, OR if the current chunk's SCOPE has a genuine question the executor needs answered: invoke `commands/ask-questions.md --scope current-chunk`. Block the loop only if the answer is required for the chunk's safe completion.

### 8. Termination

When the queue is fully DONE (no TODO, IN-FLIGHT, or BLOCKED):

- Final milestone gstack review on the entire run (commit range from queue start to HEAD).
- Notify user.
- Bump RESUME-NOTES rev with "QUEUE COMPLETE" in TL;DR.
- Print summary: total chunks, total commits, milestone-review count, P0/P1 found+fixed, time elapsed (if knowable).

### 9. Dispatch types

The controller spawns three distinct subagent types. Each has a different prompt shape; mark the type in the Agent tool's `description` field so logs are skimmable.

| Type | Purpose | Recommended wrapper layers (per `prompts/dispatch-wrapper.md`) | Reports |
|------|---------|---------------------------------------------------|---------|
| **Executor** | Implement a `\goal` chunk; writes code, runs tests, commits. | All 5 (situational, template-pointer, file-anchors, env-caveats, goal-specific self-review). Plus `prompts/executor-self-review.md`. | `git diff --stat`, self-review findings (P0/P1/P2/P3), tests run, surprises. |
| **Reviewer** | Read-only adversarial pass over a commit range or a draft. | Layers 1+3 typically; layer 4 (env caveats) usually skipped because reviewers don't need test-environment fluency. Add layer 2 if reviewing a chunk that mirrors a specific canonical pattern. **Always include topic-filtered `decisions.md`** so the reviewer knows what was deliberately rejected and doesn't re-flag it. Plus `invariants.md` tail (always). | Findings list with file:line refs, P0/P1/P2/P3, confidence rating. |
| **Planner** | Write a plan document to a pinned path; explicitly "NO code changes." | Layers 1+3 + the pinned deliverable path. **Always include topic-filtered `decisions.md`** so the planner doesn't re-open closed decisions. Plus `invariants.md` tail (always). The deliverable IS the output. | File path, word count, bottom-line recommendation, open questions. |

Wrapper layers per type are recommendations, not contracts — the audit substrate (`prompts/dispatch-wrapper.md`'s 55-dispatch sample) is from one project's executor dispatches; reviewer and planner samples were a handful each. Adjust as the work demands.

Examples from goal-flight reference project:
- Executor: goal #8 ALPHAMELTS-DIAGNOSTIC-GATE dispatch, 11 KB, layers 1–5.
- Reviewer: milestone gstack review of `a259f80..f10c405`, 4 KB, layers 1+3.
- Planner: mixed-cation oxides plan A + plan B (parallel planners), 3 KB each, layers 1+3 + pinned `docs-private/mixed-cation-oxides-plan-{A,B}-*.md` paths.

Distinguishing them prevents the controller from accidentally giving a reviewer the full wrapper (wastes their context) or a planner the executor wrapper (encourages drift into code-writing).
