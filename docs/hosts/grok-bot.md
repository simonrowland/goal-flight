# Grok Bot Notes

Run from your Goal Flight clone or the pinned skill checkout (default
`~/.goal-flight/skill/`; see README). Live controllers load that pin, not a
live source tree.

Grok Bot can run Goal Flight as an orchestrator through the installed skill
wrapper. It does **not** replace the Grok CLI adapter (`adapters/grok.json`,
`grok agent stdio`). CLI workers, the task store, and RAG stay on the portable
core.

One-shot install (wrapper copy into the Grok Bot workflows library):

```bash
./install.sh grok-bot
# same as: ./setup.sh --apply --yes --grok-bot

# when the workflows library root is not the bot-box default:
./install.sh grok-bot /home/box/agent-data/workflows
# or: GOALFLIGHT_GROK_BOT_WORKFLOWS=/path/to/workflows ./install.sh grok-bot
```

## Install destination (verified, with uncertainty)

User-created Grok Bot skills live as `SKILL.md` under the Grok Bot workflows
library. On the bot machine the verified path is:

`/home/box/agent-data/workflows/<slug>/SKILL.md`

Goal Flight copies this wrapper to
`/home/box/agent-data/workflows/goal-flight/SKILL.md` unless
`GOALFLIGHT_GROK_BOT_WORKFLOWS` (or the optional `install.sh grok-bot <root>`
argument) points at a different library root.

That box path is the least-wrong destination. It is **not** `~/.grok/skills/`
(Grok CLI) and not `~/.cursor/skills/` (Cursor Agent). A Mac checkout that
cannot see `/home/box/...` must pass the override; this repo does not invent a
second default Mac path.

Doctor's `installed_skill_drift` hashes the installed wrapper against
`configs/grok-bot/skills/goal-flight/SKILL.md`. A WARN means resync with
`./install.sh grok-bot`.

## Resync after SKILL.md changes

When the source Goal Flight repo's `SKILL.md` or tracked files under
`commands/`, `protocols/`, `templates/`, or `adapters/` change, Grok Bot's
installed copy is not auto-synced unless it is a symlink to the source repo.
Resync from the source repo (or the pinned `~/.goal-flight/skill/`) with
`./install.sh grok-bot`. To check for drift, run
`python3 scripts/goalflight_doctor.py --project-root "$PWD" --json` and inspect
`installed_skill_drift`; text mode prints `installed_skill_md_hash` WARNs.

## How a Grok Bot controller operates

Load order: `AGENTS.md` → this host wrapper (Freshness: disk-read
`SKILL.md` Hard Invariants; if you cannot quote them, resume before
acting) → repository `SKILL.md` → `protocols/guidance-extended.md` by
default (non-frontier) → newest `docs-private/RESUME-NOTES-*.md` when
resuming.

- Facts come from `goalflight_status.py`, `goalflight_task.py list/next`, and
  doctor. Conversation is not the backlog.
- Controller label is `goalflight-grokbot`. Stamp it on every dispatch.
- Dispatch CLI workers on the user's registered computer (the Mac checkout)
  via Grok Bot Shell with a `machineId`. Do not spawn CLI workers on the
  Grok Bot box and do not clone the repo onto the box. Task / executor
  subagents start on the box and must be pinned to that Mac in the prompt
  (see Executor context package).
- Wrap the existing CLIs: `goalflight_dispatch.py`, `goalflight_messages.py`,
  `goalflight_task.py`, status, doctor. Do not add a parallel store.
- Grok Bot Task / executor subagents are a **first-class** host `delegate`
  surface, distinct from CLI ACP / bash-tail workers. They start blank;
  judgment-bearing Task prompts reuse the Claude host-subagent pin (see
  Executor context package). Choose Executor vs CLI from token/budget and
  task shape. Do **not** apply the Claude-host "Host Agent as code executor
  = LAST RESORT" rule here.
- Code-writing: CLI (`--agent codex`, `--agent grok-code`, `--agent moonshot`)
  when capacity is healthy; Executor when that lane is cheaper/available or
  the chunk is host-native (recon, synthesis, mail, status).
- Reviews: kimi3 or codex as independent reviewers. kimi3 is
  `--agent moonshot` (`adapters/moonshot.json`, default model `kimi-code/k3`).
  Do not invent a `kimi3` agent id. `forbid_self_review` still applies: a
  Grok Bot controller must not Type-1-review a `grok-code` worker.
- Grok Bot Cloud Agents = optional extra worker for GitHub-branch / PR chunks
  only; never the default for a local scientific-coding project.
- Do not pin Grok model ids.
- Grok Bot has no `/compact` and no keep-vs-toss prompt. Autocompact +
  keep handoff current. The operator is not the compaction mailman.
  Do not port the Claude context-meter or SessionStart hooks. Write the
  `state-handoff.md` Before compact or sleep block on every listen
  ring/timeout and before long waves, then quote-check Hard Invariants
  from disk. Do not ask the user to compact. The one operator
  wake-hygiene job is re-arming listen doorbells after a host update or
  token-pause, surfaced as roster `wake unarmed` — see Operator role.

