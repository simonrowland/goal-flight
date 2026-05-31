#!/usr/bin/env python3
"""Regression tests for the Windows-safe goal-flight compat shim."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("lock shim POSIX branch asserts /tmp, killpg, and signals")

import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402


def case_lock_constants_are_ints() -> None:
    for name in ("LOCK_EX", "LOCK_SH", "LOCK_NB", "LOCK_UN"):
        value = getattr(goalflight_compat, name)
        assert isinstance(value, int), name


def case_flock_nonblocking_contention_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lock"
        path.write_text("", encoding="utf-8")
        with path.open("r+", encoding="utf-8") as first, path.open("r+", encoding="utf-8") as second:
            goalflight_compat.flock(first, goalflight_compat.LOCK_EX | goalflight_compat.LOCK_NB)
            try:
                blocked = False
                try:
                    goalflight_compat.flock(second, goalflight_compat.LOCK_EX | goalflight_compat.LOCK_NB)
                except BlockingIOError:
                    blocked = True
                assert blocked
            finally:
                goalflight_compat.flock(first, goalflight_compat.LOCK_UN)


def case_default_state_dir_contract() -> None:
    state_dir = goalflight_compat.default_state_dir()
    assert isinstance(state_dir, Path)
    if not goalflight_compat.is_windows():
        assert state_dir == Path("/tmp") / f"goal-flight-{os.getuid()}"


def case_pid_alive_contract() -> None:
    assert goalflight_compat.pid_alive(os.getpid()) is True
    assert goalflight_compat.pid_alive(999999) is False
    assert goalflight_compat.pid_alive(-1) is False
    assert goalflight_compat.pid_alive(0) is False
    assert goalflight_compat.pid_alive("x") is False


def case_is_windows_contract() -> None:
    assert goalflight_compat.is_windows() == (os.name == "nt")


def case_posix_pid_alive_uses_signal_zero() -> None:
    if goalflight_compat.is_windows():
        return
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("goalflight_compat.os.kill", fake_kill):
        assert goalflight_compat.pid_alive(12345) is True
    assert calls == [(12345, 0)]

    def fake_missing(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise ProcessLookupError

    calls.clear()
    with patch("goalflight_compat.os.kill", fake_missing):
        assert goalflight_compat.pid_alive(12345) is False
    assert calls == [(12345, 0)]


def case_windows_kill_pid_requires_creation_identity() -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.windows_process_identity", return_value={"pid": 12345, "creation_time": "new"}), \
        patch("goalflight_compat.os.kill", fake_kill), \
        patch("goalflight_compat.os.killpg", side_effect=AssertionError("killpg must not run"), create=True), \
        patch("goalflight_compat.log.warning") as warn:
        assert goalflight_compat.kill_pid(12345, signal.SIGTERM, pgid=99999, process_group=True) is False
    assert calls == []
    assert warn.called


def case_windows_kill_pid_kills_only_matching_identity() -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.windows_process_identity", return_value={"pid": 12345, "creation_time": "same"}), \
        patch("goalflight_compat.os.kill", fake_kill), \
        patch("goalflight_compat.os.killpg", side_effect=AssertionError("killpg must not run"), create=True):
        assert goalflight_compat.kill_pid(
            12345,
            signal.SIGTERM,
            pgid=99999,
            process_group=True,
            expected_identity={"pid": 12345, "creation_time": "same"},
        ) is True
    assert calls == [(12345, signal.SIGTERM)]


def case_windows_kill_pid_skips_reused_pid() -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.windows_process_identity", return_value={"pid": 12345, "creation_time": "new"}), \
        patch("goalflight_compat.os.kill", fake_kill), \
        patch("goalflight_compat.log.warning") as warn:
        assert goalflight_compat.kill_pid(
            12345,
            signal.SIGTERM,
            expected_identity={"pid": 12345, "creation_time": "old"},
        ) is False
    assert calls == []
    assert warn.called


def main() -> None:
    case_lock_constants_are_ints()
    case_flock_nonblocking_contention_contract()
    case_default_state_dir_contract()
    case_pid_alive_contract()
    case_is_windows_contract()
    case_posix_pid_alive_uses_signal_zero()
    case_windows_kill_pid_requires_creation_identity()
    case_windows_kill_pid_kills_only_matching_identity()
    case_windows_kill_pid_skips_reused_pid()
    print("OK: goalflight compat shim tests pass")


if __name__ == "__main__":
    main()
