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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
WATCH = ROOT / "scripts" / "goalflight_watch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
import goalflight_rate_pressure  # noqa: E402


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    assert not _process_exists(proc.pid), f"pid still alive after wait: {proc.pid}"
    return proc.pid


def _run(
    worker_cmd: list[str],
    max_idle: str = "20",
    poll: str = "1",
    *,
    confirmed_idle_cpu: bool = False,
):
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        status = Path(tmp) / "status.json"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(Path(tmp) / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(Path(tmp) / "pids")
        if confirmed_idle_cpu:
            env["GOALFLIGHT_TEST_MODE"] = "1"
            env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        t0 = time.time()
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--poll-secs", poll, "--max-idle-secs", max_idle, "--foreground", "--", *worker_cmd,
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


def _run_dispatch_with_state(dispatch_id: str, worker_code: str, *, max_idle: str = "20", poll: str = "0.2"):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        state_dir = tmp_path / "state"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH),
                "--agent", "codex",
                "--dispatch-id", dispatch_id,
                "--tail", str(tail),
                "--status-json", str(status),
                "--poll-secs", poll,
                "--max-idle-secs", max_idle,
                "--foreground",
                "--",
                sys.executable, "-c", worker_code,
            ],
            capture_output=True,
            text=True,
            timeout=float(max_idle) + 30,
            env=env,
        )
        end = {}
        for line in proc.stdout.strip().splitlines():
            if line.startswith("DISPATCH-END "):
                end = json.loads(line.split(" ", 1)[1])
        payload = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {}
        record_path = state_dir / "runs.d" / f"{dispatch_id}.json"
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
        return proc.returncode, end, payload, record


def case_dispatch_usage_limit_exit_zero_is_retryable() -> None:
    worker_code = (
        "print(\"You've hit your usage limit. Please try again at 6:13 AM.\", flush=True)\n"
    )
    rc, end, payload, record = _run_dispatch_with_state("usage-limit-exit-zero", worker_code)
    assert rc == 1, (rc, end, payload, record)
    assert end.get("terminal_state") == "rate_limited", end
    assert payload.get("state") == "rate_limited", payload
    assert payload.get("liveness_state") == "rate_limited", payload
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("message") == "dispatch_worker_rate_limited", reason
    assert reason.get("reason") == "worker_dead_no_terminal_marker", reason
    assert record.get("state") == "rate_limited", record
    assert record.get("terminal_state") == "rate_limited", record
    assert record.get("liveness_state") == "rate_limited", record
    assert record.get("error", {}).get("message") == "dispatch_worker_rate_limited", record
    assert goalflight_rate_pressure.detect_rate_limit_signature(record, None), record


def case_dispatch_success_marker_with_limit_terms_stays_complete() -> None:
    worker_code = (
        "print('Docs mention usage limit, 429, try again at 6:13 AM, rate limit, at capacity.', flush=True)\n"
        "print('READY: terminal summary includes rate limit data', flush=True)\n"
    )
    rc, end, payload, record = _run_dispatch_with_state("success-marker-limit-terms", worker_code)
    assert rc == 0, (rc, end, payload, record)
    assert end.get("terminal_state") == "complete", end
    assert payload.get("state") == "complete", payload
    assert payload.get("liveness_state") == "completed", payload
    assert payload.get("reason") == "marker:READY", payload
    assert record.get("state") == "complete", record
    assert record.get("terminal_state") == "complete", record
    assert record.get("liveness_state") == "completed", record
    assert "error" not in record, record
    assert not goalflight_rate_pressure.detect_rate_limit_signature(record, payload), record


