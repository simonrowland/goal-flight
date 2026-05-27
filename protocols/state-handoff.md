# State And Handoff Protocol

State layers:

1. Project state: git, tests, docs, queue.
2. Machine state: capacity leases, dispatch ledger, cooldowns.
3. Conversation state: current decisions and unresolved questions.

Before compact or sleep:

- run `python3 <skill-root>/scripts/goalflight_status.py`
- update RESUME-NOTES with current git head, queue state, active dispatch IDs, and next command
- do not paste raw logs

On resume (only when Goal Flight was already in play — user invoked it, an open
goal queue or dispatch ledger exists, or `docs-private/RESUME-NOTES*.md` says
so; **not** for ordinary one-off coding):

0. Reload Goal Flight: `AGENTS.md` → host wrapper (if any) → `SKILL.md` →
   `commands/resume.md` and this file. Chat summaries are hints, not substitutes.
1. Read newest relevant `docs-private/RESUME-NOTES*.md` and queue/ledger rows.
2. Check git reality.
3. Run `goalflight_status.py --json`.
4. Classify active dispatches:
   - expected live
   - stale dead PID
   - stale PID reuse
   - surplus worker-like process
   - cooldown blocked
5. Continue from status rows, not from memory. Stay controller: dispatch workers
   for implementation unless dispatch routing marks the chunk `controller-direct`.

The dispatch ledger validates process identity with PID plus process start/command.
PID alone is never authoritative.
