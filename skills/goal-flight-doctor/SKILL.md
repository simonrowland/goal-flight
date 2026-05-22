---
name: goal-flight-doctor
description: "Run goal-flight readiness diagnostics for the current repository."
---

# goal-flight-doctor

Use this skill when the user asks for `/goal-flight doctor`, goal-flight
readiness, setup verification, or host/worker diagnostics.

## Steps

1. Read `AGENT.md` or `AGENTS.md` when present.
2. Read the repository root `SKILL.md` as the canonical Goal Flight workflow.
3. Read `commands/doctor.md`.
4. Run the procedural doctor for the current repository:

```shell
python3 scripts/goalflight_doctor.py --project-root "$PWD"
```

Keep long probe details out of chat. Return the checklist summary and the
highest-priority blocker, if any.
