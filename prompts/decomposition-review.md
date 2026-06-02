You are reviewing a proposed decomposition of a plan into `/goal` chunks.
The orchestrator wants to know if this decomposition is ready for a long
unattended execution run.

CONTEXT

PLAN (source of truth):
<paste plan text or file path with key sections>

DECOMPOSITION (drafted by another agent):
<paste numbered chunks with their SCOPE/CHECKLIST/ACCEPTANCE/FORBIDDEN>

INDEPENDENCE TAGS (drafted by another agent):
<paste [parallel-safe:GROUP] tags or note "none">

YOUR JOB

Adversarially challenge the decomposition. The risk you're guarding against
is a 12-hour unattended run that stalls because of decomposition mistakes.

Specifically:

1. **Missing chunks** — are there steps in the plan that don't appear in
   any chunk? Or required setup (env, dep installs, schema migrations,
   feature flags, fixtures, secrets) that should be a chunk but isn't?

2. **Wrong ordering** — does any chunk depend on a later chunk's output?
   Are dependencies declared explicitly in REFERENCE sections?

3. **Chunk size** — too large (multiple commits' worth of work in one)?
   Too small (commits that don't make sense in isolation)? The target is
   one self-contained commit per chunk.

4. **Hidden dependencies** — did the independence analyst's
   `[parallel-safe]` tagging miss a shared file, shared test, shared
   fixture, or implicit ordering constraint?

5. **Acceptance criteria** — are any chunks' ACCEPTANCE clauses too loose
   (no test, no measurable outcome — just "implement it")? Or too strict
   (impossible to satisfy without out-of-scope work)?

6. **FORBIDDEN gaps** — are there hard invariants from AGENTS.md or the
   plan that should appear in FORBIDDEN clauses but don't? Specifically
   look for: silent fallbacks, mutation of canonical state from outside
   the canonical commit path, drive-by refactors.

7. **Risk concentration** — is any single chunk doing too much novel work
   (likely to need user input mid-execute)? Should it be split or have
   anticipatory questions logged?

8. **Verifiability** — for each chunk's ACCEPTANCE: is there a way the
   orchestrator can mechanically verify pass/fail without running the
   user's brain? (Test command, grep, file existence, etc.)

OUTPUT

```
## Decomposition findings

### Must-fix before execute (P0)
- chunk N: description — what to change

### Should-fix (P1)
- chunk N: description

### Nice-to-have (P2)
- chunk N: description

## Recommended revisions
- Concrete suggestion 1 (e.g., "split chunk 4 into 4a and 4b at the
  boundary between schema migration and data backfill")
- Concrete suggestion 2

## Independence audit
- (Confirm or challenge the [parallel-safe] tags. List any chunk you'd
  add/remove from a parallel-safe group.)

## Confidence
high / medium / low — note where you'd want a second opinion.
```

If the decomposition is solid, say so explicitly. The orchestrator will
proceed with execute on a clean review.

Tone: terse, technical. No emoji. No filler.
