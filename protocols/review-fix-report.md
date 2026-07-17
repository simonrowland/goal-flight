# REVIEW-FIX REPORT TEMPLATE (operator-prescribed, 2026-07-16)

Every review-fix worker reports per-finding blocks in EXACTLY this shape.
Summary block first (controller triage), long-form at the bottom (drill-down
only when suspicious). Declared ranges are BINDING: any diff outside the
union of all declared ranges = automatic REDO, no discussion.

## Per-finding block (FIXED)

```
FIXED <finding-id> | P<as-escrowed — the finder's severity, never re-graded by the fixer [RT-002]> | <one-line title>
Description: <2-3 lines: what was wrong, why it matters>
Repro: <1-2 line summary — how the defect manifests>
Null-hypothesis test: <1-2 lines — the A/B: without fix repro FAILS how,
  with fix PASSES how. "Reverted my change, repro failed again" is the gold shape.>
Regression test: <test file::test_name added/extended>
Files touched:
  foo.py:20-30
  bar.py:12,34-40
```

## Per-finding block (REPORTED — authority carve-outs or out-of-scope)

```
REPORTED <finding-id> | P1 | <one-line title>
Description: <what's wrong + evidence path:line>
Repro: <summary>
Why not fixed: <carve-out class: physics-semantics | decision-gated:<q-id> |
  cross-repo-contract> [RT-003/FID-005: these three ONLY; anything else
  fix-shaped goes through the DRIFT/steer escalation valve, not report-only]
Patch sketch: <3-6 lines — the fix the finder would make, anchored>
```

## Footer (once per report)

```
GATE: <focused gate command> -> <result one-liner>
ATTRIBUTION MAP [FID-006/RT-OP-05]: one row per hunk:
  | <file>:<range> | <finding-id> |
  (complete — every hunk in `git diff` appears; no remainder rows allowed)
DIFF-FOOTPRINT: <n files, n lines — must reconcile with the union of declared ranges>
Repro and null-hypothesis, long form:
<per finding-id: full repro steps + full A/B evidence, as long as needed>
```

## Controller levers (what this buys)
- ACCEPT [FID-003]: run the declared regression test + check the attribution
  map + eyeball ranges — this verifies COHERENCE only; the mandatory sample
  + path-sensitivity checks run alongside and are not optional.
- REDO: steer citing the specific block field that failed (bad null-hypothesis,
  range mismatch, gate red) — the worker re-enters with surgical context.
- REJECT: revert the declared ranges for that finding id only; the FINDING
  survives (findings-before-fixes) and re-enters the store as open.
- SCOPE-CREEP (mechanical): actual diff ⊄ declared ranges → automatic REDO.
