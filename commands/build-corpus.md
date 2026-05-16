# build-corpus [--slice <name>] [--next-wave [<N>]] [--all] [--rebuild]

Build, extend, or rebuild the `docs-private/rag/` corpus after `init` step 3.5 has already created it. Same 4-pass pipeline; invocation outside init.

## Modes

| Args | Mode | Behaviour |
|------|------|-----------|
| (none) or `--next-wave` | Next-wave extension — read most recent final-assessment's "Next-wave priorities" list from RESUME-NOTES; build those slices |
| `--next-wave <N>` | Cap at top N priority slices |
| `--slice <name>` | Build or rebuild one named slice (e.g. `--slice binding-spec/electrolysis-step.md`) |
| `--all` | Full rebuild — re-run all slices from scratch |
| `--rebuild` | Re-run every existing slice but don't add new ones |

## 4-pass pipeline (same shape as init step 3.5)

Schema reference: `templates/rag-corpus-schema.md.tpl` (directory shape + per-slice word budgets + `verified-at: <commit-SHA>` frontmatter convention).

**Pass 1 — slice builders (parallel Claude subagents).** For each target slice, dispatch a builder. Brief: read the slice's source materials (paths come from the source-list table in `commands/init.md` step 3.5), produce a slice file at the schema-defined path with frontmatter `verified-at: <current-HEAD-SHA>`. Builder OVERWRITES for `--all` / `--rebuild`; WRITES new for `--slice` / `--next-wave`. No prompt-template file needed — the slice schema + source-list + verification-first principle are sufficient brief.

**Pass 2 — per-slice reviewers (parallel Claude subagents).** Each reviewer scores its slice 1–5 per rubric (Factual / Complete / Voice / Dispatch-ready) and surfaces P0/P1/P2 findings. Block Pass 3 until P0+P1 patched.

**Pass 3 — cross-slice consolidation (one Claude Opus pass).** Pass absolute paths of ALL corpus files (including ones not rebuilt this run — drift between new and old slices is the most likely failure mode). Brief: identify cross-slice contradictions, deduplicate, fix frontmatter `verified-at` for slices reviewed but not rebuilt. Apply fixes.

**Pass 4 — final assessment (one Claude Opus pass).** Aggregate per-slice scores into a quality dashboard; recommend the NEXT next-wave priorities (slices that would most improve dispatch readiness if added). Output written to RESUME-NOTES quality dashboard section.

## After the pipeline

1. Bump RESUME-NOTES rev. Replace quality dashboard with the new Pass 4 output. Update "Next-wave priorities" so a future `/goal-flight build-corpus` (no args) knows what to build.
2. Print summary: mode, slices built/rebuilt with word counts + self-scored quality, total P0/P1/P2 fixes that landed, pipeline verdict (CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION), next-wave priorities.

## What this command does NOT do

- Modify the skill itself. If the slice schema needs to change, the user edits `templates/rag-corpus-schema.md.tpl` + the source-list table in `commands/init.md` step 3.5 — separate work.
- Delete slices. Obsolete slices are removed manually.
- Dispatch `/goal` work that USES the corpus. That's `/goal-flight execute`.

## When NOT to invoke

- Right after `init` — init step 3.5 already built the first-wave corpus.
- When source documents haven't drifted and the corpus has no known gaps. Milestone drift review (`commands/execute.md` step 4) catches incidental drift; explicit rebuild is the heavier intervention.

## Cost

Each invocation: ~N slice-builders + ~N reviewers + 1 consolidator + 1 final-assessment. For N=6 slices typical of a wave extension: ~14 subagent dispatches; 15–30 minutes wall clock if parallelized.
