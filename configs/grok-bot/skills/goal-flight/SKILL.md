---
name: goal-flight
description: "Use when the user invokes /goal-flight or asks for Goal Flight to plan, dispatch, review, recover, or resume a long-running orchestrated repository run from file-backed state."
---

# goal-flight

Use this skill when a repository task needs durable planning, resumable work,
worker dispatch, review flights, or handoff notes that survive context loss.

Grok Bot is a host projection over the portable Goal Flight core. It is not a
rewrite of that core and it does not replace the Grok CLI adapter
(`adapters/grok.json`, `grok agent stdio`).

## Freshness (always loaded)

Disk-read repository `SKILL.md` Hard Invariants. If you cannot quote them
from that read, you are stale: run Compaction resume before acting. This
rule cannot live only in compacted chat or a summarized SKILL.md.

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
box. Run scripts from that pin (`$GOALFLIGHT_ROOT/scripts` or
`~/.goal-flight/skill/scripts`). Doctor warns when an installed wrapper copy
has drifted from that pin; resync with `./install.sh grok-bot`.

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
  existing journal doorbell. Canonical arm is one tracked
  `goalflight_grok_bot_listen.py --report-pending --timeout-s 900
  --controller-label goalflight-grokbot` on the user's Mac (that helper
  wraps `listen --report-pending` and prints the quote-check banner). The
  900s quiet timeout is this host's frontier ping (anti-stall), not
  Claude's 120s follow-stream heartbeat. Optional full pool (depth 4):
  900s on one slot only; others `--timeout-s 0`. Every ring or timeout is
  a mini-resume: drain if it rang, flush RESUME-NOTES (`state-handoff.md`
  Before compact or sleep), quote-check Hard Invariants from disk, re-arm,
  `goalflight_task.py next`. Never detach.
  Do not invent a second event bus, a Settings monitor widget, a compact
  UI, a context-consumption meter, or a Grok Bot-native mail transport.
  Do not port Claude PostToolUse / SessionStart hooks. Do not arm
  unbounded `supervise` as a background shell. Missed wake is latency:
  resume still pulls status, task next, and `relay --new`. The operator
  is not the compaction mailman. The one operator wake-hygiene job is
  re-arming listen doorbells after a host update or token-pause, surfaced
  as roster `wake unarmed` — not a second reminder channel. See
  Compaction and Operator role in `docs/hosts/grok-bot.md`.

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

Stamp `--controller-label <claimed-label> --controller-pid <pid>
--controller-session-id <session-id>` on every `goalflight_dispatch.py`
launch. The claimed label is `goalflight-grokbot` unless this controller
already joined under another unused slug. Never steal a live lease that
belongs to another controller.

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

Inter-controller traffic is **only**
`goalflight_messages.py post --to-controller`. This controller joins that
journal. Do not ask the user to paste mail, and do not ask them to tell
another session to check mail. The operator is the former mailman.

SendToAgent (Grok Bot teammate DMs) is an optional extra ping among
grok-bot agents. It is never the work inbox and never the bridge to
Claude `battery-*` controllers. Do not replace the journal with
SendToAgent.

Deafness is re-arm listen, not user-as-mailman.

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

Canonical arm — one tracked `goalflight_grok_bot_listen.py` on the
**user's Mac**, label `goalflight-grokbot`, never detached (`nohup` →
listen exit 4). That helper wraps `listen` and prints the quote-check
banner on every exit (ring or timeout). The doorbell is outside the chat.

```shell
python3 <skill-root>/scripts/goalflight_grok_bot_listen.py \
  --project-root "$PWD" \
  --controller-label goalflight-grokbot \
  --report-pending \
  --timeout-s 900
```

Every ring (exit 0) or timeout (exit 1) is a **mini-resume**: drain if
it rang (`relay --drain`, act), flush RESUME-NOTES (Compaction below),
quote-check Hard Invariants from a fresh disk read, re-arm, then
`goalflight_task.py next`. The 900s quiet timeout is this host's
substitute for Claude's 80% context-meter hint. The portable four-listen
pool is full depth; one listen is the MVP. If you arm more than one
listen, put `--timeout-s 900` on a single slot (the frontier ping) and
leave the others at `--timeout-s 0` so four quiet timeouts do not fire
together. Branch on listen exit codes. Exit 1 on this host is the
frontier reminder above, not the portable "re-arm only if coverage is
still required" row. Codes 2–5 follow `protocols/controller-mail.md`;
exit 5 is did-not-arm (do not re-arm that nonce).

