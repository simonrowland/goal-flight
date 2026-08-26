---
description: "Execute queued goal chunks with capacity-aware workers."
---

# execute [--parallel <N>]

Execute the next goal-queue chunks with procedural workers and compact status.

> ⚠ **DISPATCHES WORKERS.** Spawns worker processes, leases capacity (costs
> money), and may mutate a worktree. See "Command danger classification" in
> `SKILL.md`. The standing `com.goalflight.drain` daemon (every ~60s, always-on)
> launches anything queued — draining is automatic, not a manual step.

Read:

- `protocols/session-preflight.md`
- `protocols/dispatch-routing.md`
- `protocols/worker-markers.md`
- `protocols/worker-context-package.md`
- `protocols/scout.md`
- `protocols/worker-contract.md`
- `protocols/state-handoff.md`
- `protocols/user-status-cadence.md`
- `protocols/chunk-review.md`
- `protocols/milestone-review.md`
- `protocols/worktrees-parallel.md` only for `--parallel`

## Two things that decide whether a dispatch is worth its tokens

**Ask for a plan before the build on CP/H-class chunks — by tagging the brief.**
Tag it `[design-checkpoint]`. The worker's first deliverable is then the design —
interfaces, invariants, acceptance checks, intended test names — written to the
lane's research directory, followed by
`!USER-NEED: design ready for approval at <path>; session <resume-id>; log <path>`,
and it stops. **Resume that session with your corrections; do not re-dispatch** —
re-dispatching re-pays the orientation the checkpoint exists to save. A wrong
design then costs one steer instead of a whole build plus a rewrite.

**This is opt-in per brief, and deliberately so.** It is NOT on by default and
must not be: `!USER-NEED:` ends the run, so a worker that checkpoints on its own
initiative during an unattended execute stalls the queue until a human appears —
and because the background wait correctly wakes you on that terminal, the stall
looks like normal progress. Only the codex goal-mode template carries the rule
today; the dispatch-wrapper path does not, so **tag the brief rather than
assuming the worker was told.**

**Collect approvals in a batch, not one at a time.** A claimed controller keeps
its ownership listener armed and does not enumerate dispatch ids; after each wake,
use owned status to collect compatible terminal checkpoints. For an unclaimed
scripted fixed-set join only, `--wait` takes several ids and returns when **all**
are terminal, and a design checkpoint is terminal:

```bash
python3 <skill-root>/scripts/goalflight_status.py --wait chunk-a,chunk-b,chunk-c
```

Backgrounded, that is a single fixed-set wake carrying every checkpoint that
landed. Review the designs together — they usually share surfaces, and reading
three at once is where you notice two of them deciding the same thing differently
— then resume each session with its corrections. Waking three times to approve
three designs costs three context reloads and gives you no cross-chunk view.

**Batch peers, not a sprinter with a marathon runner.** All-terminal means the
batch is as slow as its slowest member, so grouping a two-minute chunk with a
forty-minute one idles the fast result for 38 minutes. Group by expected
duration; wait separately on anything you need back early.

**Arm the wait; let it wake you.** A controller entry auto-claims its canonical
project lease. On a host whose persistent monitor turns each flushed stdout line
into a wake, arm one generation-bound `goalflight_messages.py follow --project-root
"$PWD" --controller-label <label> --lease-nonce <nonce>` through that monitor —
never through ordinary shell backgrounding — and keep six tracked `listen
--listener-slots 6 --report-pending` backup doorbells plus one separately tracked
`listen --watch-follow` watchdog. The watchdog holds its own generation lock and
never consumes a doorbell slot. It reads the stream's durable successful-record age;
three missed heartbeat intervals emit `listener-dead` on stdout with the exact monitor
re-arm command. Treat stream, backup pool, and watchdog as shared persistent coverage
`live/8`. On hosts without that
monitor (codex, grok, cursor, opencode), keep the portable tracked `listen` pool:
when one exits, peek with `relay --new --json`, process the items, cursor-CAS their
server-known positions with `advance`, then re-arm. An unclaimed
fixed-set join backgrounds the printed `goalflight_status.py --wait <ids>`
command. Do not block the turn on either. A timer is only for non-notifiable
external state such as CI, a remote queue, or a deploy. Scheduling one to ask
whether a worker finished is polling a channel that would have told you.
Portable controllers should prefer `--report-pending`, which reports an arm-time
backlog in place and stays armed for only newer mail while omission preserves the
exit-driven compatibility loop.

## Steps

1. Pre-flight:

```bash
python3 <skill-root>/scripts/goalflight_status.py
python3 <skill-root>/scripts/goalflight_capacity.py status --json
python3 <skill-root>/scripts/goalflight_rate_pressure.py --json
python3 <skill-root>/scripts/goalflight_messages.py relay || true
```

