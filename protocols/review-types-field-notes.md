# Field notes — review-types taxonomy (from the field, append-only)

Operator feedback from controllers who RAN the taxonomy. Distinct from the
architecture memo (rationale/history): this is what survived contact. Map each
note to the rule-id it touches so it stays traceable. Newest first.

---

## 2026-07-18 — worktree/base freshness (operator directive)

**[universal — gap] Bugfix controllers only sync their worktrees to head/origin
when the operator reminds them.** Sweeps and fixes then run against stale
software: findings may already be fixed, fixes conflict on merge, and the run's
evidence describes a version nobody ships. Ratified as a standing universal rule
(review-types.md) + a pre-wave gate in lane-fill-bug-sweep.md: sync/verify the
anchor at every cluster/campaign start, record the anchor SHA, pin during the
cycle, re-sync at the next boundary. Tooling task filed for a deterministic
staleness check (status WARN when a working base trails the integration tip).

## 2026-07-17 (latest) — separation-FF: isolate CONFIRMED from HELD before commit (battery bugs controller)

**[Type-1 FIX/commit — gap] When one fixer's diff mixes CONFIRMED-clean and HOLD findings in
shared files, the protocol has no rule for landing the clean part.** Field: twice in one burn a
fixer tangled a confirmed-good fix with a masked/deep one — (a) a per-row state-leak fix (clean)
interleaved with an always-on-safety-check fix that MASKED a regression via test-narrowing;
(b) an evaluator fail-close fix (confirmed) interleaved with two provenance fixes that were
review-MASKED (tests injected fields production never returns). In both, the clean and held
fixes shared files (and one shared a test file with the masking), so a by-pathspec commit
couldn't separate them.

Rule to ratify: **a fixer diff mixing CONFIRMED and HOLD findings gets a SEPARATION-FF before
commit — keep the confirmed hunks + their RED tests, REVERT the held hunks, UN-MASK any test the
held fix narrowed, and verify the un-masked test passes with the confirmed fix ALONE (proving the
regression was purely the held part). Then commit the confirmed part; capture the held part as a
scoped task with its precise requirement.** Rationale: don't hold a real fix hostage to a deep
one, and never let a masked-regression's test-narrowing ride along on the clean commit. Cost is
one extra round; it converts "hold everything" into "land the good half + scope the hard half,"
which is what honest burn-down needs. Composes with the attribution map [gap 5]: the confirmed
hunks are the attributable-and-verified ones; the held hunks are exactly what you revert.

## 2026-07-17 (later) — re-review width escalation (battery bugs controller, relayed via operator)

**[Type-1 FIND/re-review — gap] Yield-triggered width escalation is stated for the
first FIND wave but silent on re-review after a fix.** Field: a C2 patchset serialized
~8 narrow review rounds; the controller started 3-wide (initial finder wave) then
narrowed every re-review to 1-wide — that narrowing is what serialized it. The
residuals were *different classes* (publish-bypass, source-flag copy, missing test
teeth, non-binding citation), and different classes are found by different lenses,
not by re-running one lens deeper.

Rule to ratify: **re-review width inherits from the previous round's yield.** A
still-yielding subject gets 3-4 diverse lenses in one round; a wide round returning
~nothing new is a stronger convergence signal than a narrow one. Limits (part of the
rule, not caveats to drop): (1) some serialization is intrinsic — residuals that only
become reachable after a fix lands can't be found pre-fix, so the realistic target is
~2 wide rounds, not 1; (2) width = lens diversity, not reviewer count — identical
reviewers redundantly find the same top issue; (3) batch-find never implies batch-fix
— the fix side stays single-fixer (Type-1) or disjoint groups (Type-2); (4) adaptive,
not blanket — trigger on first-round yield, don't widen clean-majority patchsets.
Also a controller-token win: collapsing 4 narrow rounds into ~2 wide ones saves the
per-round read+dispatch+monitor orchestration cost. Independently validated the same
burn (c2b gallery-gate patchset): two straight narrow rounds each yielded a residual;
a 3-lens wide round then converged in ONE — code confirmed clean across all three
lenses, the lone remaining test-tooth residual pinned by 2 of 3. A clean WIDE pass is
itself a stronger "done" signal than a clean narrow one.

## 2026-07-17 — 3-cluster field pilot + backlog burn-down (bugfix controller)

