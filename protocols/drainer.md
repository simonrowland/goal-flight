# Launchd Drainer Protocol

## Why

Long-lived in-session drain loops die with the controlling session. That is the
D007/D008 worker-death family: a controller exits, the host reaper tears down
the loop, and queued dispatch rows stop launching.
For dead-worker rows that already have tail bytes, run `protocols/dispatched-worker-recovery.md` §"Worker death with tail bytes present (tail harvest)" before treating the work as lost.

`com.goalflight.drain` avoids that failure mode. Launchd starts a fresh short
`goalflight_dispatch.py drain --json` pass every 60 seconds, outside any Claude,
Codex, or other controller session. Each pass normally runs for about one
second, tops up available capacity, and exits. Session end does not remove the
launchd agent, so queue draining survives compaction, shell exit, and harness
reaping.

## How

The canonical macOS drainer is a per-user LaunchAgent:

- label: `com.goalflight.drain`
- plist: `~/Library/LaunchAgents/com.goalflight.drain.plist`
- template: `scripts/templates/com.goalflight.drain.plist.tmpl`
- installer: `scripts/install-drainer.sh`
- command: `python3 <skill-root>/scripts/goalflight_dispatch.py drain --json`
- cadence: `StartInterval` 60 plus `RunAtLoad`
- log: `~/.goal-flight/drain-launchd.log`

The checked-in plist is a template only. It uses placeholders for home,
python, skill root, log path, and PATH. The installer renders machine-local
values at install time so repository files stay portable.

## Daemon log retention (macOS)

The drainer's launchd command first runs `goalflight_daemon_logs.py`, then execs
the same drain command. Each 60-second tick checks six explicit log paths with
the built-in `/usr/sbin/newsyslog`; a retention error is reported to stderr and
does not prevent the drain pass. The existing `GOALFLIGHT_DRAIN_LOG` override
still selects the drainer log. The other five paths are under `~/.goal-flight`.
No root installation, extra daemon, or Python logging framework is required.
The wrapper skips any requested log path that is itself a symlink rather than
passing it to `newsyslog`.

`scripts/templates/daemon-logs.newsyslog.conf` sets a **16 MiB allocated-disk
threshold**, size-only rotation, and **two uncompressed archives** (`.0`, `.1`).
Apple's uncompressed implementation uses count `1` for these two suffixes.
16 MiB provides about five times the measured 3.2 MB p90 worker-tail size
(p50 278 KB, maximum 147 MB). It is a rotation threshold, not a hard byte limit:
an ongoing tick can exceed it. Existing oversized files are archived whole;
their newest bytes are never trimmed to fit. Those archives expire through
subsequent size-triggered rotations, so initial disk use need not fall immediately.
Old manually named archives are outside this policy.

Writer inventory verified on the affected host (2026-09-13):

| Log | Writer and lifecycle |
| --- | --- |
| `drain-launchd.log` | `com.goalflight.drain`, one drain pass every 60 s |
| `fleet-console-fleet-launchd.log` | `com.goalflight.fleet-console.fleet`, one producer tick every 60 s, 30 s budget |
| `fleet-console-attention-launchd.log` | `com.goalflight.fleet-console.attention`, one producer tick every 20 s, 10 s budget |
| `codex-accountd.log` | Installed provider-account rotator, external `scripts/ext/codex_seatd.py tick`, one pass every 300 s |
| `grok-accountd.log` | Installed provider-account rotator, external `scripts/ext/grok_rotate.py --refresh --quiet`, one pass every 300 s |
| `codex-rotate.log` | Retired poller; current external command refuses operation without opening the log. Existing file stays subject to the same threshold. |

