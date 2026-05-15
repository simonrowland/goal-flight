# RAG Corpus Final Assessment Prompt

Dispatched after per-slice reviews + cross-slice consolidation land.
Aggregates the per-slice quality scores into a corpus-level dashboard,
runs a cold-executor walkthrough, and recommends the next-wave priorities.

## Template

```
You are the final assessment subagent for the RAG corpus build. Aggregate
per-slice quality scores into a corpus-level readout; evaluate fitness-
for-purpose from a cold-executor perspective; recommend next-wave priorities.

CORPUS:
{{LIST_ABSOLUTE_PATHS_OF_ALL_CORPUS_FILES_BUILT_THIS_PASS}}

PER-SLICE REVIEW REPORTS (you have access to all of these):
{{LIST_PATHS_TO_REVIEWER_OUTPUTS_OR_PASTE_THEIR_QUALITY_SCORES}}

CROSS-SLICE CONSOLIDATION REPORT:
{{PATH_OR_PASTE}}

# Your assessment frame

## Exercise 1 — Cold-executor walkthrough

Pretend you are a fresh executor subagent dispatched for a hypothetical
`\goal` representative of the project's typical work. The controller
pastes you the relevant slices from the corpus per the dispatch-wrapper
layer-2/3/4 mapping. Walk through what each slice tells you and whether
you'd be able to start work without asking the controller for clarification.

Specifically: does the corpus answer:
- What invariants must I not break?
- Where does the code I'm modifying live?
- What pattern do I mirror?
- What verification do I run before reporting done?
- What's the relevant binding-spec contract for the intent I'm implementing?
- Are there prior decisions that affect what I should/shouldn't do?

Identify gaps (where you'd need to read external files anyway).

## Exercise 2 — Per-slice quality dashboard

Aggregate the per-slice reviewer scores into a single dashboard:

| Slice | Factual | Complete | Voice | Dispatch-ready | Notes |
|-------|---------|----------|-------|----------------|-------|
| slice1.md | 5/5 | 5/5 | 5/5 | 5/5 | One-line note |
| ... | | | | | |

Pull the scores from the per-slice reviewer outputs directly; do NOT
re-evaluate from scratch (the reviewers already did the work and the
rubric is applied consistently across reviewers). If a slice's reviewer
didn't emit scores (older corpus build), evaluate it yourself using
the rubric in `prompts/rag-slice-review.md`.

Then overall corpus rating (your own scoring, evaluative):
- **Cross-slice coherence**: x/5 — <one-line rationale>
- **Build-pipeline value**: x/5 — <one-line rationale; did the 3-pass
  pipeline catch issues hand-write would have missed? cite evidence>

## Exercise 3 — Next-wave priorities

The corpus is iterative. Given what this wave covers, recommend the
next N slices to build, ordered by priority. For each: one-line
rationale tying it to "what cold-executor gaps remain."

Consider:
- More `binding-spec/<intent>.md` slices for intents not yet covered
- More `patterns/*.md` slices for canonical idioms not yet covered
- Additional universal slices (glossary, contributor-style, error-taxonomy)

## Exercise 4 — Pipeline verdict

State at the end:
- CORPUS IS DISPATCH-READY (this wave is fit for paste-into-dispatches)
- NEEDS-MORE-ITERATION (specify what's blocking)

Plus a brief design-evaluation of the 3-pass pipeline: did
slice-builders → per-slice reviewers → cross-slice consolidator earn
its weight versus hand-writing? Cite specific catches from this run.

OUTPUT (under 800 words; tone terse, file:line refs):

## Cold-executor walkthrough
- Invariants covered: ...
- Code-location covered: ...
- Pattern covered: ...
- Verification covered: ...
- Binding-spec covered: ...
- Decisions covered: ...

Gaps requiring external Read: ...

## Per-slice quality dashboard

| Slice | Factual | Complete | Voice | Dispatch-ready | Notes |
|-------|---------|----------|-------|----------------|-------|
| ... | | | | | |

## Overall corpus rating

- Cross-slice coherence: x/5 — <rationale>
- Build-pipeline value: x/5 — <rationale>

## Next-wave priorities (top N in order)

1. <slice> — rationale (one line)
2. ...

## Pipeline verdict

CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION (specify).

## Pipeline design evaluation

<did the 3-pass earn its weight; cite catches>
```

## When to dispatch

After cross-slice consolidation lands and fixes are applied. ONE final
assessment subagent. Output is the corpus-build summary the controller
attaches to RESUME-NOTES.

## What to do with the output

- Quality dashboard goes into RESUME-NOTES as a small table.
- Next-wave priorities feed the next iteration of step 3.5 (or a
  user-triggered `/goal-flight build-corpus --next-wave`).
- Pipeline verdict: if NEEDS-MORE-ITERATION, controller patches and
  re-runs the affected steps; if DISPATCH-READY, corpus is live for
  the next `\goal` dispatch.
