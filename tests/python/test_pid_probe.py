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
import goalflight_status  # noqa: E402


class _Func:
    def __init__(self, impl):
        self.impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self.impl(*args)


class _Kernel32:
    def __init__(
        self, *, handle: int, exit_code: int, exit_query_ok: bool = True
    ):
        self.OpenProcess = _Func(lambda *_args: handle)

        def _exit(_handle, ptr):
            ptr._obj.value = exit_code
            return exit_query_ok

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


def case_windows_probe_failures_are_indeterminate() -> None:
    kernel32 = _Kernel32(handle=0, exit_code=0)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True), \
        patch("ctypes.get_last_error", return_value=8, create=True):
        assert goalflight_compat.pid_liveness(4242) is None
        assert goalflight_compat.pid_alive(4242) is True

    kernel32 = _Kernel32(handle=123, exit_code=0, exit_query_ok=False)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True):
        assert goalflight_compat.pid_liveness(4242) is None
        assert goalflight_compat.pid_alive(4242) is True


def case_windows_openprocess_invalid_parameter_is_dead() -> None:
    kernel32 = _Kernel32(handle=0, exit_code=0)
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("ctypes.WinDLL", return_value=kernel32, create=True), \
        patch("ctypes.get_last_error", return_value=87, create=True):
        assert goalflight_compat.pid_liveness(4242) is False
        assert goalflight_compat.pid_alive(4242) is False


def case_windows_zombie_probe_preserves_unknown() -> None:
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.pid_liveness", return_value=None):
        assert goalflight_compat.pid_is_zombie(4242) is None
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.pid_liveness", return_value=True):
        assert goalflight_compat.pid_is_zombie(4242) is False
    with patch("goalflight_compat.is_windows", return_value=True), \
        patch("goalflight_compat.pid_liveness", return_value=False):
        assert goalflight_compat.pid_is_zombie(4242) is True


def case_pid_parameters_require_positive_integers() -> None:
    invalid_pids = (True, 1.9, 0, -1, None, "1")
    with patch("goalflight_compat.is_windows", return_value=False), \
        patch("goalflight_compat.os.kill") as kill, \
        patch("goalflight_compat.os.killpg") as killpg:
        for value in invalid_pids:
            assert goalflight_compat.pid_liveness(value) is False
            assert goalflight_compat.pid_is_zombie(value) is None
            assert goalflight_compat.kill_pid(value, process_group=False) is False
        kill.assert_not_called()
        killpg.assert_not_called()

    with patch("goalflight_compat.is_windows", return_value=True), \
        patch(
            "ctypes.WinDLL",
            side_effect=AssertionError("invalid PID reached Win32"),
            create=True,
        ):
        for value in invalid_pids:
            assert goalflight_compat.windows_process_identity(value) is None


def case_kill_pid_refuses_init_targets() -> None:
    with patch("goalflight_compat.is_windows", return_value=False), \
        patch("goalflight_compat.os.kill") as kill, \
        patch("goalflight_compat.os.killpg") as killpg:
        assert goalflight_compat.kill_pid(1, process_group=False) is False
        assert goalflight_compat.kill_pid(1, process_group=True) is False
        assert goalflight_compat.kill_pid(4242, pgid=1, process_group=True) is False
        for pgid in (True, 1.9, 0, -1, "1"):
            assert goalflight_compat.kill_pid(4242, pgid=pgid, process_group=True) is False
        kill.assert_not_called()
        killpg.assert_not_called()


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
    ):
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
    ) as check_output, patch("goalflight_ledger.time.sleep", return_value=None):
        current = goalflight_ledger.process_identity(pid)
        assert current is not None
        assert current["identity_available"] is False
        assert current["identity_probe_error"] is True
        assert current["identity_source"] == "ps_identity_incomplete"
        assert check_output.call_count == 1
        assert goalflight_ledger.classify(record) == "identity_indeterminate"


def case_process_identity_uses_one_combined_ps_probe() -> None:
    pid = os.getpid()
    ps_output = "123 456 Wed Sep 23 12:34:56 2026 python3 python3 worker\n"
    with patch("goalflight_compat.pid_liveness", return_value=True), patch(
        "goalflight_compat.process_start_identity",
        return_value={"pid": pid, "start_token": "native-token"},
    ), patch(
        "goalflight_ledger.subprocess.check_output", return_value=ps_output
    ) as check_output:
        identity = goalflight_ledger.process_identity(pid)

    assert identity == {
        "pid": pid,
        "ppid": "123",
        "pgid": "456",
        "lstart": "Wed Sep 23 12:34:56 2026",
        "comm": "python3",
        "args": "python3 worker",
        "start_token": "native-token",
    }
    assert check_output.call_count == 1
    assert check_output.call_args.args[0] == [
        "ps",
        "-o",
        "ppid=,pgid=,lstart=,comm=,args=",
        "-p",
        str(pid),
    ]


