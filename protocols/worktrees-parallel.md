# Worktrees And Parallel Execution Protocol

Use for `execute --parallel N` and merge orchestration.

Rules:

- one worktree per chunk
- disjoint write ownership in the prompt
- acquire capacity before each worker spawn
- ledger every worker PID/session
- continue independent chunks when one chunk blocks
- merge completed chunks back through normal git review

Conflict classification:

- mechanical: re-dispatch on current head
- semantic: mark blocked and ask user
- validation-only: rerun tests in main worktree after merge

Parallelism is bounded by `goalflight_capacity.py`, not the command-line `N`
alone.
