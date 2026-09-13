#!/usr/bin/env python3
"""Run macOS daemon-log retention, then exec the scheduled drain tick.

These launchd jobs reopen stdout/stderr on every non-overlapping invocation.
newsyslog keeps an uncompressed archive of the same inode: an in-flight writer
and tail retain it until the job exits; the next invocation opens the pathname.
See protocols/drainer.md for the writer inventory and retention limits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> None:
    drain_log, *command = argv
    log_dir = Path.home() / ".goal-flight"
    logs = [drain_log] + [str(log_dir / name) for name in (
        "fleet-console-fleet-launchd.log",
        "fleet-console-attention-launchd.log",
        "codex-seatd.log",
        "grok-seatd.log",
        "codex-rotate.log",
    )]
    config = Path(__file__).resolve().parent / "templates/daemon-logs.newsyslog.conf"
    try:
        subprocess.run(
            ["/usr/sbin/newsyslog", "-r", "-s", "-f", str(config), *logs],
            check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Housekeeping failure must be visible without disabling queue draining.
        print(f"daemon log retention failed: {exc}", file=sys.stderr, flush=True)
    os.execv(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
