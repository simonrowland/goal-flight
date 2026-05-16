# execute [--parallel <N>]

Run the per-chunk loop. **Sequential by default.** With `--parallel N`: spawn up to N parallel-safe chunks simultaneously in separate worktrees.

## Steps

### 1. Pre-flight

- Find most recent `docs-private/RESUME-NOTES-*.md`. Delegate the read to an Explore subagent if it's large.
- Find most recent goal-queue: `docs-private/<topic>-goal-queue-*.md`.
- **Skill-update drift check** — if either file carries a `Skill-loaded:` header, compare it to the live `LOADED_LINE` per `SKILL.md` §Session pre-flight probe 1 recipe. If different, surface the one-line nudge from probe 4 before composing any dispatch wrapper (executor prompts will still be valid, but the controller should know the skill itself changed under it).
- Verify reality via Bash: `git log --oneline -10`, `git status`, `git rev-parse HEAD`. Confirm matches RESUME-NOTES' Code-state section.
- Quick test smoke if known (e.g. `pytest --collect-only`).
- If drift between RESUME-NOTES and reality: pause, surface to user, do not dispatch.

### 2. Per-chunk loop — sequential mode

For each non-DONE goal in the queue (in order):

**a. Build dispatch prompt** per `prompts/dispatch-wrapper.md` — verification-first scaffolding, target 3–5 KB. Layer 0 (worktree-isolated dispatches) captures expected base SHA from `git fetch origin && git rev-parse origin/main` in the MAIN worktree, not controller cwd. Layers 1–5 scaffold investigation rather than pre-paste content. Tail with `invariants.md` if corpus exists.

If the `docs-private/rag/` corpus exists, paste slice content per the slice-to-layer mapping in `prompts/dispatch-wrapper.md` — as starting hypotheses the executor verifies, not authoritative facts.

**b. Execute the chunk.** Branch on chunk tags from `decompose-plan` step 2 (see `SKILL.md` §Three dispatch paths for the full decision matrix):

- **`[controller-direct]`** — controller inline: Read + Edit + run test subset + adversarial self-review (Layer 5 specialized against own diff). Skip steps c–d below; jump to commit. Use only for chunks tagged at decompose-plan time. Don't retag mid-execute — if a chunk turns out trickier, abort inline and fall through to the subagent path.
- **`[goal-mode]`** — codex `/goal` mode (in-session loop) or external Opus/Grok iteration loop. Render `templates/codex-goal-prompt.md.tpl`; dispatch per the path chosen.
- **Untagged (default)** — Claude Agent (`model: "opus"`, highest reasoning for code-writing chunks). Or codex via `timeout 300 codex exec '...'` if you want model-diversity for this chunk (codex auto-activates `/goal` mode on goal-shaped prompts when `features.goals = true`).

**Fork primitives** are the heavier "branch the controller's state" tool when a chunk depends on session-loaded context AND you want a `/rewind`-able savepoint. See `SKILL.md` §Self-delegation via `/fork` for the full pattern (controller writes contract via `scripts/self-fork-detect.sh write`; fork detects via `detect`; monitor watches the marker vocabulary).

**c. Wait** for executor's task-notification. Don't poll the transcript.

**d. Verify diff briefly:** `git diff --stat` (scope contained?) + `git diff` first 200 lines (FORBIDDEN actions? mutator patterns the goal banned?) + run the test subset.

If any check fails: surface to user OR re-dispatch the executor with the failing finding as input. Don't commit on failure.

**e. Commit** (one chunk per commit). Imperative subject + `(chunk N/M)` suffix. HEREDOC for the body.

**f. Update Progress table** in goal-queue: `| #N <slug> | DONE — <hash> |`.

**g. Spawn look-ahead** (read-only Explore, fire-and-forget): "Read the goal-queue. Next 1–2 chunks about to dispatch. Scan binding-spec / AGENTS.md / current code for hidden dependencies, ambiguities, missing acceptance criteria. Report anticipatory questions per `prompts/ask-anticipatory.md`." If material questions surface, queue `commands/ask-questions.md --scope next-chunks` non-blockingly.

### 3. Per-chunk loop — parallel mode (`--parallel N`)

For each batch of up to N consecutive `[parallel-safe:<group>]` chunks:

**a. Spawn worktrees:** `git worktree add <repo-root>/.claude/worktrees/<adjective-noun-N>/ -b claude/<adjective-noun-N>`. Codex trust prefix-matches the project root, so worktrees under `<repo-root>/.claude/worktrees/*` inherit trust automatically (no per-worktree registration; see `SKILL.md` §Worktree convention).

**b. Dispatch each as a Claude subagent in its own worktree.** Pass the worktree path in the agent description.

**c. Controller monitors all N.** As each reports done:
- Verify diff in that worktree.
- Commit in the worktree.
- Cherry-pick onto main from main worktree. **Never `git merge --ff-only`** — sibling worktrees branched off main don't fast-forward cleanly when other worktrees have committed since the shared base.

