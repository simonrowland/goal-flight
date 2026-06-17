#!/usr/bin/env python3
"""Regression: a bash-tail worker left ALIVE after a non-terminal dispatch exit
must NOT be SIGKILLed by cleanup_ghosts (the live-worker SIGKILL landmine).

Bug shape (the asymmetry this fixes): goalflight_dispatch's bash-tail pidfile is
stamped with ``controller_pid = os.getpid()`` -- the EPHEMERAL dispatch process,
which then exits. On a NON-terminal watcher exit (idle-timeout rc=2, or any exit
with the worker still running for re-attach) the pidfile was left on disk with NO
``detached`` flag and an ``agent`` tag of ``<agent>-dispatch``. The next
cleanup_ghosts() sweep -- fired by ANY ACP dispatch, including a sibling project
sharing /tmp/goal-flight-acp-pids.d -- then saw dead-controller + live-worker +
no-detached-flag and SIGKILLed the live worker's process group, losing its
uncommitted work.

The fix mirrors the ACP ``mark_connection_detached`` protection: the dispatch
finally-block stamps ``detached: true`` on the pidfile whenever the worker is
still alive at cleanup time, and tags the agent ``<agent>-bash-tail`` so the
intended cleanup_ghosts branch is reachable. This test drives the real
``_cleanup_pidfile_if_worker_dead`` (the finally-block call) against a genuinely
alive worker, then runs the real ``cleanup_ghosts`` with a DEAD controller and
asserts the live worker survives -- while a genuine ghost (dead controller + dead
worker, not detached) is still reaped.

POSIX-only (real process groups + ps identity); skips native Windows.
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
from unittest.mock import patch

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses real POSIX process groups + ps identity")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import argparse  # noqa: E402

import goalflight_acp_client  # noqa: E402
import goalflight_compat  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger  # noqa: E402


def _alive(pid: int) -> bool:
    return goalflight_compat.pid_alive(pid)


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


def _dispatch_args(*, controller_pid: int, dispatch_id: str, agent: str = "codex"):
    """Minimal args namespace covering exactly what _write_pidfile reads."""
    return argparse.Namespace(controller_pid=controller_pid, agent=agent,
                              dispatch_id=dispatch_id)


def _spawn_live_worker() -> subprocess.Popen:
    """A real, own-session worker (so pgid == pid, the bash-tail invariant)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)


def _free_dead_pid() -> int:
    """A pid that is guaranteed not alive (spawn, reap, confirm dead)."""
    p = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    p.wait()
    assert _wait_dead(p.pid), "helper pid should be reaped"
    return p.pid


def _recorded_identity(pid: int) -> tuple[str, str]:
    """Return (started_at, cmd) EXACTLY as cleanup_ghosts will read them back via
    its own ``_ps_meta`` (which collapses ps's lstart padding). Recording in this
    normalized form makes the lstart/comm re-check a guaranteed MATCH, so the test
    exercises the intended branch (detached-skip vs genuine-kill) deterministically
    -- not an incidental stale-mismatch skip that varies by day-of-month padding."""
    meta = goalflight_acp_client._ps_meta(pid)
    assert meta is not None, f"ps identity unavailable for pid {pid}"
    return meta


