# Dispatch Danger Classification

Which goal-flight verbs are free reads and which spawn a fleet. Read this before
running anything that touches the dispatch surface. Summary lives in `SKILL.md`;
this is the full reference.

## READ-ONLY (safe, free — no processes, no capacity, no cost)

`goalflight_task.py status` · `list` · `next` · `show`.

These only read or derive from the task store. In particular `next` prints the
**dispatchable frontier** (what *could* be dispatched) — it does NOT dispatch it.
Safe to run anytime, as often as you like.

## ⚠ DISPATCHES WORKERS (spawns processes, leases capacity, costs money, may mutate a worktree)

- **`goalflight_task.py dispatch-frontier`** (legacy alias: `pipe`) — fans out the
  **entire** prompt-ready frontier as **one worker per item**. It is **not** a queue
  drainer (the name `pipe` misleads — it does not "flush a pipe"). It refuses without
  `--autodispatch-confirm`; `--dry-run` previews safely. Two sharp edges:
  - Workers run on a captive seat by default. Concurrent
    agents on one worktree is the collision anti-pattern — it turns a mis-dispatch into
    corrupted merges. Do not pass `--cwd` to mint a new tree.
  - Workers get the **raw task prompt** with **no mandate / 5-layer briefing** (unlike
    `execute`, which renders `prompts/dispatch-wrapper.md`). They run without the
    project's north-star frame — a correctness risk, not just hygiene.
- **`/goal-flight execute [--parallel N]`** — dispatches queued chunks with the full
  `prompts/dispatch-wrapper.md` mandate. Sequential and parallel execute
  acquire the same captive per-controller seat; isolation is not a mode.
- **Dispatcher CLI (`scripts/goalflight_dispatch.py`)** — without `--submit`, launches
  one worker in default detached mode. With `--submit`, writes a durable queue entry
  **and** runs one immediate non-blocking drain pass — drain-on-submit is the
  DEFAULT (so bare `--submit` already launches; same for `dispatch-frontier`'s
  trailing drain pass). Only `--submit --no-drain-on-submit` is queue-only, and even
  then the standing drainer below launches it within ~60s.

## The standing drainer daemon — `com.goalflight.drain`

A launchd agent at `~/Library/LaunchAgents/com.goalflight.drain.plist` (installed by
`scripts/install-drainer.sh`, template `scripts/templates/com.goalflight.drain.plist.tmpl`)
runs `goalflight_dispatch.py drain --json` every **60s**, `RunAtLoad`. Each local
tick first releases stale capacity leases, conservatively reconciles abandoned
local ledger records, then **launches anything sitting in the dispatch queue**,
with no further prompt. `goalflight_dispatch.py reconcile-abandoned --json` is a
read-only diagnostic of the same reconciliation predicate; only the drainer tick
applies it automatically.

Reconciliation is fail-closed. A record must be a stale local running dispatch
with no queue carrier; recorded status/output pointers whose referenced files
are absent or readable (and whose status, when present, matches); no live,
malformed, conflicting, or indeterminate worker, claimant, producer-group, or
persisted-descendant identity; an absent or terminal lease; and no live or
ambiguous owning-controller beacon. A terminal output marker is reconciled by
the existing marker machinery. Without one, the record becomes
`inconclusive_no_final` with an `inferred_abandonment` audit basis. Resuming such
a record creates the normal fresh tracked child and marks the old attempt
`superseded`, preserving both the abandonment evidence and observed resume
lineage.

Consequences:

- **Queuing is not free.** A `dispatch-frontier`/`--submit` that queues N items will have
  those N workers **launched by the daemon within ~60s**, even after your command has
  returned and even if you did no manual drain. There is no drain step to forget —
  draining is automatic and always-on.
- **The ledger/queue are shared across projects.** Workers from different repos interleave
  in one `$GOALFLIGHT_STATE_DIR/runs.d/` (ledger) and `dispatch-queue/` (queue). Identify a
  worker's origin project by its record's `project_root`. The same task id can appear in
  two projects at once. `drain --queue-dir <path>` scopes the pass to envelopes
  **already in that directory**; it does not restore ledger orphans into a private
  dir (that was a destination trap). To launch one queued id without touching
  others: `drain --dispatch-id <id>`.
- To pause the daemon: `launchctl unload ~/Library/LaunchAgents/com.goalflight.drain.plist`
  (reload with `launchctl load …`).

## Incident (2026-07-05) — why this page exists

An operator ran `goalflight_task.py pipe` intending to *drain* the queue. `pipe` does the
opposite — it submitted the whole prompt-ready frontier as codex dispatches, and the
standing `com.goalflight.drain` daemon then launched ~13 workers into the shared main
worktree over ~90 min. They collided with in-progress merges (two agents mutating one
worktree), all went `worker-failed`, and had to be hunted down by process `-C` flag
because the ledger recorded no origin. A single mistaken command spawned a fleet.

Guards added in response: the `--autodispatch-confirm` gate on `dispatch-frontier`, the
`dispatch-frontier` rename (with `pipe` kept as a deprecated alias), per-verb danger
labels in `--help`, and this classification.
