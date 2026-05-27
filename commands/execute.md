---
description: "Execute queued goal chunks with capacity-aware workers."
---

# execute [--parallel <N>]

Execute the next goal-queue chunks with procedural workers and compact status.

Read:

- `protocols/session-preflight.md`
- `protocols/dispatch-routing.md`
- `protocols/worker-markers.md`
- `protocols/state-handoff.md`
- `protocols/user-status-cadence.md`
- `protocols/chunk-review.md`
- `protocols/milestone-review.md`
- `protocols/worktrees-parallel.md` only for `--parallel`

## Steps

1. Pre-flight:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
python3 <skill-root>/scripts/goalflight_capacity.py status --json
python3 <skill-root>/scripts/goalflight_rate_pressure.py --json
python3 <skill-root>/scripts/goalflight_messages.py relay || true
```

`goalflight_messages.py relay` exits **2** when open `user_need` / `user_confirm`
rows exist in the fleet register aggregate (built from
`~/.goal-flight/messages/*.jsonl` and `~/.goal-flight/fleet/register/dispatches/`).
Print the line to the controller host and **stop** — do not auto-answer. After the
user responds, append steering or continue dispatch per `protocols/worker-markers.md`.

`goalflight_rate_pressure.py` reads the dispatch ledger and reports
provider-level rate-limit pressure. Be **silent on clean** — if
`providers_under_pressure` is empty, do not emit a marker or "nothing
to report" line. The controller has the routing table; default is fine.

If `providers_under_pressure` is non-empty:
- Emit a single `STATUS: rate-pressure provider=<p> count=<n>` line.
- For the next chunk, prefer the first available `fallback_providers`
  entry over the pressured provider's default (anthropic-session
  pressured → route code-writing to codex/cursor instead of Claude
  Agent). `recommended_caps` is advisory — apply by routing decision,
  not by mutating capacity state.
- If pressure crosses **two providers** in the same probe, surface
  `BLOCKED: rate-pressure across providers` to the user and pause.

**Active monitoring under `--parallel N`**: provider-specific, not a
flat N threshold. Empirically:
- Codex (OpenAI sub) scales cleanly through N=10; no bouncing observed.
- Grok / cursor (vendor subs) similar — sub-billed providers tolerate
  goal-flight-shaped parallelism comfortably.
- Claude Agent subagents (anthropic-session) start bouncing around
  N=10 in practice — they share the controller's session budget.

So: re-probe between dispatches when **3+ in-flight workers map to
the same anthropic-* provider** (especially anthropic-session). For
codex / grok / cursor-only parallel workloads (the routing-table
default), the pre-flight check alone is sufficient — no polling.

Read-only probe; the controller decides whether to act. See SKILL.md
"Worker Routing" for the per-task fallback table.

2. Pick the next non-DONE queue item.

3. Render the dispatch prompt from `prompts/dispatch-wrapper.md`.

4. Check capacity before choosing a path. Runner scripts acquire and release
their own leases; do not pre-acquire a lease unless you are spawning a worker
manually outside the runner scripts.

If status shows a relevant cooldown or full cap, do not spawn. Pick another
valid agent only if it preserves the review/implementation concern.

5. Dispatch:

- ACP: `scripts/goalflight_acp_run.py`
- Bash-tail fallback: worker stdout/stderr to files plus `scripts/goalflight_watch.py`
- Review job: `scripts/goalflight_review_job.py`

6. Record status:

Every spawned worker must have:

- dispatch id
- prompt path/hash
- agent/transport
- worker PID and process identity
- status path
- capacity lease id when applicable

Use `scripts/goalflight_ledger.py record` directly only when a runner did not
already record the worker.

**In-flight monitoring:** while workers or review jobs run, follow
`protocols/user-status-cadence.md` — poll `goalflight_status.py --json` and
surface a compact user status update at least every 15 minutes unless context
is tight (file-only row in RESUME-NOTES then). Background the poll; do not
block on raw logs.

7. Completion:

Read status JSON. Do not inspect raw logs unless the status script reports that
the log is corrupt or missing.

8. Verification (chunk review — not milestone review):

Read `protocols/chunk-review.md`.

- inspect diff
- run focused tests
- run at least one independent pre-commit review per `protocols/chunk-review.md`
  (default gstack `/review` on the chunk diff; `./scripts/autoreview.sh --mode local`
  may run in parallel as a complementary diff-local pass; background if >10s)
- run executor self-review findings when present in worker output
- fix P0/P1/P2 from review before commit
- commit when the active goal-flight workflow completes a chunk (default: one
  commit per chunk) or when the user explicitly requests a commit

9. Milestone review (separate from step 8):

At configured cadence or `[milestone]` chunks, run file-backed review flights
per `protocols/milestone-review.md` via `scripts/goalflight_review_job.py`.
Missing/stalled/session-limited reviews are inconclusive, not clean.

10. Resume/handoff:

Before compact, sleep, or long wait, update resume notes from:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
```

## Parallel Mode

`--parallel N` is a request, not authority. Effective concurrency is:

```text
min(N, machine operating cap, per-agent cap, no-active-cooldown)
```

Use worktrees for concurrent code edits. See `protocols/worktrees-parallel.md`.

## Termination

Stop when:

- queue is DONE
- a blocking user question is required (including `goalflight_messages.py relay` exit 2)
- capacity/rate limits block all valid dispatch paths
- tests or reviews find an issue that should not be delegated further
