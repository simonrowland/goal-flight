# Field notes — review-types taxonomy (from the field, append-only)

Operator feedback from controllers who RAN the taxonomy. Distinct from the
architecture memo (rationale/history): this is what survived contact. Map each
note to the rule-id it touches so it stays traceable. Newest first.

---

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
