#!/usr/bin/env python3
"""Regression tests for ACP worker PID identity checks."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("asserts POSIX bash process identity strings")

import asyncio
import sys
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
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
            patch("goalflight_compat.pid_alive", side_effect=fake_pid_alive), \
            patch("goalflight_compat.kill_pid", side_effect=AssertionError("bare reused pid killed")), \
            patch("goalflight_acp_client.log.warning") as warn:
            killed = goalflight_acp_client.cleanup_ghosts()
    assert killed == 0
    assert not stale.exists()
    assert warn.called


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
    connection = object.__new__(goalflight_acp_client.GoalflightAcpConnection)
    connection.proc = SimpleNamespace(pid=12345, returncode=None)
    connection._started_identity = {
        "pid": 12345,
        "lstart": "Wed May 20 17:55:24 2026",
        "comm": "python",
    }
    connection._registered = True
    connection._stderr_task = None
    live_identity = {
        "pid": 12345,
        "lstart": "Wed May 20 17:55:24 2026",
        "comm": "node",
    }

    with patch(
        "goalflight_acp_client.goalflight_ledger.process_identity",
        return_value=live_identity,
    ), patch(
        "goalflight_acp_client._unregister_connection",
        side_effect=AssertionError("indeterminate worker unregistered"),
    ):
        asyncio.run(connection.kill())

    assert connection._registered is True


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
    case_posix_cleanup_preserves_live_legacy_pidfile()
    case_indeterminate_connection_kill_retains_tracking()
    case_connection_fallback_refuses_reused_pid()
    case_group_kill_can_disable_unchecked_pid_fallback()
    case_atexit_pool_kill_requires_fine_identity()
    print("OK: ACP kill identity tests pass")


if __name__ == "__main__":
    main()
