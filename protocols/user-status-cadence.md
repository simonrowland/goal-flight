# User Status Cadence Protocol

Controller behavior during `execute` (and any long dispatch loop). **Separate
from** worker markers (`protocols/worker-markers.md`), chunk review
(`protocols/chunk-review.md`), and milestone review
(`protocols/milestone-review.md`).

## Purpose

Keep the user informed with compact progress while workers run. This is **not**
engagement polling — do not ask "are you still there?" or "want me to continue?"
The autonomous-throughput discipline lives in `SKILL.md` (top-level); read the
whole skill before relying on partial summaries.

## Cadence

While any worker or review job is in-flight, or while the controller is waiting
on a background job (>10s) before the queue is DONE:

- **At least every 15 minutes**, poll machine state and surface a **user-facing
  status update** in chat.
- Track `last_user_status_at` in conversation or RESUME-NOTES so long gaps are
  visible after compaction.

## Poll (compact)

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
python3 <skill-root>/scripts/goalflight_messages.py relay || true
```

Read per-dispatch `status_path` rows from the ledger aggregate when status JSON
is stale or ambiguous. Do not paste raw logs or full JSON — summarize.

## User-facing update (include when possible)

One short block, for example:

- queue: item id/slug, done vs remaining
- in-flight: dispatch id, agent, state (`running`, `running_quiet`, `blocked`, …)
- last land: chunk summary + test signal if a chunk completed since last update
- blockers: only `USER-NEED`, `USER-CONFIRM`, `BLOCKED`, or relay exit 2

Use a leading `STATUS:` line when the host renders markers. No optional
follow-ups that stall the queue.

## Context tight — skip chat, keep files

Skip the chat digest when context is tight:

- compaction imminent or user asked for minimal chatter
- conversation already near host context limits
- the turn is dominated by a required user answer or blocker relay

Still run the poll; write a one-line timestamped row to
`docs-private/RESUME-NOTES*.md` or the active goal-queue margin so the next
session or resume can reconstruct progress.

## Relation to worker STATUS markers

Workers should emit `STATUS:` markers per `prompts/dispatch-wrapper.md` (~8
minutes). That feeds watchers and status JSON. **This protocol** is the
controller translating aggregate state **to the user** on a ≤15-minute cadence.