`goalflight_messages.py relay` exits **2** when open `user_need` / `user_confirm`
rows exist in the fleet register aggregate (built from
`~/.goal-flight/messages/*.jsonl` and `~/.goal-flight/fleet/register/dispatches/`).
Print the line to the orchestrator host and **stop** — do not auto-answer. After the
user responds, append steering or continue dispatch per `protocols/worker-markers.md`.

`goalflight_rate_pressure.py` reads the dispatch ledger and reports
provider-level rate-limit pressure. Be **silent on clean** — if
`providers_under_pressure` is empty, do not emit a marker or "nothing
to report" line. The orchestrator has the routing table; default is fine.
`goalflight_capacity.py status` surfaces the same adaptive walkback warning for
operator visibility without mutating `capacity.json`.

If `providers_under_pressure` is non-empty:
- Emit a single `STATUS: rate-pressure provider=<p> count=<n>` line.
- For the next chunk, prefer the first ready `fallback_providers` entry whose
  adapter capabilities preserve the task and review concern.
  `recommended_caps` is advisory — apply it through routing, not by mutating
  capacity state.
- If pressure crosses **two independent provider pools** in the same probe,
  surface `BLOCKED: rate-pressure across providers` to the user and pause.

**Active monitoring under `--parallel N`** is pool-specific, not a flat
threshold. Re-probe between dispatches when a pool's in-flight count approaches
the current adapter cap, or when status reports adaptive walkback, cooldown, or
shared-session pressure. Otherwise the pre-flight probe is sufficient. Treat
unknown pool state conservatively; do not infer capacity from a vendor name or
past anecdote.

Read-only probe; the orchestrator decides whether to act. See SKILL.md
"Worker Routing" for the per-task fallback table.

2. Pick the next non-DONE queue item.

3. Render the dispatch prompt from `prompts/dispatch-wrapper.md`.

4. Check capacity before choosing a path. Runner scripts acquire and release
their own leases; do not pre-acquire a lease unless you are spawning a worker
manually outside the runner scripts.

If status shows a relevant cooldown or full cap, do not spawn. Pick another
valid agent only if it preserves the review/implementation concern.

5. Dispatch:

Before each wave, invoke the rolling just-in-time freshness scout in
`protocols/scout.md` for the next one or two near-frontier prompts whose named
risk or staleness signals are armed. While the current worker or review job is
waiting, use that protocol's **Scout ahead** loop to prepare those reports.
Immediately before each dispatch, compare the scout's observed `HEAD` and
named surfaces with the current tree; apply **Re-scout on tree motion** when a
named anchor, dependency, invariant, guard test, or prerequisite changed.

Every prompt row included in a scout or batch report must have its own verdict
and completed fold-in gate. A trivial prompt skipped under
`protocols/scout.md` records the evidence-backed skip disposition instead of a
fabricated verdict. A blocking verdict stops only that chunk;
already-evidenced siblings may proceed. This freshness step complements the
pinned-package gate below: it never replaces or shortens a mandatory lane
package.

Before dispatching each wave, run the `protocols/worker-context-package.md`
self-check: for every chunk in a pinned lane, does the prompt prepend the lane
brief verbatim and quote its ground truth? Missing or stale package means
building or refreshing it is the wave's first chunk. Reviewer dispatches into a
pinned lane get the same brief prepended.

- ACP: `scripts/goalflight_acp_run.py`
- Bash-tail fallback: worker stdout/stderr to files plus `scripts/goalflight_watch.py`
- Review job: `scripts/goalflight_review_job.py`

Canonical direct dispatch is background:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --agent <ready-agent> --prompt-file p.md --cwd .
```

For durable queue launch, submit and drain one non-blocking pass:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --submit --drain-on-submit --agent <ready-agent> --prompt-file p.md --cwd .
```

Use `--foreground` only for synchronous scripts/tests that need the worker exit
code:

```bash
python3 <skill-root>/scripts/goalflight_dispatch.py --agent <ready-agent> --prompt-file p.md --cwd . --foreground
```

For `--parallel N` where `N >= 2`, ACP code-writing dispatches must pass
`--worktree create`; the runner leases one lazy, reusable seat from
`worktrees/wt-1` … `worktrees/wt-N` and routes the worker `--cwd` there.
`GOALFLIGHT_WORKTREE_SEATS` sets the deliberate per-repository hard ceiling
(default 4). A full pool fails with occupant dispatch ids instead of creating
another checkout. Sequential dispatch (`--parallel 1` or no flag) stays in the
project root.

Parallel worktrees start from committed `HEAD`; they do not include uncommitted
controller-root edits. Preserve unrelated WIP. Dispatch from committed `HEAD`
in an isolated worktree and carry only authorized prerequisite facts or content
into the worker brief; commit prerequisite changes first only when they have
passed their own review gate. Never stash, move, or discard another owner's WIP
without an explicit operator decision naming the exact paths and operation.

6. Record status:

Every spawned worker must be recorded with the dispatch ledger/status field
contract in `protocols/worker-contract.md`.
Use `scripts/goalflight_ledger.py record` directly only when a runner did not
already record the worker.

