# Fleet Console Producer Protocol

## Why

The fleet console reloads generated script mirrors; it cannot refresh the
underlying fleet itself. Session-owned loops die when a controller exits, and
an unscheduled mirror becomes confidently stale. The producer therefore reuses
the drainer's reaper-survivable shape: short, per-user launchd jobs that start
outside any controller session and exit after one tick.

Two LaunchAgents keep the planes independent:

| Plane | Label | Cadence | Budget | Output |
|---|---|---:|---:|---|
| attention | `com.goalflight.fleet-console.attention` | 5s | 3s | `templates/fleet-console/attention-data.js` |
| fleet | `com.goalflight.fleet-console.fleet` | 30s | 4s | `templates/fleet-console/fleet-data.js` |

The cadence matches the renderer's reload cadence. Attention remains the 5s
mailbox plane; fleet now reloads every 30s because immutable terminal history
and prompt bodies no longer pollute either short poll.

Live read-only samples through the deployed wrapper on 1,404 local rows and
1,954 registered projects (2026-08-16) measured attention at 1.12s and fleet
at 1.84s / 76,473 bytes. Budgets are the integer ceilings of twice those
measurements: attention 3s, fleet 4s. With explicit cleanup reserves,
`5s > 3s + 1s` and `30s > 4s + 2s`.
The hourly slow-history catch-up runs only after a successful fast publication
and outside this deadline; normal dispatch/finish hooks make it a no-op.

Fast projection is live-only by record class: terminal dispatches, inactive
projects/worktrees, ended controller generations, and inactive remote
registrations contribute counts and immutable slow history but never trigger
short-poll liveness probes or per-project repository scans.

The installer records the console output directory in the user-private
`~/.goal-flight/fleet-console-output-dir` file so dispatch/finish hooks can
publish immutable prompts and history without coupling to a producer tick. A
full uninstall removes that opt-in; catch-up still receives its output path
directly from the fleet producer.

Each payload stamps its producer cadence. The renderer declares a plane stale
at two missed stamped intervals; its own reload timer is transport, never
freshness authority. Every degraded `last_error` and stale banner names the
plane launchd log plus `scripts/install-fleet-console.sh --status --plane
<plane>`. HUNG controller attention follows the wake layer's three-state
supervisor contract. Proven supervisor absence carries the exact component
command. A running supervisor suppresses controller-facing depth advice because
it owns and reports its pool. Unknown supervisor state carries numberless
verification guidance and no component command; it never guesses that direct
`listen`, `follow`, or `--watch-follow` is safe.

## Tick behavior

`scripts/goalflight_fleet_console_producer.py` acquires a nonblocking advisory
lock before starting the projection. Lock files are per plane, so a slow fleet
tick never blocks attention. A contender leaves the current mirror untouched,
writes a visible `SKIPPED: overlap lock held` line, and exits 75 (`EX_TEMPFAIL`).

The projection runs in a child process bounded by the plane budget. On timeout,
the child is killed and reaped, then the runner atomically publishes the normal
schema with `last_success_at: null`, `last_error: budget:TimeoutExpired`, and
exits 1 through the producer's existing DEGRADED contract. A budget-stopped
fleet payload reports `registry_deep_sampled: 0` and `registry_total: null`:
zero usable deep samples and an unknown total, never the false claim `0 / 0`.
Successful capped samples continue to report the exact sampled/total pair.

Publication remains same-directory temporary write, file flush and `fsync`,
then `os.replace`. Readers therefore observe either the previous complete
script or the next complete script, never an in-place partial file.

## Preview and install

Preview both generated plists without filesystem or launchctl changes:

```shell
bash scripts/install-fleet-console.sh --dry-run
```

Preview one plane:

```shell
bash scripts/install-fleet-console.sh --dry-run --plane fleet
```

Install both jobs only when the operator chooses to start scheduled production:

```shell
bash scripts/install-fleet-console.sh
```

Inspect or remove them:

```shell
bash scripts/install-fleet-console.sh --status
bash scripts/install-fleet-console.sh --uninstall
```

The installer follows `scripts/install-drainer.sh`: it renders escaped
machine-local paths from a checked-in plist template, supports modern and
legacy launchctl verbs, and offers dry-run/status/uninstall modes.
