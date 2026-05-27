---
description: "Resume from git state, status JSON, and dispatch ledger."
---

# resume

Rebuild working context from files and procedural status.

Applies only when Goal Flight was already in play (see `protocols/state-handoff.md`).
After compaction, reload the skill per `AGENTS.md` load order before the steps below.

Read `protocols/state-handoff.md`.

Run:

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
