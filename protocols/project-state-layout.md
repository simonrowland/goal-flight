# Project State Layout (canonical living-document contract)

Goal: every goal-flight project in `~/Repos` keeps its durable state in the SAME
files and folders (Rails-style — convention over configuration), so an agent or
human opening any project knows where the north star, tasks, architecture, and
bug backlog live — and re-grounds in them every session instead of letting them
slip away between handoffs.

## Two levers: convention + an explicit list

1. **The structure IS the primary pin.** Fixed paths make state discoverable by
   convention — present even when empty (a stub with a one-line "what goes
   here"), so nothing is invented ad-hoc.
2. **AGENTS.md lists the files anyway**, because convention alone is forgettable.

## Durable vs volatile — the living pin + the write-once log

Durable state lives at stable paths and must NOT depend on the lossy
handoff/summary to survive.

- **Living pin = the newest `RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md`** — current
  goals, where-we-are (HEAD, active increment, in-flight dispatch ids), the next
  action, key pins, do-not-re-litigate. This is the existing convention the
  runtime already resolves through the session-status and resume helpers; we
  reuse it, not a new filename. Loop / watchdog prompts reference the *newest*
  RESUME-NOTES and name no specific tasks, so they can't go stale
  (`templates/goalflight-loop-prompt.md`).
- **`history.md`** — an *additive* write-once project log: one compact entry per
   compaction/rotation (`## <date> · <HEAD>` + shipped/focus/next), never edited.
  One chronological file that's easy to review. Append-only — never read it back;
  under context pressure append a stub and expand later. (Complements RESUME-NOTES;
  does not replace it.)

## Canonical layout (`docs-private/` store + repo-root `dashboard/`)

`docs-private/` gitignore status is **per-repo** (ADR-006): public repos ignore
it; private repos may track it — the layout works either way. Flat, minimal:

```
docs-private/
  RESUME-NOTES-<date>.md       # LIVING pin (newest) = loop-prompt target
  history.md                  # write-once project history (append-only, additive)
  NORTH-STAR.md               # invariant goal + non-goals
  SRS.md                      # software requirements
  ARCHITECTURE.md             # current / as-built
  ARCHITECTURE-increments.md  # staged plan: done -> next -> target
  TEST-PLAN.md                # how correctness is verified
  tasks.jsonl                 # MACHINE-canonical task store (status from the ledger)
  task-decomposition.md       # tasks TO DO  (GENERATED view; anchor per id)
  tasks-done.md               # tasks DONE   (GENERATED view)
  bug-backlog.md              # bugs OPEN    (one anchored block per bug id)
  bug-patterns.md             # bug CLASSES  (-> shared sweep corpus)
  bugs-done.md                # bugs FIXED
  questions-for-user.md       # SOURCE: open decisions (blocked task ids) + ADRs
  reviews/                    # durable review verdicts (folder; link opens it)
  research/                   # investigations / sweep findings (folder)
  # runtime/infra — machine-owned, pinned by reference, NEVER relocated:
  dispatch/  mail/  prompts/  rag/  goal-queue-*.md  *.lock

dashboard/
  gf.js                       # shared static renderer
  tasks-data.js               # generated browser mirror of docs-private/tasks.jsonl
  index.html                  # static dashboard
  tickets.html  ticket.html   # ticket list/detail views
  current-activity.html       # active work view
  questions-for-user.html     # static decision view
  burndown.html               # burndown trend view
```

`tasks.jsonl` is canonical (ADR-002/004): `task-decomposition.md` /
`tasks-done.md` / `bug-backlog.md` / `bugs-done.md` are GENERATED snapshots,
while the static client-side views in repo-root `dashboard/` render over
`dashboard/tasks-data.js`. Add/edit tasks via `goalflight_task.py` (don't hand-edit
generated snapshots). A task lives in exactly one derived state; `done` marks
DONE/awaiting-review and `accept` moves it to DONE-REVIEWED. Bugs follow the
same split. Status is machine-owned — see [task-lifecycle.md](task-lifecycle.md).

## Questions & decisions (questions-for-user.md)

Open decision prose that blocks work lives here, each listing the task chunk ids
it blocks (`Blocks: t-014, t-016`). When answered, the decision becomes a
greppable `### ADR-NNN —` heading in the same file's "Decided" section — the
decision log lives in one place agents can grep. Decision items also live in
`tasks.jsonl`; `dashboard/questions-for-user.html` renders the open decision
items client-side from `dashboard/tasks-data.js`, and the dashboard surfaces
them with links to the tasks they hold up.

## Human views — dashboard/*.html

Browser-facing HTML/JS lives in repo-root `dashboard/`; private Markdown and the
canonical JSONL store stay under `docs-private/`. CSS is inlined so each page
renders standalone in a browser. Markdown sources stay chat-console previewable;
JS-rendered HTML is browser UI. Minimal text: no editorial chrome, no copyright,
no live HEAD (provenance lives in the RESUME-NOTES pin). See
[progress-dashboard.md](progress-dashboard.md) for rendering rules and
[task-lifecycle.md](task-lifecycle.md) for status sections.

## AGENTS.md pin (per project)

AGENTS.md carries a block that (a) tells the agent to read the newest
`RESUME-NOTES-*.md` first and (b) lists the canonical files. See
`templates/project-agents.md` for the exact block.

## Cross-project layer (`~/Repos/.agent-context/`)

- `sweep-corpus/` — the bug-SHAPE database (SC-xx predicates, mature). Project
  `bug-patterns.md` links UP; a class caught anywhere seeds sweeps everywhere.
- Optional portfolio roll-up indexing each project's pin / north-star / open
  counts (federated — state stays per-project).

Bug INSTANCES stay project-local; bug SHAPES are shared.

## Migration & sync/view refresh

- `init` scaffolds the layout from `templates/state-skeleton/` (stubs present-even-
  if-empty; the RESUME-NOTES pin comes from `templates/resume-notes.md`); `doctor`
  checks the tree + the AGENTS.md pin; `goalflight_task.py sync` emits
  `dashboard/tasks-data.js` plus markdown snapshots under `docs-private/`.
  Static HTML views in `dashboard/` render client-side from the mirror;
  `docs-private/questions-for-user.md` remains the human source for decision
  prose while `dashboard/questions-for-user.html` renders open decision items
  from `dashboard/tasks-data.js`. The scaffold keeps private state under
  `docs-private/` and browser-facing assets under `dashboard/` so each path can
  follow its own gitignore policy.
- Migrating existing projects is non-destructive and **branches on
   `git check-ignore docs-private/`** (some private repos track it instead of ignoring — both are fine):
   dry-run the scaffold, create canonical paths if absent (never clobber), and
   leave ad-hoc file-name mapping as an operator-assisted manual step (e.g. copy
   a prior `*-northstar.md` into `NORTH-STAR.md` only after reviewing the source).
   Runtime queue state (`dispatch/`, `*.lock`) is pinned by reference, NEVER
   relocated. Per-repo mapping: operator-local plan.
- The migration helper writes a per-repo backup before apply, rewrites only
   managed state-file pointers in `AGENTS.md` through temp+rename, and keeps
   `history.md` additive/write-once. The retired handoff file is not state; the
   living pin is always the newest `docs-private/RESUME-NOTES-*.md`.

See also [progress-dashboard.md](progress-dashboard.md) (dashboard rendering) and
[task-lifecycle.md](task-lifecycle.md) (machine-owned task status + id allocation).
