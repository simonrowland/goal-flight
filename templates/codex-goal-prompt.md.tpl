# Codex `/goal` mode prompt template

Render this template into a file, then dispatch via:

```bash
codex exec \
  --skip-git-repo-check \
  --sandbox workspace-write \
  -c approval_policy=never \
  -C <ABSOLUTE_WORKSPACE_PATH> \
  - < /tmp/goal-flight-goal-<slug>-<iso>.md \
  > /tmp/goal-flight-goal-<slug>-<iso>.tail.txt 2>&1 &
PID=$!
```

`features.goals = true` must be set in `~/.codex/config.toml` (or `codex features enable goals`). The prompt shape itself — Objective + Workspace + Rules + Acceptance + Final response — is what activates the goal-mode loop non-interactively; codex inspects the prompt at session start and treats it as a long-running thread-attached goal when the structure is present and the feature flag is on.

**Flags explanation**:
- `--skip-git-repo-check` — codex refuses non-interactive runs in non-git directories by default. `[goal-mode]` chunks under `<repo>/.claude/worktrees/` ARE git repos and don't need this flag; chunks dispatched into `/tmp/` or other non-git workspaces do.
- `--sandbox workspace-write -c approval_policy=never` — autonomous goal-mode dispatch needs to edit files without interactive human approval. `--sandbox workspace-write` permits edits within `-C <workdir>`; `-c approval_policy=never` stops codex prompting on each action. Without write permission codex emits `patch rejected: writing is blocked by read-only sandbox` on the first edit and the chunk fails with `BLOCKED:` (empirically verified 2026-05-17: a goal prompt that can write completes the edit → pytest → green loop in ~92 seconds; a read-only sandbox emits `BLOCKED:` after pytest and the patch attempt). **Do NOT use `--dangerously-bypass-approvals-and-sandbox`**: it removes the sandbox entirely (codex could edit anywhere and run arbitrary shell) AND it is rejected by some controllers' auto-mode safety classifiers, so the dispatch never starts. `workspace-write` is both safer and classifier-safe. **The safety story still depends on `-C <workdir>`** pointing at an isolated tree: when `<workdir>` is a sibling worktree under `.claude/worktrees/<slug>/` (i.e., `--parallel` mode), the worktree boundary plus the per-chunk verify-diff catches scope leaks before cherry-pick to main. When `<workdir>` is the controller cwd (sequential mode against the main worktree), `workspace-write` still bounds writes to that tree and the verify-diff step remains the fence; sequential mode against a repo with uncommitted unrelated work is still risky, so prefer parallel-isolate.

**No `timeout 300` wrapper** — `/goal` mode is designed for multi-hour autonomous runs. The controller monitors the tail file for the "Final response" block specified at the bottom of the prompt; the dispatch is complete when that block appears with the agreed schema. Watchdog inactivity thresholds from `SKILL.md` §Codex reliability do NOT apply (long pauses during plan/act/test/review-to-convergence cycles are expected).

---

## Prompt skeleton

Substitute `{{PLACEHOLDERS}}` and remove this header before rendering to the dispatch file.

