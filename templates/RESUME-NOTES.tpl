# Resume Notes — {{DATE}} (rev {{REV}})

Snapshot for a fresh Claude waking up mid-flow. Read this first, then `AGENTS.md` at the repo root, then `docs-private/{{TOPIC}}-goal-queue-{{QUEUE_DATE}}.md`'s Progress + Next-dispatch-batch sections, then whatever's relevant to the immediate task.

## TL;DR

You are the controller of an ongoing {{TOPIC}} refactor on `{{BRANCH}}` at `{{HEAD}}` (or later — `git log`). {{One sentence about high-level progress.}} {{One sentence about what is in flight or queued.}}

You dispatch {{Codex / Claude subagents — see "Codex reliability" below}} to execute each {{UNIT}} flip from `docs-private/{{TOPIC}}-goal-queue-{{QUEUE_DATE}}.md`. Each flip executor does **adversarial self-review inside its own prompt** before reporting done (amended §N rule). You verify the final diff briefly and commit.

## Two-worktree convention

| Worktree | Branch | Role |
|----------|--------|------|
| `{{REPO_ROOT}}/` (main worktree) | `main` | Actual code. All commits land here. Executors work here. |
| `{{REPO_ROOT}}/.claude/worktrees/{{CTRL_WORKTREE}}/` | `claude/{{CTRL_WORKTREE}}` | Controller workspace. Drafts only; no commits to `main` from here. |

**Work from the main worktree** for any code change. Controller artifacts live in `main/`: `AGENTS.md` at the root, plus `docs-private/{{TOPIC}}-{...}.md` files.

## Reading order on wake

1. **`AGENTS.md`** (repo root, gitignored) — project invariants, terse style.
2. **`docs-private/{{TOPIC}}-goal-queue-{{QUEUE_DATE}}.md`** — dispatch playbook. Progress section + Next-dispatch-batch at top, then the §N PER-INTENT FLIP RULE.
3. **`docs-private/{{TOPIC}}-binding-spec-{{SPEC_DATE}}.md`** — objects, intents, engine×intent authority matrix, per-engine I/O contracts.
4. **`docs-private/{{TOPIC}}-refactor-plan-{{PLAN_DATE}}.md`** — original plan of record (if separate from binding-spec).
5. {{Any other domain-specific files in their reading order.}}
6. {{Milestone-review handoff prompt, if any.}}
7. The relevant goal text in the goal-queue for whatever's queued.

## Code state (as of {{DATE}} rev {{REV}})

`{{BRANCH}}` at `{{HEAD}}`. Recent stack, newest first:

```
{{GIT_LOG_BLOCK}}
```

Suite at `{{HEAD}}`: **{{N_PASSED}} passed, {{N_SKIPPED}} skipped, {{N_DESELECTED}} deselected**.

Backup tag (if any): `{{BACKUP_TAG}}` — {{what it preserves}}.

## In-flight at time of writing

| Type | ID | Working on | Where work goes |
|------|----|----|----|
| {{Codex / Claude subagent}} | `{{EXECUTOR_ID}}` | {{Goal #N {{UNIT}} flip — what specifically}} | {{Main worktree, leaves uncommitted / committed at <hash>}} |

**When this executor lands:**

1. Brief diff verification (the amended self-review should have done the adversarial pass internally; controller just checks scope + grep for `{{MUTATOR_PATTERN}}` purity in the flipped function + confirms suite green).
2. Commit as `{{COMMIT_MESSAGE_TEMPLATE}}`.
3. Dispatch **next: {{NEXT_UNIT}}** — {{module-path-and-rough-shape}}.

## Codex reliability

`codex exec` has stalled silently in long sessions ({{frequency observed, e.g., "~2/5 runs"}}). **Default executor is now Claude subagent** for well-scoped intent flips. Reserve codex for tasks that genuinely benefit from it (long-running, well-decomposed goals where codex's strengths show).

If codex stalls mid-goal: kill the process, re-dispatch the same `\goal` text as a Claude subagent prompt; the §N rule applies identically.

## Per-unit flip rule (amended {{AMENDMENT_DATE}})

Reproduced here for fast lookup; canonical source is §N of the goal-queue.

```
1. Implement <module-path>/<unit>.
2. Register <unit> with <central-dispatcher>.
3. Run shadow mode: legacy + new in parallel.
4. Assert parity within <tolerance> on <representative-inputs>.
5. Flip the call site.
6. Adversarial SELF-review by the executor before reporting done.
   Frame: "treat the code as if a different agent submitted it; credit
   only for what you find, not what you wrote." Self-fix P0/P1/P2;
   defer P3 with a note.
7. Controller verifies diff briefly + commits. One unit per commit.
```

## Hardened infrastructure (in place since `{{COMMIT}}`)

Reusable helpers, fixtures, do-not-re-derive patterns:

- `{{HELPER_1}}` — {{purpose}}. Use this, not the bespoke `{{LEGACY_STANZA}}`.
- `{{FIXTURE_1}}` — {{purpose}}. {{N}} representative inputs covering {{coverage}}.
- {{PATTERN_NAME}}: {{description}}. **DO NOT re-derive** — `{{file}}:{{line}}` has it.
- {{Signature change}}: `{{NEW_SIGNATURE}}` (changed from `{{OLD_SIGNATURE}}` in `{{COMMIT}}`).

## Goal queue position

Currently executing: `\goal {{CURRENT_SLUG}}` (#{{N}}), {{X}} of {{Y}} units flipped.
Next after this: `\goal {{NEXT_SLUG}}` (#{{M}}).
After that: {{...}}.

Full progress in `docs-private/{{TOPIC}}-goal-queue-{{QUEUE_DATE}}.md`'s Progress table.

## Open follow-ups (deferred, not blocking)

- {{ITEM_1}} — deferred from {{date / commit / review-round}}; revisit when {{trigger}}.
- {{ITEM_2}} — deferred; rationale: {{...}}.

## How to dispatch the next intent flip

1. Read this file's "In-flight" section and confirm the previous executor has landed (suite green, diff committed).
2. Open the goal-queue, find the next undone unit per the in-flight table.
3. Construct the dispatch prompt:
   ```
   <paste goal text from queue>
   PER-UNIT FLIP RULE: see §N of the goal-queue. Embedded review flight required.
   ```
4. Dispatch as Claude subagent (general-purpose) or Codex `exec`.
5. Update this file's In-flight table with the executor ID.
6. When executor reports done: verify diff, commit, update Progress table in goal-queue.

## After resume — first 5 minutes

1. `git log -5 --oneline` — confirm `{{HEAD}}` is still HEAD (or note new commits).
2. Read `AGENTS.md` hard invariants (skim).
3. Read goal-queue Progress table — confirm in-flight table here matches.
4. If executor finished while you were away: verify diff + commit per "How to dispatch" step 6.
5. If executor still running: leave it; check back in N minutes.
