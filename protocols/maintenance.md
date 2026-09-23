# Explicit artifact cleanup

Goal Flight does not install or run an automatic retention LaunchAgent. The
old maintenance plist, installer, hourly cadence, and `RunAtLoad` hook
were removed because unattended deletion can race resume and live workers.

Cleanup is one explicit command. It is a JSON dry run unless `--apply` is
given:

```shell
python3 scripts/goalflight_maintenance.py --json
python3 scripts/goalflight_maintenance.py --apply --json \
  --max-bytes 1073741824 --max-files 100 --max-seconds 30
```

Each candidate row names `path`, `bytes`, `eligible`, `deleted`, and `reason`.
The command reads ledger and project-journal authority again immediately
before every deletion. Duplicate or split ledger rows, unreadable authority,
live process identities, live steer waiters, unsafe symlinks, and invalid
receipts are retained.

Terminal Codex homes with a valid recorded session and rollout remain retained
for the documented 30-day resume window, even when ordinary cleanup retention
has expired. They can be removed only after that window and only when the
dispatch is settled. Override the window explicitly with
`--resume-window-days`.

Trace archives, dispatch homes, dispatch sidecars, project/machine/config
backups, and gate logs are handled by this command. Trace and receipt writes
use each ledger row's own `project_root`; symlinked roots and destinations are
never followed. The default is dry run, so no receipt is written until
`--apply` is used.

`--max-bytes`, `--max-files`, and `--max-seconds` bound each run. A candidate
that would exceed a bound is reported with a budget reason and retained for a
later explicit run.

## Log rotation

The dispatch drainer keeps `drain-launchd.log` bounded through its existing
size-based `newsyslog` wrapper and two generations. OpenCode uses a bounded
rotating writer for its server output, including servers left running with
`--keep-server`; it rotates at write time and retains two generations. These
are write-time protections, not artifact cleanup.
