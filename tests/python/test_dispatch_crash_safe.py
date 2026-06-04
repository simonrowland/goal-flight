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

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("crash-safety tests launch POSIX bash workers")

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
WATCH = ROOT / "scripts" / "goalflight_watch.py"


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run(worker_cmd: list[str], max_idle: str = "20", poll: str = "1"):
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        status = Path(tmp) / "status.json"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(Path(tmp) / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(Path(tmp) / "pids")
        t0 = time.time()
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--poll-secs", poll, "--max-idle-secs", max_idle, "--", *worker_cmd,
            ],
            capture_output=True, text=True, timeout=float(max_idle) + 30, env=env,
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


def case_post_terminal_idle_worker_finishes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        worker_code = (
            "import time\n"
            "print('COMPLETE: done', flush=True)\n"
            "time.sleep(20)\n"
        )
        with tail.open("w", encoding="utf-8") as tail_out:
            worker = subprocess.Popen(
                [sys.executable, "-c", worker_code],
                stdout=tail_out,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=env,
            )
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--agent", "test",
                "--poll-secs", "0.2",
                "--max-idle-secs", "1",
                "--pgid", str(worker.pid),
                "--stay-after-terminal",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        t0 = time.time()
        try:
            out, err = watcher.communicate(timeout=15)
            elapsed = time.time() - t0
            assert watcher.returncode == 0, (watcher.returncode, out, err)
            assert elapsed < 12, f"post-terminal idle wait took {elapsed:.1f}s"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("worker_alive") is True, payload
            assert payload.get("reason") == "marker:COMPLETE:post_terminal_idle_timeout", payload
            assert worker.poll() is None, "worker should still be alive until test cleanup"
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=5)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)


def case_dispatch_post_terminal_idle_returns_success() -> None:
    rc, elapsed, end = _run(
        [
            sys.executable,
            "-c",
            "import time; print('COMPLETE: dispatch done', flush=True); time.sleep(20)",
        ],
        max_idle="1",
        poll="0.2",
    )
    try:
        assert rc == 0, f"expected exit 0 (complete), got {rc} ({end})"
        assert elapsed < 12, f"dispatch post-terminal idle wait took {elapsed:.1f}s"
        assert end.get("terminal_state") == "complete", end
        assert end.get("watcher_exit") == 0, end
        assert end.get("reason") == "marker:COMPLETE:post_terminal_idle_timeout", end
        assert end.get("worker_still_alive") is True, end
    finally:
        worker_pid = end.get("worker_pid")
        if worker_pid:
            try:
                os.killpg(int(worker_pid), signal.SIGTERM)
            except ProcessLookupError:
                pass


def case_worker_and_watcher_survive_launcher_pgroup_sigterm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        started = tmp_path / "started"
        done = tmp_path / "done"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        worker_code = (
            "import pathlib, sys, time\n"
            f"pathlib.Path({str(started)!r}).write_text('started')\n"
            "print('worker-started', flush=True)\n"
            "time.sleep(0.5)\n"
            "print('COMPLETE: code done', flush=True)\n"
            "time.sleep(1.0)\n"
            f"pathlib.Path({str(done)!r}).write_text('done')\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--poll-secs", "0.2", "--max-idle-secs", "10", "--",
                sys.executable, "-c", worker_code,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )
        try:
            assert _wait_for(lambda: started.exists() and status.exists()), "worker/watch did not start"
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            assert _wait_for(done.exists, timeout=5), "worker died with launcher process group"
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("worker_alive") is False,
                timeout=5,
            ), status.read_text(encoding="utf-8") if status.exists() else "missing status"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)


def case_watcher_sigterm_flushes_non_running_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        tail.write_text("", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            assert _wait_for(lambda: status.exists()), "watcher did not write initial status"
            os.kill(watcher.pid, signal.SIGTERM)
            watcher.wait(timeout=5)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "watcher_stopped", payload
            assert payload.get("worker_alive") is True, payload
            assert str(payload.get("reason", "")).startswith("signal:SIGTERM"), payload
        finally:
            worker.terminate()
            worker.wait(timeout=5)


def main() -> None:
    case_crash_detected_promptly()
    case_finished_via_marker()
    case_post_terminal_idle_worker_finishes()
    case_dispatch_post_terminal_idle_returns_success()
    case_worker_and_watcher_survive_launcher_pgroup_sigterm()
    case_watcher_sigterm_flushes_non_running_status()
    print("OK: goalflight_dispatch crash-safe tests pass")


if __name__ == "__main__":
    main()
