#!/usr/bin/env python3
"""Regression coverage for bash-tail pidfile cleanup.

Live identity-matching bash-tail workers survive cleanup even when their beacon
dies before the launcher detach-stamps the pidfile. Unowned pidfiles use an
explicit ``unowned`` filename marker: cleanup must parse that marker, preserve a
live worker because missing ownership is not proof of abandonment, and unlink
the pidfile after the worker dies. These tests use real processes so a parse skip
cannot masquerade as protection.

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


def _dispatch_args(
    *,
    controller_pid: int | None,
    dispatch_id: str,
    controller_session_id: str | None = None,
    agent: str = "codex",
):
    """Minimal args namespace covering exactly what _write_pidfile reads."""
    return argparse.Namespace(
        controller_pid=None,
        controller_session_id=controller_session_id,
        _controller_beacon_pid=controller_pid,
        agent=agent,
        dispatch_id=dispatch_id,
    )


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


def case_lstart_padding_is_normalized() -> None:
    """Ledger and cleanup ps timestamp formatting must compare identically."""
    recorded = ("Sun Aug  2 12:34:56 2026", "python")
    observed = ("Sun Aug 2 12:34:56 2026", "python")
    assert goalflight_acp_client._same_process(recorded, observed)


def case_real_pidfile_identity_round_trip() -> None:
    """A real writer identity must survive cleanup's independently read format."""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        identity = goalflight_ledger.process_identity(worker_pid)
        assert identity, "ps identity became unavailable after the suite-level probe"
        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
                pidfile = dispatch._write_pidfile(
                    _dispatch_args(
                        controller_pid=os.getpid(),
                        controller_session_id="live-controller-session",
                        dispatch_id="bashtail-real-identity",
                    ),
                    worker_pid=worker_pid,
                    pgid=os.getpgid(worker_pid),
                    identity=identity,
                )
            assert pidfile is not None and pidfile.exists()
            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), patch(
                "goalflight_compat.kill_pid",
                side_effect=AssertionError("real identity round-trip killed live worker"),
            ):
                assert goalflight_acp_client.cleanup_ghosts() == 0
            assert pidfile.exists(), "live worker pidfile lost after real identity round-trip"
            assert _alive(worker_pid), "real identity round-trip must preserve worker"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(worker_pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            worker.wait(timeout=5)


def case_live_bashtail_worker_not_killed_by_ghost_sweep() -> None:
    """End-to-end: write a real bash-tail pidfile via _write_pidfile, simulate a
    non-terminal exit with the worker still alive (the finally-block call), then
    run the REAL cleanup_ghosts with a dead controller. The live worker MUST
    survive (protected by the detached flag the finally-block stamps)."""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        assert _alive(worker_pid)
        # Model an owned worker whose controller dies before the cleanup sweep.
        ephemeral_controller = os.getpid()
        identity = {"lstart": "test-start", "comm": "python"}
        pgid = os.getpgid(worker_pid)
        assert pgid == worker_pid, (pgid, worker_pid)  # own-session leader

        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
                args = _dispatch_args(
                    controller_pid=ephemeral_controller,
                    controller_session_id="ephemeral-session",
                    dispatch_id="bashtail-live",
                )
                pidfile = dispatch._write_pidfile(
                    args, worker_pid=worker_pid, pgid=pgid, identity=identity)
                assert pidfile is not None and pidfile.exists()

                # The agent tag MUST be -bash-tail so cleanup_ghosts's bash-tail
                # branch is reachable (was -dispatch, which never matched).
                rec = json.loads(pidfile.read_text().splitlines()[0])
                assert rec["agent"].endswith("-bash-tail"), rec["agent"]
                assert (
                    rec.get("controller_session_id"),
                    rec.get("controller_pid"),
                ) == ("ephemeral-session", ephemeral_controller), rec
                assert rec.get("detached") in (None, False), "fresh pidfile not detached yet"

                # Simulate the NON-terminal dispatch exit: the finally-block call
                # with the worker still ALIVE. Must stamp detached:true (not unlink).
                dispatch._cleanup_pidfile_if_worker_dead(pidfile, worker_pid)
                assert pidfile.exists(), "live worker's pidfile must be preserved for re-attach"
                rec2 = json.loads(pidfile.read_text().splitlines()[0])
                assert rec2.get("detached") is True, "live worker must be flagged detached"

                # Now the REAL ghost sweep, with the recorded controller treated as
                # DEAD (the dispatch proc has exited). Real cleanup_ghosts logic.
                orig_pid_alive = goalflight_compat.pid_alive
                orig_ps_meta = goalflight_acp_client._ps_meta

                def fake_pid_alive(pid: int) -> bool:
                    if pid == ephemeral_controller:
                        return False  # the ephemeral dispatch proc has exited
                    return orig_pid_alive(pid)

                def fake_ps_meta(pid: int):
                    if pid == ephemeral_controller:
                        return None  # dead controller -> not a live-controller skip
                    if pid == worker_pid:
                        return identity["lstart"], identity["comm"]
                    return orig_ps_meta(pid)

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


def case_from_queue_detached_pidfile_spares_live_worker() -> None:
    """From-queue detached bash-tail pidfiles are protected immediately.

    Queue drain launches with a short-lived controller. The pidfile must already
    carry ``detached:true`` before that launcher exits; otherwise the next ghost
    sweep can kill the still-running worker.
    """
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        assert _alive(worker_pid)
        dead_controller = _free_dead_pid()
        identity = {"lstart": "test-start", "comm": "python"}
        pgid = os.getpgid(worker_pid)
        assert pgid == worker_pid

        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
                args = _dispatch_args(
                    controller_pid=dead_controller,
                    controller_session_id="dead-controller-session",
                    dispatch_id="bashtail-detached-live",
                )
                pidfile = dispatch._write_pidfile(
                    args,
                    worker_pid=worker_pid,
                    pgid=pgid,
                    identity=identity,
                    detached=True,
                )
            assert pidfile is not None and pidfile.exists()
            rec = json.loads(pidfile.read_text().splitlines()[0])
            assert (
                rec.get("controller_session_id"),
                rec.get("controller_pid"),
            ) == ("dead-controller-session", dead_controller), rec
            assert rec.get("detached") is True, rec

            orig_pid_alive = goalflight_compat.pid_alive
            orig_ps_meta = goalflight_acp_client._ps_meta

            def fake_pid_alive(pid: int) -> bool:
                if pid == dead_controller:
                    return False
                return orig_pid_alive(pid)

            def fake_ps_meta(pid: int):
                if pid == dead_controller:
                    return None
                if pid == worker_pid:
                    return identity["lstart"], identity["comm"]
                return orig_ps_meta(pid)

            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
                    patch("goalflight_compat.pid_alive", side_effect=fake_pid_alive), \
                    patch("goalflight_acp_client._ps_meta", side_effect=fake_ps_meta), \
                    patch("goalflight_compat.kill_pid",
                          side_effect=AssertionError("LANDMINE: detached live worker killed")):
                killed = goalflight_acp_client.cleanup_ghosts()
            assert killed == 0, "detached live worker must not be killed"
            assert _alive(worker_pid), "detached live worker survived the ghost sweep"
            assert pidfile.exists(), "live detached worker pidfile remains for later cleanup"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(worker_pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            worker.wait(timeout=5)


def case_unowned_live_worker_is_not_a_ghost() -> None:
    """Unknown ownership alone cannot authorize killing a live worker."""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        identity = {"lstart": "test-start", "comm": "python"}
        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
                pidfile = dispatch._write_pidfile(
                    _dispatch_args(
                        controller_pid=None,
                        dispatch_id="bashtail-unowned-live",
                    ),
                    worker_pid=worker_pid,
                    pgid=os.getpgid(worker_pid),
                    identity=identity,
                )
            assert pidfile is not None and pidfile.name.startswith("unowned.")
            record = json.loads(pidfile.read_text(encoding="utf-8"))
            assert record.get("controller_session_id") is None, record
            assert record.get("controller_pid") is None, record
            assert record.get("detached") in (None, False), record

            original_ps_meta = goalflight_acp_client._ps_meta

            def fake_ps_meta(pid: int):
                if pid == worker_pid:
                    return identity["lstart"], identity["comm"]
                return original_ps_meta(pid)

            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), patch(
                "goalflight_acp_client._ps_meta", side_effect=fake_ps_meta
            ), patch(
                "goalflight_compat.kill_pid",
                side_effect=AssertionError("unowned live worker killed"),
            ):
                assert goalflight_acp_client.cleanup_ghosts() == 0
            assert _alive(worker_pid), "unowned live worker must survive cleanup"
            assert pidfile.exists(), "live unowned pidfile remains for later cleanup"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(worker_pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            worker.wait(timeout=5)


def case_dead_unowned_pidfile_is_unlinked() -> None:
    """The unowned marker is parsed; dead entries do not leak forever."""
    dead_worker = _free_dead_pid()
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
            pidfile = dispatch._write_pidfile(
                _dispatch_args(
                    controller_pid=None,
                    dispatch_id="bashtail-unowned-dead",
                ),
                worker_pid=dead_worker,
                pgid=dead_worker,
                identity={"lstart": "dead", "comm": "dead"},
            )
        assert pidfile is not None and pidfile.exists()
        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir):
            assert goalflight_acp_client.cleanup_ghosts() == 0
        assert not pidfile.exists(), "dead unowned pidfile must be unlinked"


def case_reused_controller_pid_does_not_pin_dead_bashtail_pidfile() -> None:
    """A live/reused owner PID cannot bypass bash-tail worker reconciliation."""
    dead_worker = _free_dead_pid()
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        with patch.dict(os.environ, {"GOAL_FLIGHT_PIDFILE_DIR": str(pid_dir)}):
            pidfile = dispatch._write_pidfile(
                _dispatch_args(
                    controller_pid=os.getpid(),
                    controller_session_id="stale-controller-session",
                    dispatch_id="bashtail-reused-controller-pid",
                ),
                worker_pid=dead_worker,
                pgid=dead_worker,
                identity={"lstart": "dead", "comm": "dead"},
            )
        assert pidfile is not None and pidfile.exists()
        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir):
            assert goalflight_acp_client.cleanup_ghosts() == 0
        assert not pidfile.exists(), "live/reused owner PID must not pin dead worker metadata"


