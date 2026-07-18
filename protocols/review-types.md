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
  "Test must assert: <CONCRETE input -> wrong-output pair, never a vague
  property — 'the gate works' is rejected>" + patch sketch. [field-note gap 4:
  the whole chain anchors on this clause; the CONTROLLER sanity-checks all
  assertion clauses in the escrow BEFORE dispatching the fixer — weak clauses
  go back to the finder, not forward to the fixer.] The tail is the
  append-only escrow; a fix can never erase its finding.
- FIX: exactly ONE fixer — the original executor re-entering or a fresh boot;
  NEVER a wave finder. ENTRY GATE [FID-002]: fixer enters only under
  EXCLUSIVE HANDOFF — all finders terminal AND the executor terminal (marker
  + reconcile) AND the worktree lease released; machine-checkable from
  dispatch records; death WITHOUT a marker is the COMMON case, not the edge —
  reconcile via `protocols/dispatched-worker-recovery.md` before treating any
  worker as terminal. FIXER DEATH MID-FIX [field-note gap 5]: the attribution
  map is the recovery tool — hunks attributable to escrowed findings are
  keepable after verification; unattributed partial hunks are reverted; the
  replacement fixer resumes from the escrow, not from the corpse's diff.
  Never concurrent with any live worker in that worktree. Applies findings
  with per-fix NULL-HYPOTHESIS A/B evidence (without fix repro fails; with
  fix passes; record both runs) and a regression test asserting EXACTLY the
  finder-pinned behavior. Report shape: `protocols/review-fix-report.md`.
- ATTRIBUTION: every diff hunk maps to a finding-id, completely (no
  remainder); declared file:line ranges are binding. Unattributed hunks or
  range excess = mechanical REDO. Sketch drift = re-escrow (finder steer-ack
  or controller approval) BEFORE applying.
- RE-REVIEW WIDTH [field-note 2026-07-17 later]: after a fix, re-review width
  INHERITS from the previous round's yield — a still-yielding subject gets 3-4
  diverse-lens finders in ONE round, never serialized singles (field: narrowing
  3-wide→1-wide serialized a patchset into ~8 rounds; the residual classes were
  distinct and lens-specific). A wide round returning ~nothing new is the
  convergence signal. Width = lens diversity, not reviewer count; batch-find
  never implies batch-fix (the fix side stays ONE fixer); trigger on yield, do
  not blanket-widen clean patchsets; ~2 wide rounds is the realistic floor since
  fix-revealed residuals are intrinsically serial.
- FINDER DISAGREEMENT [field-note gap 2]: when finders conflict on the same
  code, resolve against GROUND TRUTH (decision-locks, specs, real oracles) —
  NEVER by vote-counting or majority. (Field: two finders split on a
  hardcoded spectrum method; the decision-lock settled it; a vote would have
  shipped the wrong call.)
- CONTROLLER VERIFY: escrow-vs-report diff [RT-OP-04]: extract `^FINDING`
  lines from each finder tail and from the fixer report; diff the two sets —
  ids, severities, and 'Test must assert' clauses must match exactly · attribution map vs `git diff` ·
  re-run the gate yourself — BROAD scope, never the fixer's touched-set (field: a touched-set-green fix broke a contract encoded in a different test file; only the wide sweep caught it) [field-note gap 3] · deep-verify a deterministic sample [RT-OP-02]:
  sample iff int(sha1(fixer_dispatch_id_utf8).hexdigest(),16) % k == 0 with
  k=3 initially; the repo's current k lives in its CANNED-CONTEXT.md (walk
  toward k=10 as fix-survival proves clean, snap to k=1 on degradation —
  LADDER UNVALIDATED at n=2 field clusters: hold k=3 until the fix-survival
  ledger has real history [field-note (e), 2026-07-17]) · path-sensitive fixes ALWAYS get full depth + finder re-check. The
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
- BASE FRESHNESS IS A STANDING DISCIPLINE, never operator-prompted [operator
  directive 2026-07-18]: before EVERY find wave, bug-sweep, or fix cluster, the
  controller verifies its worktree/branch is at the CURRENT integration tip
  (fetch; compare against origin/<trunk> or the repo's designated head; sync or
  re-anchor BEFORE dispatching) and records the anchor SHA in the run
  manifest/dispatch brief. Sweeping or fixing against a stale base is
  invalid-by-default: findings may be already-fixed, fixes may conflict, and
  the whole cluster's evidence is against software nobody ships. Freshness is
  checked at CLUSTER BOUNDARIES only — once a cluster's FIND wave starts, the
  base stays pinned until the cluster closes (re-anchoring mid-cycle
  invalidates the escrow); the next cluster re-syncs first.
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

## Field notes — 2026-07-17 (bugfix flight; PHYS + OPT clusters, n=2)
Sharpen three universal rules with second-cluster field data. Both clusters
independently confirmed FIND-after-green; these tighten the framing.

- [a] SELF-REFUTATION-DRY AND TESTS-GREEN ARE ~ZERO EVIDENCE, NOT PARTIAL —
  treat both as adversarial-until-proven. In BOTH field clusters the dry/green
  claim was not merely insufficient, it was WRONG: PHYS BUILD claimed
  self-refutation-dry + 147 green and carried 9 P1s; OPT fixer claimed 31 FIXED
  + 1,153 green and the confirming review found 1 P1 + 2 P2, one INSIDE the
  fixer's own fix. Corollary: a dry/green claim NEVER justifies reducing N or
  skipping the FIND wave.
- [b] GREEN-GATE ⊥ FIX-CORRECTNESS (orthogonal, not overlapping). "Re-run the
  gate yourself" proves NO-REGRESSION; it CANNOT prove the fix is correct —
  every field bug the FIND wave caught lived in UNTESTED behavior (the gate was
  honestly green because no test covered the EvalSpec-missing path / the
  spatial-conflation residual). The gate and the FIND wave verify different
  things; green gate is NEVER evidence of fix correctness, only of not-breaking
  what was already tested.
- [d] DEFERRED/CARVE-OUT → STORE IS A MACHINE-CHECKABLE EXIT CONDITION, not a
  best-effort. Field miss: 56 verified sweep SURVIVES + 13 OPT carve-outs lived
  in research files, invisible to the store, until the operator asked. Make it
  a hard cluster-close gate alongside attribution-complete: every REPORTED /
  carve-out / deferred finding is `capture`-d to the store (deferred lane,
  citing the finding id) BEFORE the cluster closes — an unstored deferred
  finding blocks close the same way an unattributed hunk blocks accept.
