---
description: "Resume from git state, status JSON, and dispatch ledger."
---

# resume

Rebuild working context from files and procedural status.

Applies only when Goal Flight was already in play (see `protocols/state-handoff.md`).
Read and run `protocols/session-preflight.md` as the controller-start hook before
rebuilding status.

## STEP 0 — Load the skill body FIRST (unconditional; do not skip)

Before running any step below, read the repository `SKILL.md` **end-to-end**, plus
the protocols it references for the work you are about to do. This is not optional
and not compaction-only:

- `/goal-flight resume` may load only this command body, not the full skill.
- After a compaction the `SKILL.md` already in your context is frequently STALE or
  truncated: system reminders silently drop load-bearing rules across compactions.
- Resuming from RESUME-NOTES + the resume args + your own judgment, WITHOUT the
  loaded skill body, is the documented failure mode. Controllers then improvise the
  practice instead of following it and drift into the known anti-patterns:
  - the host Agent/Task tool as a code executor instead of `goalflight_dispatch.py`;
  - engagement-question boxes over obvious matters ("I found a problem: fix it?" —
    the forbidden "are-you-still-there" pattern); just act, per §Autonomous throughput;
  - `spawn_task` chips for findings a worker could do autonomously, instead of queue-backlog worker tasks.

**Self-test:** if you cannot quote `SKILL.md`'s Hard Invariants and Dispatch Model
from what is currently loaded, you have NOT loaded it.
- Native (Claude Code): the body loads on `/goal-flight`. Confirm it is present and
  fresh by quoting one Hard Invariant; if you cannot, re-invoke `/goal-flight`.
- Non-native hosts (codex / grok / cursor / opencode): read your installed host
  wrapper, then `<skill-root>/SKILL.md` end-to-end from disk.

Do not act on any resume state until STEP 0 is satisfied.

## STEP 1 — Reload order + handoff

Follow `AGENTS.md`, then the canonical post-compaction reload order in `SKILL.md`
and `protocols/state-handoff.md`: session-status verdict, `SKILL.md` end-to-end,
named controller registration from `protocols/session-preflight.md`, store
baseline, handoff prose, status, `next`, then continue the top task without
waiting for a re-prompt when no real blocker exists.

The resume entry auto-claims/renews only the controller role's journal lease. Carry
its label. Listener, drainer, mirror, and dashboard children never claim during
resume; a verified watchdog tick may renew the controller lease.

### `label in use` — adopt by default; defer only on PROVEN live competition

A returning session normally finds its own label held by its own dead
predecessor. That is the common case, not a conflict. **Do not stall the resume
on it.** Take over unless you can prove, at this instant, that the holder is
both alive AND a different session:

1. `kill -0 <holder_pid>` — if the pid is gone, **take over immediately**.
2. If it is alive, read its elapsed time and ancestry with
   `ps -o pid=,ppid=,etime=,args= -p <holder_pid>`. **`etime` is
   `[[dd-]hh:]mm:ss` — `01:39` is one minute thirty-nine seconds, not an hour
   and a half.** Misreading it has caused a controller to defer to a process
   that had just started.
3. Walk both ancestries. A holder that shares your host-app ancestor is a
   sibling of your own launch, not a competing operator. **Take over.**
4. Only when the holder is alive, and rooted in a genuinely different session,
   surface it to the owner and ask before `--takeover`.

The cost is asymmetric and that is why the default is adopt: taking over a dead
or sibling holder costs one generation, while deferring wrongly leaves the
project with no controller at all and the owner waiting.

## STEP 1.5 — ARM THE WAKE BEFORE YOU REBUILD ANYTHING

**Do this immediately after the lease claim, before status, before reading
notes, before any measurement.** A controller without a wake is deaf: worker
results land in the journal and nothing tells it. Every minute spent
"orienting" first is a minute of missed events.

Arm ONE `supervise` process through the host's persistent monitor — never a
bounded monitor, never a shell `&`, never per-component listeners when a
supervisor is available:

```bash
python3 <skill-root>/scripts/goalflight_messages.py supervise \
  --project-root "$PWD" --controller-label <label> --lease-nonce <token>
```

On Claude Code that is the Monitor tool with `persistent: true` and **no
timeout you reason about** — a bounded monitor is killed outside the
supervisor, so no `type=stop` record appears and the controller goes deaf with
no diagnostic.

Confirm it is actually armed before moving on: the supervisor's first write is
a `{"kind":"supervise","type":"probe","reason":"stdout-peer-liveness"}` record.
If `--list-controllers` still reports `supervisor: absent` after arming, treat
that as a blocker and resolve it — do not proceed to STEP 2 deaf.

Stop any pre-existing direct listeners first; a supervisor plus loose listeners
double-deliver.

## STEP 2 — Rebuild status

```bash
python3 <skill-root>/scripts/goalflight_status.py
python3 <skill-root>/goalflight_task.py list outstanding
python3 <skill-root>/goalflight_task.py next
git status --short
git log -1 --oneline
```

Then summarize:

- current branch/head/dirty state
- active dispatches and classifications
- capacity cooldowns
- store baseline (`list outstanding`, plus `list deferred` / `list held` when relevant)
- `CONTINUE:` directive from `goalflight_task.py next`
- first safe command to run next

Do not reconstruct state from raw worker logs when status JSON exists.

If git fetch/reconcile is blocked by stale local dispatch refs, inspect with
`python3 <skill-root>/scripts/goalflight_cleanup_dispatch_refs.py --dry-run --json`
before deleting refs.
