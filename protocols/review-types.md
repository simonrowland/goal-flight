# Review types — the three-type taxonomy (locked 2026-07-17)

Reviews are cut by SUBJECT; everything else (fix authority, independence
structure, null-hypothesis locus, exit signal) follows from the subject.
Rationale + review history: the 2026-07-16 review-fix architecture memo
(operator docs-private; two 4-lens waves + closing pass + 3-cluster field
pilot). This file is the operative contract.

## Type 1 — PATCH MULTI-REVIEW (subject: one diff / change-set)
For every commit-worthy cluster chunk and every bug-patch verification.

**Shape: FIND / FIX split.**
- FIND: N parallel lens-finders (N=2 default; 3-4 for risky clusters) review
  the QUIESCENT branch READ-ONLY. Lens mix comes from the cluster's class:
  correctness + contracts always; add applicable sweep-corpus (SC) predicates
  and a path-sensitive lens when gate/validator/contract files are in the diff.
- FINDINGS ESCROW: each finder emits structured findings to stdout/tail —
  highest severity first, BEFORE any discussion prose:
  `FINDING <id> | P<0-3> | <one-line>` + Evidence path:line + Fix shape +
  "Test must assert: <falsifiable behavior>" + patch sketch. The tail is the
  append-only escrow; a fix can never erase its finding.
- FIX: exactly ONE fixer — the original executor re-entering or a fresh boot;
  NEVER a wave finder. Enters only under EXCLUSIVE HANDOFF (all finders
  terminal, worktree lease free — check dispatch records). Applies findings
  with per-fix NULL-HYPOTHESIS A/B evidence (without fix repro fails; with
  fix passes; record both runs) and a regression test asserting EXACTLY the
  finder-pinned behavior. Report shape: `protocols/review-fix-report.md`.
- ATTRIBUTION: every diff hunk maps to a finding-id, completely (no
  remainder); declared file:line ranges are binding. Unattributed hunks or
  range excess = mechanical REDO. Sketch drift = re-escrow (finder steer-ack
  or controller approval) BEFORE applying.
- CONTROLLER VERIFY: escrow-vs-report diff · attribution map vs `git diff` ·
  re-run the gate yourself · deep-verify a deterministic sample
  (hash(dispatch_id) mod 3 == 0; walk toward mod 10 as fix-survival proves
  clean) · path-sensitive fixes ALWAYS get full depth + finder re-check.
  Record verdict + sampled?/survived? on the store review record (this is
  the fix-survival ledger).
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
