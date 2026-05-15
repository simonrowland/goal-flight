# build-corpus [--slice <name>] [--next-wave [<N>]] [--all] [--rebuild]

Trigger a RAG corpus build OR extension OR rebuild after `init` step 3.5 has already run. Same 4-pass pipeline (build → per-slice review → cross-slice consolidation → final assessment) but invoked outside init.

## Modes

| Args | Mode | Behaviour |
|------|------|-----------|
| (none) or `--next-wave` | **Next-wave extension** | Read the most recent final-assessment's "Next-wave priorities" list from RESUME-NOTES; build those slices using the same pipeline. If none recommended, prompt the user for what to add. |
| `--next-wave <N>` | **Next-wave extension, capped** | Build only the top N priority slices (default unlimited). |
| `--slice <name>` | **Single-slice rebuild or addition** | Build or rebuild one named slice (e.g., `--slice binding-spec/electrolysis-step.md`). |
| `--all` | **Full rebuild** | Re-run all slices from scratch. Use when source documents have shifted significantly and per-slice drift won't catch all of it. |
| `--rebuild` | **Rebuild existing only** | Re-run every slice currently in `docs-private/rag/` but DON'T add new ones. Lighter than `--all`. |

## Steps

### 1. Pre-flight

- Verify `docs-private/rag/` exists. If not, bail: "Run `/goal-flight init <topic>` first; corpus has not been initialized."
- Read the most recent RESUME-NOTES for the quality dashboard from the last assessment (look for the dashboard table). If absent, treat all slices as un-scored.
- Read the most recent final-assessment output (look in subagent transcripts at `~/.claude/projects/<encoded>/subagents/agent-*.jsonl` for the last `rag-final-assessment` dispatch — or if persisted to a file, read that).
- Identify the slices to build based on mode args (above).

### 2. Build pipeline (same as init step 3.5)

**Pass 1 — slice builders (parallel Claude subagents).** Use `prompts/rag-slice-builder.md`. Each builder reads its per-slice template (`templates/rag-slice-<type>.md.tpl`) + the source-material paths derived from the source-list table in `commands/init.md` step 3.5. For `--all` and `--rebuild`, the builder OVERWRITES the existing slice; for `--slice <name>` or `--next-wave` it WRITES a new slice.

**Pass 2 — per-slice reviewers (parallel Claude subagents).** Use `prompts/rag-slice-review.md`. Each reviewer scores its slice 1-5 per rubric (Factual / Complete / Voice / Dispatch-ready). Block proceeding to Pass 3 until P0 + P1 patched.

**Pass 3 — cross-slice consolidation (one Claude Opus pass).** Use `prompts/rag-cross-slice-consolidation.md`. Pass absolute paths of ALL corpus files (including ones not rebuilt this run — drift between new and old slices is the most likely failure mode). Apply fixes.

**Pass 4 — final assessment (one Claude Opus pass).** Use `prompts/rag-final-assessment.md`. Aggregates per-slice scores into a dashboard; recommends the NEXT next-wave priorities.

### 3. Update RESUME-NOTES

Bump RESUME-NOTES rev. Replace the quality dashboard with the new Pass 4 output. Update the "Next-wave priorities" section so a future `/goal-flight build-corpus` (no args) knows what to build.

### 4. Print summary

- Mode: <next-wave | single | all | rebuild>
- Slices built (or rebuilt): list with one-line word counts and self-scored quality
- Total pipeline P0/P1/P2 fixes that landed across the 4 passes
- Pipeline verdict: CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION
- Next-wave priorities (from Pass 4): list

## What this command does NOT do

- Does not modify the skill itself. If the slice schema needs to change (new slice type, new template), the user edits `templates/rag-corpus-schema.md.tpl` + the per-slice template + this command's source-list table — separate work.
- Does not delete slices. If a slice is obsolete (e.g., the binding-spec section it covered was deleted from the spec), the user removes it manually; `build-corpus` only adds/rebuilds.
- Does not dispatch `\goal` work that USES the corpus. That's `/goal-flight execute`.

## When NOT to invoke

- Right after `init` — the init's own step 3.5 already built the first-wave corpus.
- When the source documents haven't drifted and the corpus has no known gaps. Drift review at milestone (`commands/execute.md` step 4) catches incidental drift; explicit rebuild is the heavier intervention.

## Cost

Each invocation: ~N slice-builders + ~N reviewers + 1 consolidator + 1 final-assessment subagent. For N=6 slices typical of a wave extension, that's ~14 subagent dispatches. Roughly 15-30 minutes wall clock if dispatched in parallel.
