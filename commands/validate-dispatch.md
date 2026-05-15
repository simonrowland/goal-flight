# validate-dispatch [<goal-slug>]

Render and print the 5-layer dispatch wrapper for a goal **without dispatching it**.
A dry-run for debugging controller-composition bugs. Cheap (no Opus subagent
spawned, no tokens billed for an executor that's about to fail on a malformed
layer).

## When to invoke

- The user explicitly typed `/goal-flight validate-dispatch [<slug>]`.
- The controller is about to dispatch a complex chunk and wants to sanity-check
  the wrapper before spending a real dispatch on it.
- A previous dispatch came back wrong; the user suspects the wrapper was
  malformed and wants to re-render.

## What the user provides

- **No args** → render the NEXT non-DONE chunk in the most recent goal-queue.
- **One arg `<goal-slug>`** → render that specific chunk (matched against the
  queue's slug field).

## Steps

1. Find the most recent `docs-private/<topic>-goal-queue-*.md` (most recent by
   date suffix, or by mtime if same date). Read it.

2. Pick the target chunk:
   - If slug provided: search for the chunk with matching slug; if not found,
     surface available slugs and ask.
   - If no slug: pick the next chunk whose STATUS is `TODO` (not `DONE` /
     `BLOCKED` / `IN-FLIGHT`).

3. Determine whether a RAG corpus exists: `[ -d <repo-root>/docs-private/rag/ ]`.

4. Render the wrapper per `prompts/dispatch-wrapper.md`:
   - **Layer 0** (if worktree mode would apply): the base-verification pre-flight
     stanza, with the expected main HEAD SHA filled in from `git rev-parse main`.
   - **Layer 1** — situational frame: last commit subject + 1-line description
     of what this dispatch's role is in the sequence.
   - **Goal text** — paste verbatim from queue (SCOPE / CHECKLIST / ACCEPTANCE
     / FORBIDDEN).
   - **Layer 2** — template-provider pointer: pick the canonical mirror per
     the slice-to-layer mapping in `prompts/dispatch-wrapper.md`. If corpus
     exists, paste the contents of one `docs-private/rag/patterns/<pattern>.md`.
     If trivial chunk: skip with annotation `(layer 2 skipped — trivial single-file)`.
   - **Layer 3** — file-path-and-line anchors: paste `file-map.md` plus relevant
     `binding-spec/<intent>.md` slices, or a flat list of paths + commit hashes
     + class names if no corpus.
   - **Layer 4** — environment caveats: `verification.md` + relevant
     `decisions.md` excerpts; or hand-composed if no corpus.
   - **Layer 5** — goal-specific self-review: pull `prompts/executor-self-review.md`
     §7 categories and specialize the patterns/nouns/line numbers to this chunk.

5. Print the assembled wrapper to the user in a single fenced block, with a
   header line counting layers used and word count: e.g.
   ```
   === DISPATCH WRAPPER for goal #N <slug> ===
   layers: 0 + 1 + (goal) + 2 + 3 + 4 + 5
   word count: ~4200
   ===
   <wrapper text>
   ```

6. Surface validation findings:
   - **Word count < 800**: warn — likely missing layers; check 2/3/4.
   - **Word count > 20 000**: warn — likely pasted whole files instead of
     curated slices; will eat executor context.
   - **Layer 3 has zero `:line` anchors**: warn — wrapper is vague; executor
     will have to grep.
   - **Layer 5 contains the literal string "P0/P1/P2"** without specialization
     to this chunk's grep patterns: warn — likely a raw paste rather than
     specialization.

7. Do NOT dispatch. The user reviews; if happy, they run `/goal-flight execute`
   (which will re-render fresh) or copy the wrapper into an Agent call manually.

## See also

- `prompts/dispatch-wrapper.md` — the canonical layer spec.
- `commands/execute.md` step 2a — where the wrapper is normally rendered.
- `commands/validate-queue.md` — validates the queue structure itself; this
  command assumes a valid queue.
