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
- Grok Bot Task / executor workers start blank (no chat, memory, SKILL.md, or
  this conversation). Every judgment-bearing Task prompt reuses the Claude
  host-subagent pin — do not invent a parallel package. See Executor context
  package below.
- Claim controller label `goalflight-grokbot` and stamp that claimed label
  on every `goalflight_dispatch.py` launch. Never steal a live lease.
- Wake on worker terminals (`!COMPLETE` is the success marker) through the
  existing journal doorbell. Canonical arm is one tracked listen on the
  user's Mac: `listen --report-pending --timeout-s 900 --controller-label
  goalflight-grokbot`. The 900s quiet timeout is this host's frontier ping
  (anti-stall), not Claude's 120s follow-stream heartbeat. Optional full
  pool (depth 4): 900s on one slot only; others `--timeout-s 0`. On ring,
  `relay --drain`, act, re-arm. On timeout (exit 1), pull status + task
  next, then re-arm. Never detach. Do not invent a second event bus, a
  Settings monitor widget, or a Grok Bot-native mail transport. Do not arm
  unbounded `supervise` as a background shell. Missed wake is latency:
  resume still pulls status, task next, and `relay --new`. See Wake path
  below.

## Dispatch and workers

Grok Bot Executors (Task / executor subagents) are a **first-class** dispatch
target on this host. Do not copy the Claude-host rule that treats the host
Agent as a last-resort code executor. That rule stays in the portable
`SKILL.md` for Claude Code only.

Choose the lane from token/budget (rate pressure, provider spend, session
limits) and from the task (code vs review vs recon vs planning). Both lanes
are valid:

- existing CLI workers via `goalflight_dispatch.py` on the user's registered
  computer (the Mac checkout), targeted with Grok Bot Shell `machineId`
- Grok Bot Task / executor subagents (host `delegate` surface)

Do not add a parallel task store. Do not clone the repository onto the Grok
Bot box. Do not pin Grok model ids.

### Grok Bot routing

| Task | Prefer when capacity is healthy | Alternate / host-native |
|---|---|---|
| Code-writing | CLI: `--agent codex`, `--agent grok-code`, or `--agent moonshot` (Kimi Code / kimi3; do not invent a `kimi3` agent id) | Grok Bot Executor when that lane is cheaper or the only one with budget; also for host-native chunks (recon, synthesis, mail, status) |
| Reviews | Independent CLI: `--agent moonshot` (kimi3) or `--agent codex` | gstack `/review` on an independent engine |
| Research / web search | `--agent grok-research` (read-only CLI) | controller-direct or Executor recon |
| Planning / status / mail | Grok Bot Executor or controller-direct | CLI only when the chunk is a large write |

`forbid_self_review` still applies: a Grok Bot controller must not Type-1-review
a `grok-code` worker. Use moonshot/kimi3 or codex (or gstack) as the
independent reviewer.

Grok Bot Cloud Agents remain an optional extra worker for GitHub-branch / PR
chunks only. They are never the default for a local scientific-coding project.

Stamp `--controller-label <claimed-label> --controller-pid
--controller-session-id` on every `goalflight_dispatch.py` launch. The
claimed label is `goalflight-grokbot` unless this controller already
joined under another unused slug. Never steal a live lease that belongs
to another controller.

## Executor context package

Grok Bot Task / executor subagents start **blank**. They have no chat
history, no memory, no installed SKILL.md, and none of this conversation.
Reuse the existing Claude host-subagent pin. Do not invent a parallel
package format and do not add a grok-executor template (Cursor / Grok CLI
wrappers do not have one).

CLI workers (`--agent codex`, `grok-code`, `moonshot`, …) keep the
existing five-layer `--prompt-file` dispatch (compose via
`prompts/dispatch-wrapper.md`; do not paste that recipe file into a Task).
Executors receive **that same composed prompt-file body** in the Task
prompt (paste it, or cite its stable Mac path as the `--prompt-file`
equivalent).

Compose every judgment-bearing grok-executor Task prompt in this order:

1. **Open** with `protocols/subagent-preamble.md` verbatim. Fill only the
   two slots: repository path and north star. The repository path is the
   Mac checkout, e.g. `/Users/simonrowland/Repos/<project>`.
2. **Grok-bot-only pin** (the only new clause), immediately after the
   preamble: executors default to the Grok Bot computer, **not** the
   user's Mac. Name `machineId` / "the user's registered computer" and
   absolute paths under `/Users/simonrowland/Repos/...`. Every Read and
   Shell in this Task — including the preamble's AGENTS.md /
   ORIENTATION.md reads — uses `machineId`. Do not clone the repo onto
   the box.
3. **Always** pointer to `docs-private/rag/ORIENTATION.md` when that file
   exists (orientation only; never scope expansion). The preamble already
   carries this pointer; keep it.
4. **If the lane is triggered**, apply `protocols/worker-context-package.md`
   in full: verbatim brief prepend (never link-instead-of-paste), quoted
   ground-truth spec, named guard tests, standing re-read. Then the
   `--prompt-file` equivalent: the same five-layer body CLI would get.
   Citing a Mac path is allowed for that composed prompt file; it does
   not replace the verbatim brief or quoted spec.

Mechanical Task prompts may carry the preamble harmlessly. Judgment-bearing
prompts (interpret evidence, review, compare designs, tradeoffs, or write
code against a spec) are not optional.

## Multi-controller etiquette

Five-controller layouts are a project convention, not a Goal Flight primitive.
When a project already partitions lanes across named controllers (the live
example uses integrator / engine / bugs / webui / perf labels), a Grok Bot
controller joining that project must:

- read the project's `docs-private/MULTI-CONTROLLER-ETIQUETTE.md` when present
- claim `goalflight-grokbot` (or another unused label) and its own worktree / branch
- send merge-request mail; leave origin push to the project's integrator
- never steal those existing leases

SendToAgent / named teammate pings may wake another Grok Bot controller that
shares the project. They are not the work plane. Controller-mail remains
`goalflight_messages.py`: leases, journal, `post --to-controller`,
merge-request / patch. Do not replace the journal with SendToAgent.

## Wake path

Journal mail is durable truth. Worker terminals are the existing markers in
`protocols/worker-markers.md`: `!COMPLETE` / `!READY` / `!FAILED` /
`!BLOCKED` / `!USER-NEED` (leading `!` optional). `!COMPLETE` is the
success terminal. The watcher already harvests those into the
terminal-outbox / controller mail. Do not invent a second event bus.

Grok Bot notifies on **job completion** (Task / executor or a tracked Shell
exits and revives the parent chat). That is the same contract as
`goalflight_messages.py listen` (exit-as-wake), not Claude's persistent
per-line `supervise` / `follow`. There is no Settings "monitor widget"; the
running background task / subagent card is the UI.

This host uses the portable listen / heartbeat process pool. The frontier
ping is `listen --timeout-s 900` (15 minutes of quiet), so the controller
is reminded of the task frontier and does not stall. That is **not**
Claude's follow-stream heartbeat (120s; the stream range is 60–300s and
would spam Grok Bot turns). Do not change the global `supervise` / `follow`
cadence for this host; 900s is a wrapper-local default on the listen arm.

Canonical arm — one tracked listen on the **user's Mac**, label
`goalflight-grokbot`, never detached (`nohup` → listen exit 4):

```shell
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" \
  --controller-label goalflight-grokbot \
  --report-pending \
  --timeout-s 900
```

On ring (exit 0): `relay --drain`, act, re-arm. On timeout (exit 1): pull
`goalflight_status.py` and `goalflight_task.py next`, then re-arm — that
exit *is* the 15-minute frontier reminder. The portable four-listen pool
is full depth; one listen is the MVP. If you arm more than one listen,
put `--timeout-s 900` on a single slot (the frontier ping) and leave the
others at `--timeout-s 0` so four quiet timeouts do not fire together.
Branch on listen exit codes. Exit 1 on this host is the frontier
reminder above (status + task next, re-arm), not the portable
"re-arm only if coverage is still required" row. Codes 2–5 follow
`protocols/controller-mail.md`; exit 5 is did-not-arm (do not re-arm
that nonce).

Do **not** arm unbounded `supervise` as a Grok Bot background shell and
expect per-line wakes. A helper, if added, must wrap `listen` or exit on
the first actionable event (ignore stream heartbeat / frontier
keepalives). A same-turn regex-wait on a live `supervise` process is a
same-turn block, not a session-life monitor; do not use it as the default.

Missed wake is latency, not data loss. Resume still pulls
`goalflight_status.py`, `goalflight_task.py next`, and `relay --new`.

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
