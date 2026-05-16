# Dispatch Wrapper — verification-first scaffolding

Raw goal text from the queue (600–1200 chars) isn't enough to brief an executor. But the inverse failure — pre-pasting "facts" the executor must trust — is worse: stale file:line refs, fabricated line numbers, outdated provider names. Frontier models trust controller-pasted text because the controller is upstream in their trust hierarchy; that trust + staleness = silent regressions.

**Operating principle: verification beats prescription.** The wrapper scaffolds what the executor should investigate; it does not substitute for that investigation. Opus + xhigh has Read, Grep, Bash, and the budget to use them. Let it.

## Layer 0 — Base-verification pre-flight (MANDATORY for worktree-isolated dispatches)

The Agent tool's `isolation: "worktree"` branches off the controller's cwd HEAD, not necessarily off `main`. A sibling-worktree controller can lag main; the executor then builds against the wrong substrate and commits won't cherry-pick. This is the one layer that asserts a verifiable fact — and the executor verifies it directly.

Include at the TOP of every worktree-isolated dispatch:

```
PRE-FLIGHT (before reading the rest):
1. Run `git rev-parse HEAD` in your working directory.
2. Compare to expected base: <PASTE SHA — captured by `git fetch origin && git rev-parse origin/main` from the MAIN worktree, not controller cwd>.
3. If mismatched: STOP. Report "Base mismatch: HEAD <actual>, expected <expected>. Aborting." Do not proceed.
4. If matched: continue.
```

**Controller-side check, mandatory.** Before dispatching, run `git -C <worktree-path> rev-parse HEAD` and compare to expected SHA. If mismatched: don't dispatch. Recreate the worktree on the right base (`git -C <worktree> reset --hard <expected>` if branch can move; else recreate with `git worktree add -b <branch> <path> <expected>`). Prompt-side Layer 0 alone is honor-system; the controller-side check costs nothing and catches the issue before any executor tokens get spent.

**Capture-timing rule** (load-bearing): capture the expected SHA AFTER any pre-dispatch admin commits (goal-queue Progress-table updates, RESUME-NOTES rev bumps, .gitignore additions, etc.) and BEFORE composing the dispatch prompt. Admin commits are part of the substrate the executor verifies against; if you capture SHA before they land and the admin commits then push HEAD forward, Layer 0 will reject the worktree because its HEAD already includes commits the dispatch prompt is unaware of. Codex correctly refuses such dispatches — the gate works as designed; the fix is capture order, not Layer 0 lenience.

**If the executor reports "Base mismatch, Aborting":** the controller-side check should have prevented this, but if it slipped through — the dispatch is aborted (no commit). Recover by: (a) running the controller-side check + worktree-reset above to fix the worktree, then (b) re-dispatching the same chunk. Don't try to cherry-pick a drifted-base commit; it won't merge cleanly downstream.

## Layers 1–5 — scaffold, don't substitute

The other layers follow the same principle: point at what to investigate; let the executor discover and verify.

| Layer | What | Framing |
|---|---|---|
| 1 — Situational frame (~30w) | Where main is, what just landed, this dispatch's role | One sentence each. Not a worked example. |
| 2 — Template-provider pointer | Canonical example name + path | "Investigate as starting hypothesis; verify the file still exists and the pattern still applies before mirroring." |
| 3 — File anchors | File-map / binding-spec slice paths | "Use as navigation map. Verify every file:line you rely on with Read/Grep before depending on it. Flag drift in your report." |
| 4 — Environment caveats | Verification commands + only what the agent can't discover | If the agent can find it with Read/Bash in <5s, the controller should NOT pre-paste it. If the agent CAN'T (test-env oddity, out-of-band convention, deliberate-skip-that-looks-like-bug), paste it. |
| 5 — Self-review categories | The 7 abstract categories from `prompts/executor-self-review.md` (INVARIANT GAP / SCOPE LEAK / MUTATION PURITY / BEHAVIOR DRIFT / DEAD CODE / CONTRACT LEAK / INTEGRITY) | Abstract scaffold in the PROMPT; executor SPECIALIZES in the REPORT, not pre-pasted. |

**Frontier models compose excellent dispatch prompts from these principles alone.** No worked examples here — they calcify around one project's idioms and over-prescribe for others. The principle generalizes; the examples don't.

## Triviality bypass

Trivial single-file chunks (< ~30 LoC delta, no new public surface, no cross-module coupling): Layers 0 + 1 + abstract Layer 5 suffice. Skip 2/3/4. Same threshold for `[controller-direct]` (see `SKILL.md`): if the chunk genuinely qualifies, the controller can do the work inline with no subagent at all.

## Corpus integration

When `docs-private/rag/` exists, the controller passes slice content as **starting hypotheses the executor verifies**, not as authoritative facts. Same principle.

- Each slice file carries `verified-at: <commit-SHA>` frontmatter recording when it was built or last reviewed. The slice-builder writes it; the slice-reviewer checks it. When `git rev-list --count <verified-at>..HEAD` exceeds ~20 commits or any milestone has landed since, the executor treats the slice with extra suspicion and verifies aggressively.
- `invariants.md` is appended to the tail of every dispatch (executor / reviewer / planner alike — universal precondition).
- `decisions.md` is available to reviewers and planners too (so they don't re-open closed decisions).

If a slice the mapping calls for doesn't exist (e.g. `patterns/<X>.md` was skipped at init because no canonical implementation existed yet): hand-compose that one layer with verification framing.

## Dispatch shape — minimal skeleton

```
/goal <SLUG>

[Layer 0: pre-flight verification (mandatory for worktree dispatches)]

[Layer 1: situational frame — ~30w]

[Goal text from queue — SCOPE / CHECKLIST / ACCEPTANCE / FORBIDDEN]

[Layer 2: pointer at canonical example to investigate]
[Layer 3: pointer at file-map / binding-spec to navigate from]
[Layer 4: env caveats — only what the agent can't discover]
[Layer 5: self-review categories — abstract; specialize in REPORT]

Report format: see prompts/executor-self-review.md.
Read AGENTS.md (or worker-context.md if it exists) before starting — treat as starting hypothesis you verify against current code.
```

## Target size

3–5 KB per dispatch, not 6–11 KB. The size reduction is the empirical test that the refactor delivered. If you're composing a 10 KB dispatch prompt against this checklist, you've regressed to pre-paste-style — examine which "facts" you're handing the executor that they could discover themselves.
