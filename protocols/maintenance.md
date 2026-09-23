# Standalone Maintenance

`com.goalflight.maintenance` runs `scripts/goalflight_maintenance.py --apply
--json` hourly. It is independent of `com.goalflight.drain`; dispatch draining
must remain available even when retention is slow or unavailable.

The command is dry-run by default:

```shell
python3 scripts/goalflight_maintenance.py --json
scripts/install-maintenance.sh --dry-run
scripts/install-maintenance.sh
```

Dispatch-artifact deletion requires a complete readable ledger row that is
terminal and older than seven days, plus a dead worker/watcher/waiter identity.
Missing rows, unreadable sidecars, missing process identities, live processes,
resumable dispatches, and symlinks are retained. A ledger read failure performs
no deletions. Dispatch tails are archived as a redacted trace or a minimal
terminal receipt before their volatile sidecars are removed. Independent setup,
config-backup, and gate-log policies retain their rollback/failure evidence
without inventing a dispatch identity.

Trace archives are retained for seven days and bounded to 2 GiB of allocated
space; pinned and unknown archives stay. Setup/config backups keep the newest
three. Gate logs keep failures and the newest three successes. OpenCode logs
rotate at 16 MiB with two generations.

The JSON report includes component-level before/after/reclaimed byte counts and
every keep/delete decision. Re-running it is idempotent.