def case_dead_detached_pidfile_still_unlinked() -> None:
    """Detached is not a leak: once the worker pid is dead, cleanup unlinks."""
    dead_controller = _free_dead_pid()
    dead_worker = _free_dead_pid()
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        pidfile = pid_dir / f"{dead_controller}.bashtail.{dead_worker}.jsonl"
        pidfile.write_text(json.dumps({
            "controller_pid": dead_controller,
            "pid": dead_worker,
            "pgid": dead_worker,
            "started_at": "dead",
            "cmd": "dead",
            "agent": "codex-bash-tail",
            "session_id": "bashtail-detached-dead",
            "detached": True,
        }, sort_keys=True) + "\n", encoding="utf-8")

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir):
            killed = goalflight_acp_client.cleanup_ghosts()
        assert killed == 0, "dead detached worker should not count as a kill"
        assert not pidfile.exists(), "dead detached pidfile must be unlinked"


def case_owned_live_worker_survives_beacon_death_before_detach_stamp() -> None:
    """Beacon death cannot make a live, not-yet-detached worker reapable."""
    worker = _spawn_live_worker()
    worker_pid = worker.pid
    try:
        assert _alive(worker_pid)
        dead_controller = _free_dead_pid()
        identity = ("test-start", "python")
        pgid = os.getpgid(worker_pid)
        assert pgid == worker_pid

        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td)
            pidfile = pid_dir / f"{dead_controller}.bashtail.{worker_pid}.jsonl"
            pidfile.write_text(json.dumps({
                "controller_pid": dead_controller,
                "controller_session_id": "dead-controller-session",
                "pid": worker_pid,
                "pgid": pgid,
                "started_at": identity[0],
                "cmd": identity[1],
                "agent": "codex-bash-tail",
                "session_id": "bashtail-beacon-race",
            }, sort_keys=True) + "\n", encoding="utf-8")

            original_ps_meta = goalflight_acp_client._ps_meta

            def fake_ps_meta(pid: int):
                if pid == dead_controller:
                    return None
                if pid == worker_pid:
                    return identity
                return original_ps_meta(pid)

            with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), patch(
                "goalflight_acp_client._ps_meta", side_effect=fake_ps_meta
            ), patch(
                "goalflight_compat.kill_pid",
                side_effect=AssertionError("live bash-tail worker killed after beacon death"),
            ):
                killed = goalflight_acp_client.cleanup_ghosts()
            assert killed == 0
            assert _alive(worker_pid), "live worker must survive beacon-death cleanup"
            assert pidfile.exists(), "live worker pidfile remains for detach stamping"
    finally:
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
    case_lstart_padding_is_normalized()
    case_from_queue_detached_pidfile_spares_live_worker()
    case_dead_unowned_pidfile_is_unlinked()
    case_reused_controller_pid_does_not_pin_dead_bashtail_pidfile()
    case_dead_detached_pidfile_still_unlinked()
    case_live_bashtail_worker_not_killed_by_ghost_sweep()
    case_unowned_live_worker_is_not_a_ghost()
    case_owned_live_worker_survives_beacon_death_before_detach_stamp()
    if goalflight_acp_client._ps_meta(os.getpid()) is None:
        print("OK: dispatch bash-tail ghost-protection detached tests pass (ps-dependent cases skipped)")
        return
    case_real_pidfile_identity_round_trip()
    case_dead_worker_pidfile_unlinked_not_marked()
    print("OK: dispatch bash-tail ghost-protection tests pass")


if __name__ == "__main__":
    main()
