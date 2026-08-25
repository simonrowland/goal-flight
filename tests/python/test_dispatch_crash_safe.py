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
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
WATCH = ROOT / "scripts" / "goalflight_watch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
import goalflight_rate_pressure  # noqa: E402
import goalflight_watch  # noqa: E402


_SLOW_INTERRUPT_EXIT_SECS = 6.0
# The loaded-exit reproducer below measures a six-second post-SIGINT delay,
# beyond the former five-second assumption. Five reproducer windows is the
# 30-second hang ceiling: event waits still return immediately when observed,
# while a process that never exits cannot hang the suite indefinitely.
_EVENT_HANG_CEILING_SECS = _SLOW_INTERRUPT_EXIT_SECS * 5
_EVENT_POLL_SECS = 0.1


def _isolate_state_env(env: dict[str, str], base: Path) -> None:
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
    ):
        env.pop(key, None)
    env["GOALFLIGHT_STATE_DIR"] = str(base / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(base / "state" / "dispatch")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(base / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(base / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(base / "messages")
    env["GOALFLIGHT_FLEET_DIR"] = str(base / "fleet")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(base / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(base / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_ROOT"] = str(ROOT)


def _wait_for(
    predicate,
    *,
    ceiling: float = _EVENT_HANG_CEILING_SECS,
    interval: float = _EVENT_POLL_SECS,
) -> bool:
    deadline = time.monotonic() + ceiling
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _observe_process_exit(
    proc: subprocess.Popen,
    *,
    expected_returncode: int | None = None,
    stderr_contains: str | None = None,
    ceiling: float = _EVENT_HANG_CEILING_SECS,
):
    """Collect output when exit is observed, bounded only against a true hang."""
    deadline = time.monotonic() + ceiling
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out, err = proc.communicate()
            raise AssertionError(
                f"process {proc.pid} did not exit within {ceiling:.1f}s hang ceiling; "
                f"stdout={out!r}; stderr={err!r}"
            )
        try:
            out, err = proc.communicate(timeout=min(_EVENT_POLL_SECS, remaining))
        except subprocess.TimeoutExpired:
            continue
        if expected_returncode is not None:
            assert proc.returncode == expected_returncode, (proc.returncode, out, err)
        if stderr_contains is not None:
            assert stderr_contains in (err or ""), err
        return out, err


def _observe_thread_exit(
    thread: threading.Thread,
    *,
    ceiling: float = _EVENT_HANG_CEILING_SECS,
) -> None:
    deadline = time.monotonic() + ceiling
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"thread {thread.name!r} did not exit within {ceiling:.1f}s hang ceiling"
            )
        thread.join(min(_EVENT_POLL_SECS, remaining))


def _delay_interrupt_exit(command: list[str]) -> list[str]:
    """Proxy SIGINT to a real child, then emulate loaded scheduling before exit."""
    proxy_code = (
        "import signal, subprocess, sys, time\n"
        "delay = float(sys.argv[1])\n"
        "child = subprocess.Popen(sys.argv[2:])\n"
        "def forward_interrupt(signum, _frame):\n"
        "    child.send_signal(signum)\n"
        "signal.signal(signal.SIGINT, forward_interrupt)\n"
        "returncode = child.wait()\n"
        "time.sleep(delay)\n"
        "raise SystemExit(returncode)\n"
    )
    return [
        sys.executable,
        "-c",
        proxy_code,
        str(_SLOW_INTERRUPT_EXIT_SECS),
        *command,
    ]


def case_event_wait_rejects_hangs_and_wrong_exit_codes() -> None:
    hanging = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            _observe_process_exit(hanging, expected_returncode=0, ceiling=0.2)
        except AssertionError as exc:
            assert "did not exit within 0.2s hang ceiling" in str(exc), exc
        else:
            raise AssertionError("a process that never exits passed the hang ceiling")
    finally:
        if hanging.poll() is None:
            hanging.terminate()
            _observe_process_exit(hanging)

    wrong_exit = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('expected hint', file=sys.stderr); raise SystemExit(7)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _observe_process_exit(
            wrong_exit,
            expected_returncode=130,
            stderr_contains="expected hint",
        )
    except AssertionError as exc:
        assert "7" in str(exc), exc
    else:
        raise AssertionError("a process with the wrong exit code passed observation")


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
    _observe_process_exit(proc, expected_returncode=0)
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
        _isolate_state_env(env, Path(tmp))
        if confirmed_idle_cpu:
            env["GOALFLIGHT_TEST_MODE"] = "1"
            env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        t0 = time.time()
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH), "--unregistered-forced",
                "--cwd", tmp,
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--dispatch-id", "crash-safe-run",
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
        ["bash", "-c", "echo working; sleep 1; printf 'COMPLETE: crash-safe-run — ok\\n'; sleep 0.3"], max_idle="20", poll="1"
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
        _isolate_state_env(env, tmp_path)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        proc = subprocess.run(
            [
                sys.executable, str(DISPATCH), "--unregistered-forced",
                "--cwd", str(tmp_path),
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


def case_dispatch_usage_limit_exit_zero_is_exhausted() -> None:
    worker_code = (
        "print(\"You've hit your usage limit. Please try again at 6:13 AM.\", flush=True)\n"
    )
    rc, end, payload, record = _run_dispatch_with_state("usage-limit-exit-zero", worker_code)
    assert rc == 1, (rc, end, payload, record)
    assert end.get("terminal_state") == "quota_exhausted", end
    assert payload.get("state") == "quota_exhausted", payload
    assert payload.get("liveness_state") == "quota_exhausted", payload
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("message") == "dispatch_worker_limit_reached", reason
    assert reason.get("limit_kind") == "exhausted", reason
    assert reason.get("reason") == (
        "worker_dead_no_terminal_marker:death_cause=no_evidence"
    ), reason
    assert record.get("state") == "quota_exhausted", record
    assert record.get("terminal_state") == "quota_exhausted", record
    assert record.get("liveness_state") == "quota_exhausted", record
    assert record.get("error", {}).get("message") == "dispatch_worker_limit_reached", record
    assert record.get("limit_kind") == "exhausted", record
    assert goalflight_rate_pressure.detect_rate_limit_signature(record, None), record


def case_dispatch_success_marker_with_limit_terms_stays_complete() -> None:
    worker_code = (
        "print('Docs mention usage limit, 429, try again at 6:13 AM, rate limit, at capacity.', flush=True)\n"
        "print('READY: success-marker-limit-terms — terminal summary includes rate limit data', flush=True)\n"
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
    worker_code = "print('COMPLETE: clean-complete — clean', flush=True)\n"
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


def case_post_terminal_idle_worker_times_out_inconclusively() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        worker_code = (
            "import json, pathlib, time\n"
            f"status = pathlib.Path({str(status)!r})\n"
            "print('COMPLETE: post-terminal-idle — done', flush=True)\n"
            "deadline = time.monotonic() + 5\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        if json.loads(status.read_text()).get('state') == 'running_after_terminal':\n"
            "            break\n"
            "    except Exception:\n"
            "        pass\n"
            "    time.sleep(0.02)\n"
            "print('TL;DR: summary flushed after the candidate', flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
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
                "--dispatch-id", "post-terminal-idle",
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
            out, err = _observe_process_exit(watcher)
            elapsed = time.time() - t0
            assert watcher.returncode == 1, (watcher.returncode, out, err)
            assert elapsed < 18, f"post-terminal idle wait took {elapsed:.1f}s"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "inconclusive_timeout", payload
            assert payload.get("liveness_state") == "inconclusive_timeout", payload
            assert payload.get("worker_alive") is True, payload
            assert payload.get("reason") == "marker:COMPLETE:post_terminal_idle_timeout", payload
            assert payload.get("terminal_pending_state") == "complete", payload
            evidence = payload.get("last_discarded_terminal_evidence") or {}
            assert evidence.get("kind") == "COMPLETE", evidence
            assert evidence.get("dispatch_id_binding") == "post-terminal-idle", evidence
            assert isinstance(evidence.get("offset"), int) and evidence["offset"] > 0, evidence
            assert not payload.get("terminal_marker"), payload
            assert worker.poll() is None, "worker should still be alive until test cleanup"
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                _observe_process_exit(watcher)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                _observe_process_exit(worker)


def case_worker_death_reconciliation_streams_beyond_tail_window() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        with tail.open("wb") as stream:
            stream.write(b"COMPLETE: deep-death-reconcile -- durable marker\n")
            stream.seek(11 * 1024 * 1024, os.SEEK_SET)
            stream.write(b"trailing worker log after sparse gap\n")
        assert tail.stat().st_size > 10 * 1024 * 1024

        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(WATCH),
                "--pid",
                str(_dead_pid()),
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--dispatch-id",
                "deep-death-reconcile",
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "20",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdout, stderr = _observe_process_exit(proc)
        payload = json.loads(status.read_text(encoding="utf-8"))
        assert proc.returncode == 0, (proc.returncode, stdout, stderr, payload)
        assert payload.get("state") == "complete", payload
        assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", payload
        assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload


def case_full_file_reconciliation_caps_newline_free_reads() -> None:
    """A huge physical line is skipped through sized readline calls only."""

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        block = b"x" * (1024 * 1024)
        with tail.open("wb") as handle:
            for _ in range(24):
                handle.write(block)
            handle.write(
                (
                    "\n"
                    "!COMPLETE: bounded-stream — earlier accepted marker\n"
                    "!READY: foreign-dispatch — rejected later marker\n"
                    "!READY: bounded-stream — docs-private/research/bounded/findings.md\n"
                ).encode("utf-8")
            )

        original_open = Path.open
        observed_sizes: list[int] = []

        class SizedReadlineOnly:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def readline(self, size: int = -1):
                observed_sizes.append(size)
                assert 0 < size <= goalflight_watch.STREAM_READ_CHUNK_CHARS, size
                return self.wrapped.readline(size)

            def __iter__(self):
                raise AssertionError("stream reconciliation used uncapped file iteration")

        def tracked_open(path: Path, *args, **kwargs):
            return SizedReadlineOnly(original_open(path, *args, **kwargs))

        with patch.object(Path, "open", tracked_open):
            terminal = goalflight_watch._full_file_terminal_marker(
                tail,
                prompt_prefix=[],
                suppress_unfenced_prompt_markers=True,
                kimi_output=False,
                expected_dispatch_id="bounded-stream",
            )

        assert observed_sizes, "streaming reconciliation did not call readline"
        assert max(observed_sizes) <= goalflight_watch.STREAM_READ_CHUNK_CHARS
        assert terminal is not None, terminal
        assert terminal.get("kind") == "READY", terminal
        assert terminal.get("text") == (
            "bounded-stream — docs-private/research/bounded/findings.md"
        ), terminal


def case_bound_marker_survives_stream_read_cap_boundary() -> None:
    dispatch_id = "bounded-marker-line"
    marker_prefix = f"!COMPLETE: {dispatch_id} — "

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        for total_chars in (
            goalflight_watch.STREAM_READ_CHUNK_CHARS,
            goalflight_watch.STREAM_READ_CHUNK_CHARS + 1,
        ):
            line = (
                marker_prefix
                + ("x" * (total_chars - len(marker_prefix) - 1))
                + "\n"
            )
            assert len(line) == total_chars
            tail.write_text(line, encoding="utf-8")

            terminal = goalflight_watch._full_file_terminal_marker(
                tail,
                prompt_prefix=[],
                suppress_unfenced_prompt_markers=True,
                kimi_output=False,
                expected_dispatch_id=dispatch_id,
            )

            assert terminal is not None, (total_chars, terminal)
            assert terminal.get("kind") == "COMPLETE", terminal
            assert str(terminal.get("text") or "").startswith(dispatch_id), terminal


def test_oversized_prefix_requires_terminated_dispatch_id() -> None:
    dispatch_id = "edge-bound"
    marker_prefix = f"!COMPLETE: {dispatch_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(marker_prefix + "-foreign" + ("x" * 80) + "\n", encoding="utf-8")
        bounded_iter = goalflight_watch._iter_bounded_text_lines

        def edge_bounded_iter(handle):
            return bounded_iter(handle, max_chars=len(marker_prefix))

        with patch.object(
            goalflight_watch,
            "_iter_bounded_text_lines",
            edge_bounded_iter,
        ):
            terminal = goalflight_watch._full_file_terminal_marker(
                tail,
                prompt_prefix=[],
                suppress_unfenced_prompt_markers=True,
                kimi_output=False,
                expected_dispatch_id=dispatch_id,
            )

    assert terminal is None, terminal


def test_oversized_line_marker_in_second_of_three_chunks_is_documented_miss() -> None:
    dispatch_id = "chunk-two"
    chunk_chars = 64
    # Only the first bounded chunk of an oversized physical line is parsed.
    # This marker begins in chunk 2 of a 3-chunk line and is deliberately missed.
    line = (
        ("x" * chunk_chars)
        + f"!COMPLETE: {dispatch_id} — later chunk"
        + ("y" * (2 * chunk_chars))
        + "\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(line, encoding="utf-8")
        bounded_iter = goalflight_watch._iter_bounded_text_lines

        def three_chunk_iter(handle):
            return bounded_iter(handle, max_chars=chunk_chars)

        with patch.object(
            goalflight_watch,
            "_iter_bounded_text_lines",
            three_chunk_iter,
        ):
            terminal = goalflight_watch._full_file_terminal_marker(
                tail,
                prompt_prefix=[],
                suppress_unfenced_prompt_markers=True,
                kimi_output=False,
                expected_dispatch_id=dispatch_id,
            )

    assert terminal is None, terminal


def case_stability_recheck_uses_its_own_growth_baseline() -> None:
    first_marker = {
        "line": 1,
        "kind": "COMPLETE",
        "text": "recheck-baseline — first candidate",
    }
    rechecked_marker = {
        "line": 2,
        "kind": "COMPLETE",
        "text": "recheck-baseline — stable replacement",
    }

    def scan_result(marker: dict, size: int) -> goalflight_watch.TailScanResult:
        return goalflight_watch.TailScanResult(
            markers=[marker],
            mail_markers=[],
            terminal=marker,
            size=size,
            content_bytes=0,
            validation_bytes=0,
            lines_materialized=0,
            resynced=False,
            resync_reason=None,
            fence_unbalanced=False,
        )

    class FakeScanner:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def scan(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return scan_result(first_marker, 100)
            return scan_result(rechecked_marker, 200)

    class FakeTraceLiveness:
        def __init__(self, **_kwargs):
            pass

        def sample(self, **_kwargs):
            return {"trace_active": False}

    clock = [0.0]

    def fake_active_monotonic() -> float:
        clock[0] += 0.5
        return clock[0]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        tail.write_text("synthetic tail\n", encoding="utf-8")
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        argv = [
            str(WATCH),
            "--pid",
            "4242",
            "--pgid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            "recheck-baseline",
            "--poll-secs",
            "0.1",
            "--max-idle-secs",
            "0.1",
            "--stay-after-terminal",
        ]
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch.object(sys, "argv", argv), \
                patch.object(goalflight_watch, "IncrementalTailScanner", FakeScanner), \
                patch.object(goalflight_watch, "TraceLiveness", FakeTraceLiveness), \
                patch.object(goalflight_watch, "worker_alive", return_value=(True, "match", {"pid": 4242})), \
                patch.object(goalflight_watch, "pgroup_cpu_pct", return_value=0.0), \
                patch.object(goalflight_watch, "system_starved", return_value=False), \
                patch.object(goalflight_watch, "active_monotonic", side_effect=fake_active_monotonic), \
                patch.object(goalflight_watch.time, "sleep", return_value=None), \
                patch.object(goalflight_watch.signal, "signal", return_value=None), \
                patch.object(goalflight_watch.atexit, "register", return_value=None), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        payload = json.loads(status.read_text(encoding="utf-8"))
        assert rc == 1, (rc, output.getvalue(), payload)
        assert "WATCHER-DISCARD" not in output.getvalue(), output.getvalue()
        assert payload.get("state") == "inconclusive_timeout", payload
        assert payload.get("terminal_marker", {}).get("text") == rechecked_marker["text"], payload
        assert payload.get("reason") == "marker:COMPLETE:post_terminal_idle_timeout", payload


def case_stability_recheck_detects_growth_after_surviving_candidate() -> None:
    survivor = {
        "line": 1,
        "kind": "COMPLETE",
        "text": "recheck-survivor — same candidate",
    }

    def scan_result(marker: dict | None, size: int) -> goalflight_watch.TailScanResult:
        return goalflight_watch.TailScanResult(
            markers=[marker] if marker else [],
            mail_markers=[],
            terminal=marker,
            size=size,
            content_bytes=0,
            validation_bytes=0,
            lines_materialized=0,
            resynced=False,
            resync_reason=None,
            fence_unbalanced=False,
        )

    class FakeScanner:
        observed_sizes: list[int] = []

        def __init__(self, *_args, **_kwargs):
            self.calls = 0
            type(self).observed_sizes = []

        def scan(self, **_kwargs):
            self.calls += 1
            if self.calls <= 2:
                size = 100
            elif self.calls <= 4:
                size = 200
            else:
                # After the growth discard, the same marker reappears at a new
                # stable offset and must receive this fresh baseline.
                size = 300
            type(self).observed_sizes.append(size)
            return scan_result(dict(survivor), size)

    class FakeTraceLiveness:
        def __init__(self, **_kwargs):
            pass

        def sample(self, **_kwargs):
            return {"trace_active": False}

    clock = [0.0]

    def fake_active_monotonic() -> float:
        clock[0] += 2.0
        return clock[0]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        tail.write_text("synthetic tail\n", encoding="utf-8")
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        argv = [
            str(WATCH),
            "--pid", "4242",
            "--pgid", "4242",
            "--tail", str(tail),
            "--status-json", str(status),
            "--dispatch-id", "recheck-survivor",
            "--poll-secs", "0.1",
            "--max-idle-secs", "0.1",
            "--stay-after-terminal",
        ]
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch.object(sys, "argv", argv), \
                patch.object(goalflight_watch, "IncrementalTailScanner", FakeScanner), \
                patch.object(goalflight_watch, "TraceLiveness", FakeTraceLiveness), \
                patch.object(goalflight_watch, "worker_alive", return_value=(True, "match", {"pid": 4242})), \
                patch.object(goalflight_watch, "pgroup_cpu_pct", return_value=0.0), \
                patch.object(goalflight_watch, "system_starved", return_value=False), \
                patch.object(goalflight_watch, "active_monotonic", side_effect=fake_active_monotonic), \
                patch.object(goalflight_watch.time, "sleep", return_value=None), \
                patch.object(goalflight_watch.signal, "signal", return_value=None), \
                patch.object(goalflight_watch.atexit, "register", return_value=None), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        payload = json.loads(status.read_text(encoding="utf-8"))
        assert rc == 1, (rc, output.getvalue(), payload)
        assert FakeScanner.observed_sizes[:3] == [100, 100, 200]
        assert output.getvalue().count("WATCHER-DISCARD") == 1, output.getvalue()
        evidence = payload.get("last_discarded_terminal_evidence") or {}
        assert evidence.get("offset") == 100, payload
        assert evidence.get("marker", {}).get("text") == survivor["text"], payload
        assert payload.get("tail_scan", {}).get("offset") == 300, payload
        assert payload.get("terminal_marker", {}).get("text") == survivor["text"], payload
        assert payload.get("reason") == "marker:COMPLETE:post_terminal_idle_timeout", payload


def case_post_terminal_busy_worker_stays_armed_after_grace() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "50.0"
        worker_code = (
            "import time\n"
            "print('COMPLETE: post-terminal-busy — done', flush=True)\n"
            "while True:\n"
            "    time.sleep(1)\n"
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
                "--dispatch-id", "post-terminal-busy",
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
        try:
            deadline = time.monotonic() + 7
            payload = {}
            while time.monotonic() < deadline:
                if status.exists():
                    payload = json.loads(status.read_text(encoding="utf-8"))
                time.sleep(0.1)
            assert watcher.poll() is None, "busy live worker must stay watched past exit grace"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "running_after_terminal", payload
            assert payload.get("liveness_state") == "running_quiet", payload
            assert payload.get("worker_alive") is True, payload
            assert payload.get("terminal_pending_state") == "complete", payload
            assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload
            assert payload.get("reason") == "marker:COMPLETE:worker_alive", payload
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                _observe_process_exit(watcher)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                _observe_process_exit(worker)


def case_post_terminal_delayed_worker_exit_is_observed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "50.0"
        worker_code = (
            "import time\n"
            "print('COMPLETE: post-terminal-delayed — delayed exit', flush=True)\n"
            "time.sleep(2)\n"
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
                "--dispatch-id", "post-terminal-delayed",
                "--agent", "test",
                "--poll-secs", "0.2",
                "--max-idle-secs", "20",
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
        worker_reaper = threading.Thread(target=worker.wait)
        worker_reaper.start()
        t0 = time.monotonic()
        try:
            out, err = _observe_process_exit(watcher)
            elapsed = time.monotonic() - t0
            assert watcher.returncode == 0, (watcher.returncode, out, err)
            assert elapsed >= 1.5, f"watcher truncated the delayed exit after {elapsed:.1f}s"
            assert elapsed < 6, f"watcher missed the delayed worker exit after {elapsed:.1f}s"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("liveness_state") == "completed", payload
            assert payload.get("worker_alive") is False, payload
            assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload
            assert payload.get("reason") == "marker:COMPLETE", payload
        finally:
            _observe_thread_exit(worker_reaper)
            if watcher.poll() is None:
                watcher.terminate()
                _observe_process_exit(watcher)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                _observe_process_exit(worker)


def case_dispatch_post_terminal_idle_returns_inconclusive() -> None:
    rc, elapsed, end = _run(
        [
            sys.executable,
            "-c",
            "import time; print('COMPLETE: crash-safe-run — dispatch done', flush=True); time.sleep(20)",
        ],
        max_idle="1",
        poll="0.2",
        confirmed_idle_cpu=True,
    )
    try:
        assert rc == 1, f"expected exit 1 (inconclusive), got {rc} ({end})"
        assert elapsed < 18, f"dispatch post-terminal idle wait took {elapsed:.1f}s"
        assert end.get("terminal_state") == "inconclusive_timeout", end
        assert end.get("watcher_exit") == 1, end
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
        _isolate_state_env(env, tmp_path)
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        env["GOALFLIGHT_TEST_MODE"] = "1"
        env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
        worker_code = (
            "import pathlib, sys, time\n"
            f"pathlib.Path({str(started)!r}).write_text('started')\n"
            "print('worker-started', flush=True)\n"
            "time.sleep(0.5)\n"
            "print('COMPLETE: launcher-pgroup — code done', flush=True)\n"
            "time.sleep(1.0)\n"
            f"pathlib.Path({str(done)!r}).write_text('done')\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable, str(DISPATCH), "--unregistered-forced",
                "--cwd", str(tmp_path),
                "--agent", "test", "--tail", str(tail), "--status-json", str(status),
                "--dispatch-id", "launcher-pgroup",
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
            ), status.read_text(encoding="utf-8") if status.exists() else "worker/watch did not start"
            os.killpg(proc.pid, signal.SIGTERM)
            _observe_process_exit(proc)
            assert _wait_for(done.exists), "worker died with launcher process group"
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("worker_alive") is False,
            ), status.read_text(encoding="utf-8") if status.exists() else "missing status"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("liveness_state") == "completed", payload
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                _observe_process_exit(proc)


def case_foreground_keyboard_interrupt_leaves_worker_and_watcher_running() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        started = tmp_path / "started"
        done = tmp_path / "done"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
        env.pop("GOALFLIGHT_CONTROLLER_SESSION_ID", None)
        env["GOALFLIGHT_CONTROLLER_PID"] = "99999991"
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
        pid_dir = Path(env["GOAL_FLIGHT_PIDFILE_DIR"])
        worker_code = (
            "import pathlib, time\n"
            f"pathlib.Path({str(started)!r}).write_text('started')\n"
            "print('worker-started', flush=True)\n"
            "time.sleep(8.0)\n"
            "print('COMPLETE: foreground-interrupt — interrupt-safe done', flush=True)\n"
            f"pathlib.Path({str(done)!r}).write_text('done')\n"
        )
        dispatch_command = [
            sys.executable, str(DISPATCH), "--unregistered-forced",
            # Pin the project root to the sandbox: the controller registry
            # is per-project, so inheriting this repo as cwd would let a
            # registered local controller be inferred as this dispatch's
            # owner and break the unowned assertions below.
            "--cwd", str(tmp_path),
            "--agent", "test",
            "--dispatch-id", "foreground-interrupt",
            "--tail", str(tail),
            "--status-json", str(status),
            "--poll-secs", "0.2",
            "--max-idle-secs", "10",
            "--foreground",
            "--",
            sys.executable, "-c", worker_code,
        ]
        proc = subprocess.Popen(
            _delay_interrupt_exit(dispatch_command),
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
            interrupt_started = time.monotonic()
            _out, err = _observe_process_exit(
                proc,
                expected_returncode=130,
                stderr_contains="goalflight_status.py --wait foreground-interrupt",
            )
            interrupt_elapsed = time.monotonic() - interrupt_started
            assert interrupt_elapsed >= _SLOW_INTERRUPT_EXIT_SECS - 0.5, interrupt_elapsed
            assert _process_exists(worker_pid), "worker died on launcher KeyboardInterrupt"

            pidfiles = list(pid_dir.glob("*.jsonl"))
            assert len(pidfiles) == 1, pidfiles
            pidfile = pidfiles[0]
            rec = json.loads(pidfile.read_text(encoding="utf-8").splitlines()[0])
            assert rec.get("pid") == worker_pid, rec
            assert pidfile.name.startswith("unowned."), pidfile
            assert rec.get("controller_session_id") is None, rec
            assert rec.get("controller_pid") is None, rec
            assert rec.get("controller_pid") != proc.pid, rec
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
            assert pidfile.exists(), "live unowned pidfile stays available for re-attach"

            assert _wait_for(done.exists), "worker did not finish after launcher interrupt"
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("state") == "complete",
            ), status.read_text(encoding="utf-8") if status.exists() else "missing status"
        finally:
            if proc.poll() is None:
                proc.terminate()
                _observe_process_exit(proc)
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
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        tail.write_text("", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--dispatch-id", "sigterm-flush",
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=env,
        )
        try:
            assert _wait_for(lambda: status.exists()), "watcher did not write initial status"
            os.kill(watcher.pid, signal.SIGTERM)
            _observe_process_exit(watcher)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "watcher_stopped", payload
            assert payload.get("worker_alive") is True, payload
            assert str(payload.get("reason", "")).startswith("signal:SIGTERM"), payload
        finally:
            worker.terminate()
            _observe_process_exit(worker)


def case_detached_watcher_ignores_dead_controller_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        tail.write_text("", encoding="utf-8")
        dead_controller = _dead_pid()
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker.pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--dispatch-id", "detached-dead-controller",
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
                "--controller-session-id", "dead-controller-session",
                "--controller-pid", str(dead_controller),
                "--detached",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=env,
        )
        try:
            assert _wait_for(
                lambda: status.exists()
                and json.loads(status.read_text(encoding="utf-8")).get("state") in {"running", "running_quiet"},
            ), status.read_text(encoding="utf-8") if status.exists() else "missing detached watcher status"
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("detached") is True, payload
            assert (
                payload.get("controller_session_id"),
                payload.get("controller_pid"),
            ) == ("dead-controller-session", dead_controller), payload
            assert payload.get("state") not in {"orphaned", "controller_dead"}, payload
            assert watcher.poll() is None, "detached watcher exited on dead controller pid"
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                _observe_process_exit(watcher)
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGTERM)
                _observe_process_exit(worker)


def case_non_detached_watcher_dead_controller_and_gone_worker_remains_orphaned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tail = tmp_path / "tail.txt"
        status = tmp_path / "status.json"
        env = os.environ.copy()
        _isolate_state_env(env, tmp_path)
        tail.write_text("", encoding="utf-8")
        dead_controller = _dead_pid()
        worker_pid = _dead_pid()
        watcher = subprocess.Popen(
            [
                sys.executable, str(WATCH),
                "--pid", str(worker_pid),
                "--tail", str(tail),
                "--status-json", str(status),
                "--dispatch-id", "owned-dead-controller",
                "--poll-secs", "0.2",
                "--max-idle-secs", "30",
                "--controller-session-id", "dead-controller-session",
                "--controller-pid", str(dead_controller),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            env=env,
        )
        try:
            out, err = _observe_process_exit(watcher)
            assert watcher.returncode == 3, (watcher.returncode, out, err)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "orphaned", payload
            assert payload.get("reason") == "controller_dead", payload
            assert (
                payload.get("controller_session_id"),
                payload.get("controller_pid"),
            ) == ("dead-controller-session", dead_controller), payload
        finally:
            if watcher.poll() is None:
                watcher.terminate()
                _observe_process_exit(watcher)


def main() -> None:
    case_event_wait_rejects_hangs_and_wrong_exit_codes()
    case_crash_detected_promptly()
    case_finished_via_marker()
    case_dispatch_usage_limit_exit_zero_is_exhausted()
    case_dispatch_success_marker_with_limit_terms_stays_complete()
    case_dispatch_clean_complete_preserves_reason_without_rate_signal()
    case_dispatch_worker_dead_ledger_liveness()
    case_post_terminal_idle_worker_times_out_inconclusively()
    case_worker_death_reconciliation_streams_beyond_tail_window()
    case_full_file_reconciliation_caps_newline_free_reads()
    case_bound_marker_survives_stream_read_cap_boundary()
    test_oversized_prefix_requires_terminated_dispatch_id()
    test_oversized_line_marker_in_second_of_three_chunks_is_documented_miss()
    case_stability_recheck_uses_its_own_growth_baseline()
    case_stability_recheck_detects_growth_after_surviving_candidate()
    case_post_terminal_busy_worker_stays_armed_after_grace()
    case_post_terminal_delayed_worker_exit_is_observed()
    case_dispatch_post_terminal_idle_returns_inconclusive()
    case_worker_and_watcher_survive_launcher_pgroup_sigterm()
    case_foreground_keyboard_interrupt_leaves_worker_and_watcher_running()
    case_watcher_sigterm_flushes_non_running_status()
    case_detached_watcher_ignores_dead_controller_pid()
    case_non_detached_watcher_dead_controller_and_gone_worker_remains_orphaned()
    print("OK: goalflight_dispatch crash-safe tests pass")


if __name__ == "__main__":
    main()
