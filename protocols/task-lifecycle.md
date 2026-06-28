# Task Lifecycle (machine-owned status + id allocation)

> **STATUS: SHIPPED / AS-BUILT (v1.1).** `goalflight_task.py`, the `--task`
> flag, `tasks.jsonl`, `.task-seq`, `tasks-data.js`, markdown snapshots, and
> mirror checks are built. Use the helper as the writer; generated docket views
> are not hand-maintained.

Goal: `docs-private/tasks.jsonl` is canonical. The docket
(`task-decomposition.md` / `tasks-done.md` / `index.html`) is derived from the
store plus the dispatch ledger, with ids assigned without grepping files.

## Canonical store

`docs-private/tasks.jsonl` is the machine-canonical **work-item** store
(append-only, house style — mirrors the dispatch ledger). **One store, not two:
a bug is an item with `kind: bug`.** Record:
`{ id, kind, title, blocked_by, links, done, tags, created_at, created_by, closed_at?, closed_by?, resolution?, audit?, prompt?, prompt_path?, acceptance?, pattern?, severity?, source?, dispatches? }`
— `kind: task | bug | decision`; `acceptance` for tasks; `pattern`/`severity`/
`source` (`review|controller|sweep`) for bugs. Decision items (`q-NNN` open
questions, `ADR-NNN` decided records) flow `decision → done` only — they have no
dispatch lifecycle — and may be referenced from any item's `blocked_by` (a task
blocked by an open `q-NNN` surfaces under Waiting / Decisions needed; see line 62
and the Decisions-needed section below). Ids `t-NNN` / `b-NNN` / `q-NNN` /
`ADR-NNN`, one allocator per family.

The HTML views are self-contained **HTML+JS** filter-views that read the data
client-side from `tasks-data.js` (a mirror of this file) — no Python page
generator; see [progress-dashboard.md](progress-dashboard.md). Optional flat
markdown snapshots (`task-decomposition.md`, `tasks-done.md`, `bug-backlog.md`,
`bugs-done.md`) are emitted for git-diffability. *"Separate bugs json" vs
"filter-view on one json" -> one json, filter-views* — so cross-kind blockers
are trivial (below).

`goal-queue-*.md` remains execution plumbing for v1.1. It is not migrated into,
or generated from, `tasks.jsonl`; worker-queued state is derived from dispatch
breadcrumbs and project-scoped ledger rows. A task or bug that needs a worker
fix carries its briefing in `prompt` / `prompt_path` and dispatches via
`--task <id>`. ADR-007's earlier generated goal-queue model is superseded by
DECISION #5 for v1.1.

## Concurrency & worktrees (one shared store, never per-worktree)

Under parallel worktrees (`execute --parallel`, `.claude/worktrees/`) the store is
NOT a per-worktree file. `docs-private/tasks.jsonl` is gitignored — and gitignored
files are per-working-directory, so a relative path gives each worktree its OWN
diverging copy, and git does NOT merge them (concurrent appends conflict;
`merge=union` is fragile and needs commits). Instead, exactly like the dispatch
ledger: ONE canonical file at the **project-root** `docs-private/` (durable,
human-visible), and `goalflight_task.py` resolves THAT absolute path (from
`project_root`, not `cwd`) and `flock`s it for every write. All worktrees + workers
mutate the one shared store through the helper, serialized. This is the load-bearing
reason the helper is the only writer (ADR-013) — a hand-edit in a worktree would hit
the wrong (worktree-local) copy and skip the lock. Cross-HOST (fleet): the store
lives on the controller host; remote workers report back via the dispatch/mail
bridge and the controller-host watcher writes (single-writer invariant).

## Id allocation (no grep)

A counter, not a file scan. `docs-private/.task-seq` holds the last integer;
`goalflight_task.py new "<title>"` takes the StateLock, increments, returns
`t-NNN`, appends the record. Mirrors `_reserve_auto_dispatch_id`
(`goalflight_dispatch.py:1253`). Give `.task-seq` its OWN co-located flock — the
StateLock guards the ledger, not this counter — and keep a single-allocator
invariant (the controller host mints ids) for fleet safety.

## Status is DERIVED from the dispatch ledger (the auto-flag)

No hand-set status. Add a `task_id` to the dispatch record (`goalflight_ledger.py
cmd_record` / `goalflight_dispatch.py _record_ledger`, via a new `--task t-NNN`
flag — neither carries task_id today). Status = join task → its ledger records by
**`(project_root, task_id)`** (filter the ledger to the current `project_root` —
it is on every record — before joining), mapping the dispatch lifecycle + markers:

| Derived status | Condition |
|---|---|
| `pending` | in registry, no dispatch, not blocked |
| `waiting` | has unresolved `blocked_by` (question, task, bug, or decision id) |
| `working` | a live, non-terminal dispatch carries this `task_id` |
| `awaiting-review` | **DONE**: latest dispatch terminal (`RESULT`/`COMPLETE`) or `goalflight_task.py done <id>`; review/accept still pending |
| `worker-failed` | dispatch dead PID / no terminal marker / `BLOCKED` / error — needs attention |
| `done-reviewed` | **DONE-REVIEWED**: `goalflight_task.py accept <id>` after a logged clean review |

`outstanding` means NOT `done-reviewed`; it includes `awaiting-review`.

**The dispatch watcher writes dispatch breadcrumbs automatically** (via the
helper, `actor: watcher`): dispatch -> `working`; terminal success marker ->
`awaiting-review`; dead PID / no terminal marker / `BLOCKED` / error ->
`worker-failed`. So an item never rots in a misleading `open`/`working` state
when the controller neglects admin. The manual closure model is two-step:
`done` marks DONE/awaiting-review, `review` records the review breadcrumb, and
`accept` moves a cleanly reviewed item to DONE-REVIEWED.

