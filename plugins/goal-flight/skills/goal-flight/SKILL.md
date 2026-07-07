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
3. Read `protocols/guidance-extended.md` by default — non-frontier controllers
   benefit from its worked examples and expanded rationale for the core's rules;
   skip only when context is tight (the core alone is complete).
4. Read the newest relevant `docs-private/RESUME-NOTES-*.md` only when the task
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

./setup.sh --cursor
./setup.sh --apply --yes --cursor
```

Dry-run output must show every planned copy, merge, link, or registration
before mutation. Apply requires explicit approval and writes a machine-local
backup manifest for rollback.

For Codex, setup registers the desktop-facing plugin and checks the CLI worker
surface. Codex Desktop is the likely orchestrator; `codex exec` remains the worker
path. Codex setup registers codedb by default when available; context-mode
registration is opt-in with `GOALFLIGHT_CODEX_CONTEXT_MODE=1` because dispatched
Codex workers disable context-mode by default. For Cursor, setup installs global
agent instructions, a personal skill, rules, and Cursor MCP config for
context-mode. Use `--cursor-project <project>` for per-project `.cursor/`
wrappers, `--cursor-agents-standard` for `~/.agents/skills/`, or
`--cursor-link-claude --addons ''` to symlink Cursor to an existing Claude skill
checkout.

Codex plugin autocomplete lists skills, not nested subcommands. Use
`goal-flight-doctor` and `goal-flight-init` when you want command-shaped
autocomplete entries, or invoke `goal-flight` with the command text.

After setup and host restart, run `/goal-flight init <topic>` in the target
project. Init runs doctor, capacity checks, worker readiness checks, and writes
compact project-local caveats.
