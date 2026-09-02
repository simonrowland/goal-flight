---
name: goal-flight
description: "Use when the user invokes /goal-flight or asks for Goal Flight to plan, dispatch, review, recover, or resume a long-running orchestrated repository run from file-backed state."
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
- Treat Grok session state, memory, and config as advisory.
- For review flights, write prompts and outputs to files, then summarize only
  decisions and findings into the live conversation.
- Prefer Goal Flight ACP or Grok `--prompt-file` for worker/reviewer smokes;
  do not paste long transcripts into chat.

## Skill root

Live controllers load the installed skill pin at `$GOALFLIGHT_ROOT` or
`~/.goal-flight/skill/`, not a live source checkout. Run every
`python3 <skill-root>/scripts/...` command from that pin
(`$GOALFLIGHT_ROOT/scripts` or `~/.goal-flight/skill/scripts`). When
working inside the goal-flight repository itself, `<skill-root>` is the
repository root. The project tree is `$GOALFLIGHT_PROJECT_ROOT`, a named
checkout, or the live controller project — pass `--project-root` when the
cwd is not that tree. Doctor warns when this installed Grok wrapper has
drifted from the repo copy; resync with `./install.sh grok`.

## Setup

From the cloned Goal Flight repository, run:

```shell
./setup.sh --agent grok
./setup.sh --controllers grok-cli-controller --workers grok-acp-worker --addons gstack
./setup.sh --apply --yes --controllers grok-cli-controller --workers grok-acp-worker --addons gstack
```

Grok setup installs this personal skill when a Grok orchestrator surface is
selected. Worker execution remains through `grok agent stdio` under the Goal
Flight ACP runner.

Grok Bot controllers that arm `listen` through Mac local-exec can add an
optional outbound wake webhook (`docs/hosts/grok-bot.md`) so a dropped
local-exec session does not leave doorbells dead while the journal still
receives mail. The webhook nudge complements exit-as-wake `listen`; it does
not replace `listen` when local-exec is up.
