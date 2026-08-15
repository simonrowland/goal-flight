"""Keep every Python test module isolated, visible, and enforced under pytest."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[1]
ISOLATED_TEST_FILE_ENV = "GOALFLIGHT_ISOLATED_TEST_FILE"


def _tail(text: str, limit: int = 4_000) -> str:
    return text if len(text) <= limit else text[-limit:]


def _interpreter_for(path: Path, *, direct_script: bool) -> Path:
    needs_acp_sdk = path.name.startswith("test_acp_") or path.name == "test_os_sandbox.py"
    if direct_script and needs_acp_sdk and os.name != "nt":
        configured = os.environ.get("GOALFLIGHT_ACP_PYTHON")
        return (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".goal-flight/venvs/acp-0.10/bin/python"
        )
    return Path(sys.executable)


def test_isolated_test_module(
    isolated_test_module: tuple[Path, bool],
    tmp_path: Path,
) -> None:
    test_path, direct_script = isolated_test_module
    interpreter = _interpreter_for(test_path, direct_script=direct_script)
    assert interpreter.is_file() and os.access(interpreter, os.X_OK), (
        f"SDK missing -- run install: {interpreter}"
    )

    env = os.environ.copy()
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE", None)
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp_path / "task-store")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    # Journal/state isolation: without these, a module that resolves default
    # paths write-opens LIVE journals — and a schema-carrying tree migrated
    # two of them mid-development (b-150). Second-level spawns that build
    # their own env are covered by the migration allow-guard, not this.
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp_path / "journals")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp_path / "messages")
    test_id = test_path.relative_to(TEST_DIR).as_posix()
    env[ISOLATED_TEST_FILE_ENV] = test_id
    command = (
        [str(interpreter), str(test_path)]
        if direct_script
        else [str(interpreter), "-m", "pytest", str(test_path), "-q", "-rs"]
    )
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    skipped_reason = (
        next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.startswith("SKIPPED ")
            ),
            None,
        )
        if not direct_script
        else None
    )
    if result.returncode == 0 and skipped_reason:
        pytest.skip(f"{test_id}: {skipped_reason}")
    assert result.returncode == 0, (
        f"{test_id} exited {result.returncode}\n"
        f"stdout:\n{_tail(result.stdout)}\n"
        f"stderr:\n{_tail(result.stderr)}"
    )


def test_directory_collection_is_visible_and_clean() -> None:
    """The obvious directory-level command must never fail without output."""
    # This early-import sentinel reaches goalflight_acp_run before another test
    # can populate a fake ACP module and mask import-time interpreter changes.
    sentinel_env = os.environ.copy()
    sentinel_path = TEST_DIR / "test_dispatch_ergonomics.py"
    sentinel_env[ISOLATED_TEST_FILE_ENV] = sentinel_path.relative_to(TEST_DIR).as_posix()
    sentinel = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sentinel_path),
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=sentinel_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sentinel.returncode == 0, (
        f"sentinel collection exited {sentinel.returncode}\n"
        f"stdout:\n{_tail(sentinel.stdout)}\n"
        f"stderr:\n{_tail(sentinel.stderr)}"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST_DIR), "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"directory collection exited {result.returncode}\n"
        f"stdout:\n{_tail(result.stdout)}\n"
        f"stderr:\n{_tail(result.stderr)}"
    )
    assert combined.strip(), "directory collection exited without any diagnostic output"

    execution = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(TEST_DIR),
            "-q",
            "-k",
            "test_isolated_test_module and test_dispatch_ergonomics",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    execution_output = execution.stdout + execution.stderr
    assert execution.returncode == 0, (
        f"directory execution exited {execution.returncode}\n"
        f"stdout:\n{_tail(execution.stdout)}\n"
        f"stderr:\n{_tail(execution.stderr)}"
    )
    assert execution_output.strip(), "directory execution exited without any diagnostic output"
