#!/usr/bin/env python3
"""t-349: a nonexistent --cwd is refused at argument-parse time.

Receipts t337-w3/w4/w5: a mistyped --cwd flowed through resolve_project_root,
whose not-a-checkout fallback rendered the missing path as its own project
root; the dispatch then refused with "controller is not registered" and
recommended --unregistered-forced -- advice that would launch an unowned
dispatch into a phantom project root for what was a path typo. The refusal
must fire at parse time, before any registry lookup, so that advice is never
reached; and the resolver's fallback must say out loud which root it chose.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_task  # noqa: E402


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_DISPATCH_SCRIPT",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_PROCESS_ROLE",
    ):
        env.pop(key, None)
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "state" / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def case_nonexistent_cwd_refused_at_parse_time() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        missing = tmp / "typo-worktree"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
                "--cwd",
                str(missing),
                "--agent",
                "test",
                "--dispatch-id",
                "cwd-missing",
                "--",
                sys.executable,
                "-c",
                "print('never launched')",
            ],
            env=_env(tmp),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
        assert f"cwd does not exist: {missing}" in proc.stderr, proc.stderr
        # The refusal must fire before any registry lookup, so the misleading
        # not-registered advice for a path typo is never emitted.
        assert "controller is not registered" not in proc.stderr, proc.stderr
        assert proc.returncode == 2, (proc.returncode, proc.stderr)


def case_file_cwd_refused_at_parse_time() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        not_a_dir = tmp / "file.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
                "--cwd",
                str(not_a_dir),
                "--agent",
                "test",
                "--dispatch-id",
                "cwd-file",
                "--",
                sys.executable,
                "-c",
                "print('never launched')",
            ],
            env=_env(tmp),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        assert f"cwd is not a directory: {not_a_dir}" in proc.stderr, proc.stderr


def case_resolver_fallback_warns_and_names_the_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        plain = Path(td).resolve() / "plain"
        plain.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resolved = goalflight_task.resolve_project_root(str(plain))
        assert resolved == plain, resolved  # return value unchanged
        warning = err.getvalue()
        assert "WARN" in warning, warning
        assert str(plain) in warning, warning
        assert "treating the path itself as the project root" in warning, warning

    missing = Path(tempfile.gettempdir()) / "gf-t349-no-such-dir"
    assert not missing.exists()
    expected = missing.resolve()  # the fallback keeps _strip_managed_worktree's loose resolution
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        resolved = goalflight_task.resolve_project_root(str(missing))
    assert resolved == expected, (resolved, expected)  # return value unchanged
    warning = err.getvalue()
    assert "WARN" in warning, warning
    assert str(missing) in warning, warning
    assert "treating the path itself as the project root" in warning, warning

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        canonical = goalflight_task.resolve_project_root(str(ROOT))
    assert canonical.is_dir(), canonical  # a real checkout canonicalizes
    assert err.getvalue() == "", err.getvalue()


if __name__ == "__main__":
    case_nonexistent_cwd_refused_at_parse_time()
    case_file_cwd_refused_at_parse_time()
    case_resolver_fallback_warns_and_names_the_fallback()
    print("PASS: test_dispatch_cwd_guard")
