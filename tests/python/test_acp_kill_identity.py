#!/usr/bin/env python3
"""Regression tests for ACP worker PID identity checks."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("asserts POSIX bash process identity strings")

import asyncio
import os
import subprocess
import sys
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
import goalflight_acp_run  # noqa: E402
import goalflight_capacity  # noqa: E402
import goalflight_ledger  # noqa: E402
import acp_pool  # noqa: E402
from goalflight_acp_client import _same_process  # noqa: E402


def case_exec_comm_change_keeps_identity() -> None:
    started = ("Wed May 20 17:55:24 2026", "bash")
    live = ("Wed May 20 17:55:24 2026", "/Users/example/.local/bin/cursor-agent")
    assert _same_process(started, live) is True


def case_pid_reuse_lstart_change_is_different() -> None:
    started = ("Wed May 20 17:55:24 2026", "cursor-agent")
    live = ("Wed May 20 17:55:25 2026", "cursor-agent")
    assert _same_process(started, live) is False


def case_unavailable_meta_preserves_kill_fallthrough() -> None:
    live = ("Wed May 20 17:55:24 2026", "cursor-agent")
    assert _same_process(None, live) is True
    assert _same_process(live, None) is True


def case_windows_cleanup_skips_bare_pidfile_pid() -> None:
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        stale = pid_dir / "999999.jsonl"
        stale.write_text(json.dumps({"pid": 12345, "agent": "codex-acp"}) + "\n", encoding="utf-8")

        def fake_pid_alive(pid: int) -> bool:
            return pid == 12345

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", return_value=None), \
            patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.pid_liveness", side_effect=lambda pid: pid == 12345), \
            patch("goalflight_compat.pid_alive", side_effect=fake_pid_alive), \
            patch("goalflight_compat.kill_pid", side_effect=AssertionError("bare reused pid killed")), \
            patch("goalflight_acp_client.log.warning") as warn:
            killed = goalflight_acp_client.cleanup_ghosts()
            missing_identity_unlinked = not stale.exists()
            warned = warn.called
    assert killed == 0
    assert missing_identity_unlinked
    assert warned


def case_windows_cleanup_does_not_kill_indeterminate_pid() -> None:
    """pid_liveness None must not reach kill_pid, even with creation identity.

    pid_alive collapses None to True; the Windows ghost path used that boolean
    as a kill gate. Reverting the liveness check to pid_alive fails this case.
    """
    import goalflight_compat

    assert goalflight_compat.pid_alive(os.getpid()) is True
    assert goalflight_compat.pid_alive(999999) is False
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        tracked = pid_dir / "999999.jsonl"
        tracked.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "agent": "codex-acp",
                    "creation_time": "same",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        kills: list[int] = []

        def fake_kill(pid, *args, **kwargs):
            kills.append(int(pid))
            raise AssertionError("indeterminate windows pid killed")

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", return_value=None), \
            patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.pid_liveness", return_value=None), \
            patch("goalflight_compat.pid_alive", side_effect=lambda pid: pid == 12345), \
            patch("goalflight_compat.kill_pid", side_effect=fake_kill):
            killed = goalflight_acp_client.cleanup_ghosts()
            preserved = tracked.exists()
    assert killed == 0
    assert kills == []
    assert preserved


def case_windows_cleanup_kills_confirmed_live_identity() -> None:
    """A confirmed-live Windows pid with matching identity still reaps."""
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        tracked = pid_dir / "999999.jsonl"
        tracked.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "agent": "codex-acp",
                    "creation_time": "same",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", return_value=None), \
            patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.pid_liveness", side_effect=lambda pid: True if pid == 12345 else False), \
            patch("goalflight_compat.pid_alive", side_effect=lambda pid: pid == 12345), \
            patch("goalflight_compat.kill_pid", return_value=True) as kill:
            killed = goalflight_acp_client.cleanup_ghosts()
            unlinked = not tracked.exists()
    assert killed == 1
    assert kill.called
    assert unlinked


def case_windows_cleanup_unlinks_confirmed_dead_pid() -> None:
    """A confirmed-dead Windows pid still skips kill and drops the pidfile."""
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        tracked = pid_dir / "999999.jsonl"
        tracked.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "agent": "codex-acp",
                    "creation_time": "same",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", return_value=None), \
            patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.pid_liveness", return_value=False), \
            patch("goalflight_compat.pid_alive", return_value=False), \
            patch(
                "goalflight_compat.kill_pid",
                side_effect=AssertionError("dead windows pid killed"),
            ):
            killed = goalflight_acp_client.cleanup_ghosts()
            unlinked = not tracked.exists()
    assert killed == 0
    assert unlinked


def case_posix_cleanup_preserves_live_legacy_pidfile() -> None:
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        stale = pid_dir / "999999.jsonl"
        stale.write_text(
            json.dumps(
                {
                    "pid": 12345,
                    "pgid": 12345,
                    "started_at": "Wed May 20 17:55:24 2026",
                    "cmd": "python",
                    "agent": "codex-acp",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def meta(pid: int):
            if pid == 12345:
                return "Wed May 20 17:55:24 2026", "node"
            return None

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", side_effect=meta), \
            patch("goalflight_compat.is_windows", return_value=False), \
            patch("goalflight_compat.pid_alive", side_effect=lambda pid: pid == 12345), \
            patch("goalflight_compat.kill_pid", side_effect=AssertionError("legacy pid killed")):
            killed = goalflight_acp_client.cleanup_ghosts()
            preserved = stale.exists()

    assert killed == 0
    assert preserved


def case_indeterminate_connection_kill_retains_tracking() -> None:
    async def run_case(state_dir: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        try:
            started = goalflight_ledger.process_identity(proc.pid)
            assert started and started.get("start_token"), started
            connection = object.__new__(
                goalflight_acp_client.GoalflightAcpConnection
            )
            connection.proc = proc
            connection.verified_pgid = proc.pid
            connection._started_identity = started
            connection._registered = True
            connection._stderr_task = None
            connection.conn = SimpleNamespace()

            # Fail both raw identity sources. The production process_identity +
            # compare path must derive identity_indeterminate; the test never
            # supplies that verdict or a termination result.
            with patch(
                "goalflight_ledger.goalflight_compat.process_start_identity",
                return_value=None,
            ), patch(
                "goalflight_ledger._posix_ps_available",
                return_value=False,
            ), patch(
                "goalflight_acp_client.goalflight_compat.kill_pid",
                side_effect=AssertionError("indeterminate worker signaled"),
            ), patch(
                "goalflight_acp_client._unregister_connection",
                side_effect=AssertionError("indeterminate worker unregistered"),
            ):
                derived = goalflight_ledger.process_identity(proc.pid)
                assert derived and not derived.get("start_token"), derived
                outcome = await connection.kill(reap_timeout_s=0.05)

            assert outcome.confirmed is False, outcome
            assert outcome.scope_alive is True, outcome
            assert outcome.reason.startswith("identity_indeterminate:"), outcome
            assert connection._registered is True
            assert proc.returncode is None

            lease_id = "identity-indeterminate-lease"
            capacity = goalflight_capacity.load_state()
            capacity["leases"][lease_id] = {
                "lease_id": lease_id,
                "dispatch_id": "identity-indeterminate",
                "agent": "codex",
                "worker_pid": proc.pid,
                "state": "active",
            }
            goalflight_capacity.save_state(capacity)
            payload: dict[str, object] = {}
            goalflight_acp_run._finalize_capacity_after_cleanup(
                payload,
                lease_id=lease_id,
                worker_pid=proc.pid,
                pgid=proc.pid,
                termination_result=outcome,
                detach_worker=False,
                detach=lambda _pid, _reason: None,
                state="failed",
                reason="identity_indeterminate",
            )
            retained = goalflight_capacity.load_state()["leases"][lease_id]
            assert retained["state"] == "active", retained
            assert retained["reason"] == goalflight_capacity.INDETERMINATE_LIVE_REASON
            assert payload["capacity_lease_disposition"] == "retained_death_unconfirmed"

        finally:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)

    with tempfile.TemporaryDirectory() as td, patch.dict(
        os.environ,
        {"GOALFLIGHT_STATE_DIR": str(Path(td) / "state")},
        clear=False,
    ):
        asyncio.run(run_case(Path(td) / "state"))


def case_connection_fallback_refuses_reused_pid() -> None:
    class FakeProc:
        pid = 12345
        returncode = None
        kill_called = False
        wait_called = False

        def kill(self) -> None:
            self.kill_called = True
            raise AssertionError("reused pid killed through subprocess fallback")

        async def wait(self) -> None:
            self.wait_called = True
            raise AssertionError("reused pid awaited after skipped fallback")

    class FakeConn:
        async def close(self) -> None:
            return None

    started_identity = {
        "pid": 12345,
        "start_token": "test:12345:generation-1",
        "lstart": "Wed May 20 17:55:24 2026",
        "comm": "python",
        "args": "worker --token secret-value",
    }
    live_identity = {**started_identity, "comm": "node"}
    reused_identity = {
        **live_identity,
        "start_token": "test:12345:generation-2",
    }
    connection = object.__new__(goalflight_acp_client.GoalflightAcpConnection)
    connection.proc = FakeProc()
    connection.conn = FakeConn()
    connection.verified_pgid = 12345
    connection._started_identity = started_identity
    connection._registered = True
    connection._stderr_task = None

    with patch(
        "goalflight_acp_client.goalflight_ledger.process_identity",
        side_effect=[live_identity, reused_identity],
    ), patch(
        "goalflight_acp_client.goalflight_compat.kill_pid",
        return_value=False,
    ), patch("goalflight_acp_client._unregister_connection") as unregister, patch(
        "goalflight_acp_client.log.warning"
    ) as warning:
        asyncio.run(connection.kill())

    assert connection.proc.kill_called is False
    assert connection.proc.wait_called is False
    assert connection._registered is False
    unregister.assert_called_once_with(connection)
    assert "secret-value" not in repr(warning.call_args_list)


def case_hard_signal_reap_deadline_retains_live_scope() -> None:
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )

    class WedgedWaitProc:
        pid = worker.pid
        returncode = None
        stdin = None
        stderr = None

        async def wait(self) -> None:
            await asyncio.Event().wait()

        def kill(self) -> None:
            raise AssertionError("bare-pid fallback should not run")

    class FakeConn:
        async def close(self) -> None:
            return None

    try:
        started = goalflight_ledger.process_identity(worker.pid)
        assert started and started.get("start_token"), started
        connection = object.__new__(goalflight_acp_client.GoalflightAcpConnection)
        connection.proc = WedgedWaitProc()
        connection.conn = FakeConn()
        connection.verified_pgid = worker.pid
        connection._started_identity = started
        connection._registered = True
        connection._stderr_task = None

        started_at = time.monotonic()
        with patch(
            "goalflight_acp_client.goalflight_compat.kill_pid",
            return_value=True,
        ):
            outcome = asyncio.run(connection.kill(reap_timeout_s=0.05))
        elapsed = time.monotonic() - started_at

        assert elapsed < 0.5, elapsed
        assert outcome.reap_timed_out is True, outcome
        assert outcome.confirmed is False, outcome
        assert outcome.scope_alive is True, outcome
        assert outcome.reason.startswith("hard_signal_reap_deadline:"), outcome
        assert connection._registered is True

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"GOALFLIGHT_STATE_DIR": str(Path(td) / "state")},
            clear=False,
        ):
            lease_id = "wedged-reap-deadline"
            capacity = goalflight_capacity.load_state()
            capacity["leases"][lease_id] = {
                "lease_id": lease_id,
                "dispatch_id": lease_id,
                "agent": "codex",
                "worker_pid": worker.pid,
                "worker_pgid": worker.pid,
                "state": "active",
            }
            goalflight_capacity.save_state(capacity)
            payload: dict[str, object] = {}
            goalflight_acp_run._finalize_capacity_after_cleanup(
                payload,
                lease_id=lease_id,
                worker_pid=worker.pid,
                pgid=worker.pid,
                termination_result=outcome,
                detach_worker=False,
                detach=lambda _pid, _reason: None,
                state="failed",
                reason="reap_deadline",
            )
            retained = goalflight_capacity.load_state()["leases"][lease_id]
            assert retained["state"] == "active", retained
            assert retained["reason"] == goalflight_capacity.INDETERMINATE_LIVE_REASON
            assert payload["capacity_lease_disposition"] == "retained_death_unconfirmed"

            worker.kill()
            worker.wait(timeout=5)
            confirmed = goalflight_acp_client.termination_result_for_process(
                pid=worker.pid,
                pgid=worker.pid,
                reason="control_group_dead",
            )
            assert confirmed.confirmed and confirmed.scope_alive is False, confirmed
            released_payload: dict[str, object] = {}
            goalflight_acp_run._finalize_capacity_after_cleanup(
                released_payload,
                lease_id=lease_id,
                worker_pid=worker.pid,
                pgid=worker.pid,
                termination_result=confirmed,
                detach_worker=False,
                detach=lambda _pid, _reason: None,
                state="failed",
                reason="group_dead",
            )
            released = goalflight_capacity.load_state()["leases"][lease_id]
            assert released["state"] == "failed", released
            assert (
                released_payload["capacity_lease_disposition"]
                == "released_group_death_confirmed"
            )
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)


def case_handshake_retry_refuses_replacement_after_unconfirmed_cleanup() -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.proc = SimpleNamespace(pid=12345)

        async def initialize(self, *, timeout: float) -> None:
            raise goalflight_acp_client.AcpError("handshake failed")

        async def kill(self) -> goalflight_acp_client.AcpTerminationResult:
            return goalflight_acp_client.AcpTerminationResult(
                pid=12345,
                pgid=12345,
                confirmed=False,
                scope_alive=True,
                reason="identity_indeterminate:process_group_live",
            )

    spawn_count = 0

    async def fake_spawn(*_args: object, **_kwargs: object) -> FakeConn:
        nonlocal spawn_count
        spawn_count += 1
        return FakeConn()

    async def run_case() -> None:
        with patch(
            "goalflight_acp_run.spawn_acp_connection",
            side_effect=fake_spawn,
        ):
            try:
                await goalflight_acp_run.spawn_and_handshake_with_retry(
                    "fake-acp",
                    [],
                    agent="codex",
                    session_id="retry-unconfirmed",
                    cwd=str(ROOT),
                    attempts=2,
                )
            except goalflight_acp_client.AcpTerminationUnconfirmed as exc:
                assert exc.result.confirmed is False
                assert exc.result.scope_alive is True
            else:
                raise AssertionError("replacement spawned after unconfirmed cleanup")

    asyncio.run(run_case())
    assert spawn_count == 1, spawn_count


def case_spawn_cleanup_signal_failure_is_bounded_and_retained() -> None:
    async def run_case() -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        reap_entered = asyncio.Event()

        class SignalDeniedWedgedProc:
            pid = worker.pid
            returncode = None

            def kill(self) -> None:
                raise PermissionError("denied")

            async def wait(self) -> None:
                reap_entered.set()
                await asyncio.Event().wait()

        proc = SignalDeniedWedgedProc()
        try:
            identity = goalflight_ledger.process_identity(proc.pid)
            assert identity and identity.get("start_token"), identity
            started_at = time.monotonic()
            cleanup_task = asyncio.create_task(
                goalflight_acp_client._raise_after_failed_spawn_cleanup(
                        proc,
                        pgid=proc.pid,
                        message="post-spawn construction failed",
                        started_identity=identity,
                        reap_timeout_s=0.05,
                )
            )
            await asyncio.wait_for(reap_entered.wait(), timeout=1)
            cleanup_task.cancel()
            cleanup_task.cancel()
            try:
                await cleanup_task
            except goalflight_acp_client.AcpTerminationUnconfirmed as exc:
                outcome = exc.result
                assert exc.proc is proc
                assert exc.started_identity == identity
            else:
                raise AssertionError("live spawn escaped without retained evidence")
            elapsed = time.monotonic() - started_at
            assert elapsed < 0.5, elapsed
            assert outcome.confirmed is False, outcome
            assert outcome.scope_alive is True, outcome
            assert outcome.reason.startswith("spawn_cleanup_signal_failed:"), outcome
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ,
                {"GOALFLIGHT_STATE_DIR": str(Path(td) / "state")},
                clear=False,
            ):
                lease_id = "spawn-cleanup-signal-failure"
                capacity = goalflight_capacity.load_state()
                capacity["leases"][lease_id] = {
                    "lease_id": lease_id,
                    "dispatch_id": lease_id,
                    "agent": "codex",
                    "worker_pid": proc.pid,
                    "worker_pgid": proc.pid,
                    "state": "active",
                }
                goalflight_capacity.save_state(capacity)
                payload: dict[str, object] = {}
                goalflight_acp_run._finalize_capacity_after_cleanup(
                    payload,
                    lease_id=lease_id,
                    worker_pid=proc.pid,
                    pgid=proc.pid,
                    termination_result=outcome,
                    detach_worker=False,
                    detach=lambda _pid, _reason: None,
                    state="failed",
                    reason="post_spawn_construction_failed",
                )
                retained = goalflight_capacity.load_state()["leases"][lease_id]
                assert retained["state"] == "active", retained
                assert (
                    retained["reason"]
                    == goalflight_capacity.INDETERMINATE_LIVE_REASON
                )
                assert (
                    payload["capacity_lease_disposition"]
                    == "retained_death_unconfirmed"
                )
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.wait(), timeout=5)

    asyncio.run(run_case())


def case_pool_handshake_unconfirmed_consumes_admission_slot() -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.proc = SimpleNamespace(pid=12345)
            self.reusable = True
            self.alive = True
            self.last_active = time.time()
            self._started_identity = {"pid": 12345, "start_token": "test:12345"}

        async def initialize(self) -> None:
            raise goalflight_acp_client.AcpError("handshake failed")

        async def kill(self) -> goalflight_acp_client.AcpTerminationResult:
            return goalflight_acp_client.AcpTerminationResult(
                pid=12345,
                pgid=12345,
                confirmed=False,
                scope_alive=True,
                reason="identity_indeterminate:process_group_live",
            )

    spawn_count = 0

    async def fake_spawn(*_args: object, **_kwargs: object) -> FakeConn:
        nonlocal spawn_count
        spawn_count += 1
        return FakeConn()

    async def run_case() -> None:
        pool = goalflight_acp_client.AcpProcessPool(
            {"codex": {"command": "fake", "acp_args": []}},
            max_processes=1,
            max_per_agent=1,
        )
        with patch(
            "goalflight_acp_client.spawn_acp_connection",
            side_effect=fake_spawn,
        ):
            try:
                await pool.get_or_create("codex", "first", str(ROOT))
            except goalflight_acp_client.AcpTerminationUnconfirmed:
                pass
            else:
                raise AssertionError("unconfirmed handshake cleanup was accepted")
            assert pool.stats == {"total": 1, "by_agent": {"codex": 1}}, pool.stats
            try:
                await pool.get_or_create("codex", "second", str(ROOT))
            except goalflight_acp_client.PoolExhaustedError:
                pass
            else:
                raise AssertionError("quarantined live worker did not consume capacity")
        assert spawn_count == 1, spawn_count

    asyncio.run(run_case())


def case_pool_handshake_cancellation_retains_admission_slot() -> None:
    async def run_case() -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        handshake_entered = asyncio.Event()
        reap_entered = asyncio.Event()

        class WedgedWaitProc:
            pid = worker.pid
            returncode = None
            stdin = None
            stderr = None

            async def wait(self) -> None:
                reap_entered.set()
                await asyncio.Event().wait()

            def kill(self) -> None:
                raise AssertionError("bare-pid fallback should not run")

        class FakeProtocol:
            async def close(self) -> None:
                return None

        class BlockingPoolConnection(
            goalflight_acp_client.GoalflightAcpConnection
        ):
            async def initialize(self) -> None:
                handshake_entered.set()
                await asyncio.Event().wait()

            async def kill(self, **_kwargs):
                return await super().kill(reap_timeout_s=0.05)

        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        connection = object.__new__(BlockingPoolConnection)
        connection.agent = "codex"
        connection.session_id = "first"
        connection.proc = WedgedWaitProc()
        connection.conn = FakeProtocol()
        connection.verified_pgid = worker.pid
        connection._started_identity = identity
        connection._registered = False
        connection._stderr_task = None
        connection.reusable = True
        connection.last_active = time.time()
        connection.context_mode = True
        connection.os_sandbox = goalflight_acp_client.OS_SANDBOX_OFF
        connection.cwd = str(ROOT)

        spawn_count = 0

        async def fake_spawn(*_args: object, **_kwargs: object):
            nonlocal spawn_count
            spawn_count += 1
            return connection

        pool = goalflight_acp_client.AcpProcessPool(
            {"codex": {"command": "fake", "acp_args": []}},
            max_processes=1,
            max_per_agent=1,
        )
        try:
            with patch(
                "goalflight_acp_client.spawn_acp_connection",
                side_effect=fake_spawn,
            ), patch(
                "goalflight_acp_client.goalflight_compat.kill_pid",
                return_value=True,
            ):
                task = asyncio.create_task(
                    pool.get_or_create("codex", "first", str(ROOT))
                )
                await asyncio.wait_for(handshake_entered.wait(), timeout=1)
                task.cancel()
                await asyncio.wait_for(reap_entered.wait(), timeout=1)
                task.cancel()
                try:
                    await task
                except goalflight_acp_client.AcpTerminationUnconfirmed as exc:
                    assert exc.result.confirmed is False, exc.result
                    assert exc.result.scope_alive is True, exc.result
                else:
                    raise AssertionError("cancelled pool worker escaped accounting")

                assert pool.stats == {
                    "total": 1,
                    "by_agent": {"codex": 1},
                }, pool.stats
                try:
                    await pool.get_or_create("codex", "second", str(ROOT))
                except goalflight_acp_client.PoolExhaustedError:
                    pass
                else:
                    raise AssertionError("cancelled pool worker allowed oversubscription")
                assert spawn_count == 1, spawn_count
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.wait(), timeout=5)
            await pool.shutdown()

    asyncio.run(run_case())


def case_pool_concurrent_admission_reservation_is_atomic() -> None:
    async def run_case() -> None:
        spawn_entered = asyncio.Event()
        allow_spawn = asyncio.Event()
        spawn_count = 0

        class FakeConn:
            def __init__(self) -> None:
                self.proc = SimpleNamespace(pid=12345)
                self.reusable = True
                self.last_active = time.time()

            async def initialize(self) -> None:
                return None

            async def new_session(self, _cwd: str) -> None:
                return None

        async def blocked_spawn(*_args: object, **_kwargs: object) -> FakeConn:
            nonlocal spawn_count
            spawn_count += 1
            spawn_entered.set()
            await allow_spawn.wait()
            return FakeConn()

        pool = goalflight_acp_client.AcpProcessPool(
            {"codex": {"command": "fake", "acp_args": []}},
            max_processes=1,
            max_per_agent=1,
        )
        with patch(
            "goalflight_acp_client.spawn_acp_connection",
            side_effect=blocked_spawn,
        ):
            first = asyncio.create_task(
                pool.get_or_create("codex", "first", str(ROOT))
            )
            await asyncio.wait_for(spawn_entered.wait(), timeout=1)
            assert pool.stats == {
                "total": 1,
                "by_agent": {"codex": 1},
            }, pool.stats

            for blocked_session in ("first", "second"):
                try:
                    await pool.get_or_create("codex", blocked_session, str(ROOT))
                except goalflight_acp_client.PoolExhaustedError:
                    pass
                else:
                    raise AssertionError(
                        f"in-flight reservation allowed session {blocked_session!r}"
                    )
            assert spawn_count == 1, spawn_count
            allow_spawn.set()
            connection = await asyncio.wait_for(first, timeout=1)
            assert isinstance(connection, FakeConn)
            assert pool.stats == {
                "total": 1,
                "by_agent": {"codex": 1},
            }, pool.stats

    asyncio.run(run_case())


def case_pool_spawn_unconfirmed_consumes_admission_slot() -> None:
    async def run_case() -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        pool = goalflight_acp_client.AcpProcessPool(
            {"codex": {"command": "fake", "acp_args": []}},
            max_processes=1,
            max_per_agent=1,
        )
        spawn_count = 0
        try:
            identity = goalflight_ledger.process_identity(worker.pid)
            assert identity and identity.get("start_token"), identity
            outcome = goalflight_acp_client.termination_result_for_process(
                pid=worker.pid,
                pgid=worker.pid,
                reason="spawn_cleanup_signal_failed",
            )
            assert outcome.confirmed is False and outcome.scope_alive is True, outcome

            async def fake_spawn(*_args: object, **_kwargs: object):
                nonlocal spawn_count
                spawn_count += 1
                raise goalflight_acp_client.AcpTerminationUnconfirmed(
                    "spawn cleanup unconfirmed",
                    result=outcome,
                    proc=worker,
                    started_identity=identity,
                )

            with patch(
                "goalflight_acp_client.spawn_acp_connection",
                side_effect=fake_spawn,
            ):
                try:
                    await pool.get_or_create("codex", "first", str(ROOT))
                except goalflight_acp_client.AcpTerminationUnconfirmed:
                    pass
                else:
                    raise AssertionError("unconfirmed spawn was accepted")
                assert pool.stats == {"total": 1, "by_agent": {"codex": 1}}, pool.stats
                try:
                    await pool.get_or_create("codex", "second", str(ROOT))
                except goalflight_acp_client.PoolExhaustedError:
                    pass
                else:
                    raise AssertionError("spawn termination hold did not consume capacity")
            assert spawn_count == 1, spawn_count
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.wait(), timeout=5)
            await pool.shutdown()

    asyncio.run(run_case())


def case_cancelled_post_spawn_cleanup_is_shielded_and_retained() -> None:
    async def run_case() -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        attach_entered = asyncio.Event()
        reap_entered = asyncio.Event()

        class WedgedWaitProc:
            pid = worker.pid
            returncode = None
            stdin = None
            stderr = None

            async def wait(self) -> None:
                reap_entered.set()
                await asyncio.Event().wait()

            def kill(self) -> None:
                raise AssertionError("bare-pid fallback should not run")

        class FakeProtocol:
            async def close(self) -> None:
                return None

        class FastCleanupConnection(
            goalflight_acp_client.GoalflightAcpConnection
        ):
            async def kill(self, **_kwargs):
                return await super().kill(reap_timeout_s=0.05)

        class BlockingCapture:
            async def attach(self, _conn) -> None:
                attach_entered.set()
                await asyncio.Event().wait()

        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        connection = object.__new__(FastCleanupConnection)
        connection.proc = WedgedWaitProc()
        connection.conn = FakeProtocol()
        connection.verified_pgid = worker.pid
        connection._started_identity = identity
        connection._registered = False
        connection._stderr_task = None

        async def fake_spawn(*_args: object, **_kwargs: object):
            return connection

        try:
            with patch(
                "goalflight_acp_run.spawn_acp_connection",
                side_effect=fake_spawn,
            ), patch(
                "goalflight_acp_client.goalflight_compat.kill_pid",
                return_value=True,
            ):
                task = asyncio.create_task(
                    goalflight_acp_run.spawn_and_handshake_with_retry(
                        "fake-acp",
                        [],
                        agent="codex",
                        session_id="cancelled-post-spawn",
                        cwd=str(ROOT),
                        attempts=1,
                        stderr_capture=BlockingCapture(),
                    )
                )
                await asyncio.wait_for(attach_entered.wait(), timeout=1)
                started_at = time.monotonic()
                task.cancel()
                await asyncio.wait_for(reap_entered.wait(), timeout=1)
                # A second cancellation lands while the bounded reap is active.
                # The cleanup task must remain shielded and return its evidence.
                task.cancel()
                try:
                    await task
                except goalflight_acp_client.AcpTerminationUnconfirmed as exc:
                    outcome = exc.result
                    assert exc.proc is connection.proc
                else:
                    raise AssertionError("cancelled live scope escaped ownership")
                elapsed = time.monotonic() - started_at

            assert elapsed < 0.5, elapsed
            assert outcome.reap_timed_out is True, outcome
            assert outcome.confirmed is False, outcome
            assert outcome.scope_alive is True, outcome

            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ,
                {"GOALFLIGHT_STATE_DIR": str(Path(td) / "state")},
                clear=False,
            ):
                lease_id = "cancelled-post-spawn"
                capacity = goalflight_capacity.load_state()
                capacity["leases"][lease_id] = {
                    "lease_id": lease_id,
                    "dispatch_id": lease_id,
                    "agent": "codex",
                    "worker_pid": worker.pid,
                    "worker_pgid": worker.pid,
                    "state": "active",
                }
                goalflight_capacity.save_state(capacity)
                payload: dict[str, object] = {}
                goalflight_acp_run._finalize_capacity_after_cleanup(
                    payload,
                    lease_id=lease_id,
                    worker_pid=worker.pid,
                    pgid=worker.pid,
                    termination_result=outcome,
                    detach_worker=False,
                    detach=lambda _pid, _reason: None,
                    state="failed",
                    reason="cancelled_post_spawn",
                )
                retained = goalflight_capacity.load_state()["leases"][lease_id]
                assert retained["state"] == "active", retained
                assert (
                    retained["reason"]
                    == goalflight_capacity.INDETERMINATE_LIVE_REASON
                )
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.wait(), timeout=5)

    asyncio.run(run_case())


def case_pool_remove_refuses_live_reusable_connection() -> None:
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    pool = goalflight_acp_client.AcpProcessPool(
        {"codex": {"command": "fake", "acp_args": []}},
        max_processes=1,
        max_per_agent=1,
    )
    connection = object.__new__(goalflight_acp_client.GoalflightAcpConnection)
    connection.agent = "codex"
    connection.session_id = "first"
    connection.proc = worker
    connection.verified_pgid = worker.pid
    connection.reusable = True
    connection._registered = False
    pool._connections[("codex", "first")] = connection

    async def second_admission() -> None:
        with patch(
            "goalflight_acp_client.spawn_acp_connection",
            side_effect=AssertionError("replacement worker spawned"),
        ):
            try:
                await pool.get_or_create("codex", "second", str(ROOT))
            except goalflight_acp_client.PoolExhaustedError:
                return
        raise AssertionError("live reusable worker left admission accounting")

    try:
        assert pool.remove("codex", "first") is False
        assert pool.stats == {"total": 1, "by_agent": {"codex": 1}}, pool.stats
        asyncio.run(second_admission())
        worker.kill()
        worker.wait(timeout=5)
        assert pool.remove("codex", "first") is True
        assert pool.stats == {"total": 0, "by_agent": {}}, pool.stats
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=5)


def case_initial_lease_attach_failure_cleans_before_running() -> None:
    async def run_case() -> None:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )

        class FakeProtocol:
            async def close(self) -> None:
                return None

        identity = goalflight_ledger.process_identity(worker.pid)
        assert identity and identity.get("start_token"), identity
        connection = object.__new__(goalflight_acp_client.GoalflightAcpConnection)
        connection.proc = worker
        connection.conn = FakeProtocol()
        connection.verified_pgid = worker.pid
        connection._started_identity = identity
        connection._registered = False
        connection._stderr_task = None

        async def fake_spawn(*_args: object, **_kwargs: object):
            return connection

        def fail_capacity_attach(
            _attempt: int,
            proc: asyncio.subprocess.Process,
        ) -> None:
            goalflight_acp_run._attach_worker_state_before_running(
                lambda _pid, _pgid: (_ for _ in ()).throw(
                    OSError("capacity state unavailable")
                ),
                proc.pid,
                proc.pid,
            )

        try:
            with patch(
                "goalflight_acp_run.spawn_acp_connection",
                side_effect=fake_spawn,
            ):
                try:
                    await goalflight_acp_run.spawn_and_handshake_with_retry(
                        "fake-acp",
                        [],
                        agent="codex",
                        session_id="attach-failure",
                        cwd=str(ROOT),
                        attempts=1,
                        on_attempt=fail_capacity_attach,
                    )
                except OSError as exc:
                    assert "capacity state unavailable" in str(exc)
                else:
                    raise AssertionError("RUNNING continued without capacity attachment")

            confirmed = goalflight_acp_client.termination_result_for_process(
                pid=worker.pid,
                pgid=worker.pid,
                reason="attach_failure_cleanup",
            )
            assert confirmed.confirmed and confirmed.scope_alive is False, confirmed
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.wait(), timeout=5)

    asyncio.run(run_case())


def case_group_kill_can_disable_unchecked_pid_fallback() -> None:
    with patch("goalflight_compat.is_windows", return_value=False), patch(
        "goalflight_compat.os.killpg", side_effect=PermissionError
    ), patch(
        "goalflight_compat.os.kill",
        side_effect=AssertionError("unchecked bare-PID fallback used"),
    ):
        killed = goalflight_acp_client.goalflight_compat.kill_pid(
            12345,
            goalflight_acp_client.signal.SIGKILL,
            pgid=12345,
            process_group=True,
            fallback_to_pid=False,
        )

    assert killed is False


def case_atexit_pool_kill_requires_fine_identity() -> None:
    started = {
        "pid": 12345,
        "start_token": "test:12345:generation-1",
        "lstart": "Wed May 20 17:55:24 2026",
        "comm": "python",
    }
    connection = SimpleNamespace(
        alive=True,
        proc=SimpleNamespace(pid=12345),
        verified_pgid=12345,
        _started_identity=started,
    )
    current = {**started, "comm": "node"}
    with patch(
        "acp_pool.goalflight_ledger.process_identity", return_value=current
    ), patch("acp_pool.goalflight_compat.kill_pid", return_value=True) as kill:
        assert acp_pool._kill_connection_sync(connection) is True
    assert kill.call_args.kwargs["fallback_to_pid"] is False
    assert kill.call_args.kwargs["expected_identity"] == started

    reused = {**current, "start_token": "test:12345:generation-2"}
    with patch(
        "acp_pool.goalflight_ledger.process_identity", return_value=reused
    ), patch(
        "acp_pool.goalflight_compat.kill_pid",
        side_effect=AssertionError("reused atexit pid killed"),
    ):
        assert acp_pool._kill_connection_sync(connection) is False


def main() -> None:
    case_exec_comm_change_keeps_identity()
    case_pid_reuse_lstart_change_is_different()
    case_unavailable_meta_preserves_kill_fallthrough()
    case_windows_cleanup_skips_bare_pidfile_pid()
    case_windows_cleanup_does_not_kill_indeterminate_pid()
    case_windows_cleanup_kills_confirmed_live_identity()
    case_windows_cleanup_unlinks_confirmed_dead_pid()
    case_posix_cleanup_preserves_live_legacy_pidfile()
    case_indeterminate_connection_kill_retains_tracking()
    case_connection_fallback_refuses_reused_pid()
    case_hard_signal_reap_deadline_retains_live_scope()
    case_handshake_retry_refuses_replacement_after_unconfirmed_cleanup()
    case_spawn_cleanup_signal_failure_is_bounded_and_retained()
    case_pool_handshake_unconfirmed_consumes_admission_slot()
    case_pool_handshake_cancellation_retains_admission_slot()
    case_pool_concurrent_admission_reservation_is_atomic()
    case_pool_spawn_unconfirmed_consumes_admission_slot()
    case_cancelled_post_spawn_cleanup_is_shielded_and_retained()
    case_pool_remove_refuses_live_reusable_connection()
    case_initial_lease_attach_failure_cleans_before_running()
    case_group_kill_can_disable_unchecked_pid_fallback()
    case_atexit_pool_kill_requires_fine_identity()
    print("OK: 22 ACP kill identity tests pass")


if __name__ == "__main__":
    main()
