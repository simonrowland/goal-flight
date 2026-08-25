# Event architecture

**Status: living document.** Written 2026-08-21 after a week of wake-layer
defects. It records what the system *is*, what is *provably broken*, and the
invariants any fix must preserve. Iterate on it; do not let it drift from the
code.

Every claim below is marked **measured** (observed on a live box) or
**inferred**. That distinction is load-bearing: three confident causal stories
were wrong in a single day, so the doc separates what we saw from what we
concluded.

---

## 1. What the system is for

A controller dispatches workers, then must learn when they finish — without
polling, and without a human watching. Three concerns are separable and are
frequently conflated:

| concern | question it answers | carrier |
|---|---|---|
| **Delivery** | did the event survive? | the journal (durable) |
| **Promptness** | how soon does the controller learn? | the wake layer |
| **Payload** | what does the controller learn? | mail rendering |

**The single most useful idea in this document:** delivery is durable, so a
controller that is not listening loses *latency*, not *events*. Much of the
week's anxiety came from treating a lapsed wake as lost work. It is not.

---

## 2. Layers

### 2.1 Journal — durable truth

Per-project SQLite/WAL. Holds `dispatch_attempts`, `terminal_outbox`,
`delivery_events`, `controller_leases`, `controller_cursors`,
`listener_coverage`.

**Invariants**

- **The journal is the only authority on what happened.** Status files under
  `/tmp` are volatile and may be reaped; anything that must survive a reboot
  belongs here. *(Violated today: the only pointer to a dispatch's brief is a
  `/tmp` path — see §6, t-290.)*
- **Nothing may mark an event consumed except a consumer that acted on it**,
  and the advancing writer must be recorded (`advanced_by`).
- **A default value must never be readable as a verdict.** `backlog_pending`
  defaulted to `0`, was only ever written as `0`, and was read as "consumed".
  A field whose default coincides with its "done" value cannot distinguish
  done from untouched. **measured**

### 2.2 Wake layer — promptness only

Two mechanisms, and the difference matters more than it looks.

**Doorbells (`listen`)** — host-agnostic. Every controller has these.
A doorbell **wakes by exiting**: the host surfaces the task's completion, and
that *is* the notification.

Consequences that follow inescapably from exit-as-wake:

- a doorbell is **one-shot**; depth decays on every fire
- it must be a **tracked** task of the controller's host; a detached listener
  refuses with *"my exit wakes nobody"* and exits `4` **measured**
- therefore it **cannot outlive the controller's session**

**Persistent monitor** — host-specific (Claude Code today). Streams events;
each line is a wake. Survives firing; needs no re-arming.

**Measured, and decisive:** on Claude Code the harness reaps its own tracked
background tasks — 149 exit-144 events since 2026-06-13, across many task
kinds, not only listeners; detached processes survive the same wipes. So on
that host the doorbell pool is caught between two failure modes — tracked and
reaped, or detached and mute — and **neither is a goal-flight bug**. The
persistent monitor is the escape, and should be the default wherever the host
provides one.

**Invariants**

- Liveness comes from **kernel slot locks**, never from `listener_coverage`.
  Coverage is a single-row audit surface; peers legitimately supersede it as
  they arm, so three of four rows reading `superseded` is *correct*. **measured**
- A wake mechanism that dies must **die loudly** — naming who killed it and
  releasing its slot. A silent death is indistinguishable from health, and cost
  two controllers hours this week. **measured**
- Depth is a **latency** control, not a correctness one. Never let a depth
  hint imply data loss.

### 2.3 Mail — payload

`relay --drain` renders what arrived.

**Invariants**

- **Only print "no mail" after actually looking.** *"I don't know who you are"*
  and *"you have nothing"* are different answers; collapsing them made a
  controller idle for an hour beside 105 unread events. **measured**
- **Never resolve a recipient by guessing.** A repo-directory-name fallback
  peeked the `pm2` mailbox for controller `pm2-main` and truthfully reported it
  empty. **measured**
- **Carry the work, not a pointer to it.** Terminal mail shows the worker's own
  headline; the state string is the fallback. Actionable types
  (`merge-request`, `patch`, `finding`, `controller-question`, `user_need`)
  sort above bare state changes — and nothing is hidden, only ordered.
