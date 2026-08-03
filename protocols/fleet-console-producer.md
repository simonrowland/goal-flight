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
| attention | `com.goalflight.fleet-console.attention` | 5s | 4s | `templates/fleet-console/attention-data.js` |
| fleet | `com.goalflight.fleet-console.fleet` | 60s | 50s | `templates/fleet-console/fleet-data.js` |

The cadence matches the renderer's reload cadence. Attention is a cheap mailbox
summary and is wanted quickly. Fleet is measured at about 18 seconds and walks
the bounded registry projection, so it runs less often.

Budget derivation: reserve one second of a five-second attention interval and
ten seconds of a sixty-second fleet interval for child termination and atomic
DEGRADED publication. Thus `5s - 1s = 4s` and `60s - 10s = 50s`; units remain
seconds. Sanity checks: `0 < 4 < 5`, and `18 < 50 < 60` (the fleet budget is
about `50 / 18 = 2.8` times its measured normal runtime).

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