```
Start a goal:

Objective:
{{ONE_LINE_OBJECTIVE — what done looks like, in measurable terms}}

Workspace:
{{ABSOLUTE_WORKSPACE_PATH}}

Rules:
- Use the current workspace; do not create a branch or worktree unless explicitly listed below.
- {{RULE_DOC_BOUNDARY — e.g., keep diagnostics under docs-private/}}
- {{RULE_DATA_PROVENANCE — e.g., do not use <X> as internal anchors}}
- {{RULE_EDIT_SCOPE — e.g., do not edit code unless needed for diagnostic tables/plots}}
- {{RULE_VALIDATION — e.g., validate facts before downstream claims}}
- Only run safe read/diagnostic commands unless explicitly authorized to edit.
- **Emit marker lines** when you hit ambiguous points, need user input, or finish. One marker per line in your output. Vocabulary (see goal-flight SKILL.md §Worker message passing): `STATUS: <update>` (informational), `RESULT: <key>=<value>` (structured output), `USER-NEED: <question>` (you can't decide without user input — stop and emit this; the controller will relay), `USER-CONFIRM: <action> [Y/N]` (irreversible op needs authorization), `BLOCKED: <reason>` (unrecoverable), `COMPLETE: <summary>` (task done). Emit a `STATUS:` line at least every ~8 minutes and before any long step; work incrementally so the controller sees live progress. The controller polls these from the tail file and relays `USER-NEED:` / `USER-CONFIRM:` to the user via the orchestrator's conversational surface. Don't guess past an ambiguous point — emit `USER-NEED:` and stop.
- **End-of-convergence-attempt self-review.** When you think the goal is met (test gates green AND acceptance criteria satisfied), DO NOT yet emit `Goal complete: true`. First run the 7-category adversarial self-review from `prompts/executor-self-review.md` (INVARIANT GAP / SCOPE LEAK / MUTATION PURITY / BEHAVIOR DRIFT / DEAD CODE / CONTRACT LEAK / INTEGRITY), specialized to this chunk's project nouns and grep patterns. Severity-rank findings P0/P1/P2/P3. **Any P0 or P1 is a continue-iterating signal** — fix it in-loop (which becomes another plan/act/test cycle) and re-run the self-review pass. Emit `Goal complete: true` only when the self-review pass yields no P0/P1, OR when every flagged P0/P1 was resolved in-loop with test gates still green. **Cadence**: ONE self-review pass per "I think I'm done" attempt — NOT after every micro-step. Typical: 1-3 passes total per chunk.

Acceptance criteria:
- {{ACCEPT_INSPECT — what to inspect/read first, by file path}}
- {{ACCEPT_COMPARE — what to compare against; observed vs latent/expected}}
- {{ACCEPT_SEPARATE — distinguish observed facts, deconvolution, priors, plotting choices, downstream implications}}
- {{ACCEPT_RECOMMEND — concrete recommendation + sensitivity/stress alternatives}}
- {{ACCEPT_WRITE — write memo/output to <docs-private/...md>}}
- {{ACCEPT_TEST_GATE — any test that must stay green / any artifact that must be produced}}
- {{ACCEPT_SELF_REVIEW_CLEAN — final 7-category adversarial self-review (per prompts/executor-self-review.md, specialized to this chunk's nouns/files) shows no P0/P1 findings, OR every flagged P0/P1 was resolved in-loop with test gates still green. The controller never sees a non-converged result.}}

Test gates (must remain true throughout):
- {{TEST_GATE_1 — e.g., pytest tests/test_invariants.py stays green}}
- {{TEST_GATE_2 — e.g., grep -c "forbidden_pattern" returns 0 in <paths>}}

If a review tool or required artifact is blocked or unavailable:
- {{BLOCKER_PROTOCOL — e.g., write a NEEDS-RESOLUTION note to docs-private/<topic>-blockers.md, surface in Final response, do NOT attempt to bypass the gate}}

Edit policy:
- {{EDIT_POLICY — one of: review-only (do NOT edit any code), diagnostic-only (edit only docs-private/*, no simulator/* writes), full (edit per ACCEPTANCE)}}

Final response (must include all of the following, in this order):
- Memo path: <path to the written memo>
- Key conclusion: <one paragraph>
- Commands run: <bulleted list>
- Dirty files: <`git status --short` style listing, or "clean" if no edits>
- Blockers: <bulleted list, or "none">
- Self-review status: <clean | resolved-in-loop with N P0/P1 findings fixed during iteration> — must be one of these two for `Goal complete: true`
- Goal complete: <true | false; explain if false>
```

---

## Notes for the controller composing this prompt

- **Length is OK** — `/goal` mode is meant to consume substantial briefing. The 4 KB CLI argument concern that motivated file-based dispatch elsewhere is doubly relevant here; passing via `<` stdin redirection sidesteps it.
- **No pre-pasted file contents** — same verification-first principle as `prompts/dispatch-wrapper.md`. Point at files (e.g. `docs-private/binding-spec.md` + the relevant source paths), don't paste their contents. The goal-mode agent has Read + Grep + Bash and the multi-hour budget to use them.
- **Acceptance criteria should be testable.** "Recommend central grid" is OK because the Final response schema demands a concrete answer. "Understand the system" is not OK — there's no completion signal.
- **Edit policy is load-bearing.** Goal-mode runs for hours; if you don't pin the edit scope, the agent may make changes you didn't authorize. `review-only` and `diagnostic-only` are the safe defaults; `full` is for chunks the goal-queue tags as code-writing.
- **Blocker protocol matters** — without it, goal-mode may loop on a wedged review tool indefinitely, eating tokens until budget exhaustion. Tell it to surface and stop.
- **Final response schema must be unambiguous.** The controller polls the tail file for that schema's appearance to detect completion. If the schema is fuzzy, you'll get false-positive-completes or miss the real one.
- **Self-review cadence is end-of-attempt, not per-step.** The 7-category pass runs ONCE per "I think I'm done" attempt, not after every plan/act/test cycle. Cost per pass: ~30s of worker thinking + a structured report block. Catches obvious P0/P1s in-loop so the controller doesn't have to dispatch a separate reviewer for each chunk; the milestone external review flight (codex + grok via gstack `/review`) remains the heavier per-K-commits gate.
