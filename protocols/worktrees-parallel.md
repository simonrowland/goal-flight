# Worktrees And Parallel Execution Protocol

Use for `execute --parallel N` and merge orchestration.

Rules:

- one worktree per chunk
- local ACP dispatch uses `scripts/goalflight_acp_run.py --worktree create`
  for `execute --parallel N` when `N >= 2`
- sequential dispatch (`--parallel 1` or no flag) stays in the project root
- worktrees start from committed `HEAD`; uncommitted controller-root edits are
  not visible inside parallel dispatch worktrees
- disjoint write ownership in the prompt
- shared-tree full-suite code writers are serialized; run `pytest tests/` (or
  equivalent whole-repo suites) concurrently only when each worker is isolated in
  its own worktree
- acquire capacity before each worker spawn
- ledger every worker PID/session
- continue independent chunks when one chunk blocks
- merge completed chunks back through normal git review
- completed/failed/wedged dispatch worktrees stay on disk for operator
  inspection; doctor reports stale managed worktrees and suggests
  `git worktree remove <path>` followed by `git worktree prune`

Conflict classification:

- mechanical: re-dispatch on current head
- semantic: mark blocked and ask user
- validation-only: rerun tests in main worktree after merge

Parallelism is bounded by `goalflight_capacity.py`, not the command-line `N`
alone.
