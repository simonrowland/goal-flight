---
description: "Resume from git state, status JSON, and dispatch ledger."
---

# resume

Rebuild working context from files and procedural status.

Applies only when Goal Flight was already in play (see `protocols/state-handoff.md`).

## STEP 0 — Load the skill body FIRST (unconditional; do not skip)

Before running any step below, read the repository `SKILL.md` **end-to-end**, plus
the protocols it references for the work you are about to do. This is not optional
and not compaction-only:

- `/goal-flight resume` may load only this command body, not the full skill.
- After a compaction the `SKILL.md` already in your context is frequently STALE or
  truncated: system reminders silently drop load-bearing rules across compactions.
- Resuming from RESUME-NOTES + the resume args + your own judgment, WITHOUT the
  loaded skill body, is the documented failure mode. Controllers then improvise the
  practice instead of following it and drift into the known anti-patterns:
  - the host Agent/Task tool as a code executor instead of `goalflight_dispatch.py`;
  - engagement-question boxes over obvious matters ("I found a problem: fix it?" —
    the forbidden "are-you-still-there" pattern); just act, per §Autonomous throughput;
  - `spawn_task` chips for out-of-scope findings instead of the queue backlog.

**Self-test:** if you cannot quote `SKILL.md`'s Hard Invariants and Dispatch Model
from what is currently loaded, you have NOT loaded it.
- Native (Claude Code): the body loads on `/goal-flight`. Confirm it is present and
  fresh by quoting one Hard Invariant; if you cannot, re-invoke `/goal-flight`.
- Non-native hosts (codex / grok / cursor / opencode): read your installed host
  wrapper, then `<skill-root>/SKILL.md` end-to-end from disk.

Do not act on any resume state until STEP 0 is satisfied.

## STEP 1 — Reload order + handoff

Follow `AGENTS.md`, then the canonical post-compaction reload order in `SKILL.md`
(session-status verdict → `SKILL.md` end-to-end → newest RESUME-NOTES → newest
queue → `goalflight_status.py --json`). Read `protocols/state-handoff.md`.

## STEP 2 — Rebuild status

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
git status --short
git log -1 --oneline
```

Then summarize:

- current branch/head/dirty state
- active dispatches and classifications
- capacity cooldowns
- next non-DONE queue item
- first safe command to run next

Do not reconstruct state from raw worker logs when status JSON exists.
