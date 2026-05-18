# execute [--parallel <N>]

Run the per-chunk loop. **Sequential by default.** With `--parallel N`: spawn up to N parallel-safe chunks simultaneously in separate worktrees.

## Steps

### 1. Pre-flight

- Find most recent `docs-private/RESUME-NOTES-*.md`. Delegate the read to an Explore subagent if it's large.
- Find most recent goal-queue: `docs-private/goal-queue-*.md` (new naming as of 0.3.0). Fall back to legacy `docs-private/<topic>-goal-queue-*.md` if no new-form file exists; queues from <0.3.0 still read.
- **Skill-update drift check** — if either file carries a `Skill-loaded:` header, compare it to the live `LOADED_LINE` per `SKILL.md` §Session pre-flight probe 1 recipe. If different, surface the one-line nudge from probe 4 before composing any dispatch wrapper (executor prompts will still be valid, but the controller should know the skill itself changed under it).
- Verify reality via Bash: `git log --oneline -10`, `git status`, `git rev-parse HEAD`. Confirm matches RESUME-NOTES' Code-state section.
- Quick test smoke if known (e.g. `pytest --collect-only`).
- If drift between RESUME-NOTES and reality: pause, surface to user, do not dispatch.

### 2. Per-chunk loop — sequential mode

For each non-DONE goal in the queue (in order):

**a. Build dispatch prompt** per `prompts/dispatch-wrapper.md` — verification-first scaffolding. For `[goal-mode]` codex chunks the controller dispatches via `codex exec -C <workdir> - < prompt.md` (non-interactive path; empirically no 4k cap — codex 0.130.0 + gpt-5.5 accepts 4407+ chars cleanly per 2026-05-17 probe). The 4k limit that exists on codex's *interactive* `/goal` slash command does not bind on the dispatcher. Real discipline across all shapes: points, not pre-paste. Layer 0 (worktree-isolated dispatches) captures expected base SHA from `git fetch origin && git rev-parse origin/main` in the MAIN worktree, not controller cwd. Layers 1–6 scaffold investigation + marker emission rather than pre-paste content. Tail with `invariants.md` if corpus exists.

If the `docs-private/rag/` corpus exists, paste slice content per the slice-to-layer mapping in `prompts/dispatch-wrapper.md` — as starting hypotheses the executor verifies, not authoritative facts.

**b. Execute the chunk** — dispatch in background per `SKILL.md` §Per-chunk loop dispatch rule (any >10s call backgrounds so the user's terminal doesn't hang). Branch on chunk tags from `decompose-plan` step 2 (see `SKILL.md` §Three dispatch paths for the full decision matrix):

