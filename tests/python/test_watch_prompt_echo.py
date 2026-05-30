#!/usr/bin/env python3
"""Regression test for the prompt-echo terminal-marker false-positive.

A worker that echoes its prompt (codex, grok) prints the prompt's own
"end with COMPLETE: ..." instruction to stdout BEFORE doing any work. Without a
guard, goalflight_watch.py matches that echoed marker and exits immediately
(observed 2026-05-30: dogfooded review jobs "completed" in ~5s). The watcher must
ignore marker lines that come verbatim from the prompt (--ignore-prompt-file) and
only complete on the worker's REAL marker.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WATCH = ROOT / "scripts" / "goalflight_watch.py"


def _run_watcher(tail: Path, status: Path, prompt: Path, ignore: bool, worker_pid: int):
    cmd = [sys.executable, str(WATCH), "--pid", str(worker_pid), "--tail", str(tail),
           "--status-json", str(status), "--poll-secs", "1", "--max-idle-secs", "30"]
    if ignore:
        cmd += ["--ignore-prompt-file", str(prompt)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    elapsed = time.time() - t0
    term = {}
    if status.exists():
        term = (json.loads(status.read_text(encoding="utf-8")).get("terminal_marker") or {})
    return proc.returncode, elapsed, term


def case_ignores_echoed_prompt_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do the review.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        sink = tail.open("wb")
        try:
            # Worker echoes the prompt (incl. the PLACEHOLDER marker), then after ~2s
            # emits the REAL terminal marker.
            worker = subprocess.Popen(
                ["bash", "-c", f'cat "{prompt}"; sleep 2; echo "COMPLETE: realdone"'],
                stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            sink.close()
        try:
            rc, elapsed, term = _run_watcher(tail, tmp / "s.json", prompt, ignore=True, worker_pid=worker.pid)
        finally:
            worker.wait()
        assert rc == 0, f"expected exit 0 (complete), got {rc}"
        assert term.get("text") == "realdone", f"must complete on the REAL marker, got {term}"
        assert elapsed > 1.5, f"must wait past the echoed prompt, elapsed={elapsed:.1f}s (false-completed?)"


def case_without_ignore_trips_on_echo() -> None:
    # Control: WITHOUT the guard, the echoed PLACEHOLDER trips the watcher early.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")  # only the echo so far
        worker = subprocess.Popen(["bash", "-c", "sleep 10"], start_new_session=True)
        try:
            rc, elapsed, term = _run_watcher(tail, tmp / "s.json", prompt, ignore=False, worker_pid=worker.pid)
        finally:
            worker.terminate()
            worker.wait()
        assert term.get("text") == "PLACEHOLDER", f"control should trip on the echo, got {term}"


def main() -> None:
    case_ignores_echoed_prompt_marker()
    case_without_ignore_trips_on_echo()
    print("OK: goalflight_watch prompt-echo guard tests pass")


if __name__ == "__main__":
    main()
