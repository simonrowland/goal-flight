# Review types — the three-type taxonomy (locked 2026-07-17)

Reviews are cut by SUBJECT; everything else (fix authority, independence
structure, null-hypothesis locus, exit signal) follows from the subject.
Rationale + review history: the 2026-07-16 review-fix architecture memo
(operator docs-private; two 4-lens waves + closing pass + 3-cluster field
pilot). This file is the operative contract.

## Type 1 — PATCH MULTI-REVIEW (subject: one diff / change-set)
For EVERY commit-worthy chunk and every bug-patch verification. [RT-001]
STAKES CARVE-DOWN [FID-001]: trivial/mechanical chunks (doc edits, renames,
data entry) may use the standing floor instead — one concern-diverse
read-only review, no find/fix apparatus. Full Type-1 is mandatory for
non-trivial chunks and ALWAYS when the diff touches path-sensitive files.

**Shape: FIND / FIX split.**
- FIND: N parallel lens-finders (N=2 default; 3-4 for risky clusters) review
  the QUIESCENT branch READ-ONLY. Lens mix comes from the cluster's class:
  correctness + contracts always; add applicable sweep-corpus (SC) predicates
  and a path-sensitive lens when gate/validator/contract files are in the diff.
- FINDINGS ESCROW: each finder emits structured findings to stdout/tail —
  highest severity first, BEFORE any discussion prose:
  `FINDING <finder-label>-<n> | P<0-3> | <one-line>` (ids namespaced per finder [RT-OP-06]) + Evidence path:line + Fix shape +
  "Test must assert: <falsifiable behavior>" + patch sketch. The tail is the
  append-only escrow; a fix can never erase its finding.
- FIX: exactly ONE fixer — the original executor re-entering or a fresh boot;
  NEVER a wave finder. ENTRY GATE [FID-002]: fixer enters only under
  EXCLUSIVE HANDOFF — all finders terminal AND the executor terminal (marker
  + reconcile) AND the worktree lease released; machine-checkable from
  dispatch records. Never concurrent with any live worker in that worktree. Applies findings
  with per-fix NULL-HYPOTHESIS A/B evidence (without fix repro fails; with
  fix passes; record both runs) and a regression test asserting EXACTLY the
  finder-pinned behavior. Report shape: `protocols/review-fix-report.md`.
- ATTRIBUTION: every diff hunk maps to a finding-id, completely (no
  remainder); declared file:line ranges are binding. Unattributed hunks or
  range excess = mechanical REDO. Sketch drift = re-escrow (finder steer-ack
  or controller approval) BEFORE applying.
- CONTROLLER VERIFY: escrow-vs-report diff [RT-OP-04]: extract `^FINDING`
  lines from each finder tail and from the fixer report; diff the two sets —
  ids, severities, and 'Test must assert' clauses must match exactly · attribution map vs `git diff` ·
  re-run the gate yourself · deep-verify a deterministic sample [RT-OP-02]:
  sample iff int(sha1(fixer_dispatch_id_utf8).hexdigest(),16) % k == 0 with
  k=3 initially; the repo's current k lives in its CANNED-CONTEXT.md (walk
  toward k=10 as fix-survival proves clean, snap to k=1 on degradation) · path-sensitive fixes ALWAYS get full depth + finder re-check. The
  path-sensitive list (fail-closed gates, validators, PROVENANCE checks,
  cross-contract surfaces) lives in the repo's CANNED-CONTEXT.md
  'PATH-SENSITIVE' section — named source of truth, controller-maintained. [FID-004]
  Record via the store CLI [RT-OP-03/07]: closures use
  `goalflight_task.py review <id> --verdict clean --dispatch <fixer-id>`
  (overturned fixes: `--verdict overturned`); sampled/survived noted via
  `append <id>`; deferred/rejected findings re-enter via `capture` (deferred
  lane) citing the finding id. This IS the fix-survival ledger.
- Authority carve-outs (report-only + sketch, all types): physics-semantics
  calls, decision-gated items (neutral evidence), cross-repo contracts.
- EXIT: attribution complete + gate green + sample clean.
- Known residual, accepted: unsampled non-path-sensitive fixes carry
  coherence-level assurance until next-cluster traversal; priced by
  fix-survival, revisit the sampling ladder if it degrades.

## Type 2 — MILESTONE REVIEW (subject: accumulated state at a checkpoint)
Deep review at milestone cadence / before a new arc. Run
`commands/bug-sweep.md --mode milestone|qa` (domain × lens matrix → lane-fill
read-only audit → harvest → consolidate → adversarial verify → DISJOINT
parallel fix-groups with serial integrator). Null-hypothesis lives at the
per-finding VERIFY stage. EXIT: verified-blocker list drained. Structured
verification runs in a verify-worker; the controller spends context only on
adjudication.

## Type 3 — BUG-DICTIONARY DEEP-SWEEP (subject: a class predicate)
After every class mint (one catch → one class → one sweep) and on
under-searched predicates. Run `commands/bug-sweep.md --mode
predicate|bug-hunt` with `protocols/review-mining.md`; the shared sweep
corpus IS the dictionary; verify against real oracles / ground truth, never
vote-counting. EXIT: `marginal_real_yield ≈ 0` (mined out) via the --anchor
re-review.

## Universal rules (all types; field-proven)
- FIND-after-green is non-negotiable: a build claiming tests-green +
  self-refutation-dry still carried 9 corroborated P1s in the field pilot.
  "Worker COMPLETE + green" is never a stopping signal.
- Build-loop exit = self-refutation dry (attack your own contracts each
  cycle, one lens per cycle) — this cheapens the FIND wave, never replaces it.
- Findings recorded before fixes, everywhere.
- Honest accounting: reviews FIND more than they FIX (field: 15/10/5
  found/fixed/deferred) — deferred findings enter the store, not the void.
- Single-fixer vs fix-groups is determined by the subject: shared-context
  diff → one fixer; independent discoveries → disjoint groups.
