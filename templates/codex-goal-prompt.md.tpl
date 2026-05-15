# Codex `/goal` mode prompt template

Render this template into a file, then dispatch via:

```bash
codex exec -C <ABSOLUTE_WORKSPACE_PATH> - < /tmp/goal-flight-goal-<slug>-<iso>.md \
  > /tmp/goal-flight-goal-<slug>-<iso>.tail.txt 2>&1 &
PID=$!
```

`features.goals = true` must be set in `~/.codex/config.toml` (or `codex features enable goals`). The prompt shape itself — Objective + Workspace + Rules + Acceptance + Final response — is what activates the goal-mode loop non-interactively; codex inspects the prompt at session start and treats it as a long-running thread-attached goal when the structure is present and the feature flag is on.

**No `timeout 300` wrapper** — `/goal` mode is designed for multi-hour autonomous runs. The controller monitors the tail file for the "Final response" block specified at the bottom of the prompt; the dispatch is complete when that block appears with the agreed schema. Watchdog inactivity thresholds from `reference/pattern.md` §Codex reliability do NOT apply (long pauses during plan/act/test/iterate cycles are expected).

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

Acceptance criteria:
- {{ACCEPT_INSPECT — what to inspect/read first, by file path}}
- {{ACCEPT_COMPARE — what to compare against; observed vs latent/expected}}
- {{ACCEPT_SEPARATE — distinguish observed facts, deconvolution, priors, plotting choices, downstream implications}}
- {{ACCEPT_RECOMMEND — concrete recommendation + sensitivity/stress alternatives}}
- {{ACCEPT_WRITE — write memo/output to <docs-private/...md>}}
- {{ACCEPT_TEST_GATE — any test that must stay green / any artifact that must be produced}}

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
- Goal complete: <true | false; explain if false>
```

---

## Notes for the controller composing this prompt

- **Length is OK** — `/goal` mode is meant to consume substantial briefing. The 4 KB CLI argument concern that motivated file-based dispatch elsewhere is doubly relevant here; passing via `<` stdin redirection sidesteps it.
- **No pre-pasted file contents** — same verification-first principle as `prompts/dispatch-wrapper.md`. Point at files (`docs-private/super_giants_grid_3d_fabric.svg and source scripts`), don't paste their contents. The goal-mode agent has Read + Grep + Bash and the multi-hour budget to use them.
- **Acceptance criteria should be testable.** "Recommend central grid" is OK because the Final response schema demands a concrete answer. "Understand the system" is not OK — there's no completion signal.
- **Edit policy is load-bearing.** Goal-mode runs for hours; if you don't pin the edit scope, the agent may make changes you didn't authorize. `review-only` and `diagnostic-only` are the safe defaults; `full` is for chunks the goal-queue tags as code-writing.
- **Blocker protocol matters** — without it, goal-mode may loop on a wedged review tool indefinitely, eating tokens until budget exhaustion. Tell it to surface and stop.
- **Final response schema must be unambiguous.** The controller polls the tail file for that schema's appearance to detect completion. If the schema is fuzzy, you'll get false-positive-completes or miss the real one.
