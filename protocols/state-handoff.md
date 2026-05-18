# State And Handoff Protocol

State layers:

1. Project state: git, tests, docs, queue.
2. Machine state: capacity leases, dispatch ledger, cooldowns.
3. Conversation state: current decisions and unresolved questions.

Before compact or sleep:

- run `python3 <skill-root>/scripts/goalflight_status.py`
- update RESUME-NOTES with current git head, queue state, active dispatch IDs, and next command
- do not paste raw logs

On resume:

1. Check git reality.
2. Run `goalflight_status.py --json`.
3. Classify active dispatches:
   - expected live
   - stale dead PID
   - stale PID reuse
   - surplus worker-like process
   - cooldown blocked
4. Continue from status rows, not from memory.

The dispatch ledger validates process identity with PID plus process start/command.
PID alone is never authoritative.