def case_probe_exceptions_are_unknown_not_dead() -> None:
    pid = os.getpid()
    ps_output = {"lstart": "Wed Sep 23 12:34:56 2026"}
    with patch(
        "goalflight_compat.pid_liveness",
        side_effect=PermissionError(errno.EPERM, "denied"),
    ):
        identity = goalflight_ledger.process_identity(pid)
        assert identity and identity["identity_probe_error"] is True
        assert goalflight_ledger.identity_matches(
            {"worker_pid": pid, "worker_identity": identity}
        ) == (True, "identity_indeterminate")

    with patch("goalflight_compat.pid_liveness", return_value=True), patch(
        "goalflight_compat.process_start_identity",
        side_effect=subprocess.CalledProcessError(1, ["native-start-probe"]),
    ), patch(
        "goalflight_ledger._ps_identity", return_value=(ps_output, True)
    ):
        identity = goalflight_ledger.process_identity(pid)
        assert identity and identity["lstart"] == ps_output["lstart"]
        assert goalflight_ledger.worker_identity_liveness(
            {"worker_pid": pid, "worker_identity": identity}
        ) == ("unknown", "identity_indeterminate")


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


def case_lstart_only_identity_is_unknown() -> None:
    pid = os.getpid()
    current = {"pid": pid, "lstart": "same-second-start", "start_token": "current-token"}
    record = {
        "worker_pid": pid,
        "worker_identity": {"pid": pid, "lstart": current["lstart"]},
    }
    with patch("goalflight_ledger.process_identity", return_value=current):
        assert goalflight_status.worker_process_identity_liveness(record) is None
        assert goalflight_ledger.worker_identity_liveness(record) == (
            "unknown",
            "identity_indeterminate",
        )


def case_legacy_lstart_mismatch_is_pid_reuse_for_status_only() -> None:
    pid = os.getpid()
    current = {"pid": pid, "lstart": "current-start", "start_token": "current-token"}
    record = {
        "dispatch_id": "legacy-reuse",
        "state": "watcher_stopped",
        "classification": "watcher_stopped",
        "worker_pid": pid,
        "worker_identity": {"pid": pid, "lstart": "prior-start"},
    }
    with patch("goalflight_ledger.process_identity", return_value=current):
        assert goalflight_ledger.worker_identity_liveness(record) == (
            "dead",
            "pid_reused_lstart",
        )
        assert goalflight_status.worker_process_identity_liveness(record) is False


def case_legacy_lstart_match_keeps_status_running_without_ownership() -> None:
    pid = os.getpid()
    current = {"pid": pid, "lstart": "same-second-start", "start_token": "current-token"}
    record = {
        "dispatch_id": "legacy-live",
        "state": "watcher_stopped",
        "classification": "watcher_stopped",
        "worker_pid": pid,
        "worker_identity": {"pid": pid, "lstart": current["lstart"]},
    }
    with patch("goalflight_ledger.process_identity", return_value=current):
        assert goalflight_status.worker_process_identity_liveness(record) is None
        assert goalflight_status.done_code(record) == 1


def case_ps_fallback_reprobe_failure_is_unknown_not_raise() -> None:
    # The combined ps probe failed; the fallback liveness re-probe then raises
    # (EPERM). The identity must come back indeterminate, never propagate.
    state = {"ps_failed": False}

    def ps_identity(_pid):
        state["ps_failed"] = True
        return None, False

    def liveness(_pid):
        if state["ps_failed"]:  # only the post-ps fallback re-probe fails
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        return True

    with patch.object(goalflight_ledger, "_ps_identity", side_effect=ps_identity), \
            patch.object(goalflight_compat, "pid_liveness", side_effect=liveness):
        ident = goalflight_ledger.process_identity(424242)
    assert state["ps_failed"], "test must reach the ps fallback"
    assert ident is not None and ident.get("identity_probe_error") is True, ident


def main() -> None:
    case_ps_fallback_reprobe_failure_is_unknown_not_raise()
    case_windows_pid_alive_does_not_call_os_kill()
    case_windows_access_denied_means_alive()
    case_windows_pid_liveness_without_windll_is_unknown()
    case_windows_pid_liveness_kernel32_load_failure_is_visible()
    case_windows_probe_failures_are_indeterminate()
    case_windows_openprocess_invalid_parameter_is_dead()
    case_windows_zombie_probe_preserves_unknown()
    case_pid_parameters_require_positive_integers()
    case_kill_pid_refuses_init_targets()
    case_posix_pid_probe_error_is_indeterminate()
    case_live_pid_probe_error_classifies_indeterminate()
    case_live_ps_probe_error_classifies_indeterminate()
    case_live_ps_missing_lstart_classifies_indeterminate()
    case_process_identity_uses_one_combined_ps_probe()
    case_probe_exceptions_are_unknown_not_dead()
    case_reaped_pid_still_classifies_dead()
    case_ledger_windows_identity_indeterminate_not_expected_live()
    case_lstart_only_identity_is_unknown()
    case_legacy_lstart_mismatch_is_pid_reuse_for_status_only()
    case_legacy_lstart_match_keeps_status_running_without_ownership()
    print("OK: pid probe tests pass")


if __name__ == "__main__":
    main()
