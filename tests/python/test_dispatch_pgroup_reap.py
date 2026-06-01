#!/usr/bin/env python3
"""Hermetic test: direct-dispatch teardown reaps a dead worker's orphaned group.

Covers the tty/process-leak fix where a worker that exited while children
lingered in its process group left them unreaped (and unlinked the pidfile = the
only pgid record, so even cleanup_ghosts could never reach them). POSIX-only
(killpg); skips native Windows.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("process-group reaping is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402


def _alive(pid: int) -> bool:
    return goalflight_compat.pid_alive(pid)


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _write_pidfile(path: Path, worker_pid: int, pgid: int | None) -> None:
    entry = {"pid": worker_pid, "controller_pid": os.getpid(),
             "agent": "test-dispatch", "session_id": "reap-test"}
    if pgid is not None:
        entry["pgid"] = pgid
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


# A child that ignores SIGHUP, so it SURVIVES the session leader's exit and
# lingers in the worker's process group -- the realistic leak the reaper targets
# (and the universal orphan case on Linux/WSL, where there is no SIGHUP without a
# controlling tty). It still dies on the reaper's SIGTERM (killpg).
_ORPHAN_CHILD_CODE = (
    "import signal, time\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "time.sleep(30)\n"
)


def _spawn_worker_with_orphan() -> tuple[int, int, int]:
    """Spawn a worker in its own session/group that leaves a SIGHUP-ignoring child
    behind, then exits. Returns (worker_pid, pgid, orphan_child_pid); the child
    outlives the leader and keeps the worker's pgid."""
    worker_code = (
        "import os, subprocess, sys\n"
        "c = subprocess.Popen([sys.executable, '-c', __ORPHAN__], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "sys.stdout.write('%d %d %d' % (os.getpid(), os.getpgrp(), c.pid))\n"
        "sys.stdout.flush()\n"
    ).replace("__ORPHAN__", repr(_ORPHAN_CHILD_CODE))
    proc = subprocess.run(
        [sys.executable, "-c", worker_code],
        capture_output=True, text=True, start_new_session=True,
    )
    worker_pid, pgid, child_pid = (int(x) for x in proc.stdout.split())
    return worker_pid, pgid, child_pid


def case_cleanup_reaps_dead_worker_orphans() -> None:
    # Full integration through _cleanup_pidfile_if_worker_dead: a dead worker
    # with an orphan still in its group must be reaped, then the pidfile unlinked.
    # pid_alive(worker) is stubbed False to make "leader is dead" deterministic
    # (avoids a pid-reuse race); the orphan + pgid + killpg are all real.
    worker_pid, pgid, child_pid = _spawn_worker_with_orphan()
    orig_pid_alive = goalflight_compat.pid_alive
    goalflight_compat.pid_alive = lambda p, _w=worker_pid: False if p == _w else orig_pid_alive(p)
    try:
        assert pgid == worker_pid, (pgid, worker_pid)  # worker is the group leader
        assert _alive(child_pid), "orphan child should be alive before reaping"
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "ctrl.bashtail.worker.jsonl"
            _write_pidfile(pidfile, worker_pid, pgid)
            # killpg(pgid) targets the group (the orphan), not the dead leader pid.
            dispatch._cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
            assert _wait_dead(child_pid), "orphan child should be reaped by teardown"
            assert not pidfile.exists(), "pidfile should be unlinked after reap"
    finally:
        goalflight_compat.pid_alive = orig_pid_alive
        with contextlib.suppress(OSError):
            os.kill(child_pid, signal.SIGKILL)  # never leak the test's own child


def case_cleanup_preserves_live_worker() -> None:
    # A still-alive worker must NOT be killed and its pidfile preserved (the
    # non-destructive re-attach path).
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    try:
        pgid = os.getpgid(proc.pid)
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "ctrl.bashtail.worker.jsonl"
            _write_pidfile(pidfile, proc.pid, pgid)
            dispatch._cleanup_pidfile_if_worker_dead(pidfile, proc.pid)
            assert _alive(proc.pid), "live worker must not be killed (re-attach)"
            assert pidfile.exists(), "live worker's pidfile must be preserved"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def case_reap_safe_on_bad_pidfile() -> None:
    # Missing / low / malformed pgid records must degrade safely: no crash, no kill.
    with tempfile.TemporaryDirectory() as td:
        pidfile = Path(td) / "ctrl.bashtail.worker.jsonl"
        _write_pidfile(pidfile, 999_999, None)
        dispatch._reap_dead_worker_pgroup(pidfile, 999_999)  # no pgid -> skip
        _write_pidfile(pidfile, 999_999, 1)
        dispatch._reap_dead_worker_pgroup(pidfile, 999_999)  # pgid<=1 -> skip
        pidfile.write_text("[]\n", encoding="utf-8")          # valid JSON, NOT a dict
        dispatch._reap_dead_worker_pgroup(pidfile, 999_999)  # must not raise (P2)
        pidfile.write_text("not json at all\n", encoding="utf-8")
        dispatch._reap_dead_worker_pgroup(pidfile, 999_999)  # must not raise


def case_guard_skips_own_pgroup() -> None:
    # If the recorded pgid is the controller's OWN group AND equals worker_pid
    # (passing the invariant), the reaper must still skip -- otherwise killpg would
    # SIGTERM this test process. Surviving past the call proves the guard fired.
    with tempfile.TemporaryDirectory() as td:
        pidfile = Path(td) / "ctrl.bashtail.worker.jsonl"
        own = os.getpgrp()
        _write_pidfile(pidfile, own, own)
        dispatch._reap_dead_worker_pgroup(pidfile, own)
        assert True, "reaper must not signal the controller's own process group"


def case_reap_skips_pgid_neq_worker() -> None:
    # The direct-dispatch invariant: if the recorded pgid != worker_pid, the reaper
    # is not certain the group is the worker's and must skip (no kill). Use a live
    # foreign group (the test's own) as pgid with a dummy worker_pid; survival of
    # this process proves no signal was sent.
    with tempfile.TemporaryDirectory() as td:
        pidfile = Path(td) / "ctrl.bashtail.worker.jsonl"
        _write_pidfile(pidfile, 999_999, os.getpgrp())  # pgid != worker_pid
        dispatch._reap_dead_worker_pgroup(pidfile, 999_999)
        assert True, "reaper must skip when pgid != worker_pid"


def main() -> None:
    case_cleanup_reaps_dead_worker_orphans()
    case_cleanup_preserves_live_worker()
    case_reap_safe_on_bad_pidfile()
    case_reap_skips_pgid_neq_worker()
    case_guard_skips_own_pgroup()
    print("OK: dispatch pgroup-reap teardown tests pass")


if __name__ == "__main__":
    main()
