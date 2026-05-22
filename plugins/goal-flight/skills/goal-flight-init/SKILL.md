---
name: goal-flight-init
description: "Initialize goal-flight project state for a repository task."
---

# goal-flight-init

Use this skill when the user asks for `/goal-flight init <topic>` or wants to
start a durable Goal Flight run for a repository task.

## Steps

1. Read `AGENT.md` or `AGENTS.md` when present.
2. Read the repository root `SKILL.md` as the canonical Goal Flight workflow.
3. Read `commands/init.md` and the protocols it references.
4. Treat the user's remaining text as the init topic.
5. Run only the procedural checks and file-backed setup that `commands/init.md`
   requires.

Keep queue, ledger, review, and handoff artifacts file-backed. Return paths and
a compact readiness summary.
