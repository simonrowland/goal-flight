# goal-flight architecture

## Direction

goal-flight is a portable skill core plus host wrappers. The core is a small
orchestrator pattern plus procedural runtime helpers. The orchestrator keeps
judgment and product context. Scripts own deterministic facts: tool readiness,
process state, capacity, logs, status, and review job files. Adapter manifests
own host bindings: tool names, invocation, permissions, packaging, memory
projection, and local readiness requirements.

Wrappers are host projections over the same core. The checked-in `SKILL.md` and
`.claude-plugin/` package are the current Claude Code wrapper surface; they are
wrapper health, not the definition of core validation.

## Context Budget

Always-loaded surface:

- `SKILL.md`: current wrapper router, invariants, command index
- invoked `commands/*.md`
- only referenced `protocols/*.md`

Load-on-demand surface:

- ACP and dispatch details: `protocols/dispatch-routing.md`
- marker parsing: `protocols/worker-markers.md`
- fork/session inheritance: `protocols/self-delegation.md`
- review flights: `protocols/milestone-review.md`
- parallel worktrees: `protocols/worktrees-parallel.md`

The fork protocol is never loaded by default. It is an explicit tool for chunks
that need inherited conversation context.

## Procedural Runtime

Scripts emit compact JSON and short human checklists:

- `goalflight_doctor.py`: wrapper/tool/runtime readiness
- `goalflight_capacity.py`: machine-global worker leases and cooldowns
- `goalflight_ledger.py`: dispatch records with PID plus process identity
- `goalflight_status.py`: aggregate capacity and dispatch status
- `goalflight_watch.py`: log marker extraction without `tail -f`
- `goalflight_acp_run.py`: ACP prompt runner with status and ledger records
- `goalflight_review_job.py`: file-backed review jobs (for example Codex/Claude)

Model reads summaries. Raw logs, JSONL streams, and full review transcripts stay
in files.

## Capacity

Raw RAM ceiling is a safety bound. Operating cap is lower and machine-global.
Multiple goal-flight sessions coordinate through `/tmp/goal-flight-$UID` unless
`GOALFLIGHT_STATE_DIR` is explicitly set.

Default operating caps:

- <=8 GB: 1 worker
- <=16 GB: 3 workers
- <=32 GB: 4 workers
- <=64 GB: 6 workers
- larger: 8 workers unless overridden

Provider/session limits are cooldowns. A host session limit or provider rate
limit (for example Claude, Codex, or Grok) blocks future acquire attempts
before a worker is spawned.

`python3 scripts/goalflight_capacity.py release --lease-id <id> --reason <text>`
releases only when worker evidence proves release safe; `--keep` retains the
terminal lease in history. Automatic cleanup never overrides unknown identity.
For unresolved legacy leases, capacity `status` prints an operator command:

```sh
python3 scripts/goalflight_capacity.py release --lease-id <id> --operator-confirmed --reason manual_legacy_release
```

Run this only after checking the worker session and confirming that releasing
its account capacity is appropriate. Replace the reason with the explanation
of that check if needed. The flag requires a nonblank reason and accepts only
unknown worker identity; a provably live worker is refused. Resolved dead workers
use normal release. Remote leases must be handled on their owning machine.
Operator confirmation, username (`released_by`), reason, and release time remain
in the terminal lease's existing 24-hour history, even without `--keep`.
Automatic release paths must never pass `--operator-confirmed`.

## Ledger

Every long worker/review has:

- dispatch id
- prompt id/path/hash
- agent and transport
- orchestrator PID identity
- worker PID identity
- optional ACP/logical session id
- status/stdout/stderr paths
- capacity lease id
- state

PID alone is not trusted. Live identity requires PID plus start time and command
to match.

## Review Flights

Milestone reviews are jobs, not prose. States include:

- `running`
- `blocked_session_limit`
- `blocked_auth`
- `inconclusive_timeout`
- `inconclusive_no_final`
- `complete`
- `failed`

Missing or inconclusive review output is never treated as clean.

## Validation Boundaries

Core validation checks adapter schemas, no-leak rules, command/protocol
contracts, and tests. Wrapper/package validation checks host projection health,
such as the current `.claude-plugin/` package. A wrapper can fail packaging
validation without redefining the portable core contract; the adapter and script
facts say what is supported, ready, and safe to run.

## Fleet (1.0)

Multi-node dispatch over SSH lives in `goalflight_fleet*.py` with a file-backed
fleet store (default `~/.goal-flight/fleet`). The orchestrator previews remote
`acp_run` plans, executes with `GOALFLIGHT_LIVE_SSH=1`, mirrors remote status,
and reconciles billing locks. Operator guide: [fleet.md](fleet.md).
