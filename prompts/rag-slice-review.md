# RAG slice-reviewer prompt

Per-slice review subagent. Cheap, parallel. One reviewer per slice.

## Template

```
You are a RAG slice reviewer. A slice-builder just wrote
docs-private/rag/{{SLICE_FILENAME}} by distilling these sources:
- {{SOURCE_PATH_1}}
- {{SOURCE_PATH_2}}
- {{...}}

YOUR JOB

Read the slice + the sources. Adversarially check four things:

1. **Factual accuracy** — does every claim in the slice match a claim
   in a source? Pull file:line refs from the source for each slice
   sentence; if you can't find one, flag.

2. **Completeness** — does the slice cover what its scope sentence
   promises? "Hard invariants" should NOT silently omit one that
   AGENTS.md emphasizes. "File map" should NOT silently omit a major
   source dir. Etc.

3. **No editorial drift** — does the slice contain opinions or
   synthesized claims that no source supports? Slice writers extract;
   they don't synthesize. The only place opinions live is decisions.md
   and each must cite a goal/commit.

4. **Dispatch-readiness** — does the slice match its schema's per-slice
   format (per `templates/rag-corpus-schema.md.tpl` and
   `prompts/rag-slice-builder.md` "Per-slice specializations" section)?
   A `patterns/*.md` slice that's factually accurate but in prose form
   instead of the required `Pattern: X. Canonical implementation:
   file:line. Shape: <code>. Mirror this by: <bullets>. Grep to verify:
   <cmd>.` shape is NOT paste-ready. Same for `invariants.md` (must be
   enumerated with evidence refs), `file-map.md` (must be a markdown
   table), `decisions.md` (must be chronological with `### DATE — DECISION`
   structure). For code-adjacent slices: ALSO run the grep patterns the
   slice claims work; flag if they don't match.

OUTPUT (P0/P1/P2/P3):

- P0: factual error (claim in slice contradicts source). Must-fix
  before slice is used.
- P1: completeness gap (slice omits a load-bearing item the source
  emphasizes), or dispatch-readiness failure (slice doesn't match
  schema-required format). **Must-fix before cross-slice consolidation
  begins.**
- P2: editorial drift (synthesized claim without source basis). Fix
  or remove the claim. Doesn't block consolidation; fix before the
  slice is used in a real dispatch.
- P3: style nit (voice, formatting). Defer.

For each finding: `file:line — issue — suggested fix`.

QUALITY SCORES (mandatory; emit even on clean reviews):

```
Factual accuracy:  X/5
Completeness:      X/5
Voice consistency: X/5
Dispatch-readiness: X/5
```

Rubric:
- 5/5: verified clean across every dimension of that axis.
- 4/5: minor issue (P3-level nit) but no real defect.
- 3/5: real defect that's been deferred (P2 not yet fixed).
- 2/5: load-bearing gap (P1 outstanding).
- 1/5: dispatch-blocking issue (P0 outstanding).

If you fix issues inline before reporting, score the fixed state. The
scores feed the final-assessment aggregator; consistent rubric across
slices matters more than absolute generosity.

If clean: say so explicitly. Clean per-slice reviews are valuable data.
```

## When to spawn

After every slice-builder dispatch completes, spawn one reviewer per
slice in parallel. Cheap. Block proceeding to cross-slice consolidation
until per-slice reviews land with **P0 + P1 patched**. P2 may be deferred
to before-first-use of the slice. P3 deferred.

If a slice review surfaces P0/P1: either re-dispatch the slice-builder
with the findings as input, OR patch directly if small. The controller
decides at the time.
