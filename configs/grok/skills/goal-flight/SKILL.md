---
name: goal-flight
description: "Goal Flight orchestration for Grok: plan, dispatch, review, recover, and resume long-running repository work from file-backed state."
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

Grok Bot controllers that arm `listen` through Mac local-exec should also
configure the outbound wake webhook (`docs/hosts/grok-bot.md`) so a dropped
local-exec session does not leave doorbells dead while the journal still
receives mail. `listen` and the webhook are independent alternative doorbells;
the journal stays the inbox. Deafness is both listen unarmed and webhook
failing. The webhook does not replace `listen` when local-exec is up.