Do **not** arm unbounded `supervise` as a Grok Bot background shell and
expect per-line wakes. A helper, if added, must wrap `listen` or exit on
the first actionable event (ignore stream heartbeat / frontier
keepalives). A same-turn regex-wait on a live `supervise` process is a
same-turn block, not a session-life monitor; do not use it as the default.

Missed wake is latency, not data loss. Resume still pulls
`goalflight_status.py`, `goalflight_task.py next`, and `relay --new`.

## Compaction

Grok Bot = **autocompact + keep handoff current**. Do **not** port
`scripts/hooks/goalflight-context-meter.sh` or Claude PostToolUse
`additionalContext` 80/90/95% bands. That hook is Claude-Code-specific.
Grok Bot has no PostToolUse `additionalContext` injection and no
trustworthy window %. A fake meter is worse than none. Claude
context-meter and SessionStart watchdog stay Claude-only. There is no
SessionStart hook on grok-bot.

Grok Bot has **no** controller-authored `/compact` and **no** keep-vs-toss
prompt. The host may summarize the chat on its own. Do not invent a compact UI.
Do not ask the user to compact. Do not emulate Claude's compact prompt in chat.

The directed-compact step on this host **is**
`protocols/state-handoff.md` "Before compact or sleep":

- update newest `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` with
  ENVIRONMENT / IDEAS / DECISIONS / FACTS / CARRIERS only — no task tables,
  dispatch codes, or next-task lists
- store baseline via `goalflight_task.py list` (outstanding, plus
  deferred / held when relevant)
- `goalflight_status.py`

The 15-minute listen timeout is the substitute for Claude's 80% hint.
On **every** ring or timeout, flush that handoff, then quote-check. Also
write before long waves. There is no warning before the host summarizes.

The Hard Invariants quoting rule cannot live only in compacted SKILL.md.
Externalize it:

1. This wrapper's Freshness preamble (always loaded): disk-read SKILL.md
   Hard Invariants; if you cannot quote them, stale → resume before acting.
2. Listen/heartbeat stdout banner (doorbell is outside the chat):
   `goalflight_grok_bot_listen.py` prints
   `QUOTE-CHECK: disk-read SKILL.md Hard Invariants; if you cannot quote them, stale — resume before acting.`
   on every listen exit.
3. Optional operator-side Grok Bot profile sentence:
   `On every Goal Flight listen exit, disk-read SKILL.md Hard Invariants; if you cannot quote them, resume before acting.`

Every 15-minute wake is a mini-resume: drain if it rang, flush
RESUME-NOTES, quote-check from disk, re-arm doorbells,
`goalflight_task.py next`.

Resume reload and rebuild are unchanged. Chat summaries are hints, not
substitutes. Grok Bot profile, `update_state` memory, and routines may
persist across summaries; they are advisory. Repository files remain
the canonical memory backend.

If this controller cannot quote `SKILL.md` Hard Invariants from a
**fresh disk read**, reload `AGENTS.md` → this grok-bot wrapper →
repository `SKILL.md` → `commands/resume.md`. Then:

1. `goalflight_session_status.py --text`
2. **Skip** `commands/resume.md` STEP 1.5 (`supervise`). Arm listen
   doorbells (portable pool) instead.
3. store baseline (`goalflight_status.py` + `goalflight_task.py list`)
4. newest RESUME-NOTES
5. `goalflight_task.py next`

The operator is not the compaction mailman. After a host update or
token-pause, listen may exit 5 (dead lease nonce) or tracked tasks may
be reaped. The next user message or next 15-minute arm/timeout should
claim and re-arm. Surface a deaf controller as roster `wake unarmed`
(`wake_armed` / `wake_covered` / `supervisor` absent). Do not invent a
second reminder channel.

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

Bare `./install.sh grok-bot` applies the default gstack and autoreview addons; pass `--addons ''` to skip them.

The checked-in destination is the Grok Bot workflows library
(`/home/box/agent-data/workflows/goal-flight/SKILL.md` on the bot machine).
The default workflows root is that box path. From a Mac checkout pass
`GOALFLIGHT_GROK_BOT_WORKFLOWS` or `./install.sh grok-bot <workflows-root>`.
This repo does not invent a second Mac default library root. Do not assume
`~/.grok/skills/` (that is Grok CLI).

Doctor's `installed_skill_drift` for grok-bot hashes the box library (or
the override). A Mac `doctor --project-root` without the override will
not see `/home/box/...`; run the drift check on the box, or set
`GOALFLIGHT_GROK_BOT_WORKFLOWS`.
