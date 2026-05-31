#!/usr/bin/env python3
"""Real native-Windows acceptance tests.

These tests intentionally skip on POSIX hosts. On native Windows they exercise
the actual Windows branches instead of patching ``os.name`` / compat probes.
"""

from __future__ import annotations

from support import note_skip, skip_unless_native_windows

skip_unless_native_windows("Windows-activated tests run only on native Windows")

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_compat  # noqa: E402


def _isolated_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
    env["GOALFLIGHT_PYTHON"] = sys.executable
    return env


def _run_script(script: str, args: list[str], *, env: dict[str, str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _assert_no_crash(name: str, proc: subprocess.CompletedProcess[str], allowed_rc: set[int]) -> None:
    combined = proc.stdout + proc.stderr
    assert proc.returncode in allowed_rc, f"{name} rc={proc.returncode}\n{combined[:2000]}"
    assert "Traceback (most recent call last)" not in combined, combined[:2000]


def case_control_plane_imports_and_runs() -> None:
    import goalflight_capacity  # noqa: F401
    import goalflight_doctor  # noqa: F401
    import goalflight_session_status  # noqa: F401
    import goalflight_status  # noqa: F401

    with tempfile.TemporaryDirectory(prefix="goalflight-win-control-") as td:
        tmp = Path(td)
        env = _isolated_env(tmp)
        commands = [
            ("status", "goalflight_status.py", ["--json", "--limit", "1"], {0}),
            ("capacity", "goalflight_capacity.py", ["profile", "--json"], {0}),
            ("session_status", "goalflight_session_status.py", ["--project-root", str(ROOT), "--json"], {0}),
            ("doctor", "goalflight_doctor.py", ["--project-root", str(ROOT), "--json"], {0, 1}),
        ]
        for name, script, args, allowed in commands:
            proc = _run_script(script, args, env=env)
            _assert_no_crash(name, proc, allowed)
            assert proc.stdout.strip().startswith("{"), f"{name} did not emit JSON"
            json.loads(proc.stdout)


def case_real_wsl_probe() -> None:
    if not (shutil.which("wsl.exe") or shutil.which("wsl")):
        note_skip("case_real_wsl_probe", "wsl.exe absent on this native Windows host")
        return
    payload = goalflight_compat.probe_wsl(ROOT)
    assert payload["is_windows"] is True
    assert payload["wsl_exe_present"] is True
    assert payload["state"] in {
        "ready",
        "no_installed_distributions",
        "distro_launch_failed",
        "probe_failed",
    }
    if payload["state"] == "ready":
        assert payload["usable"] is True
        assert payload["present"] is True
        assert payload["distributions"], payload
    if payload["state"] == "no_installed_distributions":
        assert payload["usable"] is False
        assert payload["present"] is False
        assert payload["distributions"] == []
    if payload["state"] == "distro_launch_failed":
        assert payload["usable"] is False
        assert payload["present"] is False
        assert payload["distributions"], payload


def _sleeping_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _still_running(proc: subprocess.Popen[str]) -> bool:
    return proc.poll() is None


def _cleanup(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def case_real_windows_kill_pid_identity_safe() -> None:
    proc = _sleeping_child()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not goalflight_compat.pid_alive(proc.pid):
            time.sleep(0.05)
        assert goalflight_compat.pid_alive(proc.pid) is True

        assert goalflight_compat.kill_pid(proc.pid, signal.SIGTERM) is False
        assert _still_running(proc), "bare pid kill must not terminate on native Windows"

        assert goalflight_compat.kill_pid(
            proc.pid,
            signal.SIGTERM,
            expected_identity={"pid": proc.pid, "creation_time": "reused-pid-old-token"},
        ) is False
        assert _still_running(proc), "mismatched identity kill must not terminate"

        identity = goalflight_compat.windows_process_identity(proc.pid)
        assert identity, "own child should have a queryable Windows process identity"
        assert goalflight_compat.kill_pid(proc.pid, signal.SIGTERM, expected_identity=identity) is True
        proc.wait(timeout=10)
    finally:
        _cleanup(proc)


def case_dispatch_refuses_before_side_effects() -> None:
    with tempfile.TemporaryDirectory(prefix="goalflight-win-dispatch-") as td:
        tmp = Path(td)
        env = _isolated_env(tmp)
        launched = tmp / "worker-launched.txt"

        bash_status = tmp / "bash.status.json"
        bash_tail = tmp / "bash.tail"
        bash_proc = _run_script(
            "goalflight_dispatch.py",
            [
                "--shape",
                "bash",
                "--agent",
                "custom",
                "--cwd",
                str(tmp),
                "--dispatch-id",
                "win-real-bash-refuse",
                "--tail",
                str(bash_tail),
                "--status-json",
                str(bash_status),
                "--",
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(launched)!r}).write_text('bad')",
            ],
            env=env,
        )
        _assert_no_crash("dispatch bash refuse", bash_proc, {2})
        bash_payload = json.loads(bash_status.read_text(encoding="utf-8"))
        assert bash_payload["state"] == "blocked_windows_dispatch"
        assert bash_payload["worker_pid"] is None
        assert not bash_tail.exists()
        assert not launched.exists()

        acp_status = tmp / "acp.status.json"
        acp_proc = _run_script(
            "goalflight_dispatch.py",
            [
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--cwd",
                str(tmp),
                "--dispatch-id",
                "win-real-acp-refuse",
                "--status-json",
                str(acp_status),
                "--prompt",
                "never launch",
            ],
            env=env,
        )
        _assert_no_crash("dispatch acp refuse", acp_proc, {2})
        acp_payload = json.loads(acp_status.read_text(encoding="utf-8"))
        assert acp_payload["state"] == "blocked_windows_dispatch"
        assert acp_payload["lease_id"] is None
        assert acp_payload["worker_pid"] is None
        assert acp_payload["worker_alive"] is False

        assert not (tmp / "state" / "capacity.json").exists()
        assert not (tmp / "state" / "runs.d").exists()
        assert not (tmp / "worktrees").exists()


def case_init_windows_detection_offer_branch() -> None:
    with tempfile.TemporaryDirectory(prefix="goalflight-win-init-") as td:
        project = Path(td)
        payload = goalflight_compat.probe_wsl(project)
        assert payload["is_windows"] is True
        assert payload["install_command"] == "wsl --install"
        assert payload["requires_admin"] is True
        assert payload["requires_reboot"] is True
        if not payload["usable"]:
            assert payload["declined"] is False
            assert payload["state"] in {
                "missing_executable",
                "no_installed_distributions",
                "distro_launch_failed",
                "probe_failed",
            }
            goalflight_compat.record_wsl_install_declined(project)
            declined = goalflight_compat.probe_wsl(project)
            assert declined["declined"] is True
            assert declined["decline_stamp"].endswith("docs-private/windows-wsl-install-declined.json")


def case_compat_windows_paths_and_flock() -> None:
    assert goalflight_compat.is_windows() is True
    assert goalflight_compat.is_wsl() is False
    assert goalflight_compat.temp_base() == Path(tempfile.gettempdir())
    assert goalflight_compat.default_state_dir().parent == Path(tempfile.gettempdir())
    assert os.environ.get("USERNAME", "user") in goalflight_compat.default_state_dir().name
    assert goalflight_compat.pid_alive(os.getpid()) is True
    assert goalflight_compat.pid_alive(0) is False
    assert goalflight_compat.pid_alive(-1) is False

    with tempfile.TemporaryDirectory(prefix="goalflight-win-flock-") as td:
        path = Path(td) / "lock"
        path.write_text("", encoding="utf-8")
        with path.open("r+", encoding="utf-8") as first, path.open("r+", encoding="utf-8") as second:
            goalflight_compat.flock(first, goalflight_compat.LOCK_EX | goalflight_compat.LOCK_NB)
            try:
                contended = False
                try:
                    goalflight_compat.flock(second, goalflight_compat.LOCK_EX | goalflight_compat.LOCK_NB)
                except BlockingIOError:
                    contended = True
                assert contended is True
            finally:
                goalflight_compat.flock(first, goalflight_compat.LOCK_UN)


def main() -> None:
    case_control_plane_imports_and_runs()
    case_real_wsl_probe()
    case_real_windows_kill_pid_identity_safe()
    case_dispatch_refuses_before_side_effects()
    case_init_windows_detection_offer_branch()
    case_compat_windows_paths_and_flock()
    print("OK: native Windows activated suite pass")


if __name__ == "__main__":
    main()
