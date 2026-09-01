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

Load order: `AGENTS.md` → this host wrapper → repository `SKILL.md` →
`protocols/guidance-extended.md` by default (non-frontier) → newest
`docs-private/RESUME-NOTES-*.md` when resuming.

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

**Canonical grok-bot arm** — one tracked `listen --report-pending
--timeout-s 900` on the **user's Mac** (the project checkout), with
`--project-root` and `--controller-label goalflight-grokbot`. Never
detach it (`nohup`, `&`, disown): a detached listener refuses with
**exit 4** and cannot wake an untracked parent.

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" \
  --controller-label goalflight-grokbot \
  --report-pending \
  --timeout-s 900
```

On ring (**exit 0**): drain with `relay --drain`, act, re-arm. On
timeout (**exit 1**): pull `goalflight_status.py` and
`goalflight_task.py next`, then re-arm — that exit *is* the 15-minute
frontier reminder. The portable four-listen pool is full depth
(resilience; see `protocols/controller-mail.md`). One listen is the MVP.
If you arm more than one listen, put `--timeout-s 900` on a single slot
and leave the others at `--timeout-s 0` so four quiet timeouts do not
fire together. Branch on listen exit codes instead of blindly
restarting:

| Code | Meaning for this host | Action |
|---:|---|---|
| 0 | Ring | `relay --drain`, act, re-arm |
| 1 | 15-minute frontier ping (quiet timeout) | Pull status + task next, re-arm |
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

## Multi-controller mail

SendToAgent / named teammate agents are an extra ping between Grok Bot
controllers that share one project. Controller-mail remains the work plane
(`goalflight_messages.py`: leases, journal, `post --to-controller`,
merge-request / patch).

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
