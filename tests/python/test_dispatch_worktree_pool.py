#!/usr/bin/env python3
"""Dispatch --worktree acquires a pooled seat; exhaustion refuses; fd is inherited."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_worktree_pool  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "goalflight-test@example.invalid")
    _git(repo, "config", "user.name", "Goal Flight Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _env(tmp: Path, *, seats: int) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp / "task-store")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_WORKTREE_SEATS"] = str(seats)
    env["GOALFLIGHT_DISABLE_NUDGES"] = "1"
    env.pop("GOALFLIGHT_STEER_FILE", None)
    return env


def _launched_payload(stdout: str) -> dict:
    for prefix in ("DISPATCH-LAUNCHED ", "DISPATCH-START "):
        for line in stdout.splitlines():
            if line.startswith(prefix):
                return json.loads(line[len(prefix) :])
    return {}


def _dispatch_cmd(tmp: Path, repo: Path, dispatch_id: str, *worker: str) -> list[str]:
    return [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(repo),
        "--worktree",
        "HEAD",
        "--launch-detached",
        "--poll-secs",
        "0.2",
        "--max-idle-secs",
        "20",
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--",
        *worker,
    ]


def test_worktree_exhaustion_refuses_honestly_and_does_not_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "held-occupant")
    try:
        proc = subprocess.run(
            _dispatch_cmd(tmp_path, repo, "need-a-seat", sys.executable, "-c", "print('nope')"),
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "all 1 worktree seats are held" in combined, combined
        assert "held-occupant" in combined, combined
        assert "wt-1" in combined, combined
        assert "refusing to git worktree add" in combined, combined
        assert not (repo / "worktrees" / "wt-2").exists()
    finally:
        holder.release()


def test_seat_survives_for_worker_lifetime_then_frees_on_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "worker-ready"
    worker = "\n".join(
        [
            "import os, signal, sys",
            "from pathlib import Path",
            "fd = int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])",
            "os.fstat(fd)",
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')",
            "signal.pause()",
        ]
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "inherit-seat",
            sys.executable,
            "-c",
            worker,
            str(marker),
        ),
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "wt-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    worker_pid = int(marker.read_text(encoding="utf-8"))
    try:
        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-while-live")
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            assert "inherit-seat" in str(exc) or "wt-1" in str(exc)
        else:
            raise AssertionError("live worker did not hold the kernel seat")
        os.kill(worker_pid, signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-worker-death"
        )
        try:
            assert replacement.path.name == "wt-1"
        finally:
            replacement.release()
    finally:
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_dispatch_quarantines_dirty_seat_instead_of_destroying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
    (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
    (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
    abandoned.release()

    marker = tmp_path / "second-ready"
    worker = (
        "from pathlib import Path; import os, time; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ready'); time.sleep(0.4); "
        "print('COMPLETE: pooled-reuse — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "reuse-dirty",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "wt-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    branches = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/goalflight/quarantine/",
    ).splitlines()
    assert len(branches) == 1, branches
    assert "wt-1" in branches[0]
    assert _git(repo, "show", f"{branches[0]}:abandoned.txt") == "preserve me"


def test_cwd_without_worktree_does_not_acquire_a_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "cwd-only"
    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH),
            "--unregistered-forced",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            "cwd-only",
            "--cwd",
            str(repo),
            "--launch-detached",
            "--tail",
            str(tmp_path / "cwd-only.tail"),
            "--status-json",
            str(tmp_path / "cwd-only.status.json"),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()
    assert not (repo / "worktrees").exists()
