# Worktrees And Parallel Execution Protocol

Use for `execute --parallel N` and merge orchestration.

Rules:

- Isolation is not a mode. Sequential and parallel execute use the same
  acquire path. The documented dispatch command is
  `python3 <skill-root>/scripts/goalflight_dispatch.py --agent <ready-agent> --prompt-file p.md`
  with no `--cwd`.
- Writable chunks use one exclusive repository-scoped worktree from
  `worktrees/s-N`. Existing `worktrees/<controller-label>/s-N` rings remain
  readable during migration; labels are ledger metadata, not allocator
  namespaces.
- `--at <ref>` (alias `--worktree <ref>`) prepares that worktree at a git ref,
  not an opt-in to isolation. `--cwd` is a lock, not a mint: it may name an
  existing managed worktree, the project root for explicit in-place execution,
  or resume's recorded `worker_cwd`. Other paths are refused.
- `resume <id>` skips acquire-reset so partial work is not wiped. Occupancy
  refuses a second writer; the deprecated forced override is rejected for
  ordinary dispatches.
- `worktrees_per_repo` in `~/.goal-flight/capacity.local.json` (or
  `$GOALFLIGHT_CAPACITY_CONF`) sets the per-machine repository fuse. It falls
  back to `GOALFLIGHT_WORKTREES_PER_REPO`, then default 15, with
  deprecated alias `GOALFLIGHT_WORKTREE_SEATS`. It is not an account/session
  capacity or a fan-out knob. Exhaustion reports `<N>/15 worktrees busy in
  <repo>; oldest holders: …` and never mints an unmanaged checkout. Holders
  show dispatch id, ledger controller label, state, and worker identity;
  directory labels and allocator PIDs are not ownership evidence.
  Explicit `--capacity-wait-s` also bounds seat polling. Without it, exhaustion
  refuses immediately. A refused seat admission leaves no dispatch ledger row
  or terminal notification and does not consume the task id.
- Read-only dispatches share a detached checkout keyed by commit and do not
  consume an exclusive writer worktree. Writable dispatches remain exclusive
  by default.
- Acquire prepares writable worktrees on `worktree/<dispatch-id>`; readers
  accept legacy `seat/<dispatch-id>` branches during mixed-version upgrades.
  `DISPATCH-START` / `DISPATCH-LAUNCHED` and status/ledger JSON dual-write
  `worktree_id` and legacy `worktree_seat`.
- Acquire reclaims only terminal holders whose worker PID and start identity
  are proven dead, or whose terminal record explicitly proves no worker
  launched, after acquiring both pool and path locks. It pins HEAD at
  `refs/goalflight/keep/<dispatch-id>/head`, and quarantines abandoned dirty
  product files, including untracked files, using a temporary index at
  `refs/goalflight/keep/<dispatch-id>/dirty-<UTC-time>` (also exposed through
  the legacy `goalflight/quarantine/s-<N>-<UTC-time>` branch), then checks out the new branch at `<base>` and
  runs `git clean -fd -e .goal-flight`. Unknown status, refs, or identity
  evidence retains the worktree.
- A reclaimed `BLOCKED` worker is not automatically re-seated yet. Resume
  validates the recorded seat and refuses safely; automatic resume re-seating
  is a separate fix.
- The pool holds `LOCK_EX|LOCK_NB`, inherited by the spawned worker; process
  death releases the kernel lease. The dispatch id and controller label in the
  lock file are diagnostic metadata, not allocator namespaces.
- After integration, run
  `python3 scripts/goalflight_worktree_gc.py --into main` for a dry-run report,
  or `--apply` only after review. GC emits one JSON row per candidate, pins a
  commit before removal, re-evaluates immediately before each action, and
  keeps the four-part gate: merged, clean, unowned by a live identity, and not
  current. Terminal dispatches trigger a dry-run report automatically.
- Disjoint write ownership belongs in the prompt. Acquire capacity before
  spawn. Ledger every worker PID/session. Continue independent chunks when one
  blocks. Merge completed chunks through normal review.

## Index the managed pool

Index managed worktrees. codedb and other project-keyed indexers key on
checkout root path; reuse must not create a new project key. Do not exclude
`worktrees/s-*`, `worktrees/wt-*`, or migrated nested pool paths. Historical
`.cache/worktrees` and operator-created trees remain visible for separate
triage and are never inferred safe from their names.

Conflict classification:

- mechanical: re-dispatch on current head
- semantic: mark blocked and ask user
- validation-only: rerun tests in main worktree after merge

Parallelism is bounded by `goalflight_capacity.py`, not the command-line `N`
alone.
