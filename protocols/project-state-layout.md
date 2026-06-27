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
  runtime already reads (`goalflight_session_status.py`, `state-handoff.md`); we
  reuse it, not a new filename. Loop / watchdog prompts reference the *newest*
  RESUME-NOTES and name no specific tasks, so they can't go stale
  (`templates/goalflight-loop-prompt.md`).
- **`history.md`** — an *additive* write-once project log: one compact entry per
  compaction/rotation (`## <date> · <HEAD>` + shipped/focus/next), never edited.
  One chronological file that's easy to review. Append-only — never read it back;
  under context pressure append a stub and expand later. (Complements RESUME-NOTES;
  does not replace it.)

## Canonical layout (under `docs-private/`)

`docs-private/` gitignore status is **per-repo** (ADR-006): public repos ignore
it; private repos may track it — the layout works either way. Flat, minimal:

```
docs-private/
  index.html                  # human dashboard — progress (generated)
  questions-for-user.html     # human decision view — the questions list (generated)
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
```

`tasks.jsonl` is canonical (ADR-002/004): `task-decomposition.md` / `tasks-done.md`
/ `index.html` are GENERATED — add/edit tasks via `goalflight_task.py` (don't
hand-edit the generated views). A task lives in exactly one state; `done` moves it
to the tasks-done view. Bugs follow the same split. Status is machine-owned — see
[task-lifecycle.md](task-lifecycle.md).

## Questions & decisions (questions-for-user.md)

Open decisions that block work live here, each listing the task chunk ids it
blocks (`Blocks: t-014, t-016`). When answered, the decision becomes a greppable
`### ADR-NNN —` heading in the same file's "Decided" section — the decision log
lives in one place agents can grep. Renders to `questions-for-user.html` (a
first-class human decision view); the dashboard surfaces the OPEN ones with links
to the tasks they hold up.

## Human views — index.html + questions-for-user.html

The only HTML, in `docs-private/` root (no `dashboard/` subfolder, no separate
stylesheet — CSS inlined so each page renders standalone in a browser and in the
chat-console preview). Minimal text: no editorial chrome, no copyright, no live
HEAD (provenance lives in the RESUME-NOTES pin); footer = generated date. See
[progress-dashboard.md](progress-dashboard.md) for sections + rendering rules.

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

## Migration & generator (follow-on)

- `init` scaffolds the layout from `templates/state-skeleton/` (stubs present-even-
  if-empty; the RESUME-NOTES pin comes from `templates/resume-notes.md`); `doctor`
  checks the tree + the AGENTS.md pin; the generator emits `index.html` /
  `questions-for-user.html` from `tasks.jsonl` / `questions-for-user.md`. The
  scaffold dir is named off `docs-private` on purpose so the repo's ignore-
  everywhere rule (where present) stays intact.
- Migrating existing projects is non-destructive and **branches on
  `git check-ignore docs-private/`** (private repos like rpp-kb track it — fine):
  dry-run the mapping, create canonical paths if absent (never clobber), move
  ad-hoc files in (e.g. a stray `*-northstar.md` -> `NORTH-STAR.md`). Runtime
  queue state (`dispatch/`, `*.lock`) is pinned by reference, NEVER relocated.
  Per-repo mapping: operator-local plan.

See also [progress-dashboard.md](progress-dashboard.md) (dashboard rendering) and
[task-lifecycle.md](task-lifecycle.md) (machine-owned task status + id allocation).
