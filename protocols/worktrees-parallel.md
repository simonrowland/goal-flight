# Worktrees And Parallel Execution Protocol

Use for `execute --parallel N` and merge orchestration.

Rules:

- Isolation is not a mode. Sequential and parallel execute use the same
  acquire path. The documented dispatch command is
  `python3 <skill-root>/scripts/goalflight_dispatch.py --agent <ready-agent> --prompt-file p.md`
  with no `--cwd`. If a controller can copy that example and get a new
  unmanaged checkout root, the change is not done.
- one leased captive seat per concurrent chunk, under
  `worktrees/<controller-label>/s-N`. Two controllers on one repo get
  separate rings so they do not steal each other's warm index and notes.
- `--at <ref>` (alias `--worktree <ref>`) means "prepare this seat at this
  git ref", not "opt into the pool". Default ref is `origin/main` when it
  exists, else `HEAD`.
- `--cwd` is a lock, not a mint. Allowed only when it names (1) an existing
  seat in this controller's ring, (2) the project root for explicit in-place
  (`--in-place` or `--cwd` exactly the git toplevel), or (3) resume's
  recorded `worker_cwd`. Anything else — `.cache/worktrees/…`, `/tmp/…`, a
  newly mkdir'd dir, another controller's seat — is refused. Never create
  that path. Never `git worktree add` as a fallback.
- `resume <id>` does not require `--cwd`. It already knows `worker_cwd`.
  Resume skips acquire-reset so partial work is not wiped. Occupancy still
  refuses a second writer unless `--occupied-worktree-forced`.
- seats grow to that controller's concurrent high-water mark of live
  nonterminal dispatches and never shrink automatically. `s-N` is
  lazy-created only when acquire needs it and `N` is still ≤ HWM.
- `GOALFLIGHT_WORKTREE_SEATS` is a per-repository fuse (default 24), not a
  fan-out knob and not a per-controller worker cap. Never lower the fuse to
  shape concurrency. Exhaustion waits/refuses and names occupants; it does
  not mint.
- after a worker branch is merged into the integration branch, reclaim
  ad-hoc (non-pool) worktrees with
  `python3 scripts/goalflight_worktree_gc.py --into main` (report) or
  `--apply` (remove). Registered seats — captive
  `<repo>/worktrees/<label>/s-N` and legacy `<repo>/worktrees/wt-N` with a
  matching seat lock — are never litter. A worktree merely *named* `s-N` or
  `wt-N` is reclaimable. If registration cannot be determined, GC retains.
  Historical `.cache/worktrees/…` trees are not converted into seats.
- the configured fuse is a hard ceiling: a dispatch fails with every held
  seat and occupant dispatch id named when no kernel lock is available; there
  is no force-new escape hatch
- seats start from committed `HEAD` (or `--at <ref>`); uncommitted
  controller-root edits are not visible inside dispatch worktrees
- acquire holds `LOCK_EX|LOCK_NB`, inherited by the spawned worker; process
  death releases the seat without a registry, timeout, or reaper
- the dispatch id written inside the lock file is diagnostic only; availability
  is decided exclusively by the live kernel lock, never by recorded metadata
- acquire prepares each seat on a named branch `seat/<dispatch-id>` (never a
  detached HEAD). Worker commits are therefore reachable after seat reuse.
  `DISPATCH-START` / `DISPATCH-LAUNCHED` report `worktree_path`,
  `worktree_seat`, and `worktree_branch`.
- acquire quarantines abandoned dirty *product* files to
  `goalflight/quarantine/s-<N>-<UTC-time>`, then checks out
  `seat/<dispatch-id>` at `<base>` and `git clean -fd -e .goal-flight`.
  Never `git clean -fdx`. The reserved gitignored namespace
  `.goal-flight/seat/` (worker notes / tool memory) is preserved;
  porcelain-clean checks ignore it. Uncommitted `RESULT.md` / `PLAN.md`
  from the previous occupant still quarantine. Next *new* task does not
  inherit them. Resume is how that work continues.
- acquire refuses to reset a seat when HEAD is detached-and-ahead of other
  refs, when the branch it would force-move uniquely holds commits, or when
  cleanliness cannot be determined; the reason names exactly what would be
  lost. UNKNOWN retains. Release only closes the lease
- the pool is intentionally persistent; completed/failed seats are reused,
  not removed. Captive seats are never automatically deleted.
- disjoint write ownership in the prompt
- shared-tree full-suite code writers are serialized; run `pytest tests/` (or
  equivalent whole-repo suites) concurrently only when each worker is isolated in
  its own worktree
- acquire capacity before each worker spawn
- ledger every worker PID/session
- continue independent chunks when one chunk blocks
- merge completed chunks back through normal git review
- doctor reports each pooled seat's live kernel-lock state, including nested
  `worktrees/<label>/s-N`. Legacy task-named worktrees remain visible for
  separate operator triage; pooling does not drain that backlog.

## Index the captive ring

Index captive seats. codedb (and any other project-keyed indexer) keys on
checkout root path: the same `worktrees/<label>/s-N` path is the same
project. First create of `s-N` pays one full index; reuse must not create a
new project key. Do **not** exclude `worktrees/s-*` or `worktrees/wt-*` and
do **not** teach excluding the captive ring.

Ad-hoc litter (`.cache/worktrees`, `/tmp` checkouts) may be ignored. A
repository `.codedbignore`, if added, must not ignore captive seats. Goal
Flight does not silently rewrite the user's global codedb config.

Conflict classification:

- mechanical: re-dispatch on current head
- semantic: mark blocked and ask user
- validation-only: rerun tests in main worktree after merge

Parallelism is bounded by `goalflight_capacity.py`, not the command-line `N`
alone.
