# validate-dispatch [<goal-slug>]

Render and print the dispatch wrapper for a goal **without dispatching it**. Dry-run for catching malformed wrappers before burning a real Opus / codex / Grok dispatch on them.

This is a soft-check, not a guarantee. The heuristics catch common failure modes; a clever malformed wrapper can still pass them. Controller judgment remains load-bearing.

## When to invoke

- User typed `/goal-flight validate-dispatch [<slug>]`.
- Controller is about to dispatch a complex chunk and wants to sanity-check.
- A previous dispatch came back wrong; the user suspects the wrapper.

## What the user provides

- **No args** → render the next non-DONE chunk in the most recent goal-queue.
- **One arg `<goal-slug>`** → that specific chunk.

## Render the wrapper (verification-first; per `prompts/dispatch-wrapper.md`)

1. Find the most recent `docs-private/<topic>-goal-queue-*.md`. Pick the chunk.
2. Compose the wrapper as **pointers, not pre-pasted content**:
   - **Layer 0** — base-verification pre-flight with expected SHA captured via `git fetch origin && git rev-parse origin/main` from the MAIN worktree (not controller cwd). The fetch is load-bearing — local `main` can be stale relative to `origin/main`, and Layer 0 verifying against a stale base defeats the point.
   - **Layer 1** — ~30-word situational frame.
   - **Goal text** — verbatim from queue.
   - **Layer 2** — pointer at canonical pattern (`docs-private/rag/patterns/<X>.md` if corpus exists). Framing: *"Investigate as starting hypothesis; verify before mirroring."*
   - **Layer 3** — pointer at file-map (`docs-private/rag/file-map.md`). Framing: *"Use as navigation; verify every file:line before relying."*
   - **Layer 4** — environment caveats. Only what the agent can't discover in <5s.
   - **Layer 5** — abstract self-review categories from `prompts/executor-self-review.md`. **Executor specializes in the REPORT, not in this prompt.**

3. Print in a fenced block with header (slug, layer set, byte count).

## Validation heuristics

Surfaced as warnings or P0 blockers. None is sufficient on its own; treat as suggestive.

- **Byte count > 5 KB** → WARN. Verification-first target is 3–5 KB. Strongly suggests pre-paste regression — examine which "facts" the controller pasted that the executor could discover.
- **Byte count < 800 B** → WARN. Likely missing layers.
- **Layer 0 missing for worktree-isolated dispatch** OR **expected-SHA is empty / matches `<PASTE_HERE>` placeholder / no `git fetch origin` was run in the past minute** → P0 BLOCKER. Do not dispatch; the worktree-base failure mode is real, and a stale local `main` SHA fails the spirit of the check. ("Worktree-isolated dispatch" = the executor's filesystem is a separate `git worktree` branched off some base; relevant whenever the Agent tool's `isolation: "worktree"` mode is used OR when parallel mode spawns chunks under `<repo>/.claude/worktrees/*` per `commands/execute.md` step 3. Non-isolated dispatches — `codex exec`, single-shot Agent without `isolation`, all run in the controller's cwd — skip Layer 0.)
- **Any layer (2/3/4) contains `:line-number` anchors without verification framing nearby** (the strings "verify", "starting hypothesis", "before relying", "map", "investigate" within the same paragraph) → WARN. Pre-paste regression. Counts the anchors, NOT just presence — > 10 file:line anchors total is the threshold (the pointer pattern usually has 3–5).
- **Layer 5 contains chunk-specific specialization** (anything beyond the 7 abstract category names + "specialize in the report") → WARN. Layer 5 stays abstract in the prompt; specialization moves to the executor's report.
- **Goal text section is missing or empty** → P0 BLOCKER.

## What this does NOT catch

- Whether rendered file:line anchors point at files that actually exist. (Would require executing the wrapper. Layer 0 + Layer 3 verification framing pushes that check onto the executor — by design.)
- Whether Layer 5's abstract category set is appropriate for THIS chunk. Judgment call.
- Substantive errors in the goal text itself — `validate-queue` partially covers structural problems; semantic errors aren't catchable from heuristics.
- Whether `origin/main` itself is at the right SHA for this dispatch's intended base — if the user wants to dispatch against a non-`main` base, they pass it explicitly.

## After validation

- All checks pass → user proceeds to `/goal-flight execute` (the wrapper rendered there is a fresh re-render).
- Warnings → user examines, may decide the heuristic is wrong for this chunk OR fixes the wrapper logic.
- P0 blocker → do not dispatch. Fix the layer that's wrong.

## See also

- `prompts/dispatch-wrapper.md` — canonical wrapper spec (verification-first).
- `commands/execute.md` step 2 — where the wrapper is normally rendered + dispatched.
- `commands/validate-queue.md` — schema-check the queue itself.