### Dispatch provenance (durable breadcrumb)

On a `--task <id>` dispatch the dispatch script also writes a durable summary onto
the item's `dispatches` array — `{ dispatch_id, agent (e.g. codex-1d9d0904), log, started_at, ended_at, state, marker, worker_pid? }`
— copied from the ledger record at start, updated on finish. The item then carries
its own worker / log / timing audit trail, shown in the views (ticket detail +
current-activity) WITHOUT joining the ledger, and it survives a `/tmp` ledger wipe
— the durable breadcrumb that resolves the review's "ephemeral ledger → false
pending" finding. Live status still derives from the ledger when present; the
breadcrumb is the durable fallback.

## Blockers + links (first-class, cross-kind)

`blocked_by` holds a list of ANY item ids — task `t-NNN`, bug `b-NNN`, or a
decision `q-NNN` — so "task blocked by a bug" / "bug blocked by a decision" needs
no join (one store). `links` holds free pointers (other items, files,
`reviews/<slug>`, `#anchors`) the generator renders inline. The **Waiting** view
is any item with an unresolved `blocked_by`, linked to its blocker.

## Capture — nothing gets lost

Items enter the ledger automatically, not by hand:

- **Review-finding stream:** each confirmed finding NOT controller-direct-fixed-
  immediately becomes a `kind:bug, source:review` item tagged with its minted
  `pattern`. Immediate fixes get a `done` bug record (not an open backlog item).
  This is the canonical capture so review bugs never vanish.
- **Controller-sourced:** a bug the controller finds that needs a worker dispatch
  becomes an item with a fix `prompt`/`prompt_path`.
- **Harvest:** `goalflight_task.py harvest` scans the newest RESUME-NOTES + other
  files for un-filed action items / bugs and expands them into draft items to
  confirm — so things mentioned only in prose aren't stranded.

## Dashboard sections

`outstanding` covers every item whose derived status is not `done-reviewed`.
Markdown snapshots use these sections: To do (pending) · In progress (working)
· Awaiting review · Failed / needs attention · Waiting. DONE-REVIEWED items move
to the done snapshots; HTML views may add decision/filter panels from the same
mirror.

## Helper contract — `goalflight_task.py` (built)

- `new "<title>" [--kind task|bug|decision] [--prompt-path F | --prompt "..."] [--json]` -> allocate id, append record, print id
- `show <id> [--prompt] [--json]` -> read one item or its prompt
- `block <id> --on <id>` / `unblock <id> [--on <id>]`
- `done <id> [--resolution R] [--force]` -> mark DONE / awaiting-review
- `review <id> --verdict clean|findings --dispatch D [--findings F] [--bug "..."]` -> append review breadcrumb and capture review bugs
- `accept <id>` -> require latest clean review, then mark DONE-REVIEWED
- `list [outstanding|awaiting-review|working|waiting|delegated|done-reviewed] [--since T] [--kind K] [--blocked-by ID] [--json]`
- `status [--json]` -> print derived status rows
- `sync` -> write `tasks-data.js` plus markdown snapshots from the store and project dispatch ledger
- `harvest [--dry-run] [--no-history] [--json]` -> draft open work from RESUME-NOTES/review sources
- check: `node scripts/check_tasks_mirror.js docs-private` validates mirror parity; `goalflight_task.py` runs it on writes.

Importable Python read API (any agent, no grep):

```python
import goalflight_task

item = goalflight_task.get("t-014")
rows = goalflight_task.list("awaiting-review", kind="task")
todo = goalflight_task.outstanding()
```

Dispatch integration: `--task t-NNN` records `task_id` in the ledger AND resolves
the chunk's prompt through the existing `_resolve_prompt_file`
(`goalflight_dispatch.py:557`) — which already accepts an inline `--prompt` or a
`--prompt-file <path>`, so `prompt` / `prompt_path` map straight onto it. The
watcher's terminal-marker handling (`protocols/worker-markers.md`) then yields
worker-finished.

## Mutation surface + audit (the helper is the only writer)

Agents NEVER hand-edit `tasks.jsonl` — every create / update / close goes through
`goalflight_task.py`. That buys three things:

1. **Audit** — each mutation appends an `audit:[]` entry `{ at, actor, action }`
   (`at` = timestamp, the mandatory floor; richer detail — `resolution`, a dispatch
   ref, the fields changed — layers on top) and sets `closed_by`/`closed_at`. The
   actor is auto-detected: a dispatched worker via `GOALFLIGHT_DISPATCH_ID` →
   `worker:<dispatch_id>`; the orchestrator → `controller`; `--by user` for a human.
   So every change carries a dated, attributed trail — "who closed this bug, when."
2. **Invariants** — validates the transition (no double-close; closing with an open
   `blocked_by` needs `--force`) and forces required fields (`close` needs
   `--resolution fixed|wontfix|duplicate|cannot-repro`).
3. **No drift** — every write re-emits `tasks-data.js` from the canonical record,
   so the browser mirror can't fall out of sync (the mirror test becomes a backstop
   for a rare hand-edit, not the primary guard).

Built mutation verbs: `new`, `block`, `unblock`, `done`, `review`, `accept`,
`harvest`, and `sync`. Built read/query verbs: `show`, `list`, `status`, plus
the importable Python read API above. There is no `edit`, `close`, `reopen`,
`tag`, `link`, or `archive` CLI surface in v1.1.

## As-built scope

The v1.1 docket/helper surface above is built. Future work belongs in
`docs-private/tasks.jsonl` and the generated snapshots; do not duplicate a
build-task list here.
