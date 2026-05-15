# validate-queue [<queue-file>]

Schema-check a goal-queue file. Read-only. Surfaces structural problems
before they bite mid-execute.

## When to invoke

- The user explicitly typed `/goal-flight validate-queue [<path>]`.
- After hand-editing a goal-queue and before running `/goal-flight execute`.
- Pre-flight inside `commands/execute.md` step 1 (called automatically;
  bail with a clear error if validation fails).

## What the user provides

- **No args** → validate the most recent `docs-private/<topic>-goal-queue-*.md`.
- **One arg `<queue-file>`** → validate that specific path.

## Steps

1. Resolve the queue file (most recent by date suffix if no arg).

2. Parse the queue. Each chunk is a `## #N <slug>` heading followed by
   four mandatory subsections (per `templates/goal-queue.tpl`):
   - `### SCOPE` — one paragraph.
   - `### CHECKLIST` — bulleted list.
   - `### ACCEPTANCE` — testable criteria, bulleted.
   - `### FORBIDDEN` — explicit anti-scope, bulleted.

3. Run these checks. Each failure is a P0/P1/P2:

   **Structural (P0 — blocks execute):**
   - Every chunk heading matches `^## #(\d+) (.+)$` — extract chunk number + slug.
   - Numbering is sequential starting from 1. No gaps. No duplicates.
   - Each chunk has all four mandatory subsections (`SCOPE`, `CHECKLIST`,
     `ACCEPTANCE`, `FORBIDDEN`). None are empty.
   - Slugs are unique across the queue.
   - Slugs are lowercase + hyphens only (no spaces, no underscores, no caps).

   **Tags (P1 — execute can proceed but flag):**
   - `[parallel-safe:<group>]` tags reference groups that have ≥ 2 members.
     A lone `[parallel-safe:foo]` chunk is parallelism-broken and should be
     untagged or grouped.
   - `[milestone]` tags should appear roughly every 5 chunks for projects
     ≥ 15 chunks; warn if absent.
   - `[controller-direct]` tags (when present): chunk SCOPE should imply
     trivial single-file work, < ~30 LoC delta, no cross-module coupling.
     Heuristic check: SCOPE word count < 60 AND CHECKLIST item count ≤ 3.
     Warn if violated (probably mis-tagged).

   **Content (P2 — quality hints):**
   - SCOPE word count: 40–250 words. Outside this range → flag.
   - CHECKLIST item count: 3–12. Outside this range → flag.
   - ACCEPTANCE items use testable language (regex hits on patterns like
     "test\b", "pytest", "grep", "assert\b", "raises", "equals", "returns").
     Zero testable hits → flag.
   - FORBIDDEN should mention at least one specific path, function, or
     pattern (regex hits on `\.py`, `simulator/`, function-name shape).
     Zero specifics → flag.

   **Cross-reference (P2):**
   - Every chunk references at least one file path or function name that
     exists in the repo (sample: grep its CHECKLIST and ACCEPTANCE against
     `git ls-files`). Zero hits → flag (likely stale slug).

4. Report:

   ```
   === goal-queue validation: <path> ===
   chunks: N (M [parallel-safe], K [milestone], L [controller-direct])
   P0 (blockers): X
   P1 (quality): Y
   P2 (hints):   Z
   ===

   <per-finding list, ranked by severity, with chunk number + line>
   ```

5. Exit non-zero if any P0 (when called from `commands/execute.md` pre-flight,
   this signals "do not dispatch"). Exit zero otherwise — P1/P2 are surfaced
   but don't block.

## See also

- `templates/goal-queue.tpl` — the canonical structure.
- `commands/decompose-plan.md` — produces the queue this validates.
- `commands/validate-dispatch.md` — validates per-chunk wrapper rendering.
