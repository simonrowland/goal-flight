# Worktrees And Parallel Execution Protocol

Use for `execute --parallel N` and merge orchestration.

Rules:

- one leased worktree seat per concurrent chunk
- local ACP dispatch uses `scripts/goalflight_acp_run.py --worktree create`
  for `execute --parallel N` when `N >= 2`
- sequential dispatch (`--parallel 1` or no flag) stays in the project root
- seats are the lazy, fixed range `worktrees/wt-1` … `worktrees/wt-N`; `N`
  defaults to 4 and is set deliberately per repository with
  `GOALFLIGHT_WORKTREE_SEATS`
- the configured range is a hard ceiling: a dispatch fails with every held
  seat and occupant dispatch id named when no kernel lock is available; there
  is no force-new escape hatch
- seats start from committed `HEAD`; uncommitted controller-root edits are
  not visible inside parallel dispatch worktrees
- acquire holds `LOCK_EX|LOCK_NB`, inherited by the spawned worker; process
  death releases the seat without a registry, timeout, or reaper
- the dispatch id written inside the lock file is diagnostic only; availability
  is decided exclusively by the live kernel lock, never by recorded metadata
- acquire quarantines abandoned dirty work to
  `goalflight/quarantine/wt-<N>-<UTC-time>`, then runs
  `git checkout -f <base>` and `git clean -fd`; release only closes the lease
- the pool is intentionally persistent; completed/failed seats are reused,
  not removed
- disjoint write ownership in the prompt
- shared-tree full-suite code writers are serialized; run `pytest tests/` (or
  equivalent whole-repo suites) concurrently only when each worker is isolated in
  its own worktree
- acquire capacity before each worker spawn
- ledger every worker PID/session
- continue independent chunks when one chunk blocks
- merge completed chunks back through normal git review
- doctor reports each pooled seat's live kernel-lock state. Legacy task-named
  worktrees remain visible for separate operator triage; pooling does not drain
  that backlog.

## Index exclusion

Exclude `worktrees/wt-*` in the repository indexer's ignore/exclude list so
pooled near-duplicates are not indexed. For codedb, add that one line to the
repository's `.codedbignore`. Goal Flight documents the pattern but does not
edit user tool configuration.

Conflict classification:

- mechanical: re-dispatch on current head
- semantic: mark blocked and ask user
- validation-only: rerun tests in main worktree after merge

Parallelism is bounded by `goalflight_capacity.py`, not the command-line `N`
alone.