def case_dispatch_clean_complete_preserves_reason_without_rate_signal() -> None:
    worker_code = "print('COMPLETE: clean', flush=True)\n"
    rc, end, payload, record = _run_dispatch_with_state("clean-complete", worker_code)
    assert rc == 0, (rc, end, payload, record)
    assert end.get("terminal_state") == "complete", end
    assert payload.get("state") == "complete", payload
    assert payload.get("liveness_state") == "completed", payload
    assert payload.get("reason") == "marker:COMPLETE", payload
    assert record.get("state") == "complete", record
    assert record.get("terminal_state") == "complete", record
    assert record.get("liveness_state") == "completed", record
    assert record.get("reason") == "marker:COMPLETE", record
    assert "error" not in record, record
    assert not goalflight_rate_pressure.detect_rate_limit_signature(record, payload), record


def case_dispatch_worker_dead_ledger_liveness() -> None:
    worker_code = "print('worker crashed before sign-off', flush=True)\nraise SystemExit(9)\n"
    rc, end, payload, record = _run_dispatch_with_state("worker-dead-liveness", worker_code)
    assert rc == 1, (rc, end, payload, record)
    assert end.get("terminal_state") == "worker_dead", end
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("liveness_state") == "worker_dead", payload
    assert record.get("state") == "worker_dead", record
    assert record.get("terminal_state") == "worker_dead", record
    assert record.get("liveness_state") == "worker_dead", record


def case_post_terminal_idle_worker_finishes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
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
            out, err = watcher.communicate(timeout=25)
            elapsed = time.time() - t0
            assert watcher.returncode == 0, (watcher.returncode, out, err)
            assert elapsed < 18, f"post-terminal idle wait took {elapsed:.1f}s"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("liveness_state") == "completed", payload
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
        confirmed_idle_cpu=True,
    )
    try:
        assert rc == 0, f"expected exit 0 (complete), got {rc} ({end})"
        assert elapsed < 18, f"dispatch post-terminal idle wait took {elapsed:.1f}s"
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
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
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
                "--poll-secs", "0.2", "--max-idle-secs", "10", "--foreground", "--",
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
            def _status_payload() -> dict:
                try:
                    return json.loads(status.read_text(encoding="utf-8"))
                except Exception:
                    return {}

            assert _wait_for(
                lambda: started.exists()
                and status.exists()
                and _status_payload().get("state") != "starting",
                timeout=15,
            ), status.read_text(encoding="utf-8") if status.exists() else "worker/watch did not start"
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            assert _wait_for(done.exists, timeout=10), "worker died with launcher process group"
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("worker_alive") is False,
                timeout=15,
            ), status.read_text(encoding="utf-8") if status.exists() else "missing status"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("liveness_state") == "completed", payload
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)


