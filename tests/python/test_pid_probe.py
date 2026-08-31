"""Windows pid-probe and ledger identity honesty tests."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402
import goalflight_ledger  # noqa: E402


class _Func:
    def __init__(self, impl):
        self.impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self.impl(*args)


class _Kernel32:
    def __init__(self, *, handle: int, exit_code: int):
        self.OpenProcess = _Func(lambda *_args: handle)

        def _exit(_handle, ptr):
            ptr._obj.value = exit_code
            return True

        self.GetExitCodeProcess = _Func(_exit)
        self.CloseHandle = _Func(lambda _handle: True)


def case_windows_pid_alive_does_not_call_os_kill() -> None:
    kernel32 = _Kernel32(handle=123, exit_code=259)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True), \
        patch("goalflight_compat.os.kill", side_effect=AssertionError("os.kill must not run")):
        assert goalflight_compat.pid_alive(4242) is True

    kernel32 = _Kernel32(handle=123, exit_code=0)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True), \
        patch("goalflight_compat.os.kill", side_effect=AssertionError("os.kill must not run")):
        assert goalflight_compat.pid_alive(4242) is False


def case_windows_access_denied_means_alive() -> None:
    kernel32 = _Kernel32(handle=0, exit_code=0)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True), \
        patch("ctypes.get_last_error", return_value=5, create=True):
        assert goalflight_compat.pid_alive(4242) is True


def case_windows_pid_liveness_without_windll_is_unknown() -> None:
    import ctypes

    prior = getattr(ctypes, "WinDLL", None)
    had = hasattr(ctypes, "WinDLL")
    try:
        if had:
            del ctypes.WinDLL
        with patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.os.kill", side_effect=AssertionError("os.kill must not run")):
            assert not hasattr(ctypes, "WinDLL")
            assert goalflight_compat.pid_liveness(4242) is None
            assert goalflight_compat.pid_alive(4242) is True
    finally:
        if had:
            ctypes.WinDLL = prior


def case_windows_pid_liveness_kernel32_load_failure_is_visible() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch(
            "ctypes.WinDLL",
            side_effect=OSError(2, "The specified module could not be found"),
            create=True,
        ), \
        patch("goalflight_compat.os.kill", side_effect=AssertionError("os.kill must not run")):
        try:
            goalflight_compat.pid_liveness(4242)
        except OSError as exc:
            assert exc.errno == 2
        else:
            raise AssertionError("kernel32 load failure must not become unknown")


def case_posix_pid_probe_error_is_indeterminate() -> None:
    with patch("goalflight_compat.is_windows", return_value=False), \
        patch(
            "goalflight_compat.os.kill",
            side_effect=OSError(errno.ENFILE, "file table full"),
        ):
        assert goalflight_compat.pid_liveness(os.getpid()) is None
        assert goalflight_compat.pid_alive(os.getpid()) is True


def case_live_pid_probe_error_classifies_indeterminate() -> None:
    pid = os.getpid()
    prior = goalflight_ledger.process_identity(pid)
    assert prior is not None
    record = {"worker_pid": pid, "worker_identity": prior}
    with patch(
        "goalflight_compat.os.kill",
        side_effect=OSError(errno.ENFILE, "file table full"),
    ):
        current = goalflight_ledger.process_identity(pid)
        assert current is not None
        assert current["identity_available"] is False
        assert current["identity_probe_error"] is True
        assert goalflight_ledger.identity_matches(record) == (
            True,
            "identity_indeterminate",
        )
        assert goalflight_ledger.classify(record) == "identity_indeterminate"


def case_live_ps_probe_error_classifies_indeterminate() -> None:
    pid = os.getpid()
    prior = goalflight_ledger.process_identity(pid)
    assert prior is not None
    record = {"worker_pid": pid, "worker_identity": prior}
    with patch(
        "goalflight_ledger.subprocess.check_output",
        side_effect=OSError(errno.ENFILE, "file table full"),
    ), patch("goalflight_ledger._posix_ps_available", return_value=True):
        current = goalflight_ledger.process_identity(pid)
        assert current is not None
        assert current["identity_available"] is False
        assert current["identity_probe_error"] is True
        assert current["identity_source"] == "ps_probe_error"
        assert goalflight_ledger.compare_process_identities(pid, prior, current) == (
            True,
            "identity_indeterminate",
        )
        assert goalflight_ledger.compare_fine_process_identities(
            pid, prior, current
        ) == (False, "identity_indeterminate")
        assert goalflight_ledger.classify(record) == "identity_indeterminate"


def case_live_ps_missing_lstart_classifies_indeterminate() -> None:
    pid = os.getpid()
    prior = goalflight_ledger.process_identity(pid)
    assert prior is not None
    record = {"worker_pid": pid, "worker_identity": prior}

    def ps_without_lstart(args, **_kwargs):
        return "" if args[-1] == "lstart=" else "1"

    with patch(
        "goalflight_ledger.subprocess.check_output",
        side_effect=ps_without_lstart,
    ), patch("goalflight_ledger._posix_ps_available", return_value=True), patch(
        "goalflight_ledger.time.sleep", return_value=None
    ):
        current = goalflight_ledger.process_identity(pid)
        assert current is not None
        assert current["identity_available"] is False
        assert current["identity_probe_error"] is True
        assert current["identity_source"] == "ps_identity_incomplete"
        assert goalflight_ledger.classify(record) == "identity_indeterminate"


def case_reaped_pid_still_classifies_dead() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    prior = goalflight_ledger.process_identity(proc.pid)
    assert prior is not None
    proc.wait(timeout=5)
    record = {"worker_pid": proc.pid, "worker_identity": prior}
    assert goalflight_compat.pid_liveness(proc.pid) is False
    assert goalflight_ledger.identity_matches(record) == (False, "dead")
    assert goalflight_ledger.classify(record) == "stale_dead"


def case_ledger_windows_identity_indeterminate_not_expected_live() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.pid_liveness", return_value=True):
        ident = goalflight_ledger.process_identity(os.getpid())
        assert ident is not None
        assert ident["identity_available"] is False
        ok, reason = goalflight_ledger.identity_matches({"worker_pid": os.getpid(), "worker_identity": ident})
        assert ok is False
        assert reason == "identity_indeterminate"
        assert goalflight_ledger.classify({"worker_pid": os.getpid(), "worker_identity": ident}) == "identity_indeterminate"


def main() -> None:
    case_windows_pid_alive_does_not_call_os_kill()
    case_windows_access_denied_means_alive()
    case_windows_pid_liveness_without_windll_is_unknown()
    case_windows_pid_liveness_kernel32_load_failure_is_visible()
    case_posix_pid_probe_error_is_indeterminate()
    case_live_pid_probe_error_classifies_indeterminate()
    case_live_ps_probe_error_classifies_indeterminate()
    case_live_ps_missing_lstart_classifies_indeterminate()
    case_reaped_pid_still_classifies_dead()
    case_ledger_windows_identity_indeterminate_not_expected_live()
    print("OK: pid probe tests pass")


if __name__ == "__main__":
    main()
