# State And Handoff Protocol

State layers:

1. Project state: git, tests, docs, queue.
2. Machine state: capacity leases, dispatch ledger, cooldowns.
3. Conversation state: current decisions and unresolved questions.

## Activation contract

Goal Flight is **active** when any one of:

- `docs-private/goal-queue-<slug>.md` exists with frontmatter
  `state: active` AND `last-touched` within the TTL (default 7 days).
- Dispatch ledger has active leases for this `project_root` (filter
  `goalflight_capacity.py status --json` by `active[].project_root`).
- Newest `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` TL;DR declares an
  active run.

Single canonical check:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py --text
```

Outputs either `active goal-flight session (...)` with breakdown, or
`no active goal-flight session ...`. Agents post-compaction should run
this BEFORE auto-loading the skill end-to-end.

## RESUME-NOTES file convention

Canonical filename: `docs-private/RESUME-NOTES-<YYYY-MM-DD>[-rev<N>].md`.
ISO 8601 dates so lexicographic sort = chronological sort. **No topic
prefixes in the filename** — topic context goes inside the file's
TL;DR. Find newest:

```bash
ls -1 docs-private/RESUME-NOTES-*.md | sort | tail -1
```

The naming convention is forward-looking; pre-existing topic-prefixed
files (e.g. `RESUME-NOTES-generalize-2026-05-20.md`) are historical
exceptions — leave in place, do not retroactively rename.

## Session identity

While a run is active, the orchestrator's `current_session` is stamped
into the active goal-queue's frontmatter:

```yaml
current_session:
  id: <uuid>
  pid: <orchestrator PID>
  started_at: <ts>
  hostname: <host>
session_history:
  - {id, pid, started_at, claimed_at, ended_at, ended_reason}
```

`session_history` is append-only. Two layers of identity: the RUN
(slug + started date) survives across many sessions; the SESSION
(uuid + pid) is per-terminal and turns over on takeover/crash/exit.
See `scripts/goalflight_session_status.py --claim` / `--release`.

## Before compact or sleep

- run `python3 <skill-root>/scripts/goalflight_status.py`
- update newest `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` (bump
  `-rev<N>` if needed) with current git head, queue state, active
  dispatch IDs, and next command
- do not paste raw logs

## On resume

Only when Goal Flight was already in play — verdict `active` per the
activation contract above; **not** for ordinary one-off coding.

0. Reload Goal Flight: `AGENTS.md` → host wrapper (if any) → `SKILL.md` →
   `commands/resume.md` and this file. Chat summaries are hints, not substitutes.
0.5. **Skill-freshness + designated-controller check.** If a system
   reminder says `goal-flight (previously invoked)` but you can't quote
   SKILL.md line 35 ("⚠️ Read this skill end-to-end before acting"),
   the loaded skill body is STALE (truncated reminders silently drop
   load-bearing rules like "background >10s", `git commit -- <files>`,
   "no `tail -f`"). Re-invoke `/goal-flight` to reload fresh, then
   confirm you're the designated orchestrator: compare your terminal's
   session id (`goalflight_session_status.py --ensure-session`) against
   the active queue's `current_session.id` field. If `current_session.pid`
   is alive and the id mismatches yours, ANOTHER orchestrator owns this
   run — surface to user before claiming. If `current_session.pid` is
   dead, `--force-release-stale` then claim.
1. Activation check: `goalflight_session_status.py --text`. Bail out
   if "no active session".
2. Read newest RESUME-NOTES via the find-newest one-liner above + queue
   frontmatter (`state`, `current_session`, `session_history`).
3. Check git reality.
4. Run `goalflight_status.py`.
5. Classify active dispatches:
   - expected live
   - stale dead PID
   - stale PID reuse
   - surplus worker-like process
   - cooldown blocked
6. Continue from status rows, not from memory. Stay orchestrator: dispatch workers
   for implementation unless dispatch routing marks the chunk `controller-direct`.

The dispatch ledger validates process identity with PID plus process start/command.
PID alone is never authoritative.

## Cross-machine / takeover

If `current_session.hostname` differs from your host OR
`current_session.pid` is not alive on this machine, you are NOT the
session driver. Options:

- `goalflight_session_status.py --force-release-stale` — clears
  current_session entries whose pid is dead, then claim cleanly.
- `goalflight_session_status.py --claim --queue <path> --force` —
  explicit takeover when the prior session is unrecoverable.

Bare claim (no `--force`) refuses when an alive-pid different session
owns the run — review with the user before forcing.
