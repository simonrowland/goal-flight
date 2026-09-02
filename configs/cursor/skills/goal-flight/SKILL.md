---
name: goal-flight
description: "Use when the user invokes /goal-flight or asks for Goal Flight to plan, dispatch, review, recover, or resume a long-running orchestrated repository run from file-backed state."
disable-model-invocation: true
---

# goal-flight

Use this skill when a repository task needs durable planning, resumable work,
worker dispatch, review flights, or handoff notes that survive context loss.

## Load Order

1. Read the repository `AGENTS.md` first when present.
2. Read `<skill-root>/SKILL.md` as the canonical Goal Flight workflow, where
   `<skill-root>` is resolved per `## Skill root` below.
3. Read `protocols/guidance-extended.md` by default — non-frontier controllers
   benefit from its worked examples and expanded rationale for the core's rules;
   skip only when context is tight (the core alone is complete).
4. Read the newest relevant `docs-private/RESUME-NOTES-*.md` only when the task
   asks to resume prior Goal Flight work.

## Operating Rules

- Treat repository Markdown plus Git as canonical state.
- Keep queue, ledger, status, review, and handoff artifacts file-backed.
- Treat Cursor chat, rules, memories, and host state as advisory.
- For significant coding, decompose the task before implementation.
- For review flights, write prompts and outputs to files, then summarize only
  decisions and findings into the live conversation.
- Do not rewrite the root `SKILL.md` during setup. Setup registers checked-in
  wrappers and config only.

## Skill root

Live controllers load the installed skill pin at `$GOALFLIGHT_ROOT` or
`~/.goal-flight/skill/`, not a live source checkout. Run every
`python3 <skill-root>/scripts/...` command from that pin
(`$GOALFLIGHT_ROOT/scripts` or `~/.goal-flight/skill/scripts`). When
working inside the goal-flight repository itself, `<skill-root>` is the
repository root. The project tree is `$GOALFLIGHT_PROJECT_ROOT`, a named
checkout, or the live controller project — pass `--project-root` when the
cwd is not that tree. The pin is a detached `git worktree` of a goal-flight checkout at a release
tag; nothing in install or setup creates it. Create it once with
`git -C <goal-flight-checkout> worktree add ~/.goal-flight/skill <tag>` (or point
`$GOALFLIGHT_ROOT` at any checkout), then `python3 <skill-root>/scripts/goalflight_skill_link.py --pin`.
Doctor warns when this installed wrapper has drifted from the repo copy; resync
it with the setup command in `## Setup` below.

## Setup

Use setup for host registration and machine bootstrap. Use init for
project-local state and execution readiness.

From the cloned Goal Flight repository, run:

```shell
./setup.sh --cursor
./setup.sh --apply --yes --cursor

# Optional project-local install for one repository:
./setup.sh --apply --yes --cursor-project /path/to/project
```

Dry-run output must show every planned copy, merge, link, or registration
before mutation. Apply requires explicit approval and writes a machine-local
backup manifest for rollback.

Cursor setup installs global agent instructions, this personal skill, the Goal
Flight rule, and a Cursor MCP entry for context-mode. Project-local setup writes
the same wrapper under `.cursor/` in the target repository. Use
`--cursor-agents-standard` for the shared `~/.agents/skills/` location, or
`--cursor-link-claude` to symlink Cursor's skill directory to an existing Claude
skill checkout. If `cursor-agent mcp list` says context-mode needs approval,
run `cursor-agent mcp enable context-mode`, then verify with
`cursor-agent mcp list-tools context-mode`.

After setup and Cursor restart, run Goal Flight init in the target project. Init
runs doctor, capacity checks, worker readiness checks, and writes compact
project-local caveats.
