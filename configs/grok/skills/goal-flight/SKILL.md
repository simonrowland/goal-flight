---
name: goal-flight
description: "Goal Flight orchestration for Grok: plan, dispatch, review, recover, and resume long-running repository work from file-backed state."
---

# goal-flight

Use this skill when a repository task needs durable planning, resumable work,
worker dispatch, review flights, or handoff notes that survive context loss.

## Load Order

1. Read `AGENT.md` or `AGENTS.md` when present.
2. Read the repository root `SKILL.md` as the canonical Goal Flight workflow.
3. Read the newest relevant `docs-private/RESUME-NOTES-*.md` only when the task
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

Grok setup installs this personal skill when a Grok controller surface is
selected. Worker execution remains through `grok agent stdio` under the Goal
Flight ACP runner.