**If `git cherry-pick` reports CONFLICT** (sibling chunks edited adjacent territory or made conflicting assumptions):

- Capture: `git status` for conflicted paths, the failing chunk's slug + branch, the prior-landed commits in this batch.
- `git cherry-pick --abort` in main. The failing chunk's commit still exists on its branch; can be re-dispatched.
- Classify:
  - **Mechanical** (disjoint edits the 3-way merge couldn't reconcile): re-dispatch this chunk with current main HEAD as the Layer 0 base SHA. Usually right for surface conflicts.
  - **Semantic** (conflicting design assumptions — same function renamed differently, same constant introduced with different value): mark `[REBASE-NEEDED:<reason>]` in the queue, notify the user, continue the batch with remaining chunks. Resolve at next milestone review.
- **Never** manually edit conflict markers in main. Re-dispatch or re-decompose; both preserve the per-chunk commit shape the Progress table assumes.

- After cherry-pick (or skip-with-reason): run integration tests from main to catch cross-cluster regressions.
- Update Progress table.

**d. After all N land:** collapse worktrees (`git worktree remove`), delete branches, update Progress table for the batch.

**e. If any one chunk fails:** isolate the failure (mark BLOCKED in queue), notify user, continue with the rest.

### 4. Periodic gstack review

Every K commits (default K=5; configurable via `--review-every <K>`), or at user-flagged `[milestone]` chunks:

**a. Capture commit range:** `<last-review-head>..<current-head>`.

**b. Two reviewers in parallel.** Choose the diversity axis:

- **Model diversity** (Claude + codex). Good for algorithmic chunks where the two models' blind spots differ.
- **Concern diversity** (two Claude subagents with disjoint lenses, e.g. correctness vs code-quality). Higher load-bearing for long refactor runs — one reviewer can't cover both axes deeply enough.

Mix is fine: one Claude reviewer (concern A) + one codex reviewer (concern B) splits both axes.

**Claude challenger:** invoke `Skill(skill: "review", ...)` directly if gstack registered Claude-side; else dispatch subagent with `prompts/gstack-claude-review.md`.

**Codex challenger:** invoke gstack `/review` via codex if registered codex-side; else point codex at `prompts/gstack-codex-challenge.md` (don't paste content into the exec arg — `SKILL.md` §Codex reliability covers the pointer pattern).

**Optional third pass** for security-relevant changes: gstack `/cso` (same dispatch logic).

**Fourth pass — RAG corpus drift review** when `docs-private/rag/` exists: dispatch after fix-clusters have landed (range = `<milestone-start>..<HEAD-after-fix-clusters>`). Brief: read every corpus slice; for each, verify file:line refs / grep patterns / decisions against current code; report per-slice P0/P1/P2/P3. Apply small drift inline; re-dispatch a slice-builder for major drift (>30% rewrite).

**c. Wait for both reviewers.** Consolidate findings; dedupe; severity-rank P0/P1/P2/P3.

**d. Fix clusters in parallel.** Group findings by ownership (which file/module). One Claude subagent per cluster. **Each cluster prompt MUST explicitly forbid touching files owned by other clusters** — without this, parallel fix clusters race on shared files. The forbid list is as load-bearing as SCOPE.

Cherry-pick each cluster's commits onto main.

**e. After convergence:**
- Bump RESUME-NOTES rev with milestone summary (commit range, findings count, cluster count).
- Update Progress table.

### 5. Handoff before compact

Per `SKILL.md` §Handoff before compact: write fresh `docs-private/RESUME-NOTES-<today>.md` at ~80% context, calibrated by remaining queue + cost-of-waking-afresh. Delegate the write to a subagent so the controller doesn't burn its own context on the template. Bump `(rev N)` if same-day file exists.

### 6. Notifications

Only fire on:
- **Blocker**: `osascript -e 'display notification "Blocker: <reason>" with title "goal-flight" sound name "Funk"'`
- **Queue completion**: `osascript -e 'display notification "Queue complete: N chunks done" with title "goal-flight" sound name "Glass"'`

Never on routine commit success. Never on expected milestone-review completion.

### 7. Mid-execute ask-questions

If the controller hits an ambiguity the look-ahead surfaced but couldn't resolve, OR the current chunk has a SCOPE question the executor needs answered: invoke `commands/ask-questions.md --scope current-chunk`. Block the loop only if the answer is required for safe completion.

### 8. Termination

When the queue is fully DONE:
- Final milestone gstack review on the full run (commit range from queue start to HEAD).
- Notify user.
- Bump RESUME-NOTES rev with "QUEUE COMPLETE" in TL;DR.
- Print summary: total chunks, total commits, milestone-review count, P0/P1 found+fixed, time elapsed.

See `SKILL.md` §Three subagent types for Executor / Reviewer / Planner wrapper-layer recommendations.
