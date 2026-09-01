---
name: goal-flight
description: "Goal Flight orchestration for Grok Bot: plan, dispatch, review, recover, and resume long-running repository work from file-backed state."
---

# goal-flight

Use this skill when a repository task needs durable planning, resumable work,
worker dispatch, review flights, or handoff notes that survive context loss.

Grok Bot is a host projection over the portable Goal Flight core. It is not a
rewrite of that core and it does not replace the Grok CLI adapter
(`adapters/grok.json`, `grok agent stdio`).

## Load Order

1. Read the repository `AGENTS.md` first when present.
2. Read this Grok Bot host wrapper.
3. Read the repository root `SKILL.md` as the canonical Goal Flight workflow.
4. Read `protocols/guidance-extended.md` by default — non-frontier controllers
   benefit from its worked examples and expanded rationale for the core's rules;
   skip only when context is tight (the core alone is complete).
5. Read the newest relevant `docs-private/RESUME-NOTES-*.md` only when the task
   asks to resume prior Goal Flight work.

## Skill pin

Live controllers load the detached pin at `~/.goal-flight/skill/` (or
`$GOALFLIGHT_ROOT`), not a live source checkout and not a clone on the Grok Bot
box. Resolve `<skill-root>` before running scripts. Doctor warns when an
installed wrapper copy has drifted from that pin; resync with
`./install.sh grok-bot`.

## Operating Rules

- Treat repository Markdown plus Git as canonical state.
- Keep queue, ledger, status, review, and handoff artifacts file-backed.
- Facts come from `goalflight_status.py`, `goalflight_task.py list/next`, and
  doctor. Conversation is not the backlog.
- Treat Grok Bot session state, named-teammate memory, and host config as advisory.
- Do not rewrite the root `SKILL.md` during setup. Setup registers this wrapper
  only.

## Dispatch and workers

Dispatch CLI workers with the existing scripts (`goalflight_dispatch.py` to the
codex / grok CLI / cursor / claude adapters). Do not add a parallel task store.

Run those CLI workers on the user's registered computer (the Mac checkout),
not on the Grok Bot box, and not by cloning the repository onto the box. Grok
Bot Shell can target that machine with a `machineId`.

- Grok Bot Task / executor subagents are recon, analysis, and review only.
  They are not code executors. Same rule as the host Agent in the portable core.
- Grok Bot Cloud Agents are an optional extra worker for GitHub-branch / PR
  chunks only. They are never the default for a local scientific-coding project.
- Independent review stays on a different engine (codex / gstack). This host
  inherits `forbid_self_review`: a Grok Bot controller must not Type-1-review a
  `grok-code` worker.
- Do not pin Grok model ids.

Stamp `--controller-label --controller-pid --controller-session-id` on every
dispatch. Claim a Grok Bot controller label of your own. Never steal a live
lease that belongs to another controller.

## Multi-controller etiquette

Five-controller layouts are a project convention, not a Goal Flight primitive.
When a project already partitions lanes across named controllers (the live
example uses integrator / engine / bugs / webui / perf labels), a Grok Bot
controller joining that project must:

- read the project's `docs-private/MULTI-CONTROLLER-ETIQUETTE.md` when present
- claim a new label and its own worktree / branch
- send merge-request mail; leave origin push to the project's integrator
- never steal those existing leases

SendToAgent / named teammate pings may wake another Grok Bot controller that
shares the project. They are not the work plane. Controller-mail remains
`goalflight_messages.py`: leases, journal, `post --to-controller`,
merge-request / patch. Do not replace the journal with SendToAgent.

## Wake path

`supervise` needs a host monitor where each flushed stdout line is a controller
notification, unbounded, `persistent: true`. Grok Bot does not have that
monitor.

Use the portable `goalflight_messages.py listen` doorbell fallback (exit-as-wake).
On each ring (exit 0), process reported or authoritative mail, then restore
listen depth. Five-controller setups still use `post --to-controller` plus that
listen pool.

Grok Bot may revive the parent when a background Task finishes. That is useful
for executor recon. It is not a substitute for listen doorbells.

## Setup

From the cloned Goal Flight repository (or the pinned `~/.goal-flight/skill/`),
run:

```shell
./setup.sh --grok-bot
./setup.sh --apply --yes --grok-bot

# one-shot; optional workflows-library root overrides the default
./install.sh grok-bot
./install.sh grok-bot /home/box/agent-data/workflows
```

The checked-in destination is the Grok Bot workflows library
(`/home/box/agent-data/workflows/goal-flight/SKILL.md` on the bot machine).
Override with `GOALFLIGHT_GROK_BOT_WORKFLOWS` when installing from a Mac
checkout whose library root is different. This path is the least-wrong
verified location; do not assume `~/.grok/skills/` (that is Grok CLI).
