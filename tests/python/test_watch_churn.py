#!/usr/bin/env python3
"""Designed-red regression tests for quiet watcher I/O and probe churn."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch as watch  # noqa: E402
import goalflight_liveness as liveness  # noqa: E402


class _Result:
    returncode = 0

    def __init__(self, stdout: str = ""):
        self.stdout = stdout


def _runner(calls: list[list[str]]):
    def run(argv, **_kwargs):
        calls.append(argv)
        return _Result()

    return run


def _diskio_bytes_written() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pid_rusage = libproc.proc_pid_rusage
        proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        proc_pid_rusage.restype = ctypes.c_int
        raw = ctypes.create_string_buffer(2048)
        if proc_pid_rusage(os.getpid(), 4, ctypes.byref(raw)) != 0:
            return None
        # rusage_info_v4: uuid (16 bytes), then 13 uint64 counters through
        # ri_diskio_byteswritten (the 13th counter, offset 16 + 12*8).
        return int(ctypes.c_uint64.from_buffer(raw, 16 + 12 * 8).value)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def test_quiet_poll_budget_is_zero_status_writes_and_backed_off_spawns(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    base = {
        "schema": "goalflight.status.v1",
        "dispatch_id": "quiet-budget",
        "state": "running_quiet",
        "liveness_state": "running_quiet",
        "worker_alive": True,
        "worker_pid": 10,
    }
    watch.write_status(status_path, dict(base))
    previous = json.loads(status_path.read_text(encoding="utf-8"))
    inode = status_path.stat().st_ino
    replacements = 0
    before_bytes = _diskio_bytes_written()

    ps_calls: list[list[str]] = []
    lsof_calls: list[list[str]] = []
    trace = watch.TraceLiveness(
        dispatch_id="quiet-budget",
        worker_pid=10,
        effective_account="seat-a",
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        started_mono=0,
        retry_secs=300,
        ps_runner=_runner(ps_calls),
        lsof_runner=_runner(lsof_calls),
    )

    quiet_polls = 10
    for tick in range(0, quiet_polls * 2, 2):
        candidate = {
            **previous,
            "updated_at": tick,
            "seconds_since_event": tick,
            "pgroup_cpu_pct": float(tick),
            "idle_tree_age_s": float(tick),
        }
        assert not watch._status_payload_changed(previous, candidate)
        if watch._status_payload_changed(previous, candidate):
            watch.write_status(status_path, candidate)
            replacements += 1
        trace.sample(now_epoch=tick, now_mono=tick, idle_threshold=30)
        assert status_path.stat().st_ino == inode

    after_bytes = _diskio_bytes_written()
    assert replacements == 0
    assert len(ps_calls) == 4
    assert len(lsof_calls) == 4
    if before_bytes is not None and after_bytes is not None:
        assert after_bytes == before_bytes

    changed = {**previous, "state": "complete", "updated_at": quiet_polls * 2}
    assert watch._status_payload_changed(previous, changed)
    watch.write_status(status_path, changed)
    assert status_path.stat().st_ino != inode


def test_worker_probe_reuses_native_exit_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = os.getpid()
    expected = watch._lightweight_process_identity(pid)
    probe = watch._WorkerProcessProbe(pid, expected)
    try:
        if probe._pidfd is None and probe._kqueue is None:
            pytest.skip("native pid exit observer unavailable")
        alive, reason, identity = probe.sample()
        assert alive and reason == "live"
        assert identity == expected
        monkeypatch.setattr(
            watch,
            "_lightweight_process_identity",
            lambda _pid: (_ for _ in ()).throw(AssertionError("identity must be captured once")),
        )
        for _ in range(9):
            alive, reason, identity = probe.sample()
            assert alive and reason == "live"
            assert identity == expected
    finally:
        probe.close()


def test_worker_probe_without_start_token_still_observes_exit() -> None:
    # A pin with no start token (the watcher's own probe failed) cannot prove
    # the generation, so it never reports "live" -- but the native exit
    # observer must still report the worker dead once it exits.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    expected = {
        "pid": child.pid,
        "identity_available": False,
        "identity_source": "pid_probe_only",
    }
    probe = watch._WorkerProcessProbe(child.pid, expected)
    try:
        if probe._pidfd is None and probe._kqueue is None:
            pytest.skip("native pid exit observer unavailable")
        alive, reason, _identity = probe.sample()
        assert alive and reason == "identity_indeterminate"
        child.kill()
        child.wait()
        alive, reason, _identity = probe.sample()
        assert not alive and reason == "dead"
    finally:
        probe.close()
        if child.poll() is None:
            child.kill()
            child.wait()


def test_native_cpu_sample_does_not_spawn_process_table_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("native process counters unavailable")
    monkeypatch.setattr(
        liveness.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CPU sampling must not fork ps")
        ),
    )
    sample = liveness.pgroup_cputime_snapshot(os.getpgrp())
    assert sample is not None


def test_native_descendant_sample_does_not_spawn_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("native process rows unavailable")
    monkeypatch.setattr(
        watch.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("process rows must not fork ps")
        ),
    )
    rows = watch._native_process_rows()
    assert rows is not None
    assert os.getpid() in {pid for pid, _ppid, _state in rows}


def test_production_trace_resolution_has_no_quiet_subprocess_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watch, "_native_open_file_paths", lambda _pid: [])
    monkeypatch.setattr(
        watch.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("quiet trace resolution must not fork")
        ),
    )
    trace = watch.TraceLiveness(
        dispatch_id="native-trace",
        worker_pid=os.getpid(),
        effective_account="seat-a",
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        started_mono=0,
        retry_secs=300,
    )
    for tick in range(0, 20, 2):
        assert trace.sample(now_epoch=tick, now_mono=tick, idle_threshold=30) == {}


def test_trace_resolution_retries_after_horizon_at_bounded_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    late_trace = tmp_path / "late.trace"
    monkeypatch.setattr(
        watch,
        "_newest_trace_file",
        lambda _root, _roots: late_trace if late_trace.exists() else None,
    )
    monkeypatch.setattr(watch, "_trace_from_lsof", lambda *_args, **_kwargs: None)
    trace = watch.TraceLiveness(
        dispatch_id="late-trace",
        worker_pid=10,
        effective_account="seat-a",
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        started_mono=0,
        retry_secs=300,
    )
    for tick in (0, 2, 6, 14, 30, 62, 122, 182, 242):
        assert trace.sample(now_epoch=tick, now_mono=tick, idle_threshold=30) == {}
    late_trace.write_text("trace", encoding="utf-8")
    sample = trace.sample(now_epoch=302, now_mono=302, idle_threshold=30)
    assert sample["trace_path"] == str(late_trace)


def test_v17_status_sidecar_remains_a_compatible_change_view() -> None:
    # This is the status.json shape emitted by the v1.7.0 watcher: consumers
    # still receive the same compatibility fields while polling observations
    # stop causing rewrites.
    legacy = {
        "schema": "goalflight.status.v1",
        "dispatch_id": "legacy-sidecar",
        "state": "running",
        "reason": None,
        "worker_pid": 1234,
        "worker_alive": True,
        "worker_identity": {"pid": 1234, "start_token": "darwin:1:2"},
        "updated_at": 1700000000,
        "seconds_since_event": 2.0,
        "pgroup_cpu_pct": 0.0,
        "idle_tree_age_s": 2.0,
    }
    next_poll = {
        **legacy,
        "updated_at": 1700000002,
        "seconds_since_event": 4.0,
        "pgroup_cpu_pct": 0.0,
        "idle_tree_age_s": 4.0,
    }
    assert not watch._status_payload_changed(legacy, next_poll)
    assert next_poll["schema"] == legacy["schema"]
    assert next_poll["worker_identity"] == legacy["worker_identity"]
