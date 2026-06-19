# Lane-fill bug sweep (light guidance)

A throughput pattern for harvesting bugs across a repo without interrupting the build or
saturating the controller's context. Complements `protocols/review-mining.md` (durable
verdicts + class minting) and the milestone review flow. Proven by a self-dogfood on this
repo (2026-06-19): one sweep surfaced 18 verified-real blockers including the root cause of a
long-standing worker-death issue.

Scale to the ask: a few finders for "any quick bugs"; a full lane-fill + verify for "audit this
thoroughly". The floor is ≥1 verify pass on anything you'll act on — never act on raw
single-pass findings.

## Pipeline
`frame + matrix → lane-fill READ-ONLY audit → harvest → consolidate → adversarial verify →
surface REAL blockers → routing table → grouped fixes → serial integrator`

1. **Frame (basematter)** — one shared file every worker reads: the audit protocol, the
   BLOCKING rule (a finding BLOCKS only if it lives in a subsystem the queued backlog
   modifies/builds-on), the finding schema, the class taxonomy, and the queued-work backlog
   (the BLOCKING yardstick). Write-once-read-many: keeps per-worker prompts tiny.
2. **Matrix (targets)** — N rows of `domain × lens`, one per worker, disjoint and exhaustive.
   Row count is driven by COVERAGE (the domains × lenses the audit needs), NOT by how many
   lanes you have. Lanes only set wall-clock: the durable queue + drainer process rows in
   waves (≈ ceil(rows / lanes)), priority-ordered, so oversubscription is safe — e.g. 30 rows
   over 6 lanes runs in ~5 waves on a constrained box. Sizing the matrix to a multiple of the
   available lanes is an OPTIONAL wall-clock tuning (cleaner waves), never a cap on coverage.
   Lane-split by difficulty: subtle/critical rows → the stronger engine, breadth rows → the
   cheaper/wider pool ("fill all lanes" = saturate the pools you have, in waves if needed).
3. **Audit** — one worker per row, READ-ONLY, **one-shot** (breadth comes from the matrix
   partition, not per-worker loops), inline findings to its tail, BLOCKING first, end `READY:`.
4. **Harvest** — a write-enabled step parses the worker tails into an append-only
   `BUG-LOG.jsonl` (one finding/line: id, reviewer, sev, class, file, title, detail,
   confidence, bucket). Mandatory: read-only workers can't write the sink themselves.
5. **Consolidate** — single serial worker: dedup → triage BLOCKING-vs-backlog → rank
   (severity × confidence × blast-radius) → class rollups + systemic clusters → top-N → write
   `BUG-LOG-CONSOLIDATED.md`. While its context is loaded it also drafts catalogue entries
   (existing class → ledger row; novel → minted shape) marked `proof_basis: SPECULATIVE`,
   **staged** locally — promote to any shared corpus only after verify.
6. **Adversarial verify** — the trust gate. Single-pass findings are NOT work until verified.
   Verifiers grouped BY CHUNK (not lens), refute-by-default, READ-ONLY, classify each
   candidate-blocker REAL / FALSE-POSITIVE / ALREADY-FIXED / NOT-ACTUALLY-BLOCKING with
   concrete code-path evidence + recent git history. Use a different engine than the audit for
   diversity. Treat a suspiciously clean 0-false-positive result as a sign the refute stance
   was too soft, not as proof.
7. **Surface** — the controller sees ONLY the verified-REAL, genuinely-blocking slice; the
   rest stays in files.

## Dispatch (load-bearing reliability rules, dogfood-proven)
- **Launch sweep/fix workers via the out-of-session (launchd/systemd) drainer**, not in-session
  (`--submit --no-drain-on-submit`, then kick the drainer). A bash-tail worker launched inside a
  short-lived controller process can be reaped by `cleanup_ghosts` / release-stale once that
  controller exits — see the D007/D008 class. Out-of-session launch gave ~96% survival vs
  frequent mid-run deaths in-session.
- **codex workers: instruct "do NOT use context-mode / ctx_* tools"** (use ripgrep/git/read).
  The exec-mode elicitation wedge that wrecks codex review workers did not fire once when this
  was stated (0 hits across a verify fleet). Or route to a non-wedging engine.
- Workers are READ-ONLY in audit/verify; only harvest/consolidate/integrator write.

## Fixing (thrifty on controller context; controller keeps final say)
- **Routing table** (verifier emits it, controller dispositions ~1 line/bug): per blocker
  propose a tier from blast-radius / #plausible-fixes / subsystem / poison-pair feasibility.
  - **A autonomous/pinpoint** → worker self-converges (internal review lanes + a fast-model
    falsifier that tries to disprove "the bug is fixed"), TL;DR only.
  - **B RAG-assisted** → attach the named context-pack/RAG facts; worker self-converges, TL;DR.
  - **C controller-in-loop** → thematic / cross-cutting / ambiguous / security / spine; approve
    the fix contract up front and review the result.
- **Concurrency without worktree sprawl**: group blockers into **disjoint file-sets** (bugs
  sharing a file → same group). Tier A/B groups run concurrently in the shared checkout
  (disjoint files), self-converge vs SCOPED tests, and emit a **patch + TL;DR + test result —
  no commit** (avoids index-lock races + cross-edit suite flakiness). A single **serial
  integrator** applies each patch to a clean tree, runs the FULL suite per apply, makes one
  pathspec'd commit per group; overlap/regression → flag that group. Worktrees only for Tier-C.
- **Poison-pair is the default fix-test shape** (fails on the bug, passes on the fix).

## Modes (same plumbing; frame + matrix content changes)
1. overdue-milestone review · 2. general QA pass · 3. open bug-hunt / class discovery ·
4. pattern-search keyed to under-searched predicates in the shared sweep-corpus (the class-hunt
arm; mint/promote per `protocols/review-mining.md`).

## Re-review as a controlled experiment
Bake a single named knob (the **AUDIT-ANCHOR**, e.g. latest-HEAD vs N-commits-back) into the
frame and record each run in a manifest. A re-review changes ONLY that knob; compare the
two runs' POST-VERIFY REAL sets — `marginal_real_yield ≈ 0` ⇒ saturated/"mined out", `> 0` ⇒
the framing matters. One variable at a time.

## Controller-context budget
Steady state: the controller reads only the verified blocker list + the integrator's per-group
pass/fail (posture A — path-not-payload). The full backlog is read only at a milestone "bug
breather", after which reload the critical-path context via a directed compaction (posture B).

Full design history + the dogfood run live under `docs-private/research/` and
`docs-private/reviews/` (gitignored).
