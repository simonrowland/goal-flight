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


## Standing class: a field asserting a state it never measured

This one earned a permanent lens rather than another entry in the archive.
**Eight confirmed instances catalogued in a single sweep**, across code,
comments and a protocol — different subsystems, one shape.

**Predicate.** A value is presented as an observation but is derived from
something adjacent that was convenient to reach: an intent instead of an
outcome, a label instead of a measurement, a request instead of a grant, a
population instead of the subset asked about. The value is usually *correct
about something* — just not about what its name claims.

**The hunter's question.** For every field on a status line, a dashboard cell,
a guard, or a sentence in a doc: *what measurement backs this, and when was it
taken?* If the answer is "it follows from X", ask whether X can be true while
the claim is false. That gap is the bug.

**Confirmed instances, as recognition training.** Every row below was checked
back to the code it describes; a ninth candidate was dropped because the
hardcode it alleged had never actually landed. Verify before you cite — a
lens against unmeasured claims cannot afford unmeasured evidence.

| asserted | actually measured |
|---|---|
| `--wait` says COMPLETE | a marker appeared in output; the process was still running |
| `guarded_action_authorized` | a weaker signal — an affirmative, not-denied reply — never a grant |
| `local_workers` (1541) | every record the ledger ever kept; 37 were running |
| `registered: false` | this tick did not sample it; it *is* registered |
| attention `user_need` | producer-labelled automation, relabelled by an unrecognised-type fallback |
| `os_sandbox` shows no profile | goal-flight's seatbelt never applied on that path; codex was enforcing its own |
| a `--wait` comment saying "deliberately mail-free" | mail waking had landed; the comment outlived its behaviour |
| "the worker can run its own review" | the sandbox denies DNS to every child; it never could |

**Two lessons the instances agree on.**

*Prose counts.* Two of the eight were English, and one of those cost the most.
A protocol sentence promised a capability the sandbox made impossible, so
three workers escalated `BLOCKED:` against an instruction nobody could
satisfy — and their correct behaviour read as sloppiness until someone
checked. The other was a comment that outlived the behaviour it described.
Review docs for this predicate exactly as you review fields.

*Component checks do not catch it.* Most of the code rows had tests that
stayed green over the wrong measurement — the test asserted the field's value,
which was never in doubt, rather than the thing the field claimed to observe.
The prose rows had no test at all. What caught them was exercising the SYSTEM:
a real worker, a live payload, a mutation. When the fix is for this class,
verify end to end or do not claim it.

**The tell in the wild:** an error or a status that does not name its own
cause. `Operation not permitted` does not say who denied it; an `os_sandbox`
field with no profile reads as "no sandbox" while one is enforcing. When a
diagnostic cannot distinguish two very different worlds, the field is already
lying by omission and the next debugger will fix the wrong layer — twice, in
this case.

## Cadence

- On every new bug class: run the loop immediately while the anchor
  instance is fresh (the sweep brief writes itself from the diagnosis).
- At milestones: check the durable review archive for noted-but-unswept
  observations (P3s, "pre-existing" remarks, deferred edge cases) and
  promote any that describe a class.

## At scale

See `lane-fill-bug-sweep.md` for the multi-worker sweep that runs this loop at
scale (audit → harvest → consolidate → adversarial verify → grouped fixes). Its
consolidator mints `proof_basis: SPECULATIVE` class entries while its context is
loaded; this review-mining loop promotes them to proven and records the backwards
sweep once verification confirms them.