- **Guidance belongs on the line the reader is already looking at**, in one
  line. Two separate warnings were invisible this week because they sat below
  where readers cut with `tail -n`. **measured**

---

## 3. Identity and ownership — **persist or refuse**

**Inferred implementation invariant:** dispatch ownership is the exact **nonce +
PID + label** of a kernel-lock-live controller lease. The dispatcher re-resolves
that triple at attempt preparation; `--controller-label` alone is not ownership.

**Inferred implementation invariant:** a plain dispatch or resume needs no new
ownership flags when exactly one kernel-live project controller is proven to be
in the invocation's PID-and-start-token ancestry. The dispatcher reads that
lease's nonce and records its full owner triple. An explicit
`--controller-label` plus `--controller-pid` likewise resolves the matching live
nonce only when the holder is in that ancestry. Zero matches, several matches,
or a sole unrelated live holder refuse rather than guessing by proximity.

**Inferred implementation invariant:** an unresolved controller now refuses
before worker launch or capacity leasing. With no kernel-live controller, the
refusal prints the advertised-install registration command:

```shell
python3 <advertised-skill>/scripts/goalflight_session_status.py --controller-startup --controller-pid-from-ancestry
```

**Inferred implementation invariant:** `--unregistered-forced` is the deliberate
escape hatch. It launches with the same NULL owner used by the legacy path and
prints the warning to stderr (and the dispatch tail where one exists), making
the resulting project-wide terminal-event fanout visible. Deliberate broadcast
remains a separate supported operation.

**Measured:** 421 of 1,704 observed dispatch rows were owned, and an owned
dispatch's terminal event fanned out to exactly one recipient. **Inferred from
code and journal rows:** the legacy failure was silent NULL ownership: when the
write path could not resolve the controller triple, the read path treated NULL
as "wake every SQL-ACTIVE controller label in this project." One ownerless
dispatch therefore spent a one-shot doorbell for every recipient, including
stale SQL lease rows.

**Invariants any fix must satisfy**

- **A flag that cannot be honoured must fail loudly.** A silently ignored
  `--controller-label` is worse than no flag: it convinced this controller it
  had prevented a fanout it had not.
- **Teach at dispatch time**, because mail provably cannot reach an
  unregistered controller. The dispatch line is the only channel that reaches
  them.
- **Never displace a live holder.** Adopting a session into a healthy
  controller's identity is worse than refusing.
- Deliberate broadcast must remain possible — a shared sweep worker may
  legitimately address everyone. Suppressing all fanout would recreate the class
  of bug where events exist and reach nobody.

---

## 4. Recovery

- **Resume** re-uses the engine session id, so a worker can be corrected,
  nudged, or survive a restart as one continuous session. Every worker CLI in
  the fleet supports it.
- The Goal Flight launch id is new per spawn (new process, lease, status file);
  `parent_dispatch_id` links them. The *conversation* is what is preserved.
- **A resumed worker re-reads its brief, and that file outranks its summarised
  memory.** Therefore the brief pointer must be durable — currently it is not
  (§6).
- Refusals must name the real obstacle (*"no recorded session handle"*), never a
  broad category (*"not a codex dispatch"*).

---

## 5. Cross-cutting principle: **records that lie**

The dominant defect class this week was not missing functionality. It was
**state recorded that did not match reality, then acted upon**:

| record | read as | actually |
|---|---|---|
| stale lease | live controller | holder dead ~2 days |
| `listener_coverage: superseded` | listener died | expected audit bookkeeping |
| `idle_timeout` | worker exited | worker still running |
| `backlog_pending: 0` | mail consumed | default, never written |
| terminal verdict | work lost | work complete, marker unharvested |
| unresolved controller (legacy) | ownership recorded | silently NULL and fanned out |

**Design rule:** a field must be able to distinguish *unset* from *asserted*,
and a verdict must name the evidence it rests on. When a record and the live
system disagree, prefer the live check and record the disagreement.

---

## 6. Known broken (open work)

- **t-290** — a resumed worker cannot find its brief after a reboot: no prompt
  columns in `dispatch_attempts`; the only pointer is `/tmp`. Store path **and
  content hash** durably.
