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

## 3. Identity and ownership — **currently broken**

This is the weakest part of the system and the source of most cross-controller
noise.

**Measured, 2026-08-21:** `--controller-label` is accepted and **silently
discarded**. Five dispatches launched with the flag explicitly set all recorded
`controller_label=None`, `controller_pid=None`, and NULL
`owner_controller_label` / `owner_session_digest` in the ledger. No error was
raised. `project_root` and `worker_cwd` are also `None`.

Consequences, all measured:

- **Every dispatch is unowned**, so terminal events fan out to *every*
  controller in the project — one dispatch reached four controllers, another
  three, one broadcast to `*`. Each foreign event costs a one-shot doorbell.
- **An unowned controller cannot be messaged.** Posting to an unresolvable label
  returns `recorded_only_no_dispatch`: recorded, never delivered. So "notify the
  affected controllers" is not merely blocked — it is undefined.
- **An unowned dispatch cannot be attributed**, so we cannot even identify whose
  it is.

**Ambient identity is available and unused.** Where the dispatcher runs, the git
toplevel and origin URL both resolve; `GOALFLIGHT_CONTROLLER_LABEL` and
`GOALFLIGHT_CONTROLLER_LEASE_NONCE` are the env vars other controllers already
export. Yet `dispatch` reads the label env var once, never reads a controller
pid, and records no project root. **measured**

**Design question for the next iteration (open):** should ownership be keyed on
the *label* — a human string that already collided (`pm2` vs `pm2-main`) — or on
the **lease identity**, which has kernel-lock liveness behind it? A label is a
hint; a lease is a fact.

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
| `--controller-label` accepted | ownership recorded | silently discarded |

**Design rule:** a field must be able to distinguish *unset* from *asserted*,
and a verdict must name the evidence it rests on. When a record and the live
system disagree, prefer the live check and record the disagreement.

---

## 6. Known broken (open work)

- **t-290** — a resumed worker cannot find its brief after a reboot: no prompt
  columns in `dispatch_attempts`; the only pointer is `/tmp`. Store path **and
  content hash** durably.
- **b-174** — ownership discarded → universal fanout (§3).
- **b-175** — doctor never inspects lease liveness (zero references to
  `controller_leases` / `classify_controller` / `holder_lock`) and reported
  `active_leases_in_project: 0, ok: true` while a lease was demonstrably live.
- **t-293** — teach at dispatch time when ownership will not resolve.
- **b-176** — listeners die silently at exit 144; cause is the host's task
  reaper, so the fix is legibility, not survival.
- **b-173 / b-020** — quiet treated as terminal; workers outlive their verdict
  and accumulate. A reaper needs **both** facts: owning dispatch terminal **and**
  process idle.
- **b-165** — journal reads bounce instead of waiting (`busy_timeout = 0`, no
  recorded rationale).
- **b-167** — a uniform finite listener timeout blanks a whole pool at once.

---

## 7. What has not been measured

Named because their absence shapes the priority order:

- **Work actually lost** to a missed wake, as distinct from latency added.
  Nobody has counted it. If the true cost is minutes of latency, this queue's
  ordering is wrong.
- Whether non-Claude hosts suffer the tracked-task reap. Detached processes
  survived the wipes, which suggests not — one observation, not a proof.
- How long ownership has been NULL, and whether it was ever populated.
