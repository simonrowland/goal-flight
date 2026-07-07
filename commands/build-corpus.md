---
description: "Build or refresh the private dispatch-context corpus."
---

# build-corpus [--slice <name>] [--next-wave [<N>]] [--all] [--rebuild]

Build, extend, or rebuild the `docs-private/rag/` corpus after `init` step 9 has
created `docs-private/rag/ORIENTATION.md`. Same 4-pass pipeline; invocation
outside init.

## Modes

| Args | Mode | Behaviour |
|------|------|-----------|
| (none) or `--next-wave` | Next-wave extension — read most recent final-assessment's "Next-wave priorities" list from RESUME-NOTES; build those slices |
| `--next-wave <N>` | Cap at top N priority slices |
| `--slice <name>` | Build or rebuild one named slice (e.g. `--slice binding-spec/electrolysis-step.md`) |
| `--all` | Full rebuild — re-run all slices from scratch |
| `--rebuild` | Re-run every existing slice but don't add new ones |

## 4-pass pipeline

Schema reference: `templates/rag-corpus-schema.md.tpl` (directory shape + per-slice word budgets + `verified-at: <commit-SHA>` frontmatter convention). Orientation reference: `templates/worker-orientation.md` writes the canonical `docs-private/rag/ORIENTATION.md` brief.

**Pass 0 — orientation refresh (orchestrator inline).** Rebuild
`docs-private/rag/ORIENTATION.md` from `templates/worker-orientation.md` and the
project's north-star, architecture, SRS, and project-map docs before dispatching
slice builders. Keep it pointer-heavy and scoped: orientation only, never scope
expansion.

**Pass 1 — slice builders (parallel workers via the orchestrator's host `delegate` mechanism).** For each target slice, dispatch a builder through a ready adapter. Brief: read the slice's source materials (the orchestrator names the project docs each slice distills — north star, architecture, SRS, protocol/spec files — when it picks the slice), produce a slice file at the schema-defined path with frontmatter `verified-at: <current-HEAD-SHA>`. Builder OVERWRITES for `--all` / `--rebuild`; WRITES new for `--slice` / `--next-wave`. No prompt-template file needed — the slice schema + source-list + verification-first principle are sufficient brief.

**Pass 2 — per-slice reviewers (parallel workers via the orchestrator's host `delegate` mechanism).** Each reviewer scores its slice 1–5 per rubric (Factual / Complete / Voice / Dispatch-ready) and surfaces P0/P1/P2 findings. Block Pass 3+4 until P0+P1 patched.

**Pass 3+4 — parallel high-capability review workers, two concurrent dispatches when workload permits.** Pass 3 and Pass 4 share inputs (Pass-1 slices + Pass-2 scores) and don't depend on each other's outputs except for the final DISPATCH-READY verdict, so they run concurrently by default. Model choice and session-limit headroom are adapter-specific: the current Claude wrapper may route these to Opus and account for Claude session limits; other hosts use their adapter's model-tier and capacity policy.

- **Pass 3 — cross-slice consolidation.** Pass absolute paths of ALL corpus files (including ones not rebuilt this run — drift between new and old slices is the most likely failure mode). Brief: identify cross-slice contradictions, deduplicate, fix frontmatter `verified-at` for slices reviewed but not rebuilt. Apply fixes. Returns: contradiction list + deduplicated slice content.
- **Pass 4 — final assessment.** Aggregate per-slice Pass-2 scores into a quality dashboard; recommend the NEXT next-wave priorities (slices that would most improve dispatch readiness if added). Returns: dashboard + prioritized rebuild queue.
- **Final composition** (orchestrator inline, once both return): write dashboard to RESUME-NOTES quality-dashboard section; persist next-wave priorities for future `/goal-flight build-corpus` invocations; issue the CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION verdict — DISPATCH-READY requires Pass 3's contradiction list is empty AND Pass 4's mean score ≥ threshold (configurable, default 4.0).

## After the pipeline

1. Bump RESUME-NOTES rev. Replace quality dashboard with the new Pass 4 output. Update "Next-wave priorities" so a future `/goal-flight build-corpus` (no args) knows what to build.
2. Print summary: mode, slices built/rebuilt with word counts + self-scored quality, total P0/P1/P2 fixes that landed, pipeline verdict (CORPUS IS DISPATCH-READY / NEEDS-MORE-ITERATION), next-wave priorities.

## What this command does NOT do

- Modify the skill itself. If the slice schema needs to change, the user edits `templates/rag-corpus-schema.md.tpl` — separate work.
- Delete slices. Obsolete slices are removed manually.
- Dispatch `/goal` work that USES the corpus. That's `/goal-flight execute`.

## When NOT to invoke

- Right after `init` when only orientation is needed — init step 9 already wrote
  `docs-private/rag/ORIENTATION.md`.
- When source documents haven't drifted and the corpus has no known gaps. Milestone drift review (`commands/execute.md` step 4) catches incidental drift; explicit rebuild is the heavier intervention.

## Cost

Each invocation: ~N slice-builders + ~N reviewers + 1 consolidator + 1 final-assessment. For N=6 slices typical of a wave extension: ~14 subagent dispatches; 15–30 minutes wall clock if parallelized.