- **b-174** — ownership discarded → universal fanout (§3).
- **t-293** — teach at dispatch time when ownership will not resolve.
- **b-176** — listeners die silently at exit 144; cause is the host's task
  reaper, so the fix is legibility, not survival.
- **b-173 / b-020** — quiet treated as terminal; workers outlive their verdict
  and accumulate. A reaper needs **both** facts: owning dispatch terminal **and**
  process idle.
- **b-167** — a uniform finite listener timeout blanks a whole pool at once.

---

## 7. What has not been measured

Named because their absence shapes the priority order:

- **Work actually lost** to a missed wake, as distinct from latency added.
  Nobody has counted it. If the true cost is minutes of latency, this queue's
  ordering is wrong.
- Whether non-Claude hosts suffer the tracked-task reap. Detached processes
  survived the wipes, which suggests not — one observation, not a proof.
- How ownership rates changed over time before the measured 421-of-1,704
  snapshot. Ownership was populated; its historical trend remains unmeasured.

---

## 8. Open design decision: mail is fleet-scoped, dispatch state is not

**Operator, 2026-08-21:** *"the mail queue should be centralized not between a
million worktrees."*

The current design is **inconsistent with itself**, and the numbers show it:

| artefact | scope | count |
|---|---|---|
| message envelopes (`~/.goal-flight/messages/*.jsonl`) | **fleet-global** | 3,773 files |
| delivery events (the wake path) | **per project journal** | 4,582 rows across **37** journals |

Message *content* is already centralized. Only the *wake path* is fragmented.
Mail did not choose project scoping — it inherited it by living in a journal
built for dispatch state. That is an accident of colocation.

**Why it hurts, all measured:**

- A worktree mints a new journal, so it mints a new mail universe.
  `bt-pins` is a live example: one delivery event, one controller, invisible to
  the four controllers in the parent project's journal.
- Cross-project mail lands in the **sender's** journal. Two replies from
  `goal-flight` to `kiln-main` are sitting undelivered right now, including a
  correction kiln is currently acting against.
- `--project-root` resolves from CWD, so **where a controller happens to stand
  decides which mail universe it lives in.**
- One journal has no `controller_leases` table at all — an older-schema island
  that cannot participate.

**The distinction that resolves it:**

- **Fleet-scoped** — who exists, who is live, and messages *between* controllers.
  These are inter-controller concerns and must not be partitioned by directory.
- **Project-scoped** — dispatch attempts, capacity leases, cursors, listener
  coverage. These are genuinely per-project and should stay put.

**Direction (operator-decided 2026-08-21):** the mail queue belongs in the
**local install folder** — `~/.goal-flight/` — not in any project journal and
never in a worktree. The envelopes are already there
(`~/.goal-flight/messages/`); the delivery/wake state should sit beside them,
fleet-scoped and install-local.

That gives a clean split of homes:

| state | home | scope |
|---|---|---|
| envelopes, delivery/wake, controller registry | `~/.goal-flight/` | **fleet** |
| dispatch attempts, capacity leases, cursors, listener coverage | `~/.local/state/goal-flight/journals/<project>/` | **project** |

The invariant this buys: **a message reaches the recipient's wake path
regardless of where either party is standing** — no cwd, worktree, clone, or
choice of tooling install can partition it. Routing is by **recipient**, which
the envelope already records.

**Do not centralize the whole journal.** Dispatch state is legitimately
per-project, and a single global SQLite would concentrate the contention this
system already struggles with (b-165 originally recorded reads bouncing rather
than waiting; bounded read retries now classify that contention explicitly).

**Constraint on any fix:** deliberate broadcast must survive. A shared sweep
worker may legitimately address everyone — the goal is that *unintended* fanout
stops, not that fanout becomes impossible.

---

## 9. Decided direction: one persistent listener, and the heartbeat that proves it

**Operator, 2026-08-21:** *"one persistent line-break-wakeup listener is the
way."*

Everything in §2.2 describes a wake layer built around **exit-as-wake**, and
almost every open defect in §6 is downstream of that one choice. This section
records the replacement.

### The shape

A single long-lived listener writes lines to stdout; the host surfaces each line
as a wake. Three line kinds, structurally distinguishable:

| line | meaning | wakes with |
|---|---|---|
| **event** | mail arrived | the payload |
| **heartbeat** | nothing arrived | proof the channel is open |
| **frontier** | periodic | a non-authoritative materialized projection of the store frontier |

### What it deletes

Exit-as-wake forces every doorbell to be one-shot, which forces a **pool**,
which forces depth accounting, re-arming every turn, and a separate liveness
probe. None of those are requirements — they are consequences. A persistent
wake removes all four. **The cap was never the problem; the pool was.** Raising
`--listener-slots` tunes a symptom.

It also subsumes the `/loop` idle timer. A timer wakes on the clock; a heartbeat
wakes on the clock *and* proves the channel is open while doing it.

### Bidirectional liveness — the load-bearing idea

The same write proves liveness in both directions:

- heartbeat **arrives** → the listener is alive, proven to the controller
- heartbeat **write succeeds** → the controller is alive, proven to the listener
- write fails `EPIPE` → the controller is gone; the listener exits and releases
  its slot rather than lingering
- heartbeats **stop arriving** → the listener is gone; the controller re-arms

**This dissolves b-176 rather than fixing it.** That defect is that the
dropped-pipe → `orphaned` path may fire on *quiet* instead of a genuinely closed
fd. Its root cause is that a listener with no mail never writes, so it has
nothing to discover a closed pipe *with* — quiet and dead are indistinguishable.
A heartbeat means there is no such thing as a legitimately quiet listener:
every interval produces a write, so `EPIPE` becomes evidence rather than an
inference from silence. **measured**: this is the same mechanism the operator
identified as *"controller is running vs claude app crashed"*.

It likewise removes b-167 (a uniform timeout blanking a whole pool at once) —
with one listener there is no pool to blank — and supplies the orphan reaper
(b-020/b-173) its missing second fact, since a listener that self-exits on
`EPIPE` stops being an orphan at the source.

### The case neither side can catch alone

If compaction leaves the process running but the host stops **surfacing** its
lines, the write still succeeds while nobody is semantically listening. The
listener cannot detect this — its pipe is fine. It collapses into the
backup supervisor's side: durable successful-record time stops advancing, and
missing heartbeats read as death. **The independently tracked watchdog timeout
therefore stays load-bearing** and must not be dropped merely because the
listener has `EPIPE` detection. The supervisor emits `listener-dead` on its
waking stdout/exit channel; stderr is not a substitute.

### Invariants this must satisfy

- **A missing heartbeat reads as death, loudly.** The entire value is that
  silence stops being ambiguous. A stream that dies quietly is strictly worse
  than a doorbell that dies, because a doorbell's exit is itself a signal.
- **Heartbeat and frontier lines are structurally tagged**, never distinguished
  by parsing prose. A controller that mistakes a heartbeat for work burns a turn
  on nothing — the "are you still there?" anti-pattern, self-inflicted.
- **The frontier line is information, not instruction.** A controller mid-chunk
  must not pivot because a heartbeat named a different `next`; an explicit
  user-directed mission already outranks the store frontier.
- **Do not repeat an unchanged frontier every beat.** Noise trains controllers
  to ignore the channel — the same way a false `MAIL-ERROR` (b-184) trains them
  to ignore real delivery failures.
- **Cadence is derived, not guessed.** Death is detected within roughly one
  interval, so choose the interval from how long a controller may acceptably be
  deaf without knowing it, and record the reasoning.

### What stays

Doorbells remain the portable path for hosts with no persistent monitor
(codex, grok, cursor, opencode). Both paths share the journal as durable truth,
so a missed wake costs **latency, not events** — which is what makes running two
mechanisms safe rather than merely redundant.

**Persistent is not immortal.** A streaming listener is a tracked background
task, and this host reaps those — 149 exit-144 events since 2026-06-13 across
many task kinds. The stream is cheaper per event than a pool; it is not more
durable than one. That is an argument for keeping a backup doorbell pool, not
against the design.

### Implemented contract

