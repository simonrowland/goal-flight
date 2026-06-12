# Review mining: save findings durably, mint bug classes, sweep backwards

One catch is rarely one bug. Generic re-review passes converge to silence;
**class hunts keep paying**. This protocol is the loop that turns each caught
bug into a searchable pattern and each pattern into a backwards sweep.

## The norm: review results are durable, never /tmp-only

Every review or verification dispatch writes (or has its verdict copied to)
`docs-private/reviews/<date>-<slug>/` or the chunk's
`docs-private/research/<slug>/` directory **before the controller moves on**.
Dispatch tails under `/tmp/goal-flight-*/` die at reboot; a verdict that
lived only there cannot be mined. Minimum durable record per review: the
prompt (or its path), the P1/P2/P3 findings, the VERDICT line, and the
round number. The dispatch-id alone is not a record.

## The MINT-generalize loop

Trigger: any NEW bug class caught — by a reviewer, a field report from a
peer controller, a production incident, or your own diagnosis. "New class"
means the predicate is new, not the instance (a second off-by-one in the
same parser is the same class; a fence that fails on offset input when the
last one failed on decoration is a NEW class).

A REFUTED FIX closure is a mint candidate: the refuted resolution/test shape
is a caught bug class until proven local, so run the predicate + backwards
sweep question before treating it as one-off cleanup.

1. **MINT the class.** Write the predicate in one or two sentences,
   sanitized and project-neutral: what shape of code/assumption fails, and
   the question a hunter asks to find another instance. Record it with the
   durable review/findings record. If the operator maintains a cross-project
   sweep corpus, mint the class there too in its format.
2. **SWEEP BACKWARDS.** Dispatch a class-hunt (read-only, bash-tail) over
   the existing code and the durable review archive: "find every other
   place this predicate holds." A class hunt brief states the predicate and
   the anchor instance — it is NOT a generic re-review. Old saved findings
   are part of the hunt surface: a P3 noted-but-not-fixed in a past review
   is often the same class waiting (anchor case: a parser fence assumed its
   input started at line 1; an earlier review had recorded "holds when the
   input starts at offset 0" as a passing observation — the class was
   visible in the archive before production found it).
3. **Record the sweep.** Write the result — hit or no-hit — to
   `docs-private/research/<date>-<class-slug>/sweep-findings.md` (predicate,
   surfaces hunted, instances found or "no hit", date). Hits become fixes
   (normal review-before-commit path) plus regression tests encoding the
   class. If the operator maintains a cross-project sweep corpus with a
   ledger, record the sweep there too. An unswept class is an open
   liability, not an unknown.
4. **Encode forward.** The class predicate joins the standing review lenses
   for future chunks touching that surface (a line in the chunk-review
   rubric or the relevant protocol), so the next instance is caught at
   review time, not in production.

## Cadence

- On every new bug class: run the loop immediately while the anchor
  instance is fresh (the sweep brief writes itself from the diagnosis).
- At milestones: check the durable review archive for noted-but-unswept
  observations (P3s, "pre-existing" remarks, deferred edge cases) and
  promote any that describe a class.