## Executor context package

Grok Bot Task / executor subagents start **blank**: no chat, no memory, no
SKILL.md, and none of the controller conversation. Reuse the existing
Claude host-subagent pin. Do not invent a parallel package format. Do not
add a grok-executor template — Cursor and Grok CLI wrappers have none.

CLI workers keep the existing five-layer `--prompt-file` dispatch
(compose via `prompts/dispatch-wrapper.md`; do not paste that recipe
file into a Task). Executors get **that same composed prompt-file body**
in the Task prompt (paste it, or cite its stable Mac path).

Compose every judgment-bearing grok-executor Task prompt in this order:

1. Open with `protocols/subagent-preamble.md` verbatim. Fill only
   repository path + north star. Repository path is the Mac checkout,
   e.g. `/Users/simonrowland/Repos/<project>`.
2. Immediately after the preamble, the grok-bot-only pin: executors
   default to the Grok Bot computer, **not** the user's Mac. Name
   `machineId` / "the user's registered computer" and absolute paths
   under `/Users/simonrowland/Repos/...`. Every Read and Shell —
   including the preamble's AGENTS.md / ORIENTATION.md reads — uses
   `machineId`. Do not clone the repo onto the box.
3. Always pointer to `docs-private/rag/ORIENTATION.md` when present
   (orientation only).
4. If the lane is triggered, apply `protocols/worker-context-package.md`
   in full: verbatim brief prepend (never link-instead-of-paste), quoted
   ground-truth spec, named guard tests, standing re-read. Then the
   `--prompt-file` equivalent: the same five-layer body CLI would get.
   Citing a Mac path is allowed for that composed prompt file; it does
   not replace the verbatim brief or quoted spec.

Live Codex dispatch on the user's Mac is a host smoke, not part of this
port's hermetic coverage.

## Wake path

How a Grok Bot controller learns a worker finished: use the existing
journal doorbell (`listen`), not a new event bus and not a Settings widget.

**Journal is durable truth.** Worker terminals are the markers in
`protocols/worker-markers.md`: `!COMPLETE` / `!READY` / `!FAILED` /
`!BLOCKED` / `!USER-NEED` (leading `!` optional). `!COMPLETE` is the
success terminal. The watcher already harvests those into the
terminal-outbox / controller mail.

**Host monitor is exit-as-wake.** Grok Bot revives the parent chat when a
Task / executor or a tracked Shell completes. That is the same contract as
`goalflight_messages.py listen`, not Claude's persistent line-break
`supervise` / `follow`. There is no separate Settings "monitor widget"; the
running background task / subagent card is the UI.

**Portable listen / heartbeat pool.** Use the existing listen doorbells.
This host's frontier ping is `listen --timeout-s 900` (15 minutes of
quiet): the timeout exit reminds the controller of the task frontier so
it does not stall. That is **not** Claude's follow-stream heartbeat
(120s; the stream range is 60–300s and would spam Grok Bot turns). Do
not change the global `supervise` / `follow` cadence in this port; 900s
is a grok-bot wrapper-local default on the listen arm.

**Canonical grok-bot arm** — one tracked
`goalflight_grok_bot_listen.py --report-pending --timeout-s 900` on the
**user's Mac** (the project checkout), with `--project-root` and
`--controller-label goalflight-grokbot`. That helper wraps
`listen --report-pending` and prints the quote-check banner on every
exit. Never detach it (`nohup`,
`&`, disown): a detached listener refuses with **exit 4** and cannot
wake an untracked parent.

```bash
python3 <skill-root>/scripts/goalflight_grok_bot_listen.py \
  --project-root "$PWD" \
  --controller-label goalflight-grokbot \
  --report-pending \
  --timeout-s 900
```

Every ring (**exit 0**) or timeout (**exit 1**) is a **mini-resume**:
drain if it rang (`relay --drain`, act), flush RESUME-NOTES (Compaction
below), quote-check Hard Invariants from a fresh disk read, re-arm,
then `goalflight_task.py next`. The 900s quiet timeout is this host's
substitute for Claude's 80% context-meter hint. The portable
four-listen pool is full depth
(resilience; see `protocols/controller-mail.md`). One listen is the MVP.
If you arm more than one listen, put `--timeout-s 900` on a single slot
and leave the others at `--timeout-s 0` so four quiet timeouts do not
fire together. Branch on listen exit codes instead of blindly
restarting:

| Code | Meaning for this host | Action |
|---:|---|---|
| 0 | Ring | Drain, act, handoff write, quote-check, re-arm, task next |
| 1 | 15-minute frontier ping (quiet timeout) | Handoff write, quote-check, re-arm, task next |
| 2 | Journal unreadability | Repair/escalate; do not restart-loop |
| 3 | Contention / stale lease | Reconcile the live lease; re-arm only under it |
| 4 | Detached refusal (`nohup` / `&` / disown) | Launch a tracked listener; do not detach again |
| 5 | Did-not-arm (dead nonce) | Do not re-arm that nonce |