**In-flight monitoring:** while workers or review jobs run, follow
`protocols/user-status-cadence.md` — report event wakes and, if none arrive,
sample `goalflight_status.py` for a compact user update at least every 15 minutes
unless context is tight (file-only row in RESUME-NOTES then). This cadence is
for user reporting, not completion discovery; background it and do not read logs.

For an unclaimed deliberate fixed-set join, use in the background:
`python3 <skill-root>/scripts/goalflight_status.py --wait id1,id2 --wait-timeout <s>`.
Exit 0 means every requested dispatch is terminal; exit 1 means pending/timeout;
**exit 3 means mail arrived while you were waiting** and the wait returned early
so you can read it. Workers may still be running -- that is why it is not 0 --
and the re-arm command, pre-filled with the ids still pending, is printed on
stderr.

Branch on all three. A controller that treats any non-zero as failure will read
an early mail wake as a broken wait, abandon its workers, and go back to
polling by hand. That is the exact behaviour this replaced: before the wake
existed, worker escalations sat unread for hours while a human relayed messages
between sessions.

For the normal claimed-controller path, background the one-shot journal listener.
It prints nothing until an assignment exists after the cursor, then exits as a
body-free doorbell:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen \
  --project-root "$PWD" --controller-label <label> --lease-nonce <nonce>
```

Peek with `relay --new --json`, process the items, advance their server-known
positions with the returned cursor version, then re-arm. Never use the listener to
renew the lease.

7. Completion:

Read status JSON. Do not inspect raw logs unless the status script reports that
the log is corrupt or missing.

Dispatch prompts use the file-backed findings, return-shape, no-bypass, marker,
and verify-survival contract in `protocols/worker-contract.md`.

The orchestrator reads TL;DR + headline (READY path / COMMIT sha / BLOCKED
reason) on first pass. Open DETAILED only when TL;DR raises a flag,
defer to the chunk-review pass in step 8, or when failure analysis is
needed.

8. Verification (chunk review — not milestone review):

Read `protocols/chunk-review.md`.

- inspect diff
- run focused tests
- re-take the null-hypothesis stance yourself: prove the patch did the stated
  thing and did not no-op or break a neighbor before accepting worker evidence
- run at least **two** independent, **concern-diverse** pre-commit reviews per
  `protocols/chunk-review.md` (the parallel flight is the FLOOR, not the target) — e.g.
  gstack `/review` on the chunk diff AND `./scripts/autoreview.sh --mode local` in parallel,
  or two concern-diverse engines; scale above two as complexity rises; background
  if >10s. Review each patch **to convergence** — a clean (zero-P0/P1/P2) round,
  not a round count
- run executor self-review findings when present in worker output
- fix P0/P1/P2 from review before commit
- commit when the active goal-flight workflow completes a chunk (default: one
  commit per chunk) or when the user explicitly requests a commit. Use
  explicit pathspecs: `git commit -m '<scope>' -- <file1> <file2> ...`. For
  commit messages longer than 3 lines, write the message to
  `docs-private/commit-msgs/<chunk-slug>.txt` first and use
  `git commit -F docs-private/commit-msgs/<chunk-slug>.txt -- <files>`. Inline
  `git commit -m "$(cat <<'EOF' ... EOF)"` heredocs put the full prose into
  the orchestrator's conversation context for the rest of the session; the
  file-backed version is read once by git and never re-enters context.
  Never bare `git commit` while other workers may have staged WIP — the
  commit guard (`scripts/goalflight_commit_guard.py`) refuses to prevent
  bundling. The guard's error message names the lease IDs in flight, the
  partial-commit fix shape, and the override flag if needed.

9. Milestone review (separate from step 8):

At the configured cadence — **default: every 5 commit-worthy chunks since the last milestone
sweep, unless the active plan sets K** — on any `[milestone]` chunk, or before any push, run
file-backed review flights per `protocols/milestone-review.md` via
`scripts/goalflight_review_job.py`. Routine status surfaces the commit-count/tag nudge,
`chunks since last milestone sweep = M (sweep due at K)`; it does not infer push intent or
block dispatch/drain by itself. After a clean sweep, record it with
`goalflight_status.py --record-milestone-sweep`. This gate is **mandatory and the
most-forgotten**: a DUE sweep is an open liability; do not dispatch new implementation
chunks or push until it converges to a clean round.
Missing/stalled/session-limited reviews are inconclusive, not clean.

10. Resume/handoff:

Before compact, sleep, or long wait, update resume notes from:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```

## Parallel Mode

`--parallel N` is a request, not authority. Effective concurrency is:

```text
min(N, machine operating cap, per-agent cap, no-active-cooldown)
```

Use `scripts/goalflight_acp_run.py --worktree create` for concurrent code
edits. See `protocols/worktrees-parallel.md`.

## Termination

Stop when:

- queue is DONE
- a blocking user question is required (including `goalflight_messages.py relay` exit 2)
- capacity/rate limits block all valid dispatch paths
- tests or reviews find an issue that should not be delegated further
