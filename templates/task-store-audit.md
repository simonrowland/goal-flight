# Task-store audit worker brief

**Cadence:** weekly project duty. The project-duty owner reviews and approves every
proposed change before updating the task store.

## Task

Audit the current project task store for duplicate items, stale items that may be
closed, incorrect ranking, and blockers that should be rechecked. Use the canonical
`goalflight_task.py` read commands and cite the task IDs and evidence.

## Constraints

- Read-only: do not change task state, merge duplicates, close items, or rewrite
  priorities or blockers.
- Preserve active worker links and unresolved requirements.
- Do not infer that an item is stale only because it has not changed recently.
- Keep recommendations limited to actions supported by task-store history or
  current project evidence.

## Output

Return a short table of proposed actions with item IDs, the action, evidence, and
any dependency or ownership concern. Include separate lists for duplicates,
possible closures, ranking changes, and blocker rechecks. The project-duty owner
decides which changes to apply.