**Do not arm unbounded `supervise` as a Grok Bot background shell** expecting
per-line wakes. Grok Bot only notifies on job completion. If you add a
helper, it must wrap `listen` or exit on the first actionable event
(ignore stream heartbeat / frontier keepalives).

A same-turn regex-wait on a live `supervise` process (host AwaitShell-style
blocking) is a same-turn block, not a session-life monitor. Do not recommend
it as the default.

**Missed wake is latency, not data loss.** Resume still pulls
`goalflight_status.py`, `goalflight_task.py next`, and `relay --new`.

This port documents the listen doorbell mapping only. Do not implement a Grok
Bot-native mail transport.

## Compaction

Grok Bot = **autocompact + keep handoff current**. Do **not** port
`scripts/hooks/goalflight-context-meter.sh` or Claude PostToolUse
`additionalContext` 80/90/95% bands. That hook is Claude-Code-specific.
Grok Bot has no PostToolUse `additionalContext` injection and no
trustworthy window %. A fake meter is worse than none. Claude
context-meter and SessionStart watchdog stay Claude-only. There is no
SessionStart hook on grok-bot.

Claude's directed compact is a controller-authored keep-vs-toss prompt,
then `/compact`, then `/goal-flight resume`. Grok Bot has **no**
controller-authored `/compact` and **no** keep-vs-toss prompt. The host
may summarize the chat on its own. Do not invent a compact UI.
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

1. Wrapper Freshness preamble (always loaded): disk-read SKILL.md Hard
   Invariants; if you cannot quote them, stale → resume before acting.
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
**fresh disk read**, reload `AGENTS.md` → grok-bot wrapper → repository
`SKILL.md` → `commands/resume.md`. Then:

1. `goalflight_session_status.py --text`
2. **Skip** `commands/resume.md` STEP 1.5 (`supervise`). Arm listen
   doorbells (portable pool) instead.
3. store baseline (`goalflight_status.py` + `goalflight_task.py list`)
4. newest RESUME-NOTES
5. `goalflight_task.py next`

## Operator role

The operator is **not** the compaction mailman. No `/compact`, no
keep-vs-toss ask. Autocompact plus the 15-minute doorbell mini-resume
(flush RESUME-NOTES, quote-check Hard Invariants from disk, task next)
owns that job. Do not ask the user to manage compactions.

The operator **is** allowed one wake-hygiene job, the same one Claude
already has: after a host update or token-exhaustion pause, re-arm the
listen doorbells (Claude re-arms Monitor / `supervise`). Surface that
via roster fields already measured — `wake_armed`, `wake_covered`,
`supervisor` absent — the same signal as a battery-engine
`wake unarmed` note. `goalflight_session_status.py` already prints
`wake unarmed` / `wake unarmed with N non-terminal dispatches`. Do not
invent a second reminder channel.

On grok-bot the listen pool is the arm. `supervisor: absent` is expected
when no `supervise` process is running; that alone is not the alarm.
The operator-facing alarm is `wake unarmed` (`wake_armed` false, usually
with `wake_covered` false).

A token-pause or host update typically shows up as listen **exit 5**
(dead lease nonce) or as reaped tracked tasks. The next user message, or
the next 15-minute listen arm/timeout attempt, should claim and re-arm.
Do not wait for the human to remember compaction.

The operator is the **former mailman**. Do not ask them to paste messages
between controllers or to tell another session to check mail. Deafness
is re-arm listen, not user-as-mailman.

## Multi-controller mail

Inter-controller traffic is **only**
`goalflight_messages.py post --to-controller`. Grok-bot controllers join
that journal. Do not ask the user to paste mail, and do not ask them to
tell another session to check mail.

SendToAgent (Grok Bot teammate DMs) is an optional extra ping among
grok-bot agents. It is never the work inbox and never the bridge to
Claude `battery-*` controllers.

Deafness is re-arm listen, not user-as-mailman. A controller that is not
seeing mail has an unarmed or dead doorbell (`wake unarmed`); re-arm
it. Do not route the missing wake through the operator.

Five-controller setups still use `post --to-controller` plus the portable
listen pool. That layout is a project convention (lane claims, one integrator,
cluster dispatch), not a Goal Flight primitive. A Grok Bot controller joining
such a project must claim `goalflight-grokbot` (or another unused label)
and stamp that claimed label with `--controller-pid
--controller-session-id` on every dispatch, and never steal the existing
leases. Read
`docs-private/MULTI-CONTROLLER-ETIQUETTE.md` when the project has one.

## Advanced setup

Dry-run first, omit `--apply --yes`:

```bash
./setup.sh --grok-bot
./setup.sh --agent grok-bot --addons ''
```

After setup, run Goal Flight init in the target project on the Mac checkout.
Init runs doctor, capacity checks, worker readiness checks, and writes compact
project-local caveats.