- **`[controller-direct]`** — controller inline: Read + Edit + run test subset + adversarial self-review (Layer 5 specialized against own diff). Skip steps c–d below; jump to commit. Use only for chunks tagged at decompose-plan time AND expected to complete in seconds (the inline path blocks the parent thread). Don't retag mid-execute — if a chunk turns out trickier, abort inline and fall through to the subagent path.
- **`[goal-mode]`** — codex `/goal` mode (in-session loop) or external Opus/Grok iteration loop. Render `templates/codex-goal-prompt.md.tpl`; dispatch per the path chosen (codex `/goal` via `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C <workdir> - < prompt.md > <tail-file> 2>&1 &` then watcher — same bypass / `-C <workdir>` rules as the `[bash-tail]` recipe below; external Opus/Grok loop is one background Agent or `grok -p &` per iteration). **`[goal-mode] [acp]` combo**: drive goal-mode through ACP by prefixing the prompt text with `/goal` (codex), `/implement` (grok), or `/goal` (claude-via-claude-code-cli-acp); the adapter's prompt parser routes to the in-session loop. Verified working 2026-05-18 against codex-acp via `test/dispatch_acp_chunk.py` (live ACP smoke; requires the relevant adapter on PATH + auth).
- **`[acp]` (ACP transport — prefer when target speaks it)** — drive `scripts/acp_client.AcpProcessPool` instead of Bash-`&`-tail-file. Spawn via `pool.get_or_create(agent, session_id, cwd)`, dispatch with `scripts/acp_runner.run_prompt(conn, prompt_text, idle_timeout=300)`, collect a `PromptResult` (text accumulator + thoughts + tool_calls + stop_reason). Marker extraction is unchanged — feed `result.text` through `extract_markers()`. Structured events replace tail-file grep; persistent sessions allow `USER-CLARIFICATION:` re-dispatch through the same connection instead of re-spawning. Use the pool's signal-handler context manager (`managed_pool()`) so controller crashes don't orphan workers. Capacity ceiling auto-derived from `docs-private/env-caveats.md` — fail fast if a chunk would exceed `max_processes`. See `SKILL.md` §Dispatch model — Transport choice for the picker rule (untagged-with-ACP-capable-target also defaults to ACP).
- **`[bash-tail]` (legacy Bash-`&`-tail-file — explicit fallback)** — Claude Agent with `run_in_background: true` and `model: "opus"`, OR codex via `codex exec -C <workdir> --dangerously-bypass-approvals-and-sandbox [--skip-git-repo-check] '<prompt>' > /tmp/codex-<slug>.txt 2>&1 &` (capture `$!` as `WORKER_PID`, then background `scripts/watch-dispatch-tail.sh` per the watcher recipe below), OR grok via `grok --prompt-file /tmp/grok-<slug>-prompt.md --cwd <workdir> --permission-mode acceptEdits --output-format plain > /tmp/grok-<slug>.txt 2>&1 &` (same watcher pattern). **Watcher recipe**: `bash <skill-root>/scripts/watch-dispatch-tail.sh --pid $WORKER_PID --tail /tmp/codex-<slug>.txt --controller-pid $$ --agent codex-bash-tail --session-id <slug> > /tmp/watcher-<slug>.txt 2>&1 &` — then dispatch a Bash task with `run_in_background: true` that does `wait $WATCHER_PID; echo "exit=$?"; cat /tmp/watcher-<slug>.txt` so the harness fires a task-notification with the watcher's exit code surfaced. The watcher is content-aware: exits **0** when a terminal marker (`^\**(COMPLETE|BLOCKED|USER-NEED|USER-CONFIRM):\**` — emphasis-tolerant for grok) appears in the tail, **1** if the worker PID dies without a terminal marker, **2** on idle-timeout (no tail update for ≥180s — wedge detection), **3** if the controller PID dies (orphan watcher self-detection). The watcher registers in `/tmp/goal-flight-acp-pids.d/<controller-pid>.bashtail.<worker-pid>.jsonl` on startup and removes the entry on any clean exit — so `cleanup_ghosts()` in `scripts/acp_client.py` reaps orphaned workers uniformly across both ACP and Bash-tail dispatch paths on the next goal-flight startup. **Always pass `-C <workdir>` (codex) / `--cwd <workdir>` (grok)** — without an explicit working-directory pin, both tools edit the controller's cwd, which collapses the worktree-isolation safety story for the bypass flags below. **Codex needs the bypass-approvals flag** for autonomous edits — without it, codex correctly emits `BLOCKED: filesystem is read-only and approvals are disabled` after attempting the first patch (empirically verified 2026-05-17). **Grok needs `--permission-mode acceptEdits`** to edit files without interactive approval. **Safety**: the bypass flags trade sandboxing for autonomy; the worktree boundary provides external sandboxing **only when `<workdir>` is a sibling worktree** (`--parallel` mode, where each chunk has its own `.claude/worktrees/<slug>/` tree). In **sequential mode** with `<workdir>` = controller cwd, there is no sandbox — the per-chunk diff-verify in step d is the scope check. Don't dispatch sequential bypass-mode chunks against repos with uncommitted unrelated work; either parallel-isolate or accept the diff-verify-is-your-only-fence reality. Pick `[bash-tail]` explicitly when (a) target worker doesn't speak ACP (e.g., Claude Agent tool subagent — Claude Code itself isn't ACP-mode-spawnable; use `claude-code-cli-acp` PTY-wrap if you need claude over ACP), (b) ACP adapter is missing per env-caveats, or (c) chunk is genuinely one-shot and ACP session overhead isn't worth it.
- **Untagged (default)** — frontier model picks per `SKILL.md` §Dispatch model transport-defaults rule: ACP transport (`[acp]`) when target speaks it AND `docs-private/env-caveats.md` confirms the adapter is installed; else Bash-`&`-tail-file (`[bash-tail]`).

**Fork primitives** are the heavier "branch the controller's state" tool when a chunk depends on session-loaded context AND you want a `/rewind`-able savepoint. See `SKILL.md` §Self-delegation via `/fork` for the full pattern (controller writes contract via `scripts/self-fork-detect.sh write`; fork detects via `detect`; monitor watches the marker vocabulary).

**c. End the dispatch turn.** Emit a one-line status (`Dispatching chunk #N (\`<slug>\`). Agent task <id> / shell PID <pid> -> <output-path>.`) and stop. The next assistant turn fires when the task-notification arrives — from the Agent's own completion (for Agent dispatches) or from the watcher exiting (for Bash codex/grok dispatches). Don't poll Agent subagent transcripts; `kill -0` on the shell PID via a watcher Bash call is the right pattern for headless dispatches.

**c.5. Parse marker lines** (completion turn, before diff verify). Read the worker's output (tail file for Bash dispatch; Agent's returned blob for Agent dispatch). Grep for the marker vocabulary from `SKILL.md` §Worker message passing — tolerate **optional markdown emphasis** around markers (grok wraps them in `**STATUS:**` style; codex emits unwrapped `STATUS:`; pattern: `^\**(STATUS|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**`). Handle each in order:

- `STATUS: <update>` — log; keep processing.
- `RESULT: <key>=<value>` — capture for downstream use (Progress table, premises file, RESUME-NOTES).
- `USER-NEED: <question>` — relay the question to the user via the orchestrator's conversational surface (just emit the question as your next assistant text); wait for the user's answer; re-dispatch this chunk's wrapper with `USER-CLARIFICATION: <answer>` prepended to the chunk body; do NOT proceed to diff verify or commit. Surface to user that the chunk is paused-on-clarification.
- `USER-CONFIRM: <action> [Y/N]` — relay; wait for explicit yes/no; if yes, re-dispatch with `USER-CLARIFICATION: confirmed`; if no, mark BLOCKED in the Progress table and surface.
- `BLOCKED: <reason>` — surface to user via orchestrator; don't auto-retry; mark BLOCKED in Progress table.
- `COMPLETE: <summary>` — proceed to step d (verify diff briefly).

Multiple markers can co-occur. Process in order; the first `USER-NEED` / `USER-CONFIRM` / `BLOCKED` short-circuits and pauses the chunk; only after `COMPLETE` (and no preceding blocker) does the controller move to step d.

**d. Verify diff briefly:** `git diff --stat` (scope contained?) + `git diff` first 200 lines (FORBIDDEN actions? mutator patterns the goal banned?) + run the test subset.

If any check fails: surface to user OR re-dispatch the executor with the failing finding as input. Don't commit on failure.

**e. Commit** (one chunk per commit). Imperative subject + `(chunk N/M)` suffix. HEREDOC for the body.

**f. Update Progress table** in goal-queue: `| #N <slug> | DONE — <hash> |`.

**g. Spawn look-ahead** (read-only Explore, fire-and-forget): "Read the goal-queue. Next 1–2 chunks about to dispatch. Scan binding-spec / AGENTS.md / current code for hidden dependencies, ambiguities, missing acceptance criteria. Report anticipatory questions per `prompts/ask-anticipatory.md`." If material questions surface, queue `commands/ask-questions.md --scope next-chunks` non-blockingly.

### 3. Per-chunk loop — parallel mode (`--parallel N`)

For each batch of up to N consecutive `[parallel-safe:<group>]` chunks:

**a. Spawn worktrees:** `git worktree add <repo-root>/.claude/worktrees/<adjective-noun-N>/ -b claude/<adjective-noun-N>`. Codex trust prefix-matches the project root, so worktrees under `<repo-root>/.claude/worktrees/*` inherit trust automatically (no per-worktree registration; see `SKILL.md` §Worktree convention).

**b. Dispatch each as a Claude subagent in its own worktree.** Pass the worktree path in the agent description. **ACP-parallel is forward work** — the current `--parallel` mode dispatches Claude subagents only; routing per-worktree through `AcpProcessPool` (one `session_id` per worktree, shared pool) requires the pool-aware parallel coordinator that 0.3.0 doesn't yet ship. Until then, parallel chunks fall to the Claude-subagent path regardless of `[acp]` tag; non-Claude `[parallel-safe]` chunks should run sequentially or be re-decomposed.

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
