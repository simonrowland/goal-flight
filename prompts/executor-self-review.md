SELF-REVIEW BEFORE REPORTING DONE

Treat the code as if a different agent submitted it; you gain credit only
for what you find, not for what you wrote. Severity-rank P0/P1/P2/P3.

**Orchestrator note**: the categories below are intentionally abstract for
portability. When pasting this into an executor dispatch, **specialize each
bullet to this goal's project nouns and grep patterns** — the abstract
category names mean nothing to an executor until you tell them which file,
which mutation, which artifact to check. See `prompts/dispatch-wrapper.md`
for the verification-first principle and the slice-to-layer mapping when a
corpus exists.

- INVARIANT GAP    — does every state mutation close the relevant
                     conservation/balance/schema/contract invariant exactly?
                     does the proof artifact (if any) actually prove it?
                     [specialize: which invariant? which conservation law?]
- SCOPE LEAK       — does the new code read or write any resource not
                     declared in SCOPE?
                     [specialize: which accounts/tables/modules are in/out?]
- MUTATION PURITY  — does any flipped call site still use the legacy
                     mutator? (Grep for it; must be empty.)
                     [specialize: which grep pattern? which file tree?]
- BEHAVIOR DRIFT   — existing tests still pass numerically/structurally?
                     Snapshot diffs zero?
                     [specialize: which test suite? which numeric tolerance?]
- DEAD CODE        — leftover legacy branches now unreachable? sibling
                     duplication that should be lifted into a shared helper?
                     [specialize: which functions might now be unreachable?]
- CONTRACT LEAK    — does the new payload carry the exact data the legacy
                     path needed (units, names, types)?
                     [specialize: which payload? which downstream consumer?]
- INTEGRITY        — for authoritative units, does the new code mirror the
                     legacy algorithm exactly, not a re-derivation?
                     [specialize: which algorithm? which legacy path?]

**Proof shape (applies to any test you add with a fix, across the categories
above):** the default is a poison-pair — the green test asserts success
semantics, and a paired poison proves the named failure category actually
fires through the production predicate/shared helper (not a parallel
reimplementation). A lone green that cannot go red proves nothing.

Self-fix any P0/P1/P2 before reporting done. P3 may be deferred with a note.

REPORT FORMAT (in your reply to the orchestrator, before the diff summary):

```
## Self-review findings

### P0 (fixed)
- file:line — description — fix-summary

### P1 (fixed)
- ...

### P2 (fixed or deferred with rationale)
- ...

### P3 (deferred)
- ...

## Files changed
<git diff --stat output>

## Tests run
<commands and pass/fail>

## Surprises
<anything the orchestrator should know that wasn't in the goal text>
```

If you found no P0/P1 issues, say so explicitly. Clean self-reviews are
valuable data; do not invent issues to look thorough.

REJECTED FINDINGS → REGRESSION TESTS

If a milestone review (or any reviewer subagent) flagged a finding that
you investigated and found to be a misread of the code — i.e., the
reviewer was wrong, no fix is needed — DO NOT just dismiss it. The
finding identified a subtle invariant the code holds that wasn't obvious
to a reviewer. Add a regression test that locks in the correct behavior,
explicitly framed as "this test exists because Reviewer X claimed behavior
Y; the code actually does Z and the test guards against future drift
toward Y." This converts a one-time misread into a permanent guard.
