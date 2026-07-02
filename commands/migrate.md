---
description: "Preview and import existing markdown task lists into draft task-store items."
---

# migrate

Use when an existing project already has markdown task, bug, or issue lists and
the controller needs a guided, non-destructive entry point into the task store.

## Flow

1. Pick one or more project-root-relative markdown globs:
   `python3 goalflight_task.py migrate --source "docs/**/*.md"`.
2. Read the preview. It runs harvest in dry-run JSON mode and prints candidates
   grouped by source file. `--dry-run --json` returns the same harvest-shaped
   JSON preview for tooling. No source file is deleted, moved, or edited.
3. Apply only when the preview is right:
   `python3 goalflight_task.py migrate --source "docs/**/*.md" --apply`.
4. Curate the created drafts:
   `python3 goalflight_task.py list --tag harvest`, then `lane`, `done`,
   `review`, and `accept` as appropriate.

Options:

- `--source <glob>` may be repeated. Globs are project-root-relative.
- `--kind task|bug|decision` sets the default kind for source-list items.
- `--lane <lane>` stamps created source-list drafts; default `deferred`.
- `--source-limit N` caps files consumed per glob before reporting dropped
  matches; raise it for intentionally large imports.
- `--no-history` passes through to harvest and skips RESUME-NOTES history backfill.
- `--all-bullets` includes bullets outside task/backlog/action/TODO/open/next
  sections; default preview filters those prose sections out.
- `--no-implicit-resume` passes through to harvest. `--source` imports are
  source-only by default and do not create implicit RESUME-NOTES candidates.
- `--dry-run --json` prints the harvest-shaped JSON preview without applying.
- `--apply` creates draft items after printing the same preview.

The wrapper composes `harvest`; it does not duplicate harvest parsing or move
the source markdown.
