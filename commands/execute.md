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
- `protocols/milestone-review.md`
- `protocols/worktrees-parallel.md` only for `--parallel`

## Steps

1. Pre-flight:

```bash
python3 <skill-root>/scripts/goalflight_status.py --json
python3 <skill-root>/scripts/goalflight_capacity.py status --json
python3 <skill-root>/scripts/goalflight_rate_pressure.py --json
```

`goalflight_rate_pressure.py` reads the dispatch ledger and reports
provider-level rate-limit pressure (anthropic-session, anthropic-api,
openai, xai, cursor). If `providers_under_pressure` is non-empty:

- Emit `STATUS: rate-pressure provider=<p> count=<n> recommended-cap=<half>`.
- For the next chunk, prefer the first available `fallback_providers`
  entry over the pressured provider's default (e.g., if anthropic-session
  is pressured, route a code-writing chunk to codex/cursor instead of
  Claude Agent). The script's `recommended_caps` is advisory — apply by
  routing decision, not by mutating capacity state.
- If pressure is severe (multiple providers, repeated runs), surface
  `BLOCKED: rate-pressure across providers` to the user and pause.

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

7. Completion:

Read status JSON. Do not inspect raw logs unless the status script reports that
the log is corrupt or missing.

8. Verification:

- inspect diff
- run focused tests
- run self-review against changed files
- commit only when requested by the user or by the active workflow

9. Milestone review:

At configured cadence or `[milestone]` chunks, run file-backed review flights
via `scripts/goalflight_review_job.py`. Missing/stalled/session-limited reviews
are inconclusive, not clean.

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
- a blocking user question is required
- capacity/rate limits block all valid dispatch paths
- tests or reviews find an issue that should not be delegated further
