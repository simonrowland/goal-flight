# RAG cross-slice consolidation prompt

Single subagent (codex preferred — 1M context helps when holding all slices
at once). Runs AFTER per-slice reviews are green. Finds contradictions and
drift across the corpus that per-slice review can't see.

## Template

```
You are the RAG cross-slice consolidation reviewer. Per-slice reviewers
have validated each individual slice against its sources. Your job is
finding issues that only surface when slices are compared against each
other.

CORPUS: <ABSOLUTE_REPO_ROOT>/docs-private/rag/ — read every file in the tree. Caller (see `commands/init.md` step 3.5 invocation pattern) MUST substitute the absolute repo-root path before dispatching, since codex `exec`'s cwd may not match the repo root and direct prompt reuse without substitution will fail silently on a wrong-cwd tree-walk.

YOUR JOB

Adversarially scan for:

1. **Contradiction** — slice A claims X; slice B claims not-X. Either
   the slices disagree on a factual matter, OR they're describing
   different things and one slice is misnaming it. File:line refs for
   both sides.

2. **Voice drift** — one slice is terse + file-line-ref-heavy; another
   is prose-y or hedging. Normalize toward the terse end (the spec
   style).

3. **Decision-log gaps** — patterns.md says "X is the canonical pattern"
   but decisions.md doesn't record when/why X became canonical. Adding
   to decisions.md closes the gap.

4. **Stale references** — slice references a file path or commit hash
   that no longer exists (run `ls` and `git cat-file -e <hash>` to
   verify). Common after rebases.

5. **Forbidden inline duplication** — same content pasted into two
   slices verbatim. If it's needed in two places, one slice should
   reference the other (a rare exception to the self-contained rule).

OUTPUT (P0/P1/P2/P3):

- P0: contradiction. Must-fix; corpus is unusable until resolved.
- P1: stale reference, decision-log gap. Fix before next dispatch
  uses the slice.
- P2: voice drift, minor duplication. Patch.
- P3: stylistic preferences. Defer.

For each finding: which slices are involved, what the issue is, what
fix to apply.

If clean: say so. The corpus is now ready for dispatch use.
```

## Which model — Claude Opus default, codex as fallback

Each slice is small (<1500 words) but the corpus aggregate is 5-12 KB across
~15 files. The consolidation pass benefits from holding every slice in
context simultaneously. Both Claude Opus 1M-context and codex 1M-context
handle this cleanly.

**Default**: Claude Opus subagent (Agent tool, `model: "opus"`). Reliability
beats codex (codex stalls ~2/5 in long sessions per documented experience).
Plain-text cross-reference finding doesn't lean on codex's numeric-reasoning
edge.

**Codex fallback**: use codex only when you specifically want a model-
diversity second opinion (rare for this pass — the corpus is a single
controller's output, not contested input), or when Claude Opus is
unavailable. Invocation pattern at `commands/init.md` step 3.5 includes
absolute paths so codex's cwd isn't load-bearing.
