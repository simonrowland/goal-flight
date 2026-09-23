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

- **`/goal-flight execute [--parallel N]`** — dispatches queued chunks with the full
  `prompts/dispatch-wrapper.md` mandate. `--parallel N≥2` isolates each worker in a
  leased pooled seat (`scripts/goalflight_acp_run.py --worktree create`); sequential
  dispatch stays in the project root.
- **Dispatcher CLI (`scripts/goalflight_dispatch.py`)** — launches one worker
  immediately in default detached mode. It waits for the lane's capacity window;
  if capacity remains unavailable it writes `blocked_capacity`, prints
  `DISPATCH-BLOCKED`, exits nonzero, and creates no queue entry. Dispatch frontier
  items individually; there is no bulk fan-out command.

## The backlog drainer daemon — `com.goalflight.drain`

A launchd agent at `~/Library/LaunchAgents/com.goalflight.drain.plist` (installed by
`scripts/install-drainer.sh`, template `scripts/templates/com.goalflight.drain.plist.tmpl`)
runs `goalflight_dispatch.py drain --json` every **60s**, `RunAtLoad`. Each local
tick first releases stale capacity leases, conservatively reconciles abandoned
local ledger records, then launches entries created before dispatch became
direct-only. It is a graceful backlog consumer, not a producer.
`goalflight_dispatch.py reconcile-abandoned --json` is a
read-only diagnostic of the same reconciliation predicate; only the drainer tick
applies it automatically.

To retire a dispatch that must never run, use
`goalflight_dispatch.py withdraw <dispatch_id> --reason TEXT --controller-label <owner>`.
It never kills a live worker: steer that worker to stop and wait for exit first.
Withdrawal commits journal terminal state `withdrawn`, projects the terminal ledger row, then
moves the queue carrier to `dispatch-queue-withdrawn/<id>.<utc-stamp>.json`.
Do not remove a carrier by hand: drain can restore it from a nonterminal ledger row.
The human owner can override controller ownership with `--operator`. If the
record's project moved, pass `--project-root PATH`; that journal must contain the
dispatch attempt. `--dry-run` lists planned record fields without writing;
`--json` returns machine-readable results. Repeating a completed withdrawal is
a no-op. A spawn intent without a worker PID must age past the claim-stale window
before withdrawal is allowed.
Use `--superseded-by <replacement-id>` to record `superseded` instead. Both
outcomes settle the journal as `TERMINAL`, release the seat, and do not hold
linked tasks against a fresh dispatch. The replacement keeps its own task claim.

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

- **The legacy backlog still launches.** Existing queue entries are consumed by the
  daemon within its normal cadence. New dispatches never enter that queue: they
  launch immediately or refuse visibly after the capacity wait.
- **The ledger/queue are shared across projects.** Workers from different repos interleave
  in one `$GOALFLIGHT_STATE_DIR/runs.d/` (ledger) and `dispatch-queue/` (queue). Identify a
  worker's origin project by its record's `project_root`. The same task id can appear in
  two projects at once. `drain --queue-dir <path>` scopes the pass to envelopes
  **already in that directory**; it does not restore ledger orphans into a private
  dir (that was a destination trap). To launch one queued id without touching
  others: `drain --dispatch-id <id>`.
- To pause the daemon: `launchctl unload ~/Library/LaunchAgents/com.goalflight.drain.plist`
  (reload with `launchctl load …`).
