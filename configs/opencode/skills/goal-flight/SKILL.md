---
name: goal-flight
description: "Goal Flight orchestration for OpenCode: plan, dispatch, review, recover, and resume long-running repository work from file-backed state."
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
- Treat OpenCode session state, chat, and host config as advisory.
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
./setup.sh --opencode
./setup.sh --apply --yes --opencode

# Optional project-local install for one repository:
./setup.sh --apply --yes --opencode-project /path/to/project
```

Dry-run output must show every planned copy, merge, link, or registration
before mutation. Apply requires explicit approval and writes a machine-local
backup manifest for rollback.

OpenCode setup installs global agent instructions, this personal skill, and a
context-mode MCP entry in `opencode.json`. Project-local setup writes the same
wrapper under `.opencode/` in the target repository and merges project
`AGENTS.md` when selected. Use `--opencode-agents-standard` for the shared
`~/.agents/skills/` location, or `--opencode-link-claude` to symlink OpenCode's
skill directory to an existing Claude skill checkout. After setup, verify MCP
discovery with `opencode mcp list` and `opencode mcp auth list` when OAuth
servers need attention.

After setup, run Goal Flight init in the target project. Init runs doctor,
capacity checks, worker readiness checks, and writes compact project-local
caveats.