Ran the taxonomy end-to-end: C1 (CalculiX export, BUILD→FIND→FIX→VERIFY),
C2 (fail-closed gates, audit sweep), then a ~490-item store burn-down. Verdict:
doctrine is right; the mechanical guards target the real failure modes. Five
notes — one to **keep-because-proven**, four **gaps to close**.

### Proven in the field (keep — do not water down)
- **FIND-after-green [universal].** C1's BUILD was tests-green + self-committed +
  self-review-dry and still shipped a production-breaking regression (exporter
  required an ack the production path never emitted, masked by a fixture-injecting
  test). This rule is load-bearing; the 9-P1 example in the contract is real.
- **Escrow-before-prose [RT-OP-06].** A FIND worker died `worker_dead` mid-run;
  its full 9-finding escrow was recoverable from the tail. Append-only escrow paid
  off exactly as designed.
- **Deterministic sample + verify-worker-for-context [RT-OP-02, Type-2].** These
  are the answer to the pilot's loudest cost problem: I deep-verified *every* fix
  in C1/C2 and it was controller-context-expensive. The sample ladder + verify-worker
  are the right dials. Keep.

### Gaps to close (ranked by leverage)

1. **[operability — HIGHEST] Tool the three scriptable checks; don't leave them as prose.**
   Honest failure of adherence: I ran the taxonomy's *spirit* (find/fix/verify/
   dual-review/ground-truth) but not its mechanical *letter* — never computed a
   sha1 sample, never ran a formal `^FINDING` set-diff, never built an attribution
   map. Under deep context + a flaky reservoir, a controller eyeballs. Ship as
   one-liner tooling or `goalflight_task.py` subcommands: (a) escrow-vs-report
   `^FINDING` set-diff [RT-OP-04], (b) sha1-mod-k sample selection [RT-OP-02],
   (c) attribution-map-vs-`git diff` reconcile [RT-OP-05]. Tooled → executed;
   prose → skipped precisely when rigor matters most.

2. **[Type-1 / RT-OP-04] Add "resolve finder DISAGREEMENT against ground truth,
   never by vote" to CONTROLLER VERIFY.** Field: two C1 finders *disagreed* on a
   hardcoded spectrum method — one blessed it as a honesty-upgrade, one flagged it
   as a D-Q59 (decision-lock) violation. Vote-counting ships the wrong call; reading
   the decision-lock settled it (the flag was correct). This rule currently lives
   only in Type-3; but per-diff finder disagreement is a **Type-1** event. Duplicate
   it into Type-1.

3. **[Type-1 / RT-OP-04] Make "re-run the gate yourself" explicitly BROAD, not
   touched-set.** Field: a fixer's touched-file tests were green, but its change
   broke a test in a *different* file that encoded the old contract — caught only by
   a broader sweep, not the touched-set. State plainly: the controller's gate re-run
   must be wide enough to catch cross-file contract breaks.

4. **[FIND / RT-001] Guard finder-assertion quality — the whole chain anchors on it.**
   The null-hyp + attribution + regression machinery all rest on the finder's
   "Test must assert: <falsifiable behavior>" clause. A vague clause ("the gate
   works") makes every downstream guard hollow — the fixer satisfies it trivially.
   The fixer is heavily reviewed; the finder is not. Cheap fix: require the clause
   to be a concrete input→wrong-output, and have the controller sanity-check the
   escrowed assertion clauses BEFORE the fixer runs (they're already in the tail).

5. **[FID-002 entry gate] Handle fixer DEATH, not just fixer-terminal.** The entry
   gate assumes "executor terminal (marker + reconcile)". Field reality: ~5 worker
   deaths from reservoir flakiness this run, including a **fixer that died mid-edit**
   leaving a partial change of uncertain completeness, and a launch that lost its
   claim token (never ran). Death-without-marker is the common case, not the edge.
   Cross-reference `dispatched-worker-recovery.md` from FID-002 and define the
   partial-fixer-worktree recovery (verify completeness before trust; re-dispatch
   the remainder, don't commit a half-fix).

### One-line summary for the maintainer
Doctrine sound; before "done": (a) **tool** the escrow-diff / sample / attribution
checks so they survive a tired controller, (b) move **ground-truth-not-vote** into
Type-1, (c) broaden the gate re-run, (d) guard finder-assertion quality, (e) wire
FID-002 to worker-death recovery.
