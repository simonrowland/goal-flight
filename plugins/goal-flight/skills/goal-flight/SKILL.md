---
name: goal-flight
description: "Goal Flight orchestration for Codex: plan, dispatch, review, recover, and resume long-running repository work from file-backed state."
---

# goal-flight

Use this skill when a repository task needs durable planning, resumable work,
worker dispatch, review flights, or handoff notes that survive context loss.

## Load Order

1. Read the repository `AGENTS.md` first when present.
2. Read the repository root `SKILL.md` as the canonical Goal Flight workflow.
3. Read the newest relevant `docs-private/RESUME-NOTES-*.md` only when the task
   asks to resume prior Goal Flight work.

## Operating Rules

- Treat repository Markdown plus Git as canonical state.
- Keep queue, ledger, status, review, and handoff artifacts file-backed.
- Treat host-native memory, chat history, and plugin config as advisory.
- For significant coding, decompose the task before implementation.
- For review flights, write prompts and outputs to files, then summarize only
  decisions and findings into the live conversation.
- Do not rewrite the root `SKILL.md` during setup. Setup registers checked-in
  wrappers and config only.

## Setup

Use setup for host registration and machine bootstrap. Use init for
project-local state and execution readiness.

From the cloned Goal Flight repository, run:

```shell
./setup.sh --agent codex
./setup.sh --apply --yes --agent codex
```

Dry-run output must show every planned copy, merge, link, or registration
before mutation. Apply requires explicit approval and writes a machine-local
backup manifest for rollback.

For Codex, setup registers the desktop-facing plugin/personal skill and also
checks the CLI worker surface. Codex Desktop is the likely controller; `codex
exec` remains the worker path. Setup also registers the context-mode MCP server
when needed. For Cursor, setup installs global agent instructions, a personal
skill, and rules; context-mode remains deferred until a verified Cursor hook or
plugin API exists.

After setup and host restart, run `/goal-flight init <topic>` in the target
project. Init runs doctor, capacity checks, worker readiness checks, and writes
compact project-local caveats.
