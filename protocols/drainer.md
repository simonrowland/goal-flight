# Launchd Drainer Protocol

## Why

Long-lived in-session drain loops die with the controlling session. That is the
D007/D008 worker-death family: a controller exits, the host reaper tears down
the loop, and queued dispatch rows stop launching.

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