def case_live_bashtail_worker_not_killed_by_ghost_sweep() -> None:
    """End-to-end: write a real bash-tail pidfile via _write_pidfile, simulate a
    non-terminal exit with the worker still alive (the finally-block call), then
    run the REAL cleanup_ghosts with a dead controller. The live worker MUST
    survive (protected by the detached flag the finally-block stamps)."""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        assert _alive(worker_pid)
        # The dispatch process's OWN pid acts as the (about-to-be-dead) controller
        # pid recorded in the pidfile -- exactly the ephemeral-pid landmine. We do
        # NOT pass --controller-pid, so _controller_pid(args) falls back to it.
        ephemeral_controller = os.getpid()
        identity = goalflight_ledger.process_identity(worker_pid)
        assert identity and identity.get("lstart") and identity.get("comm"), identity
        pgid = os.getpgid(worker_pid)
        assert pgid == worker_pid, (pgid, worker_pid)  # own-session leader

        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
                args = _dispatch_args(controller_pid=None, dispatch_id="bashtail-live")
                # Force the recorded controller_pid to a value we will then make
                # "dead" for the sweep, mirroring the ephemeral dispatch proc.
                with patch.object(dispatch, "_controller_pid",
                                  return_value=ephemeral_controller):
                    pidfile = dispatch._write_pidfile(
                        args, worker_pid=worker_pid, pgid=pgid, identity=identity)
                assert pidfile is not None and pidfile.exists()

                # The agent tag MUST be -bash-tail so cleanup_ghosts's bash-tail
                # branch is reachable (was -dispatch, which never matched).
                rec = json.loads(pidfile.read_text().splitlines()[0])
                assert rec["agent"].endswith("-bash-tail"), rec["agent"]
                assert rec.get("detached") in (None, False), "fresh pidfile not detached yet"

                # Simulate the NON-terminal dispatch exit: the finally-block call
                # with the worker still ALIVE. Must stamp detached:true (not unlink).
                dispatch._cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
                assert pidfile.exists(), "live worker's pidfile must be preserved for re-attach"
                rec2 = json.loads(pidfile.read_text().splitlines()[0])
                assert rec2.get("detached") is True, "live worker must be flagged detached"

                # Normalize started_at/cmd to the exact form cleanup_ghosts reads
                # back (its _ps_meta collapses lstart padding; _write_pidfile records
                # via the ledger's _ps_field which does not). This makes the sweep's
                # lstart/comm re-check a guaranteed MATCH, so the ONLY thing that can
                # spare the worker is the detached-skip we are testing -- not an
                # incidental stale-mismatch. (The padding delta is a separate
                # pre-existing quirk, noted in RESULT, not in scope here.)
                norm_lstart, norm_cmd = _recorded_identity(worker_pid)
                rec2["started_at"] = norm_lstart
                rec2["cmd"] = norm_cmd
                pidfile.write_text(json.dumps(rec2, sort_keys=True) + "\n", encoding="utf-8")

                # Now the REAL ghost sweep, with the recorded controller treated as
                # DEAD (the dispatch proc has exited). Real cleanup_ghosts logic.
                def fake_pid_alive(pid: int) -> bool:
                    if pid == ephemeral_controller:
                        return False  # the ephemeral dispatch proc has exited
                    return _alive(pid)

                def fake_ps_meta(pid: int):
                    if pid == ephemeral_controller:
                        return None  # dead controller -> not a live-controller skip
                    return goalflight_acp_client._ps_meta(pid)

                with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
                        patch("goalflight_compat.pid_alive", side_effect=fake_pid_alive), \
                        patch("goalflight_acp_client._ps_meta", side_effect=fake_ps_meta), \
                        patch("goalflight_compat.kill_pid",
                              side_effect=AssertionError("LANDMINE: live bash-tail worker SIGKILLed")):
                    killed = goalflight_acp_client.cleanup_ghosts()
                assert killed == 0, "detached live worker must not be killed"
            assert _alive(worker_pid), "live bash-tail worker survived the ghost sweep"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(worker_pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            worker.wait(timeout=5)


def case_genuine_ghost_still_reaped() -> None:
    """Safety not neutered: dead controller + LIVE worker + NOT detached + correct
    -bash-tail tag + pgid==pid is a genuine ghost (the orchestrator crashed without
    leaving a detached marker) and MUST still be reaped. We spawn a real worker,
    record a matching pidfile WITHOUT the detached flag, and assert cleanup_ghosts
    kills it. (This is the post-crash case the reaper exists for.)"""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    reaped = False
    try:
        assert _alive(worker_pid)
        dead_controller = _free_dead_pid()
        norm_lstart, norm_cmd = _recorded_identity(worker_pid)
        pgid = os.getpgid(worker_pid)
        assert pgid == worker_pid

        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            pidfile = pid_dir / f"{dead_controller}.bashtail.{worker_pid}.jsonl"
            # A crashed-orchestrator ghost: correct tag, NO detached flag.
            pidfile.write_text(json.dumps({
                "controller_pid": dead_controller,
                "pid": worker_pid,
                "pgid": pgid,
                "started_at": norm_lstart,
                "cmd": norm_cmd,
                "agent": "codex-bash-tail",
                "session_id": "bashtail-ghost",
            }, sort_keys=True) + "\n", encoding="utf-8")

            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir):
                killed = goalflight_acp_client.cleanup_ghosts()
            assert killed == 1, "genuine ghost (dead controller + dead worker class) must be reaped"
            # The worker is a direct child here, so after killpg it is a ZOMBIE
            # until reaped (pid_alive(zombie) is True -- the very trap the dispatch
            # reaper thread exists to avoid). Reap via wait() and assert the group
            # actually took SIGKILL, which proves the reap fired.
            worker.wait(timeout=5)
            reaped = True
            assert worker.returncode == -signal.SIGKILL, (
                f"ghost worker's group must be SIGKILLed (rc={worker.returncode})")
            assert not pidfile.exists(), "ghost pidfile unlinked after reap"
    finally:
        if not reaped:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(worker_pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            worker.wait(timeout=5)


def case_dead_worker_pidfile_unlinked_not_marked() -> None:
    """If the worker is already DEAD at the finally-block call, the pidfile is
    reaped+unlinked as before (NOT stamped detached) -- the detached path is only
    for live workers. Confirms the new branch did not change the dead path."""
    dead_pid = _free_dead_pid()
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        pidfile = pid_dir / f"{os.getpid()}.bashtail.{dead_pid}.jsonl"
        pidfile.write_text(json.dumps({
            "controller_pid": os.getpid(),
            "pid": dead_pid,
            "pgid": dead_pid,
            "agent": "codex-bash-tail",
            "session_id": "bashtail-dead",
        }, sort_keys=True) + "\n", encoding="utf-8")
        dispatch._cleanup_pidfile_if_worker_dead(pidfile, dead_pid)
        assert not pidfile.exists(), "dead worker's pidfile must be unlinked (re-attach impossible)"


def main() -> None:
    if goalflight_acp_client._ps_meta(os.getpid()) is None:
        print("OK: dispatch bash-tail ghost-protection tests skipped (ps unavailable)")
        return
    case_live_bashtail_worker_not_killed_by_ghost_sweep()
    case_genuine_ghost_still_reaped()
    case_dead_worker_pidfile_unlinked_not_marked()
    print("OK: dispatch bash-tail ghost-protection tests pass")


if __name__ == "__main__":
    main()