def case_foreground_keyboard_interrupt_leaves_worker_and_watcher_running() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        started = tmp_path / "started"
        done = tmp_path / "done"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        pid_dir = Path(env["GOAL_FLIGHT_PIDFILE_DIR"])
        worker_code = (
            "import pathlib, time\n"
            f"pathlib.Path({str(started)!r}).write_text('started')\n"
            "print('worker-started', flush=True)\n"
            "time.sleep(8.0)\n"
            "print('COMPLETE: interrupt-safe done', flush=True)\n"
            f"pathlib.Path({str(done)!r}).write_text('done')\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable, str(DISPATCH),
                "--agent", "test",
                "--dispatch-id", "foreground-interrupt",
                "--tail", str(tail),
                "--status-json", str(status),
                "--poll-secs", "0.2",
                "--max-idle-secs", "10",
                "--foreground",
                "--",
                sys.executable, "-c", worker_code,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        worker_pid: int | None = None
        try:
            assert _wait_for(lambda: started.exists() and status.exists()), "worker/watch did not start"

            def status_payload() -> dict:
                try:
                    return json.loads(status.read_text(encoding="utf-8"))
                except Exception:
                    return {}

            def worker_pid_from_status() -> int | None:
                try:
                    return int(status_payload().get("worker_pid") or 0) or None
                except Exception:
                    return None

            assert _wait_for(
                lambda: status_payload().get("state") == "running" and worker_pid_from_status() is not None
            ), "watcher never published running status"
            worker_pid = worker_pid_from_status()
            assert _process_exists(worker_pid), "worker not alive before interrupt"

            proc.send_signal(signal.SIGINT)
            _out, err = proc.communicate(timeout=5)
            assert proc.returncode == 130, (proc.returncode, err)
            assert "goalflight_status.py --wait foreground-interrupt" in err, err
            assert _process_exists(worker_pid), "worker died on launcher KeyboardInterrupt"

            pidfiles = list(pid_dir.glob("*.jsonl"))
            assert len(pidfiles) == 1, pidfiles
            pidfile = pidfiles[0]
            rec = json.loads(pidfile.read_text(encoding="utf-8").splitlines()[0])
            assert rec.get("pid") == worker_pid, rec
            assert rec.get("controller_pid") == proc.pid, rec
            assert rec.get("agent", "").endswith("-bash-tail"), rec
            assert rec.get("detached") is True, "foreground interrupt must detach-stamp live worker pidfile"

            meta = goalflight_acp_client._ps_meta(worker_pid)
            if meta is not None:
                rec["started_at"], rec["cmd"] = meta
                pidfile.write_text(json.dumps(rec, sort_keys=True) + "\n", encoding="utf-8")

            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
                    patch("goalflight_compat.kill_pid",
                          side_effect=AssertionError("live foreground-interrupt worker killed")):
                killed = goalflight_acp_client.cleanup_ghosts()
            assert killed == 0, "detached foreground-interrupt worker must not be reaped"
            assert _process_exists(worker_pid), "worker died during cleanup_ghosts sweep"
            if meta is not None:
                assert pidfile.exists(), "live detached pidfile stays available for re-attach"
            else:
                assert not pidfile.exists(), "unverifiable detached pidfile is safely unlinked"

            assert _wait_for(done.exists, timeout=8), "worker did not finish after launcher interrupt"
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("state") == "complete",
                timeout=8,
            ), status.read_text(encoding="utf-8") if status.exists() else "missing status"
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            if worker_pid and _process_exists(worker_pid):
                try:
                    os.killpg(worker_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


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


def case_detached_watcher_ignores_dead_controller_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        tail.write_text("", encoding="utf-8")
        dead_controller = _dead_pid()
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
                "--controller-pid", str(dead_controller),
                "--detached",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("state") in {"running", "running_quiet"},
                timeout=5,
            ), status.read_text(encoding="utf-8") if status.exists() else "missing detached watcher status"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("detached") is True, payload
            assert payload.get("state") not in {"orphaned", "controller_dead"}, payload
            assert watcher.poll() is None, "detached watcher exited on dead controller pid"
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=5)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)


def case_non_detached_watcher_dead_controller_remains_orphaned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        tail.write_text("", encoding="utf-8")
        dead_controller = _dead_pid()
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
                "--controller-pid", str(dead_controller),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            out, err = watcher.communicate(timeout=8)
            assert watcher.returncode == 3, (watcher.returncode, out, err)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "orphaned", payload
            assert payload.get("reason") == "controller_dead", payload
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=5)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)


def main() -> None:
    case_crash_detected_promptly()
    case_finished_via_marker()
    case_dispatch_usage_limit_exit_zero_is_retryable()
    case_dispatch_success_marker_with_limit_terms_stays_complete()
    case_dispatch_clean_complete_preserves_reason_without_rate_signal()
    case_dispatch_worker_dead_ledger_liveness()
    case_post_terminal_idle_worker_finishes()
    case_dispatch_post_terminal_idle_returns_success()
    case_worker_and_watcher_survive_launcher_pgroup_sigterm()
    case_foreground_keyboard_interrupt_leaves_worker_and_watcher_running()
    case_watcher_sigterm_flushes_non_running_status()
    case_detached_watcher_ignores_dead_controller_pid()
    case_non_detached_watcher_dead_controller_remains_orphaned()
    print("OK: goalflight_dispatch crash-safe tests pass")


if __name__ == "__main__":
    main()