`goalflight_messages.py supervise` is the preferred one-task front door: it
spawns the stream, backup doorbell pool, and watchdog from
`coverage_rearm_commands`, multiplexes their stdout, and restarts them so a
controller never re-arms N listeners by hand. `goalflight_messages.py follow`
remains the persistent JSON-line surface for hosts that still arm components
separately. The host persistent monitor must own the tracked task's stdout
directly; ordinary shell backgrounding, detaching, or a task surface that
reports only at process exit produces no wakes.
Only `event`, `heartbeat`, and `frontier` records go to stdout. Diagnostics go to
stderr, which the measured host contract does not notify. Fatal journal, cursor,
ring, and durable-state failures are therefore structural `event` records on
stdout as well as supplemental stderr diagnostics. A regular-file stdout is
rejected before monitor coverage is claimed.

The default heartbeat is 120 seconds: seconds-scale traffic risks the host's
automatic noisy-monitor stop, while a multi-minute beat still turns otherwise
indefinite deafness into a bounded failure. Production values are rejected outside
the 60-to-300-second range. On a box carrying six-plus concurrent
workers, a 30-second grace fell inside normal scheduling jitter. The detector now
requires three complete missed beats: 360 seconds at the default cadence. The
stream durably records every successful stdout record. Six separately tracked
`listen --listener-slots 6 --report-pending` backup doorbells deliver mail, while an
independently locked `listen --watch-follow` watchdog polls generation-bound state
and emits `event`/`listener-dead` plus the persistent re-arm command when state is
stale, faulted, missing, or invalid. The watchdog never claims a delivery slot or
reads the mail cursor. This makes persistent coverage a shared eight-component
`live/8` fact. It stays persistent after stream death, so the surviving backup pool and
watchdog report `7/8`, not portable `1/4`.
Unchanged frontiers have a 15-minute floor and changed frontiers emit on the next
idle beat. The host may batch lines produced within 200 ms, so every line is an
independently parseable JSON object and consumers enumerate all records in a batch.
An event defers the next idle heartbeat, avoiding a contradictory event plus
"nothing arrived" beat in the same batch.

The heartbeat path never calls `goalflight_task.py next` or `TaskStore.next_frontier`:
those surfaces may repair publishes, scan the global dispatch ledger, and emit task
nudges. `follow` reads only the already-materialized `tasks-data.js` projection. If
the canonical task file is newer than that projection, the frontier is structurally
tagged `state: stale`; otherwise it says `state: projected`, never authoritative
`ready`, because dispatch-ledger changes can outpace the generated view. The record
also carries projection `age_s`; an hour-old projection becomes `stale` even when
`tasks.jsonl` has not changed.

`--listener-slots`, `GOALFLIGHT_LISTENER_SLOTS`, and
`GOALFLIGHT_LISTENER_LOW_WATER` remain portable-pool controls. Persistent backup
depth is `GOALFLIGHT_PERSISTENT_BACKUP_SLOTS` (default 6; target depth, not a
ceiling). `follow` rejects
the CLI knob and warns on the portable environment knobs; persistent depth is
the stream, the backup doorbell pool, and the watchdog.

`follow` does not create a journal `listener_coverage` row, so it has no synthetic
`EXITED/event` row to write. Its generation-bound monitor flock is the liveness
authority. Every exit path releases that flock and restores signal handlers; fatal
runtime paths publish `listener-fault`, while the watchdog publishes the exact
stream re-arm command. A merely busy journal is not a fault: the stream then
uses a 300-second continuous-failure window, publishing one
`listener-degraded` record when the window opens and one `listener-recovered`
when it closes, and heartbeats keep beating so the watchdog never reads load as
death. Because the clock opens after the first bounded operation, a busy outage
can take about 350 seconds plus scheduler delay to fault (10s first operation +
300s window + 30s backoff + 10s final operation). `EPIPE` has no re-arm payload
because its reader is gone.
The watchdog remains an ordinary `listen`, so it deliberately retains journal arm
and exit audit, tracked-task completion, and kernel-slot release.

Cursor-ring ownership is a reversible reservation until stdout delivery succeeds.
An `EPIPE` before the first event rolls it back, leaving unread mail deliverable to
the replacement. `EAGAIN`/`EWOULDBLOCK` retries the buffered line without spinning,
but a 60-second stall records durable fault state and releases the monitor flock so
the watchdog can surface it and re-arm.
