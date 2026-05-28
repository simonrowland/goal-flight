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

**Capture-timing rule** (load-bearing): capture the expected SHA AFTER any pre-dispatch admin commits (goal-queue Progress-table updates, RESUME-NOTES rev bumps, .gitignore additions) and BEFORE composing the dispatch prompt. Admin commits are part of the substrate the executor verifies against. Capture before they land and Layer 0 rejects: the worktree HEAD already includes commits the dispatch prompt does not know about. Codex correctly refuses such dispatches — the gate works as designed; the fix is capture order, not Layer 0 lenience.

**If the executor reports "Base mismatch, Aborting":** the controller-side check should have prevented this, but if it slipped through — the dispatch is aborted (no commit). Recover by: (a) running the controller-side check + worktree-reset above to fix the worktree, then (b) re-dispatching the same chunk. Don't try to cherry-pick a drifted-base commit; it won't merge cleanly downstream.

## Layers 1–6 — scaffold, don't substitute

The other layers follow the same principle: point at what to investigate; let the executor discover and verify.

| Layer | What | Framing |
|---|---|---|
| 1 — Situational frame (~30w) | Where main is, what just landed, this dispatch's role | One sentence each. Not a worked example. |
| 2 — Template-provider pointer | Canonical example name + path | "Investigate as starting hypothesis; verify the file still exists and the pattern still applies before mirroring." |
| 3 — File anchors | File-map / binding-spec slice paths | "Use as navigation map. Verify every file:line you rely on with Read/Grep before depending on it. Flag drift in your report." |
| 4 — Environment caveats | Verification commands + only what the agent can't discover | If the agent can find it with Read/Bash in <5s, the controller should NOT pre-paste it. If the agent CAN'T (test-env oddity, out-of-band convention, deliberate-skip-that-looks-like-bug), paste it. For ACP workers: run the **chunk's focused test subset** in verification — not `pytest tests/` (full suite) unless acceptance explicitly requires it. The controller runs the authoritative full gate after merge. |
| 5 — Self-review categories | The 7 abstract categories from `prompts/executor-self-review.md` (INVARIANT GAP / SCOPE LEAK / MUTATION PURITY / BEHAVIOR DRIFT / DEAD CODE / CONTRACT LEAK / INTEGRITY) | Abstract scaffold in the PROMPT; executor SPECIALIZES in the REPORT, not pre-pasted. |
| 6 — Marker vocabulary | The worker message-passing contract: emit `STATUS:`, `RESULT:`, `USER-NEED:`, `USER-CONFIRM:`, `BLOCKED:`, `COMPLETE:` lines as needed. See `protocols/worker-markers.md`. | One-line instruction in the prompt: "Emit marker lines per protocols/worker-markers.md when you hit ambiguous points, need user input, or finish. Emit `STATUS:` at least every ~8 minutes and before any long step; work incrementally." Workers without this instruction guess and proceed silently. |

**Frontier models compose excellent dispatch prompts from these principles alone.** No worked examples here — they calcify around one project's idioms and over-prescribe for others. The principle generalizes; the examples don't.

## Triviality bypass

Trivial single-file chunks (< ~30 LoC delta, no new public surface, no cross-module coupling): Layers 0 + 1 + abstract Layer 5 + the one-line Layer 6 marker instruction suffice. Skip 2/3/4. Same threshold for `[controller-direct]` (see `SKILL.md`): if the chunk genuinely qualifies, the controller can do the work inline with no subagent at all (and the marker convention is moot — controller-direct has no marker channel because the controller IS the worker).

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
Return shape: see `commands/execute.md` step 7 — pick one of Investigator (READY + file-backed findings), Executor (COMMIT + TL;DR + DETAILED), or Blocked (BLOCKED + reason + recommended controller action). The dispatch prompt MUST specify which shape applies so the controller can parse the headline without reading the body. Workers do NOT execute workarounds (alternate APIs, git plumbing, inline content dumps) when blocked — escalate via Blocked shape; controller decides.
Read AGENTS.md (or worker-context.md if it exists) before starting — treat as starting hypothesis you verify against current code.
```

## Prompt size — entry path matters

Two distinct entry paths to codex goal-mode loops:

- **Interactive `/goal` slash command** (user types `/goal` in an interactive codex session and pastes the goal text): **~4000-character limit on the goal text — codex bounces longer inputs.** This is the limit the skill's prior prose was citing. Applies when a human is hand-driving codex.
- **Non-interactive `codex exec -C <workdir> - < prompt.md`** (the path goal-flight actually uses for `[goal-mode]` chunks per `templates/codex-goal-prompt.md.tpl`): **no 4k limit.** Empirical probe with codex 0.130.0 + gpt-5.5 (2026-05-17) accepted a 4407-char prompt cleanly, echoing back all four anchor strings spread across the prompt (probe: `/tmp/codex-goal-size-probe.md` → `/tmp/codex-goal-probe.out`). No bounce, no truncation.

Since goal-flight dispatches via the non-interactive path, the 4k limit doesn't bind on the controller's automated dispatches. It DOES bind on the human-paste path (if you ever hand-drive codex `/goal` interactively for ad-hoc work — keep the goal text under 4k or split into multiple steps).

The **real constraint on the non-interactive path is verification-first hygiene**: pre-paste prompts that hand the executor "facts" go stale on the timescale of minutes; frontier models trust controller-pasted text uncritically; large dispatch prompts almost always indicate the controller is over-explaining rather than pointing the executor at what to read. Bigger prompt = more rope to over-paste with. Across all dispatch shapes (codex non-interactive / exec, Agent tool, `grok -p` / `/implement`, ACP), the principle is the same: **points, not pre-paste**. The size discipline is a smell test for that principle, not a mechanical truncation budget.

Other dispatch shapes (Agent, grok, codex `exec '<inline>'`, ACP) have harness/CLI limits in the 8K–200K char range — well above any prompt the verification-first wrapper should produce. The failure mode there isn't "executor never saw the rest of the prompt"; it's "drift from intent over the run because the prompt over-asserted."