All five active writers use launchd `StandardOutPath` / `StandardErrorPath`,
including the provider-account jobs; none uses an in-process file logger. Launchd opens
these descriptors with `O_APPEND` at each job start and does not overlap the
same job's invocations. See [Apple's descriptor setup](https://github.com/apple-oss-distributions/launchd/blob/main/src/core.c#L4919)
and [active-job check](https://github.com/apple-oss-distributions/launchd/blob/main/src/core.c#L4164).
The fleet budgets and launch arguments are in `scripts/install-fleet-console.sh`
and `scripts/templates/com.goalflight.fleet-console.plist.tmpl`. Detached drain
workers receive their own stdout/stderr in `goalflight_dispatch.py`'s launch path.
The provider-account implementations and installed plists are machine-local, not distributed
by this template. The retired poller's historical open flags were not established.

Rotation uses **the same inode, no compression, no signals** (`BN`, plus `-s`).
Apple's [archive and replacement code](https://github.com/apple-oss-distributions/syslog/blob/main/newsyslog/newsyslog.c#L1904)
hardlinks the log to `.0`, then atomically renames a new file onto the current
pathname. Archives stay beside the regular log files on the host's filesystem,
which supports hardlinks; moving archives across filesystems would activate a
copy fallback and is unsafe for live writers. The open writer keeps its inode
and offset, so appends during and after rotation remain readable in `.0`.
The next invocation opens the fresh current pathname.
While the previous invocation remains active it is the only writer, so the new
current file cannot fill and trigger eviction of that invocation's archive.
This lifecycle is why finite archives are safe here. Copytruncate has a copy/write/
truncate race even with `O_APPEND`; SIGHUP is not a reopen protocol for these jobs.
Compression would unlink an inode that a writer could still be using.

An existing `tail -f` stays on the archived inode through the rest of the tick;
`tail -F` follows the current pathname on later invocations. Inspect `.0` for the
closing lines of a tick that crossed rotation. Finite retention does not preserve
an arbitrarily stalled reader forever. Do not reuse this policy for a permanent
writer that never reopens its file.

Activation follows deployment of the updated skill pin. Compare
`scripts/install-drainer.sh --dry-run` with the installed plist first: the
installer overwrites its arguments and immediately reloads it. For an unmodified
installation, `scripts/install-drainer.sh` installs the wrapper. If the installed
drain command has custom arguments (the affected host includes `--cross-project`),
carry those arguments into the rendered plist before replacing and reloading the
agent; do not apply a plain reinstall that drops them. Merely updating source
does not change an installed plist. Linux's documented service below is unchanged.
Fleet-console `history-data.js` retention is a separate concern; this policy only
manages the six daemon log paths.

## Install

```shell
scripts/install-drainer.sh
```

Override the skill checkout when needed:

```shell
scripts/install-drainer.sh --skill-root ~/.goal-flight/skill
```

Preview the exact rendered plist without writing files or invoking launchctl:

```shell
scripts/install-drainer.sh --dry-run
```

## Verify

```shell
scripts/install-drainer.sh --status
launchctl list com.goalflight.drain
launchctl kickstart -k gui/$UID/com.goalflight.drain
```

Queue warnings from `goalflight_status.py` use the same launchd label. If the
queue has pending rows and neither launchd nor a live drain process exists,
status reports `queue_pending_no_drainer`.

## Uninstall

```shell
scripts/install-drainer.sh --uninstall
```

The uninstall path unloads the user agent when present and removes
`~/Library/LaunchAgents/com.goalflight.drain.plist`.

## Linux Systemd Equivalent

Linux controllers should use a user-level systemd service and timer. This repo
documents the equivalent but does not install it.

`~/.config/systemd/user/goalflight-drain.service`:

```ini
[Unit]
Description=Goal Flight dispatch queue drain pass

[Service]
Type=oneshot
WorkingDirectory=%h/.goal-flight/skill
Environment=HOME=%h
Environment=PATH=%h/.local/bin:%h/.grok/bin:%h/bin:%h/.goal-flight/skill/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/env python3 %h/.goal-flight/skill/scripts/goalflight_dispatch.py drain --json
StandardOutput=append:%h/.goal-flight/drain-systemd.log
StandardError=append:%h/.goal-flight/drain-systemd.log
```

`~/.config/systemd/user/goalflight-drain.timer`:

```ini
[Unit]
Description=Run Goal Flight dispatch queue drainer every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Unit=goalflight-drain.service

[Install]
WantedBy=timers.target
```

Enable:

```shell
systemctl --user daemon-reload
systemctl --user enable --now goalflight-drain.timer
systemctl --user list-timers goalflight-drain.timer
```
