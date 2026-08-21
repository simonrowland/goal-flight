#!/usr/bin/env python3
"""Hot-loop terseness: one-line launch hint, one-line argparse failures."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch terseness launch test uses POSIX isolation")

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402

# Fields observed on a real isolated launch at HEAD 9896bf6 before this change.
# DISPATCH-START / DISPATCH-LAUNCHED are a parsed contract; do not drop keys.
_START_REQUIRED = ("dispatch_id", "agent", "worker_pid", "tail", "status_json")
_LAUNCHED_REQUIRED = _START_REQUIRED + (
    "lease_id",
    "state",
    "watcher_pid",
    "watcher_log",
    "worker_identity",
)


def _run_dispatch_cli(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), *argv],
        cwd=str(ROOT),
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )


def _isolate_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_CONTROLLER_SESSION_ID"] = f"terse-{tmp.name}"
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    return env


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        return


def _json_line(stdout: str, prefix: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"missing {prefix!r} in stdout:\n{stdout}")


def test_unrecognized_flag_is_one_line() -> None:
    proc = _run_dispatch_cli("--no-such-flag")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "--no-such-flag" in err
    assert "--help" in err
    assert "--agent" in err
    assert len(err.encode()) < 250, len(err.encode())


def test_invalid_os_sandbox_is_one_line() -> None:
    proc = _run_dispatch_cli("--os-sandbox", "bogus")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "bogus" in err
    assert "read-only" in err
    assert len(err.encode()) < 300, len(err.encode())


def test_steer_missing_id_is_one_line() -> None:
    proc = _run_dispatch_cli("steer")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "dispatch_id" in err or "dispatch-id" in err
    assert "--list" in err


def test_help_still_prints_full_map() -> None:
    proc = _run_dispatch_cli("--help")
    assert proc.returncode == 0, proc.stderr
    assert "--hints" in proc.stdout
    assert "--agent" in proc.stdout
    assert len(proc.stdout.encode()) > 4000


def test_readonly_alias_is_accepted() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-ro-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli("--readonly", "--cwd", str(tmp), env=_isolate_env(tmp))
    assert proc.returncode == 64, proc.stderr
    assert "unrecognized arguments" not in proc.stderr
    assert "no worker" in proc.stderr


def test_os_sandbox_readonly_alias_is_accepted() -> None:
    assert D._parse_os_sandbox_arg("readonly") == "read-only"
    assert D._parse_os_sandbox_arg("read_only") == "read-only"
    assert D._parse_os_sandbox_arg("workspace_write") == "workspace-write"
    assert D._parse_os_sandbox_arg("off") == "off"
    with tempfile.TemporaryDirectory(prefix="gf-terse-os-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli(
            "--os-sandbox", "readonly", "--cwd", str(tmp), env=_isolate_env(tmp)
        )
    assert proc.returncode == 64, proc.stderr
    assert "invalid --os-sandbox" not in proc.stderr
    assert "unrecognized arguments" not in proc.stderr
    assert "no worker" in proc.stderr


def test_default_launch_hint_and_json_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-") as td:
        tmp = Path(td)
        env = _isolate_env(tmp)
        did = "terse-contract"
        status = tmp / f"{did}.status.json"
        tail = tmp / f"{did}.tail"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--unregistered-forced",
                "--dispatch-id",
                did,
                "--cwd",
                str(tmp),
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "8",
                "--",
                sys.executable,
                "-c",
                "import time; print('hi', flush=True); time.sleep(2)",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        start = launched = None
        try:
            assert proc.returncode == 0, proc.stderr
            start = _json_line(proc.stdout, "DISPATCH-START ")
            launched = _json_line(proc.stdout, "DISPATCH-LAUNCHED ")
            for key in _START_REQUIRED:
                assert key in start, (key, start)
            for key in _LAUNCHED_REQUIRED:
                assert key in launched, (key, launched)
            assert launched["dispatch_id"] == did
            assert launched["status_json"]
            assert launched["state"] == "running"

            reminder_lines = [
                line
                for line in proc.stderr.splitlines()
                if line.startswith("[goal-flight] dispatched ")
            ]
            assert len(reminder_lines) == 1, proc.stderr
            reminder = reminder_lines[0]
            assert did in reminder
            assert launched["status_json"] in reminder or str(status.resolve()) in reminder
            assert "do NOT hand-roll" not in proc.stderr
            assert "watch-dispatch-tail.sh" not in proc.stderr
            assert "BACKGROUND" not in proc.stderr
            assert len(reminder.encode()) < 400, reminder
        finally:
            for payload in (start, launched):
                if not payload:
                    continue
                _kill(payload.get("worker_pid"))
                _kill(payload.get("watcher_pid"))
                _kill(payload.get("caffeinate_pid"))


def test_hints_flag_restores_teaching_block() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-hints-") as td:
        tmp = Path(td)
        env = _isolate_env(tmp)
        did = "terse-hints"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--hints",
                "--agent",
                "test-dispatch",
                "--unregistered-forced",
                "--dispatch-id",
                did,
                "--cwd",
                str(tmp),
                "--tail",
                str(tmp / f"{did}.tail"),
                "--status-json",
                str(tmp / f"{did}.status.json"),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "8",
                "--",
                sys.executable,
                "-c",
                "import time; print('hi', flush=True); time.sleep(1)",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        launched = None
        try:
            assert proc.returncode == 0, proc.stderr
            launched = _json_line(proc.stdout, "DISPATCH-LAUNCHED ")
            assert "do NOT hand-roll" in proc.stderr
            assert "--wait" in proc.stderr
            assert "BACKGROUND" in proc.stderr
        finally:
            if launched:
                _kill(launched.get("worker_pid"))
                _kill(launched.get("watcher_pid"))
                _kill(launched.get("caffeinate_pid"))


def main() -> None:
    test_unrecognized_flag_is_one_line()
    test_invalid_os_sandbox_is_one_line()
    test_steer_missing_id_is_one_line()
    test_help_still_prints_full_map()
    test_readonly_alias_is_accepted()
    test_os_sandbox_readonly_alias_is_accepted()
    test_default_launch_hint_and_json_contract()
    test_hints_flag_restores_teaching_block()
    print("OK: dispatch CLI terseness tests pass")


if __name__ == "__main__":
    main()
