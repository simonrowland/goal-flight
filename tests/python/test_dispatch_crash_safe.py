#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py crash-safe dispatch.

Locks the 2026-05-30 zombie-reap fix: a worker that exits WITHOUT a terminal
marker must be detected as worker_dead PROMPTLY (via pid-death), NOT escape only
via the much slower idle-timeout. If the dispatched worker is left un-reaped it
becomes a POSIX zombie, os.kill(pid, 0) false-positives "alive", and the crash is
missed until idle-timeout -> the prompt-detection assertion below fails.

Also covers the clean-finish (terminal marker) path and verifies the watcher's
real exit code is propagated by the dispatcher (no masking).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"


def _run(worker_cmd: list[str], max_idle: str = "20", poll: str = "1"):
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        status = Path(tmp) / "status.json"
        t0 = time.time()
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--poll-secs", poll, "--max-idle-secs", max_idle, "--", *worker_cmd,
            ],
            capture_output=True, text=True, timeout=float(max_idle) + 30,
        )
        elapsed = time.time() - t0
        lines = proc.stdout.strip().splitlines()
        end = {}
        if lines and lines[-1].startswith("DISPATCH-END "):
            end = json.loads(lines[-1].split(" ", 1)[1])
        return proc.returncode, elapsed, end


def case_crash_detected_promptly() -> None:
    # Worker exits after ~2s with NO terminal marker, leaving a lingering child.
    rc, elapsed, end = _run(["bash", "-c", "sleep 8 & echo started; sleep 2; exit 9"], max_idle="20", poll="1")
    assert rc == 1, f"expected exit 1 (worker_dead), got {rc} ({end})"
    assert end.get("terminal_state") == "worker_dead", end
    # Zombie-regression guard: caught by pid-death (~3s), NOT idle-timeout (20s).
    assert elapsed < 12, f"crash took {elapsed:.1f}s — zombie regression (expected ~3s, not idle-timeout)"


def case_finished_via_marker() -> None:
    rc, elapsed, end = _run(
        ["bash", "-c", "echo working; sleep 1; printf 'COMPLETE: ok\\n'; sleep 0.3"], max_idle="20", poll="1"
    )
    assert rc == 0, f"expected exit 0 (complete), got {rc} ({end})"
    assert end.get("terminal_state") == "complete", end


def main() -> None:
    case_crash_detected_promptly()
    case_finished_via_marker()
    print("OK: goalflight_dispatch crash-safe tests pass")


if __name__ == "__main__":
    main()
