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
- Dispatch CLI workers on the user's registered computer (the Mac checkout)
  via Grok Bot Shell with a `machineId`. Do not spawn workers on the Grok Bot
  box and do not clone the repo onto the box.
- Wrap the existing CLIs: `goalflight_dispatch.py`, `goalflight_messages.py`,
  `goalflight_task.py`, status, doctor. Do not add a parallel store.
- Grok Bot Task / executor subagents are a **first-class** host `delegate`
  surface, distinct from CLI ACP / bash-tail workers. Choose Executor vs CLI
  from token/budget and task shape. Do **not** apply the Claude-host
  "Host Agent as code executor = LAST RESORT" rule here.
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

## Wake path

`supervise` needs a host monitor where each flushed stdout line is a
controller notification, unbounded, `persistent: true`. Grok Bot does not
have Claude Code's persistent stdout monitor.

Documented wake path: the portable `goalflight_messages.py listen` doorbell
(exit-as-wake). On each ring, process mail, then restore listen depth. Do not
implement a Grok Bot-native mail transport.

Grok Bot may revive the parent when a background Task / Executor finishes.
That helps host-native Executor work. It is not a substitute for listen
doorbells.

## Multi-controller mail

SendToAgent / named teammate agents are an extra ping between Grok Bot
controllers that share one project. Controller-mail remains the work plane
(`goalflight_messages.py`: leases, journal, `post --to-controller`,
merge-request / patch).

Five-controller setups still use `post --to-controller` plus the portable
listen pool. That layout is a project convention (lane claims, one integrator,
cluster dispatch), not a Goal Flight primitive. A Grok Bot controller joining
such a project must claim its own label, stamp
`--controller-label --controller-pid --controller-session-id` on every
dispatch, and never steal the existing leases. Read
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
