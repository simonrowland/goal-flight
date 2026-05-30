"""Windows pid-probe and ledger identity honesty tests."""

from __future__ import annotations

import os
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


def case_ledger_windows_identity_indeterminate_not_expected_live() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.pid_alive", return_value=True):
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
    case_ledger_windows_identity_indeterminate_not_expected_live()
    print("OK: pid probe tests pass")


if __name__ == "__main__":
    main()
