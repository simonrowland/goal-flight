#!/usr/bin/env python3
"""Dispatch --worktree acquires a pooled seat; exhaustion refuses; fd is inherited."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402
import goalflight_capacity  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_worktree_gc  # noqa: E402
import goalflight_worktree_pool  # noqa: E402
from test_worktree_seat_pool import (
    _AdvancingDeadlineClock,
    _rewrite_seat_holder_as_dead,
    finish_seat_holder,
    record_finished_holder,
)


@pytest.fixture(autouse=True)
def pool_ledger_matches_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))


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


def _mark_test_seat_lock_identity_dead(lock_file) -> None:
    metadata = goalflight_worktree_pool._lock_metadata(lock_file)
    if not isinstance(metadata.get("dispatch_id"), str):
        return
    metadata.update(pid=2147483647, start_token="exited-test-worker")
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(metadata, lock_file, sort_keys=True)
    lock_file.write("\n")
    lock_file.flush()


def _rewrite_test_seat_lock_identity_dead(repo: Path, seat_name: str) -> None:
    """Stamp a synthetic dead owner without releasing the fixture's flock."""
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat_name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        _mark_test_seat_lock_identity_dead(lock_file)


def _write_terminal_seat_record(
    dispatch_id: str,
    repo: Path,
    seat: Path,
    *,
    parent_dispatch_id: str | None = None,
) -> None:
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "state": "complete",
        "terminal_state": "complete",
        "worker_pid": 2147483647,
        "worker_identity": {
            "pid": 2147483647,
            "start_token": "exited-test-worker",
        },
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_head": _git(seat, "rev-parse", "HEAD"),
    }
    if parent_dispatch_id:
        record["parent_dispatch_id"] = parent_dispatch_id
    goalflight_ledger.write_record(record)
    _rewrite_test_seat_lock_identity_dead(
        repo, seat.name
    )


def _write_seat_holder_record(
    dispatch_id: str,
    repo: Path,
    lease,
    *,
    state: str,
    live: bool = False,
    ended_at: str | None = None,
    **extra,
) -> None:
    pid = os.getpid() if live else 2147483647
    identity = (
        goalflight_ledger.process_identity(pid)
        if live
        else {"pid": pid, "start_token": "exited-test-worker"}
    )
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "state": state,
        "terminal_state": goalflight_ledger.terminal_state_for(state),
        "worker_pid": pid,
        "worker_identity": identity,
        "project_root": str(repo),
        "worker_cwd": str(lease.path),
        "worktree_id": lease.path.name,
        "worktree_path": str(lease.path),
        "worktree_branch": _git(lease.path, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_head": _git(lease.path, "rev-parse", "HEAD"),
        **extra,
    }
    if ended_at:
        record["ended_at"] = ended_at
    goalflight_ledger.write_record(record)
    if not live:
        if lease._lock_file is not None:
            _mark_test_seat_lock_identity_dead(lease._lock_file)
        else:
            _rewrite_test_seat_lock_identity_dead(repo, lease.path.name)


def _write_terminal_unknown_status(
    dispatch_id: str,
    repo: Path,
    seat: Path,
    *,
    lock_holder: str | None = None,
) -> None:
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    status = {
        "schema": "goalflight.status.v1",
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "worker_pid": os.getpid(),
        "worker_identity": identity,
        "expected_worker_identity": identity,
        "worker_alive": False,
        "watcher_pid": os.getpid(),
        "detached": True,
        "pgid": os.getpgrp(),
        "worker_identity_reason": "identity probe unavailable",
        "pgroup_cpu_pct": 0.0,
        "tail_path": str(seat / "tail"),
        "terminal_marker": "!COMPLETE",
        "state": "complete",
        "updated_at": int(time.time()),
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_head": _git(seat, "rev-parse", "HEAD"),
    }
    if lock_holder is not None:
        status["lock_holder"] = lock_holder
    status_dir = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"])
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / f"{dispatch_id}.status.json").write_text(
        json.dumps(status, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def isolated_pool_state(tmp_path, monkeypatch):
    paths = {
        "GOALFLIGHT_STATE_DIR": "state",
        "GOALFLIGHT_DISPATCH_DIR": "dispatch",
        "GOALFLIGHT_JOURNAL_DIR": "journal",
        "GOALFLIGHT_PIDFILE_DIR": "pidfiles",
        "GOAL_FLIGHT_PIDFILE_DIR": "pidfiles",
        "GOALFLIGHT_TASK_STORE": "task-store",
        "GOALFLIGHT_TASK_STORE_DIR": "task-store",
        "GOALFLIGHT_MESSAGES_DIR": "messages",
        "GOALFLIGHT_WAKE_LEDGER": "wake-ledger",
        "GOALFLIGHT_WAKE_LEDGER_DIR": "wake-ledger",
    }
    for variable, directory in paths.items():
        monkeypatch.setenv(variable, str(tmp_path / directory))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    return tmp_path


def _make_journal_only_seat(
    tmp_path: Path,
    dispatch_id: str,
    *,
    journal_state: str,
    lock_pid: int | None,
    lock_start_token: object,
    dirty: bool = False,
    terminal_state: str = "complete",
) -> tuple[Path, Path, dict]:
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    if dirty:
        (seat / "journal-holder-untracked.txt").write_text(
            "preserve through quarantine\n", encoding="utf-8"
        )
    lease.release()

    lock_path = goalflight_worktree_pool._seat_lock_root(repo) / f"{seat.name}.lock"
    lock_metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_metadata.update(
        {
            "pid": lock_pid,
            "start_token": lock_start_token,
            "controller_label": "test-controller",
        }
    )
    lock_path.write_text(
        json.dumps(lock_metadata, sort_keys=True) + "\n", encoding="utf-8"
    )

    if journal_state == "unreadable":
        journal_path = goalflight_journal.resolve_journal_path(repo)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(b"not a sqlite journal\n")
    else:
        authority = goalflight_journal.open_or_create_journal(repo)
        attempt = authority.prepare_attempt(dispatch_id).value
        if journal_state in {
            goalflight_journal.ATTEMPT_STARTING,
            goalflight_journal.ATTEMPT_RUNNING,
        }:
            starting = authority.start_attempt(
                attempt.attempt_id, attempt.launch_token
            ).value
            if journal_state == goalflight_journal.ATTEMPT_RUNNING:
                assert authority.mark_attempt_running(
                    starting.attempt_id,
                    starting.launch_token,
                    launch_epoch=starting.launch_epoch,
                    worker_instance={
                        "pid": lock_pid,
                        "start_token": lock_start_token,
                    },
                ).committed
        elif journal_state == goalflight_journal.ATTEMPT_TERMINAL:
            assert authority.commit_terminal(
                attempt.attempt_id, terminal_state=terminal_state
            ).committed
        else:
            assert journal_state == goalflight_journal.ATTEMPT_PREPARED

    assert goalflight_ledger.read_record(dispatch_id) is None
    status_path = tmp_path / "dispatch" / f"{dispatch_id}.status.json"
    assert not status_path.exists()
    return repo, seat, lock_metadata


def test_journal_terminal_unknown_identity_retains_without_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "journal-terminal-dead"
    repo, seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )

    journal_path = goalflight_journal.resolve_journal_path(repo)
    files_before_probe = {path.name for path in journal_path.parent.iterdir()}
    description = goalflight_worktree_pool._holder_description(
        seat.name, metadata, project_root=repo
    )
    assert {path.name for path in journal_path.parent.iterdir()} == files_before_probe
    assert "journal=TERMINAL" in description
    assert "lock_pid=absent_or_reused" in description
    census_calls: list[Path] = []

    def fail_census(*, timeout_s: float):
        census_calls.append(seat)
        raise AssertionError("unknown journal identity must retain without census")

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", fail_census)
    assert not goalflight_worktree_pool._holder_is_terminal(
        dispatch_id, project_root=repo, lock_metadata=metadata
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
    assert any(
        name == seat.name and reason == "identity_unknown"
        for name, reason in caught.value.skipped_slots
    )
    assert census_calls == []
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{dispatch_id}"


@pytest.mark.parametrize("lock_holder", ("native", "wrapper", None))
def test_terminal_unknown_identity_is_not_terminal_for_any_lock_holder(
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
) -> None:
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_holder_record",
        lambda *_args, **_kwargs: (
            {"state": "complete", "lock_holder": lock_holder},
            None,
        ),
    )

    assert not goalflight_worktree_pool._holder_is_terminal("terminal-unknown")


def test_terminal_prelaunch_proof_normalizes_absent_worker_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "state": "failed",
        "prelaunch_failure": True,
        "spawn_state": "none",
    }
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_holder_record",
        lambda *_args, **_kwargs: (record, None),
    )

    assert not goalflight_worktree_pool._holder_is_terminal("raw-unknown-prelaunch")
    assert goalflight_worktree_pool._holder_effective_liveness(record, None) is False
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_holder_record",
        lambda *_args, **_kwargs: (
            record,
            goalflight_worktree_pool._holder_effective_liveness(record, None),
        ),
    )
    assert goalflight_worktree_pool._holder_is_terminal("proven-no-launch")


@pytest.mark.parametrize("holder_source", ("ledger", "journal"))
def test_live_lock_metadata_retains_when_branch_owner_is_dead(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    holder_source: str,
) -> None:
    repo = _make_repo(tmp_path)
    branch_owner_id = "dead-branch-owner"
    live_holder_id = f"live-lock-holder-{holder_source}"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, branch_owner_id)
    seat = lease.path
    lease.release()
    _write_terminal_seat_record(branch_owner_id, repo, seat)
    marker = seat / "preserve-live-lock-holder.txt"
    marker.write_text("live holder owns this checkout\n", encoding="utf-8")

    if holder_source == "ledger":
        _write_seat_holder_record(
            live_holder_id,
            repo,
            lease,
            state="running",
            live=True,
            worker_cwd=str(repo),
        )
    else:
        journal_path = goalflight_journal.resolve_journal_path(repo)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(b"unreadable journal\n")

    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        goalflight_worktree_pool._write_occupant(
            lock_file, seat_name=seat.name, dispatch_id=live_holder_id
        )
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [],
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "live-lock-reclaimer")

    assert marker.read_text(encoding="utf-8") == "live holder owns this checkout\n"
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{branch_owner_id}"
    )
    assert _lock_dispatch_id(lock_path) == live_holder_id


def test_native_dead_holder_requires_clear_cwd_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    holder_id = "native-dead-census-holder"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = lease.path
    _write_seat_holder_record(
        holder_id,
        repo,
        lease,
        state="complete",
        lock_holder="native",
    )
    marker = seat / "preserve-native-census-holder.txt"
    marker.write_text("seat cwd remains occupied\n", encoding="utf-8")
    lease.release()
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [(os.getpid(), str(seat))],
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "native-census-reclaimer")

    assert marker.read_text(encoding="utf-8") == "seat cwd remains occupied\n"
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{holder_id}"


def test_running_prelaunch_row_does_not_bypass_holder_validation(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    branch_owner_id = "prelaunch-branch-owner"
    child_id = "running-prelaunch-child"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, branch_owner_id)
    seat = lease.path
    lease.release()
    _write_terminal_seat_record(branch_owner_id, repo, seat)
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "project_root": str(repo),
            "parent_dispatch_id": branch_owner_id,
            "state": "running",
            "terminal_state": "unknown",
            "prelaunch_failure": True,
            "worker_pid": None,
            "worker_identity": None,
            "worker_cwd": None,
            "worktree_path": str(seat),
        }
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [],
    )
    assert goalflight_worktree_pool._attribute_prelaunch_seat_holder(
        seat, child_id, project_root=repo
    ) == child_id
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        goalflight_worktree_pool._write_occupant(
            lock_file, seat_name=seat.name, dispatch_id=child_id
        )
        _mark_test_seat_lock_identity_dead(lock_file)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    marker = seat / "preserve-running-prelaunch-row.txt"
    marker.write_text("do not reset an active row\n", encoding="utf-8")

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "prelaunch-reclaimer")

    assert marker.read_text(encoding="utf-8") == "do not reset an active row\n"
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{branch_owner_id}"
    )


def test_journal_terminal_live_lock_identity_keeps_seat_held(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    dispatch_id = "journal-terminal-live"
    repo, seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=identity["start_token"],
    )

    description = goalflight_worktree_pool._holder_description(
        seat.name, metadata, project_root=repo
    )
    assert "journal=TERMINAL" in description
    assert "lock_pid=live" in description
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


@pytest.mark.parametrize(
    ("lock_pid", "lock_start_token"),
    ((None, "missing-pid"), (os.getpid(), None), (os.getpid(), 7)),
)
def test_journal_terminal_unknown_lock_identity_keeps_seat_held(
    tmp_path: Path,
    isolated_pool_state: Path,
    lock_pid: int | None,
    lock_start_token: object,
) -> None:
    dispatch_id = "journal-unknown-identity"
    repo, _seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=lock_pid,
        lock_start_token=lock_start_token,
    )

    assert "lock_pid=unknown" in goalflight_worktree_pool._holder_description(
        "s-1", metadata, project_root=repo
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


def test_journal_terminal_reused_pid_with_different_start_token_retains_seat(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-reused-pid"
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    stale_token = f"{identity['start_token']}:older-generation"
    assert goalflight_worktree_pool.goalflight_compat.process_identity_matches(
        os.getpid(), stale_token
    ) is False
    repo, seat, _metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=stale_token,
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
    assert any(
        name == seat.name and reason == "identity_unknown"
        for name, reason in caught.value.skipped_slots
    )
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{dispatch_id}"


def test_journal_terminal_unknown_identity_probe_keeps_seat_held(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    repo, _seat, metadata = _make_journal_only_seat(
        tmp_path,
        "journal-identity-probe-unknown",
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=identity["start_token"],
    )
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: None,
    )

    assert "lock_pid=unknown" in goalflight_worktree_pool._holder_description(
        "s-1", metadata, project_root=repo
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


def test_journal_terminal_unknown_identity_without_lifetime_holder_is_retained(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    dispatch_id = "journal-terminal-unknown-lock-lifetime"
    repo, seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=identity["start_token"],
    )
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: None,
    )

    record, live = goalflight_worktree_pool._holder_record(
        dispatch_id,
        project_root=repo,
        lock_metadata=metadata,
    )
    assert record["_holder_source"] == "journal"
    assert record["journal_state"] == goalflight_journal.ATTEMPT_TERMINAL
    assert record["_lock_identity_live"] is None
    assert live is None
    assert goalflight_ledger.read_record(dispatch_id) is None
    assert not (tmp_path / "dispatch" / f"{dispatch_id}.status.json").exists()

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


@pytest.mark.parametrize("lock_holder", ("native", "wrapper", None))
def test_unknown_status_lock_holder_is_retained_without_cwd_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"status-holder-{lock_holder}"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = holder.path
    holder.release()

    _write_terminal_unknown_status(
        dispatch_id, repo, seat, lock_holder=lock_holder
    )
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: None,
    )
    census_calls = []

    def empty_census(**_kwargs):
        census_calls.append(True)
        return []

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", empty_census)

    record, live = goalflight_worktree_pool._holder_record(dispatch_id)
    assert record.get("lock_holder") == lock_holder
    assert live is None
    assert goalflight_ledger.read_record(dispatch_id) is None
    assert not goalflight_worktree_pool._holder_is_terminal(dispatch_id)

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"status-{lock_holder or 'missing'}-reclaimer"
        )
    assert census_calls == []
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


@pytest.mark.parametrize("engine_in_seat", (True, False))
def test_proven_dead_wrapper_worker_requires_cwd_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine_in_seat: bool,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"dead-wrapper-engine-{engine_in_seat}"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = holder.path
    holder.release()
    _write_terminal_unknown_status(dispatch_id, repo, seat, lock_holder="wrapper")
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    census_calls = []

    def census(**_kwargs):
        census_calls.append(True)
        return [(424242, str(seat))] if engine_in_seat else []

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", census)
    if engine_in_seat:
        monkeypatch.setattr(
            goalflight_worktree_pool.os,
            "getpgid",
            lambda _pid: os.getpgrp(),
        )
    record, live = goalflight_worktree_pool._holder_record(dispatch_id)
    assert record["lock_holder"] == "wrapper"
    assert live is False

    if engine_in_seat:
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo, f"dead-wrapper-reclaimer-{engine_in_seat}"
            )
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{dispatch_id}"
        )
    else:
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"dead-wrapper-reclaimer-{engine_in_seat}"
        )
        try:
            assert replacement.path == seat
            assert replacement.reclaimed_dispatch_id == dispatch_id
        finally:
            replacement.release()
    assert census_calls == [True]


def test_proven_dead_native_holder_with_occupied_cwd_is_retained(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = "dead-native-occupied-cwd"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = holder.path
    holder.release()
    _write_terminal_unknown_status(dispatch_id, repo, seat, lock_holder="native")
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    census_calls = []

    def occupied_census(**_kwargs):
        census_calls.append(True)
        return [(os.getpid(), str(seat))]

    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", occupied_census
    )

    record, live = goalflight_worktree_pool._holder_record(dispatch_id)
    assert record.get("lock_holder") == "native"
    assert live is False
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "native-cwd-reclaimer")
    assert census_calls == [True]
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


def test_live_lock_identity_retains_dead_branch_owner_after_clear_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    branch_owner = "dead-branch-owner"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, branch_owner)
    seat = holder.path
    holder.release()
    _write_seat_holder_record(
        branch_owner,
        repo,
        holder,
        state="complete",
        lock_holder="native",
    )

    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    lock_path = goalflight_worktree_pool._seat_lock_root(repo) / f"{seat.name}.lock"
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    metadata.update(
        {
            "dispatch_id": "live-lock-holder",
            "pid": os.getpid(),
            "start_token": identity["start_token"],
        }
    )
    lock_path.write_text(
        json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", lambda **_kwargs: []
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "case-f-reclaimer")
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{branch_owner}"
    )


@pytest.mark.parametrize("lock_holder", (None, "wrapper"))
@pytest.mark.parametrize("cwd_kind", ("seat", "subdirectory"))
def test_legacy_unknown_holder_with_live_seat_cwd_is_retained_without_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
    cwd_kind: str,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"legacy-{lock_holder or 'missing'}-{cwd_kind}"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = holder.path
    holder.release()
    _write_terminal_unknown_status(dispatch_id, repo, seat, lock_holder=lock_holder)
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: None,
    )

    if cwd_kind == "seat":
        process_cwd = seat
    elif cwd_kind == "subdirectory":
        process_cwd = seat / "nested"
        process_cwd.mkdir()
    census_calls = []

    def live_census(**_kwargs):
        census_calls.append(True)
        return [(os.getpid(), str(process_cwd))]

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        live_census,
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"legacy-{lock_holder or 'missing'}-reclaimer"
        )
    assert census_calls == []
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


@pytest.mark.parametrize("failure", ("timeout", "error", "partial"))
def test_dead_legacy_holder_is_retained_when_cwd_census_is_unknown(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"legacy-census-{failure}"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = holder.path
    holder.release()
    _write_terminal_unknown_status(dispatch_id, repo, seat, lock_holder="wrapper")
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    census_calls = []
    lsof_commands = []

    def lsof(**kwargs):
        census_calls.append(True)
        lsof_commands.append(kwargs["args"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(kwargs["args"], kwargs["timeout"])
        if failure == "error":
            raise OSError("lsof unavailable")
        return subprocess.CompletedProcess(
            kwargs["args"],
            0,
            stdout=f"p{os.getpid()}\nfcwd\nn{seat}",
            stderr="",
        )

    original_run = subprocess.run

    def run_with_lsof(args, **kwargs):
        if args and args[0] == "lsof":
            return lsof(args=args, **kwargs)
        return original_run(args, **kwargs)

    monkeypatch.setattr(
        goalflight_worktree_pool.subprocess,
        "run",
        run_with_lsof,
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
        goalflight_worktree_pool.acquire_worktree_seat(
            repo, f"legacy-census-{failure}-reclaimer"
        )
    assert census_calls == [True]
    assert lsof_commands == [["lsof", "-nP", "-d", "cwd", "-Fpfn"]]
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


def test_cwd_census_is_fresh_for_each_locked_proven_dead_candidate(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "legacy-first")
    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "legacy-second")
    first_seat = first.path
    second_seat = second.path
    assert first_seat != second_seat
    first.release()
    second.release()
    _write_terminal_unknown_status("legacy-first", repo, first_seat)
    _write_terminal_unknown_status("legacy-second", repo, second_seat)
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    census_calls = []
    current_candidate = None
    original_validate = goalflight_worktree_pool._validate_holder

    def trace_validate(worktree_path, *args, **kwargs):
        nonlocal current_candidate
        if kwargs.get("process_cwd_census") is not None:
            current_candidate = worktree_path
        return original_validate(worktree_path, *args, **kwargs)

    def changing_census(**_kwargs):
        census_calls.append(current_candidate)
        if len(census_calls) == 1:
            return []
        return [(os.getpid(), str(current_candidate))]

    def refuse_first_reset(worktree_path, _prior_dispatch_id):
        if not census_calls or len(census_calls) == 1:
            raise goalflight_worktree_pool.WorktreeSeatResetRefused(
                "advance to the next candidate"
            )
        return None

    monkeypatch.setattr(goalflight_worktree_pool, "_validate_holder", trace_validate)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        changing_census,
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatResetRefused):
        goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "legacy-sweep-reclaimer",
            before_reset=refuse_first_reset,
        )
    assert census_calls == [first_seat, second_seat]
    assert _git(first_seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        "worktree/legacy-first"
    )
    assert _git(second_seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        "worktree/legacy-second"
    )


def test_journal_prepared_holder_stays_held_with_state_description(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-prepared"
    repo, seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_PREPARED,
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )

    description = goalflight_worktree_pool._holder_description(
        seat.name, metadata, project_root=repo
    )
    assert "state=PREPARED" in description
    assert "journal=PREPARED" in description
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


@pytest.mark.parametrize(
    "journal_state",
    (goalflight_journal.ATTEMPT_STARTING, goalflight_journal.ATTEMPT_RUNNING),
)
def test_journal_starting_or_running_holder_stays_held_even_with_dead_pid(
    tmp_path: Path, isolated_pool_state: Path, journal_state: str
) -> None:
    dispatch_id = f"journal-{journal_state.lower()}"
    repo, _seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=journal_state,
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )

    assert f"journal={journal_state}" in goalflight_worktree_pool._holder_description(
        "s-1", metadata, project_root=repo
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


def test_journal_worker_dead_terminal_with_live_pid_stays_held(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-worker-dead-live"
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    repo, _seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=identity["start_token"],
        terminal_state="worker_dead",
    )

    assert "journal=TERMINAL" in goalflight_worktree_pool._holder_description(
        "s-1", metadata, project_root=repo
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


def test_unreadable_journal_holder_stays_held_with_description(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-unreadable"
    repo, seat, metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state="unreadable",
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )

    description = goalflight_worktree_pool._holder_description(
        seat.name, metadata, project_root=repo
    )
    assert "journal=unreadable" in description
    assert "lock_pid=absent_or_reused" in description
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")


def test_unreadable_journal_does_not_claim_terminal_reclaim_during_resume(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    repo, seat, _metadata = _make_journal_only_seat(
        tmp_path,
        "journal-unreadable-resume",
        journal_state="unreadable",
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatReclaimed) as caught:
        goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "journal-resume",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id="different-recorded-holder",
        )
    assert caught.value.terminal_reclaimed is False


def test_journal_terminal_dirty_seat_is_retained_without_quarantine(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-terminal-dirty"
    repo, seat, _metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
        dirty=True,
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as caught:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
    assert any(
        name == seat.name and reason == "identity_unknown"
        for name, reason in caught.value.skipped_slots
    )
    assert (seat / "journal-holder-untracked.txt").read_text(encoding="utf-8") == (
        "preserve through quarantine\n"
    )


def test_locked_journal_probe_is_bounded_and_keeps_seat_held(
    tmp_path: Path, isolated_pool_state: Path
) -> None:
    dispatch_id = "journal-locked"
    repo, seat, _metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=2147483647,
        lock_start_token="dead-test-generation",
    )
    journal_path = goalflight_journal.resolve_journal_path(repo)
    code = (
        "import sqlite3, sys, time; "
        "db = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None); "
        "db.execute('PRAGMA journal_mode=DELETE'); "
        "db.execute('BEGIN EXCLUSIVE'); "
        "print('locked', flush=True); time.sleep(10)"
    )
    writer = subprocess.Popen(
        [sys.executable, "-c", code, str(journal_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None
        assert writer.stdout.readline().strip() == "locked"
        started = time.monotonic()
        assert goalflight_worktree_pool._bounded_journal_attempt_state(
            repo, dispatch_id
        ) == "unreadable"
        assert time.monotonic() - started < 0.5

        started = time.monotonic()
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "journal-reclaimer")
        assert time.monotonic() - started < 1.0
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{dispatch_id}"
        )
    finally:
        writer.terminate()
        writer.communicate(timeout=3)


def test_resume_racing_reclaim_serializes_without_losing_seat_work(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_id = "journal-race-holder"
    identity = goalflight_ledger.process_identity(os.getpid())
    assert identity and identity.get("start_token")
    repo, seat, _metadata = _make_journal_only_seat(
        tmp_path,
        dispatch_id,
        journal_state=goalflight_journal.ATTEMPT_TERMINAL,
        lock_pid=os.getpid(),
        lock_start_token=f"{identity['start_token']}:older-generation",
        dirty=True,
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
        }
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [],
    )
    reclaim_holds_seat = threading.Event()
    resume_finished = threading.Event()
    results: dict[str, tuple[object | None, Exception | None]] = {}

    def hold_reclaim_until_resume() -> None:
        reclaim_holds_seat.set()
        assert resume_finished.wait(timeout=5)

    def resume() -> None:
        assert reclaim_holds_seat.wait(timeout=5)
        try:
            lease = goalflight_worktree_pool.acquire_worktree_seat(
                repo,
                "journal-racer-resume",
                occupy_path=seat,
                reset=False,
                expected_prior_dispatch_id=dispatch_id,
            )
            results["resume"] = (lease, None)
            lease.release_unlaunched()
        except Exception as exc:
            results["resume"] = (None, exc)
        finally:
            resume_finished.set()

    def reclaim() -> None:
        try:
            results["reclaimer"] = (
                goalflight_worktree_pool.acquire_worktree_seat(
                    repo,
                    "journal-racer-reclaimer",
                    before_reset=lambda _path, _prior: hold_reclaim_until_resume(),
                ),
                None,
            )
        except Exception as exc:
            results["reclaimer"] = (None, exc)

    reclaim_worker = threading.Thread(target=reclaim)
    reclaim_worker.start()
    assert reclaim_holds_seat.wait(timeout=5)
    resume_worker = threading.Thread(target=resume)
    resume_worker.start()
    resume_worker.join(timeout=15)
    reclaim_worker.join(timeout=15)
    assert not resume_worker.is_alive()
    assert not reclaim_worker.is_alive()
    assert results["resume"][0] is None
    assert isinstance(results["resume"][1], goalflight_worktree_pool.WorktreeSeatError)
    lease = results["reclaimer"][0]
    assert lease is not None, repr(results["reclaimer"][1])
    try:
        assert lease.path == seat
        assert lease.quarantine_branch
        quarantine = f"refs/heads/{lease.quarantine_branch}"
        assert _git(repo, "show", f"{quarantine}:journal-holder-untracked.txt") == (
            "preserve through quarantine"
        )
    finally:
        lease.release()


def _write_grok_resume_source(
    account: str, seat: Path, session_id: str
) -> None:
    session = goalflight_dispatch._seat_session_dir(
        account, "grok", str(seat), session_id
    )
    assert session is not None
    session.mkdir(parents=True)
    (session / "summary.json").write_text("{}\n", encoding="utf-8")
    (session / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")


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
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = str(seats)
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_WORKTREE_SEATS"] = str(seats)
    env["GOALFLIGHT_DISABLE_NUDGES"] = "1"
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GOALFLIGHT_WORKTREE_LOCK_FD", None)
    env.pop("GOALFLIGHT_OCCUPANCY_LOCK_FD", None)
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


def _start_flock_holder(
    tmp_path: Path,
    repo: Path,
    seat: Path,
    dispatch_id: str,
    *,
    occupancy: bool = False,
    record_ledger: bool = True,
) -> tuple[subprocess.Popen[str], Path, Path]:
    mode = "occupancy" if occupancy else "seat"
    ready = tmp_path / f"{dispatch_id}.ready"
    release = tmp_path / f"{dispatch_id}.release"
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    code = r"""
import fcntl, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import goalflight_compat, goalflight_ledger, goalflight_worktree_pool
scripts, repo, seat, dispatch_id, mode, lock_path, ready, release, record_ledger = sys.argv[1:]
repo, seat = Path(repo), Path(seat)
def publish_holder():
    pid = os.getpid()
    identity = goalflight_compat.process_start_identity(pid)
    goalflight_ledger.write_record({
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "state": "running",
        "project_root": str(repo),
        "worker_pid": pid,
        "worker_identity": identity,
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": goalflight_worktree_pool._git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_head": goalflight_worktree_pool._git(seat, "rev-parse", "HEAD"),
    })
if mode == "seat":
    with open(lock_path, "r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        goalflight_worktree_pool._write_occupant(
            lock_file, seat_name=seat.name, dispatch_id=dispatch_id, controller_label=None
        )
        if record_ledger == "1":
            publish_holder()
        Path(ready).touch()
        while not Path(release).exists():
            time.sleep(0.01)
else:
    lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(seat, dispatch_id)
    try:
        if record_ledger == "1":
            publish_holder()
        Path(ready).touch()
        while not Path(release).exists():
            time.sleep(0.01)
    finally:
        lock.release()
"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(SCRIPTS),
            str(repo),
            str(seat),
            dispatch_id,
            mode,
            str(lock_path),
            str(ready),
            str(release),
            "1" if record_ledger else "0",
        ],
        cwd=str(repo),
        env=_env(tmp_path, seats=2),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.exists():
            return proc, release, lock_path
        if proc.poll() is not None:
            _stdout, stderr = proc.communicate()
            raise AssertionError(f"flock holder exited early: {stderr}")
        time.sleep(0.01)
    release.touch()
    try:
        _stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()
    raise AssertionError(f"flock holder did not become ready: {stderr}")


def _start_allocation_flock_holder(
    tmp_path: Path, repo: Path
) -> tuple[subprocess.Popen[str], Path, Path]:
    ready = tmp_path / "allocation-lock.ready"
    release = tmp_path / "allocation-lock.release"
    lock_path = goalflight_worktree_pool._seat_lock_root(repo) / "allocation.lock"
    code = r"""
import fcntl, sys, time
from pathlib import Path
lock_path, ready, release = map(Path, sys.argv[1:])
with open(lock_path, "r+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready.touch()
    while not release.exists():
        time.sleep(0.01)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path), str(ready), str(release)],
        cwd=str(repo),
        env=_env(tmp_path, seats=2),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.exists():
            return proc, release, lock_path
        if proc.poll() is not None:
            _stdout, stderr = proc.communicate()
            raise AssertionError(f"allocation flock holder exited early: {stderr}")
        time.sleep(0.01)
    release.touch()
    try:
        _stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()
    raise AssertionError(f"allocation flock holder did not become ready: {stderr}")


def _record_bind_probe_durations(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    durations: list[float] = []
    original = goalflight_dispatch._bind_dispatch_worktree

    def timed_bind(args):
        started = time.monotonic()
        try:
            return original(args)
        finally:
            durations.append(time.monotonic() - started)

    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", timed_bind)
    return durations


def _record_flock_operations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    operations: list[int] = []
    original = goalflight_worktree_pool.fcntl.flock

    def recording_flock(fd: int, operation: int) -> None:
        if operation != goalflight_worktree_pool.fcntl.LOCK_UN:
            operations.append(operation)
            if not operation & goalflight_worktree_pool.fcntl.LOCK_NB:
                raise AssertionError(f"blocking flock operation: {operation!r}")
        return original(fd, operation)

    monkeypatch.setattr(goalflight_worktree_pool.fcntl, "flock", recording_flock)
    return operations


def _assert_nonblocking_flock_operations(operations: list[int]) -> None:
    assert operations, "expected at least one flock probe"
    assert all(operation & goalflight_worktree_pool.fcntl.LOCK_NB for operation in operations), (
        f"flock probes must be non-blocking: {operations!r}"
    )


def _assert_refusal_finishes_well_before_budget(elapsed: float, budget_s: float) -> None:
    # This allows loaded-box scheduling latency while catching waits near the lock budget.
    limit_s = min(budget_s * 0.25, 10.0)
    assert elapsed < limit_s, (
        f"refusal took {elapsed:.3f}s; expected under {limit_s:.3f}s "
        f"for a {budget_s:.3f}s wait budget"
    )


def test_admission_wall_bound_rejects_a_bind_that_waits_out_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "budget-holder")
    args = SimpleNamespace(
        project_root=str(repo),
        worktree="HEAD",
        dispatch_id="budget-waiter",
        cwd=None,
        skip_seat_reset=False,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        capacity_wait_s=0.5,
    )
    try:
        started = time.monotonic()
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_dispatch._admit_dispatch_worktree(args)
        elapsed = time.monotonic() - started
        # The real admission path polls a held seat through its budget; the backstop must reject it.
        with pytest.raises(AssertionError, match="expected under"):
            _assert_refusal_finishes_well_before_budget(elapsed, args.capacity_wait_s)
    finally:
        holder.release()


def _record_path_probe_durations(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    durations: list[float] = []
    original = goalflight_worktree_pool.try_acquire_worktree_path_lock

    def timed_probe(*args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            durations.append(time.monotonic() - started)

    monkeypatch.setattr(
        goalflight_worktree_pool, "try_acquire_worktree_path_lock", timed_probe
    )
    return durations


def _drive_dispatch_deadline(monkeypatch: pytest.MonkeyPatch) -> _AdvancingDeadlineClock:
    clock = _AdvancingDeadlineClock()
    monkeypatch.setattr(goalflight_dispatch, "time", clock)
    return clock


def _queue_retry_args(
    tmp_path: Path,
    repo: Path,
    seat: Path,
    holder_id: str,
    *,
    wait_s: float,
) -> tuple[SimpleNamespace, Path, bytes]:
    dispatch_id = "queue-retry"
    claim = tmp_path / "queue-retry.claimed"
    goalflight_dispatch._write_json_atomic(
        claim,
        {
            "dispatch_id": dispatch_id,
            "queue_launch_token": "queue-token",
            "worktree_path": str(seat),
            "worktree_seat": seat.name,
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--worktree",
                "HEAD",
                "--skip-seat-reset",
                "--cwd",
                str(seat),
            ],
        },
    )
    args = SimpleNamespace(
        agent="test-dispatch",
        worktree="HEAD",
        parent_dispatch_id=None,
        dispatch_id=dispatch_id,
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        from_queue=True,
        queue_claim_path=str(claim),
        queue_launch_token="queue-token",
        worktree_pin_holder=holder_id,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=wait_s,
    )
    return args, claim, claim.read_bytes()


def _set_pinned_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.delenv("GOALFLIGHT_WORKTREE_LOCK_FD", raising=False)
    monkeypatch.delenv("GOALFLIGHT_OCCUPANCY_LOCK_FD", raising=False)


def _assert_pinned_resume_refusal(
    args: SimpleNamespace,
    repo: Path,
    seat: Path,
    holder_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pinned seat waits through the positive budget before refusing.
    args.capacity_wait_s = 0.05
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    lock_before = lock_path.read_bytes()
    branch_before = _git(seat, "rev-parse", "--abbrev-ref", "HEAD")
    head_before = _git(seat, "rev-parse", "HEAD")
    clock = _drive_dispatch_deadline(monkeypatch)
    bind_probes = _record_bind_probe_durations(monkeypatch)
    flock_operations = _record_flock_operations(monkeypatch)
    with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
        goalflight_dispatch._admit_dispatch_worktree(args)
    assert len(bind_probes) == 2
    _assert_nonblocking_flock_operations(flock_operations)
    message = str(exc_info.value)
    assert f"worktree seat {seat.name}; wait for a seat: holder={holder_id}" in message
    assert "flock_liveness=held" in message
    assert message.count(holder_id) == 1
    assert len(clock.requested_sleeps) == 1
    assert sum(clock.requested_sleeps) == pytest.approx(args.capacity_wait_s)
    assert sum(clock.requested_sleeps) <= args.capacity_wait_s
    assert clock.elapsed == pytest.approx(args.capacity_wait_s)
    assert args.cwd == str(seat)
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert lock_path.read_bytes() == lock_before
    assert _lock_dispatch_id(lock_path) == holder_id
    assert not (seat.parent / "s-2").exists()


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
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "1/1 worktrees busy in repo" in combined, combined
        assert "held-occupant" in combined, combined
        assert "s-1" in combined, combined
        assert "refusing to git worktree add" in combined, combined
        assert not (repo / "worktrees" / "s-2").exists()
    finally:
        holder.release()


def test_full_pool_branch_refusal_leaves_main_checkout_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "held-occupant")
    before = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "branch", "--show-current"),
        (repo / ".git" / "index").read_bytes(),
    )
    try:
        command = _dispatch_cmd(
            tmp_path,
            repo,
            "need-a-seat-at-main",
            sys.executable,
            "-c",
            "print('nope')",
        )
        separator = command.index("--")
        command[separator:separator] = ["--worktree", "main"]
        proc = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 2, combined
        assert "worktrees busy in repo" in combined, combined
        assert (
            _git(repo, "rev-parse", "HEAD"),
            _git(repo, "branch", "--show-current"),
            (repo / ".git" / "index").read_bytes(),
        ) == before
    finally:
        holder.release()


def test_main_worktree_mutation_guard_refuses_reset(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatError,
        match="repository main worktree",
    ):
        goalflight_worktree_pool._git(repo, "reset", "--hard")
    assert _git(repo, "rev-parse", "HEAD") == before


def test_release_refuses_missing_unresolved_or_main_seat(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "branch", "--show-current"),
        (repo / ".git" / "index").read_bytes(),
    )
    goalflight_dispatch._release_withdrawn_worktree(
        {"project_root": str(repo), "worker_cwd": None, "worktree_path": None},
        "completed-seatless",
    )
    for path, expected in (
        (None, "no worktree seat path was recorded"),
        (str(repo), "resolves to project root"),
        (str(tmp_path / "missing-seat"), "unresolved"),
    ):
        released, reason = goalflight_worktree_pool.release_worktree_for_dispatch(
            repo, path, "completed-seatless"
        )
        assert not released
        assert expected in reason
    assert (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "branch", "--show-current"),
        (repo / ".git" / "index").read_bytes(),
    ) == before


def test_resume_refuses_missing_or_relative_recorded_seat(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError, match="no worker cwd evidence"
    ):
        goalflight_dispatch._resume_worker_cwd({"worker_cwd": "."})
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="missing or unresolved",
    ):
        goalflight_dispatch._resume_worker_cwd(
            {"worker_cwd": str(tmp_path / "missing-seat")}
        )


def test_unknown_free_lock_counts_against_pool_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "unknown-holder")
    seat = holder.path
    holder.release()
    goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name).write_text(
        "\n", encoding="utf-8"
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as exc_info:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "new-dispatch")

    message = str(exc_info.value)
    assert "1/1 worktrees busy" in message
    assert "s-1=unknown-dispatch" in message
    assert "none recorded" not in message


def test_existing_checkout_without_lock_counts_against_pool_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "missing-lock")
    seat = holder.path
    holder.release()
    goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name).unlink()

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as exc_info:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "new-dispatch")

    message = str(exc_info.value)
    assert "1/1 worktrees busy" in message
    assert f"{seat.name}=unknown-dispatch" in message
    assert "none recorded" not in message


def test_legacy_label_checkout_without_lock_counts_against_pool_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    legacy = repo / "worktrees" / "old-label" / "s-1"
    legacy.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(legacy), "HEAD")

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as exc_info:
        goalflight_worktree_pool.acquire_worktree_seat(repo, "new-dispatch")

    message = str(exc_info.value)
    assert "1/1 worktrees busy" in message
    assert "s-1=unknown-dispatch" in message
    assert "none recorded" not in message


def test_occupancy_refuses_before_existing_seat_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "old-holder")
    seat = holder.path
    holder.release()
    record_finished_holder(holder.dispatch_id, lock_holder="native")
    _rewrite_test_seat_lock_identity_dead(
        repo, seat.name
    )
    branch_before = _git(seat, "rev-parse", "--abbrev-ref", "HEAD")
    seen_branches: list[str] = []

    def refuse(args) -> None:
        seen_branches.append(_git(seat, "rev-parse", "--abbrev-ref", "HEAD"))
        raise goalflight_dispatch.DispatchUsageError("occupancy refused")

    monkeypatch.setattr(
        goalflight_dispatch, "_prepare_attempt_worktree_occupancy", refuse
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=None,
        dispatch_id="new-dispatch",
        cwd=str(seat),
        skip_seat_reset=False,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        dispatch_warnings=[],
    )

    with pytest.raises(goalflight_dispatch.DispatchUsageError, match="occupancy refused"):
        goalflight_dispatch._admit_dispatch_worktree(args)

    assert seen_branches == [branch_before]
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == branch_before


def test_unpinned_dispatch_retries_after_rejected_occupancy_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "s-1", tmp_path / "s-2"
    args = SimpleNamespace(cwd=None)
    prepared: list[str] = []
    probed: list[Path] = []
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_worktree_occupancy",
        lambda current: prepared.append(current.cwd) or None,
    )
    occupancy_check = goalflight_dispatch._worktree_occupancy_before_reset(args)

    def probe(candidate: Path) -> None:
        occupancy_check(candidate)
        probed.append(candidate)
        if len(probed) == 1:
            # The pool calls _validate_holder after before_reset succeeds.
            raise goalflight_worktree_pool.WorktreeSeatUnavailable(
                "first probed seat rejected", seat_name=candidate.name
            )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatUnavailable,
        match="first probed seat rejected",
    ):
        probe(first)

    retry_candidate = Path(args.cwd) if args.cwd else second
    probe(retry_candidate)
    assert probed == [first, second]
    assert prepared == [str(first), str(second)]
    assert args.cwd is None


def test_unpinned_admission_retry_drops_first_candidate_preclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    first_seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "retry-seed-1")
    second_seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "retry-seed-2")
    first, second = first_seed.path, second_seed.path
    first_seed.release()
    second_seed.release()
    _write_terminal_seat_record("retry-seed-1", repo, first)
    _write_terminal_seat_record("retry-seed-2", repo, second)

    args = SimpleNamespace(
        agent="test-dispatch",
        project_root=str(repo),
        cwd=None,
        worktree="HEAD",
        worktree_root=None,
        dispatch_id="retry-after-preclaim",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        from_queue=False,
        capacity_wait_s=5.0,
        dispatch_warnings=[],
        _worktree_seat=None,
    )
    bind_dispatch_worktree = goalflight_dispatch._bind_dispatch_worktree
    bind_attempts = 0
    blocker = None

    def fail_after_first_candidate(current):
        nonlocal bind_attempts, blocker
        bind_attempts += 1
        if bind_attempts == 1:
            goalflight_dispatch._worktree_occupancy_before_reset(current)(first)
            blocker = _start_flock_holder(
                tmp_path,
                repo,
                first,
                "retry-first-seat-holder",
                record_ledger=True,
            )
            raise goalflight_worktree_pool.WorktreeSeatUnavailable(
                "first candidate preparation failed", seat_name=first.name
            )
        return bind_dispatch_worktree(current)

    monkeypatch.setattr(
        goalflight_dispatch, "_bind_dispatch_worktree", fail_after_first_candidate
    )
    lease = None
    try:
        lease = goalflight_dispatch._admit_dispatch_worktree(args)
        assert bind_attempts == 2
        assert lease.path == second
        assert args._worktree_occupancy_lock.path == second
    finally:
        if lease is not None:
            lease.release()
        if blocker is not None:
            holder, release, _lock_path = blocker
            release.touch()
            holder_out, holder_err = holder.communicate(timeout=10)
            assert holder.returncode == 0, (holder.returncode, holder_out, holder_err)
        goalflight_dispatch._release_worktree_occupancy_lock(args)


def test_pinned_dispatch_keeps_its_cwd_during_occupancy_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = tmp_path / "s-1"
    args = SimpleNamespace(cwd=str(pinned))
    prepared: list[str] = []
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_worktree_occupancy",
        lambda current: prepared.append(current.cwd) or None,
    )

    goalflight_dispatch._worktree_occupancy_before_reset(args)(pinned)
    assert prepared == [str(pinned)]
    assert args.cwd == str(pinned)


def test_occupancy_is_rechecked_when_resume_falls_back_to_another_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "s-1"
    second = tmp_path / "s-2"
    args = SimpleNamespace(
        cwd=str(first),
        dispatch_id="resume-child",
        _worktree_occupancy_checked=False,
        _worktree_occupancy_checked_path=str(first.resolve()),
        _worktree_occupancy_warning=None,
        _worktree_occupancy_lock=None,
    )
    seen: list[str] = []
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_worktree_occupancy",
        lambda current: seen.append(current.cwd) or None,
    )
    monkeypatch.setattr(goalflight_dispatch, "_worker_cwd", lambda current: Path(current.cwd))

    def bind(current):
        goalflight_dispatch._worktree_occupancy_before_reset(current)(first)
        current.cwd = str(second)
        return object()

    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", bind)

    goalflight_dispatch._admit_dispatch_worktree(args)

    assert seen == [str(first), str(second)]


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
            "occ = int(os.environ['GOALFLIGHT_OCCUPANCY_LOCK_FD'])",
            "os.fstat(occ)",
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
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    worker_pid = int(marker.read_text(encoding="utf-8"))
    worker_identity = goalflight_worktree_pool.goalflight_compat.process_start_identity(worker_pid)
    assert worker_identity and worker_identity.get("start_token")
    try:
        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-while-live")
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            assert "inherit-seat" in str(exc) or "s-1" in str(exc)
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
        # Killing the process releases the lock; a terminal dispatch verdict
        # is separately required before the pool may reuse its work.
        record_finished_holder("inherit-seat", worker_identity, state="worker_dead")
        replacement = goalflight_worktree_pool.acquire_worktree_seat(
            repo, "after-worker-death"
        )
        try:
            assert replacement.path.name == "s-1"
        finally:
            replacement.release()
    finally:
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_writer_with_inherited_lock_fd_does_not_spawn_in_project_root(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "inherited-root-worker-cwd"
    inherited_fd_path = tmp_path / "unrelated-open-fd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    with inherited_fd_path.open("w", encoding="utf-8") as inherited_fd:
        env[goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] = str(
            inherited_fd.fileno()
        )
        command = _dispatch_cmd(
            tmp_path,
            repo,
            "inherited-root-writer",
            sys.executable,
            "-c",
            worker,
        )
        command[command.index("--") : command.index("--")] = [
            "--worktree",
            "HEAD",
        ]
        proc = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            pass_fds=(inherited_fd.fileno(),),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 64, combined
    assert "project root" in combined.lower(), combined
    assert "WorktreeSeatUnavailable" not in combined
    assert "DISPATCH-REFUSED " in proc.stdout
    assert not marker.exists()


def test_writer_with_inherited_lock_fd_keeps_its_managed_seat(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "seat-parent")
    marker = tmp_path / "inherited-seat-worker-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    try:
        lock_path = goalflight_worktree_pool._candidate_lock_path(
            repo,
            parent.path,
            managed_root=goalflight_worktree_pool.repository_worktree_root(repo),
        )
        registry_path = goalflight_worktree_pool._lock_registry_path(
            goalflight_worktree_pool._git_common_dir(repo)
        )
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["locks"].pop(goalflight_worktree_pool._lock_registry_key(lock_path))
        registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        env[goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] = str(parent.fileno())
        command = _dispatch_cmd(
            tmp_path,
            repo,
            "inherited-seat-writer",
            sys.executable,
            "-c",
            worker,
        )
        command[command.index("--") : command.index("--")] = [
            "--worktree",
            "HEAD",
        ]
        proc = subprocess.run(
            command,
            cwd=str(parent.path),
            env=env,
            pass_fds=(parent.fileno(),),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0, combined
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), combined
        assert Path(marker.read_text(encoding="utf-8")).resolve() == parent.path.resolve()
    finally:
        parent.release()


def test_inherited_lock_fd_must_match_effective_ring_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "fd-seat-first")
    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "fd-seat-second")
    second_path = second.path
    args = SimpleNamespace(
        agent="test-dispatch",
        shape="bash",
        worker=["python"],
        worktree="HEAD",
        project_root=str(repo),
        cwd=str(second_path),
        read_only=False,
        in_place=False,
        dispatch_id="fd-seat-mismatch",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )
    try:
        assert first.path != second.path
        second.release()
        _write_terminal_seat_record("fd-seat-second", repo, second_path)
        monkeypatch.setenv(
            goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
            str(first.fileno()),
        )
        rebound = goalflight_dispatch._bind_dispatch_worktree(args)
        assert rebound is not None
        try:
            assert rebound.path == second_path
            assert rebound.seat_name == second_path.name
        finally:
            rebound.release()
    finally:
        second.release()
        first.release()


def test_read_only_mismatched_inherited_fd_gets_target_shared_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    first_writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "seed-read-only-inherited-first", base=base
    )
    second_writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "seed-read-only-inherited-second", base=base
    )
    seat = second_writer.path
    finish_seat_holder(second_writer)
    args = SimpleNamespace(
        agent="claude-acp",
        shape="acp",
        worker=[],
        worktree="off",
        project_root=str(repo),
        cwd=str(seat),
        read_only=True,
        in_place=False,
        dispatch_id="read-only-inherited-lock",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )

    def refuse_writer_acquire(*_args, **_kwargs):
        raise AssertionError("read-only admission attempted writer seat acquisition")

    monkeypatch.setattr(
        goalflight_worktree_pool, "acquire_worktree_seat", refuse_writer_acquire
    )
    monkeypatch.setenv(
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
        str(first_writer.fileno()),
    )
    try:
        assert goalflight_dispatch._effective_read_only(args)
        assert goalflight_dispatch._occupancy_exempt_read_only(args)
        assert goalflight_worktree_pool.classify_dispatch_cwd(
            seat,
            project_root=repo,
            controller_label=None,
        ) == "ring-seat"
        assert goalflight_dispatch._bind_dispatch_worktree(args) is None
        dispatch_hold = args._worktree_read_only_hold
        assert dispatch_hold is None
        assert Path(args.cwd).resolve() != seat.resolve()
        assert Path(args.cwd).parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        writer_path_lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            seat, "writer-path-during-review"
        )
        writer_path_lock.release()
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(args)
        first_writer.release()


def test_read_only_resume_mismatched_fd_uses_parent_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    parent_base = _git(repo, "rev-parse", "HEAD")
    (repo / "new-head.txt").write_text("later head\n", encoding="utf-8")
    _git(repo, "add", "new-head.txt")
    _git(repo, "commit", "-m", "later head")
    current_base = _git(repo, "rev-parse", "HEAD")

    mismatch_writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "seed-read-only-resume-mismatch", base=current_base
    )
    target_writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "seed-read-only-resume-target", base=parent_base
    )
    seat = target_writer.path
    finish_seat_holder(target_writer)
    goalflight_ledger.write_record(
        {
            "dispatch_id": "read-only-resume-parent",
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_head": parent_base,
        }
    )
    args = SimpleNamespace(
        agent="claude-acp",
        shape="acp",
        worker=[],
        worktree="HEAD",
        project_root=str(repo),
        cwd=str(seat),
        read_only=True,
        in_place=False,
        dispatch_id="read-only-resume-child",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=True,
        parent_dispatch_id="read-only-resume-parent",
        from_queue=False,
        _worktree_seat=None,
    )

    monkeypatch.setenv(
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
        str(mismatch_writer.fileno()),
    )
    try:
        assert _git(seat, "rev-parse", "HEAD") == parent_base
        assert current_base != parent_base
        assert goalflight_dispatch._bind_dispatch_worktree(args) is None
        hold = args._worktree_read_only_hold
        assert hold is None
        assert Path(args.cwd).resolve() != seat.resolve()
        assert Path(args.cwd).parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert _git(seat, "rev-parse", "HEAD") == parent_base
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(args)
        mismatch_writer.release()


def test_unreadable_inherited_lock_state_refuses_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = SimpleNamespace(
        worktree="off",
        project_root=str(repo),
        cwd=None,
        read_only=False,
        in_place=False,
        dispatch_id="unreadable-lock-fd",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )
    monkeypatch.setenv(
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
        "2147483647",
    )
    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="cannot verify the inherited worktree lock fd",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)


def test_unreadable_inherited_lock_registry_refuses_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "bad-registry-seat")
    registry_path = goalflight_worktree_pool._lock_registry_path(
        goalflight_worktree_pool._git_common_dir(repo)
    )
    registry_path.write_text("invalid json\n", encoding="utf-8")
    args = SimpleNamespace(
        worktree="HEAD",
        project_root=str(repo),
        cwd=str(holder.path),
        read_only=False,
        in_place=False,
        dispatch_id="unreadable-lock-registry",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )
    monkeypatch.setenv(
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
        str(holder.fileno()),
    )
    try:
        with pytest.raises(
            goalflight_worktree_pool.WorktreeCwdRefused,
            match="cannot verify that the inherited worktree lock fd owns",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
    finally:
        holder.release()


def test_resume_reacquires_exact_seat_and_blocks_fresh_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, seat)
    head_before = _git(seat, "rev-parse", "HEAD")
    dirty_file = seat / "resume-dirty.txt"
    dirty_file.write_text("keep this checkout\n", encoding="utf-8")

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    assert resumed is not None
    try:
        assert resumed.path == seat
        assert args.cwd == str(seat)
        assert _git(seat, "rev-parse", "HEAD") == head_before
        assert dirty_file.read_text(encoding="utf-8") == "keep this checkout\n"
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="resume-child",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "fresh-dispatch")
        assert not (seat.parent / "s-2").exists()
    finally:
        resumed.release()


def test_prelaunch_resume_releases_its_seat_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, seat)
    keep = seat / "original-worker.txt"
    keep.write_text("original checkout\n", encoding="utf-8")
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
        repo,
        seat.name,
        controller_label=goalflight_worktree_pool.default_controller_ring_label(
            None, project_root=repo
        ),
    )

    refused = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "resume-refused",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    refused.release_unlaunched()

    assert _lock_dispatch_id(lock_path) == "resume-parent"
    assert keep.read_text(encoding="utf-8") == "original checkout\n"
    resumed = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "resume-after-refusal",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    try:
        assert resumed.path == seat
        assert keep.read_text(encoding="utf-8") == "original checkout\n"
    finally:
        resumed.release()


def test_resume_refuses_legacy_prelaunch_child_with_worker_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, seat)
    head_before = _git(seat, "rev-parse", "HEAD")
    refused = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "legacy-refused-child",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    refused.release()
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "legacy-refused-child",
            "parent_dispatch_id": "resume-parent",
            "state": "failed",
            "terminal_state": "failed",
            "prelaunch_failure": True,
            "worktree_reclaimed_dispatch_id": "resume-parent",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_branch": "worktree/resume-parent",
        }
    )

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatReclaimed):
        goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "resume-after-legacy-refusal",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id="resume-parent",
        )
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert _lock_dispatch_id(
        goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    ) == "legacy-refused-child"


@pytest.mark.parametrize("lock_holder", (None, "wrapper"))
@pytest.mark.parametrize("engine_in_seat", (False, True))
def test_resume_legacy_holder_uses_cwd_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
    engine_in_seat: bool,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, seat)
    if lock_holder is not None:
        record = goalflight_ledger.read_record("resume-parent")
        assert record is not None
        record["lock_holder"] = lock_holder
        goalflight_ledger.write_record(record)
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda _pid, _token: False,
    )
    census_calls = []

    def census(**_kwargs):
        census_calls.append(True)
        return [(424242, str(seat))] if engine_in_seat else []

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", census)
    if engine_in_seat:
        monkeypatch.setattr(
            goalflight_worktree_pool.os,
            "getpgid",
            lambda _pid: os.getpgrp(),
        )

    if engine_in_seat:
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(
                repo,
                "resume-child",
                occupy_path=seat,
                reset=False,
                expected_prior_dispatch_id="resume-parent",
            )
    else:
        resumed = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "resume-child",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id="resume-parent",
        )
        try:
            assert resumed.path == seat
        finally:
            resumed.release()
    assert census_calls == [True]


@pytest.mark.parametrize("lock_holder", (None, "wrapper"))
@pytest.mark.parametrize("engine_in_seat", (False, True))
def test_recycled_dispatch_resume_uses_cwd_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_holder: str | None,
    engine_in_seat: bool,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    finish_seat_holder(parent)
    _write_terminal_seat_record("resume-parent", repo, seat)

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    finish_seat_holder(reclaimer)
    holder_fields = {
        "worktree_reclaimed_dispatch_id": "resume-parent",
        "worktree_keep_ref": reclaimer.keep_ref,
        "worktree_quarantine_ref": f"refs/heads/{reclaimer.quarantine_branch}",
    }
    if lock_holder is not None:
        holder_fields["lock_holder"] = lock_holder
    _write_seat_holder_record(
        "reclaimer", repo, reclaimer, state="complete", **holder_fields
    )

    census_calls = []

    def census(**_kwargs):
        census_calls.append(True)
        return [(424242, str(seat))] if engine_in_seat else []

    monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", census)
    if engine_in_seat:
        monkeypatch.setattr(
            goalflight_worktree_pool.os,
            "getpgid",
            lambda _pid: os.getpgrp(),
        )

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    if engine_in_seat:
        branch_before = _git(seat, "rev-parse", "--abbrev-ref", "HEAD")
        head_before = _git(seat, "rev-parse", "HEAD")
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatReclaimed) as exc_info:
            goalflight_dispatch._bind_dispatch_worktree(args)
        assert getattr(exc_info.value.__cause__, "reason", None) == "worker_cwd_live"
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
        assert _git(seat, "rev-parse", "HEAD") == head_before
    else:
        resumed = goalflight_dispatch._bind_dispatch_worktree(args)
        try:
            assert resumed.path == seat
            assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
                "worktree/resume-parent"
            )
        finally:
            resumed.release()
    assert census_calls == [True]


@pytest.mark.parametrize("shape", ["bash", "acp"])
def test_read_only_bind_uses_shared_checkout_for_bash_and_acp(
    tmp_path: Path, shape: str
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "review-base.txt").write_text("review base\n", encoding="utf-8")
    _git(repo, "add", "review-base.txt")
    _git(repo, "commit", "-m", "review base")
    base = _git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "writer-review-base", base=base
    )
    seat = writer.path
    finish_seat_holder(writer)
    args = SimpleNamespace(
        agent="codex-acp" if shape == "acp" else "grok-code",
        shape=shape,
        read_only=True,
        worker=[],
        project_root=str(repo),
        cwd=None,
        worktree="shared-read-only",
        worktree_base=base,
        worktree_root=None,
        dispatch_id=f"review-{shape}",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        from_queue=False,
        _worktree_seat=None,
    )

    assert goalflight_dispatch._bind_dispatch_worktree(args) is None
    hold = args._worktree_read_only_hold
    try:
        assert hold is None
        assert Path(args.cwd).resolve() != seat.resolve()
        assert Path(args.cwd).parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert (repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR).exists()
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(args)


@pytest.mark.parametrize("shape", ["bash", "acp"])
def test_read_only_shared_checkout_leaves_authors_seat_available(
    tmp_path: Path, shape: str
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "review-base.txt").write_text("review base\n", encoding="utf-8")
    _git(repo, "add", "review-base.txt")
    _git(repo, "commit", "-m", "review base")
    base = _git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "writer-path-hold", base=base
    )
    seat = writer.path
    writer.release()
    _write_terminal_seat_record("writer-path-hold", repo, seat)

    def read_only_args(dispatch_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            agent="codex-acp" if shape == "acp" else "grok-code",
            shape=shape,
            read_only=True,
            worker=[],
            project_root=str(repo),
            cwd=None,
            worktree="shared-read-only",
            worktree_base=base,
            worktree_root=None,
            dispatch_id=dispatch_id,
            controller_label=None,
            skip_seat_reset=False,
            in_place=False,
            from_queue=False,
            _worktree_seat=None,
        )

    first = read_only_args("review-path-one")
    assert goalflight_dispatch._bind_dispatch_worktree(first) is None
    first_hold = first._worktree_read_only_hold
    assert first_hold is None
    assert Path(first.cwd).resolve() != seat.resolve()
    assert Path(first.cwd).parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
    try:
        resume = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "author-resume-during-review",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id="writer-path-hold",
        )
        resume.release()

        second = read_only_args("review-path-two")
        assert goalflight_dispatch._bind_dispatch_worktree(second) is None
        second_hold = second._worktree_read_only_hold
        assert second_hold is None
        try:
            assert Path(second.cwd).resolve() == Path(first.cwd).resolve()
        finally:
            goalflight_dispatch._release_read_only_worktree_hold(second)
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(first)

    writer_path_lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(
        seat, "writer-after-reviews"
    )
    writer_path_lock.release()


def test_read_only_worker_environment_drops_inherited_writer_seat_fd() -> None:
    env = {
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV: "123",
        "GIT_OPTIONAL_LOCKS": "1",
    }
    args = SimpleNamespace(read_only=True, os_sandbox=None, agent="grok-code")

    goalflight_dispatch._apply_read_only_env(env, args)

    assert goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV not in env
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_non_in_place_acp_read_only_resume_admits_detached_checkout(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _ = goalflight_worktree_pool.shared_read_only_worktree(repo, base=base)
    args = SimpleNamespace(
        agent="claude-acp",
        shape="acp",
        read_only=True,
        worker=[],
        project_root=str(repo),
        cwd=str(checkout),
        worktree="shared-read-only",
        worktree_base=base,
        worktree_root=None,
        parent_dispatch_id="readonly-acp-parent",
        dispatch_id="readonly-acp-child",
        _worktree_base_commit=base,
        controller_label=None,
        skip_seat_reset=True,
        in_place=False,
        from_queue=False,
        _worktree_seat=None,
    )

    assert goalflight_dispatch._requested_worktree_base(args) == base
    assert goalflight_dispatch._bind_dispatch_worktree(args) is None
    assert Path(args.cwd).resolve() == checkout.resolve()
    assert len(goalflight_worktree_pool._read_only_registered_worktrees(repo)) == 1


def test_read_only_resume_falls_back_when_recorded_pool_seat_cannot_be_held(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "readonly-resume-writer", base=base
    )
    seat = writer.path
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-resume-parent",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_head": base,
        }
    )
    try:
        args = SimpleNamespace(
            agent="claude-acp",
            shape="acp",
            read_only=True,
            worker=[],
            project_root=str(repo),
            cwd=str(seat),
            worktree="shared-read-only",
            worktree_base=base,
            worktree_root=None,
            parent_dispatch_id="readonly-resume-parent",
            dispatch_id="readonly-resume-child",
            controller_label=None,
            skip_seat_reset=True,
            in_place=False,
            from_queue=False,
            _worktree_seat=None,
        )

        assert goalflight_dispatch._bind_dispatch_worktree(args) is None
        assert Path(args.cwd).parent.name == goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
        assert Path(args.cwd).resolve() != seat.resolve()
        assert args._worktree_read_only_hold is None
    finally:
        writer.release()


def test_read_only_resume_recreates_reclaimed_shared_checkout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    old_path, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    tail = tmp_path / "readonly-parent.tail"
    tail.write_text("!COMPLETE: readonly-parent — review report\n", encoding="utf-8")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "readonly-parent",
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(repo),
            "worker_cwd": str(old_path),
            "worktree_path": str(old_path),
            "worktree_base": base,
            "worktree_head": base,
            "os_sandbox": {"requested_profile": "read-only"},
            "stdout_path": str(tail),
        }
    )
    _git(repo, "worktree", "remove", "--force", str(old_path))
    assert not old_path.exists()
    args = SimpleNamespace(
        agent="codex",
        shape="bash",
        worker=[],
        read_only=True,
        project_root=str(repo),
        cwd=str(old_path),
        worktree="shared-read-only",
        parent_dispatch_id="readonly-parent",
        dispatch_id="readonly-child",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=True,
        in_place=False,
        from_queue=False,
        _worktree_seat=None,
        _worktree_capacity_lease_active=False,
    )

    try:
        goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
        assert args._read_only_resume_reservation is None
        assert goalflight_dispatch._bind_dispatch_worktree(args) is None
        fresh = Path(args.cwd)
        assert fresh.is_dir()
        assert goalflight_worktree_pool.read_only_worktree_path_verdict(
            fresh, project_root=repo
        )[0] == goalflight_worktree_pool.YES
        assert _git(fresh, "rev-parse", "HEAD") == base
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(args)
        goalflight_dispatch._release_read_only_resume_reservation(args)


@pytest.mark.parametrize("checkout_kind", ["shared", "pooled"])
def test_cmd_resume_reaches_fresh_checkout_after_read_only_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkout_kind: str,
) -> None:
    _set_pinned_test_env(monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "readonly-cli-parent"
    child_id = "readonly-cli-child"
    base = _git(repo, "rev-parse", "HEAD")
    if checkout_kind == "shared":
        old_path, _selected = goalflight_worktree_pool.shared_read_only_worktree(
            repo, base=base
        )
    else:
        lease = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id, base=base)
        old_path = lease.path
        lease.release()
    tail = tmp_path / f"{parent_id}.tail"
    tail.write_text(f"!COMPLETE: {parent_id} — review report\n", encoding="utf-8")
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": parent_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "state": "complete",
        "terminal_state": "complete",
        "worker_pid": 2147483647,
        "worker_identity": {
            "pid": 2147483647,
            "start_token": "exited-test-worker",
        },
        "project_root": str(repo),
        "worker_cwd": str(old_path),
        "worktree_path": str(old_path),
        "worktree_base": base,
        "worktree_head": base,
        "attempt_id": "readonly-parent-attempt",
        "terminal_marker": {
            "kind": "COMPLETE",
            "dispatch_id": parent_id,
            "attempt_id": "readonly-parent-attempt",
        },
        "resume_mode": "resume",
        "os_sandbox": {
            "requested_profile": "read-only",
            "enforced_profile": "read-only",
        },
        "stdout_path": str(tail),
    }
    if checkout_kind == "pooled":
        record["worktree_branch"] = _git(old_path, "rev-parse", "--abbrev-ref", "HEAD")
    goalflight_ledger.write_record(record)
    if checkout_kind == "shared":
        _git(repo, "worktree", "remove", "--force", str(old_path))
    else:
        _rewrite_test_seat_lock_identity_dead(
            repo, old_path.name
        )
        ledger_dir = goalflight_ledger.runs_dir(create=False)
        report = goalflight_worktree_gc.terminal_dry_run(repo, ledger_dir=ledger_dir)
        entry = next(
            item for item in report["entries"]
            if os.path.realpath(item["path"]) == os.path.realpath(old_path)
        )
        assert entry["decision"] == "remove", entry
        current_checkout, current_error = goalflight_worktree_gc.current_checkout_path(repo)
        goalflight_worktree_gc.apply_removals(
            repo,
            report["entries"],
            into="main",
            ledger_dir=ledger_dir,
            main_path=goalflight_worktree_gc.main_worktree_path(repo),
            current_checkout=current_checkout,
            current_error=current_error,
        )
        assert entry["outcome"] == "removed", entry
    assert not old_path.exists()
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue the read-only review.\n", encoding="utf-8")

    source = {
        "record": record,
        "engine": "codex",
        "agent": "codex",
        "shape": "bash",
        "session_id": "00000000-0000-4000-8000-000000000033",
        "codex_home": str(tmp_path / "codex-home"),
        "codex_home_owner_dispatch_id": parent_id,
    }
    monkeypatch.setattr(goalflight_dispatch, "_admit_dispatch_disk_floor", lambda: True)
    monkeypatch.setattr(goalflight_dispatch, "_resume_policy_argv", lambda *_args: [])
    monkeypatch.setattr(goalflight_dispatch, "_launch_policy_args", lambda *_args: None)
    monkeypatch.setattr(goalflight_dispatch, "_refuse_launch_model_policy", lambda *_args: None)
    monkeypatch.setattr(goalflight_dispatch, "_validate_resume_source", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(goalflight_dispatch, "_default_dispatch_id", lambda *_args: child_id)
    monkeypatch.setattr(
        goalflight_dispatch,
        "_refuse_existing_dispatch_id_for_resume",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_resume_launch_argv",
        lambda *_args, **_kwargs: ["--cwd", str(old_path)],
    )

    def prepare(source_arg, *, candidate_argv, dispatch_id):
        assert goalflight_dispatch._resume_worker_cwd(source_arg["record"]) == old_path
        goalflight_dispatch._validate_resume_worktree_source(
            parent_id, source_arg["record"], old_path
        )
        args = SimpleNamespace(
            agent="codex",
            shape="bash",
            worker=[],
            read_only=True,
            project_root=str(repo),
            cwd=str(old_path),
            worktree="shared-read-only",
            parent_dispatch_id=parent_id,
            dispatch_id=dispatch_id,
            controller_label=None,
            worktree_root=None,
            skip_seat_reset=True,
            in_place=False,
            from_queue=False,
            _worktree_seat=None,
            _worktree_capacity_lease_active=True,
            _original_argv=list(candidate_argv),
        )
        return {"dispatch_id": dispatch_id, "args": args}

    monkeypatch.setattr(goalflight_dispatch, "_preflight_resume_dispatch", prepare)

    def launch(_argv, *, resume_plan):
        args = resume_plan["args"]
        try:
            goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
            assert args._read_only_resume_reservation is None
            assert goalflight_dispatch._bind_dispatch_worktree(args) is None
            fresh = Path(args.cwd)
            assert fresh.is_dir()
            assert goalflight_worktree_pool.read_only_worktree_path_verdict(
                fresh, project_root=repo
            )[0] == goalflight_worktree_pool.YES
            assert _git(fresh, "rev-parse", "HEAD") == base
        finally:
            goalflight_dispatch._release_read_only_worktree_hold(args)
            goalflight_dispatch._release_read_only_resume_reservation(args)
        return 0

    monkeypatch.setattr(goalflight_dispatch, "main", launch)
    assert goalflight_dispatch._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0


def test_read_only_resume_refuses_missing_recorded_review_base(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    writer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "readonly-missing-base-writer", base=base
    )
    seat = writer.path
    writer.release()
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-missing-base-parent",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
        }
    )
    args = SimpleNamespace(
        agent="claude-acp",
        shape="acp",
        read_only=True,
        worker=[],
        project_root=str(repo),
        cwd=str(seat),
        worktree="shared-read-only",
        worktree_base=None,
        worktree_root=None,
        parent_dispatch_id="readonly-missing-base-parent",
        dispatch_id="readonly-missing-base-child",
        controller_label=None,
        skip_seat_reset=True,
        in_place=False,
        from_queue=False,
        _worktree_seat=None,
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="without a resolvable review base",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert not (repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR).exists()


def test_public_shared_read_only_mode_is_rejected_for_writers(tmp_path: Path) -> None:
    parser = goalflight_dispatch._build_launch_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--worktree", "shared-read-only"])

    args = SimpleNamespace(
        worktree="shared-read-only",
        read_only=False,
        project_root=str(tmp_path),
    )
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="internal mode and requires --read-only",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)


def test_dispatch_admission_reaps_read_only_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "reap_read_only_worktrees",
        lambda project_root: calls.append(project_root),
    )
    args = SimpleNamespace(
        agent="codex",
        shape="bash",
        read_only=False,
        worker=[],
        project_root=str(repo),
        cwd=None,
        worktree="off",
        in_place=False,
        dispatch_id="writer-admission",
        _worktree_seat=None,
    )

    assert goalflight_dispatch._bind_dispatch_worktree(args) is None
    assert calls == [repo.resolve()]


@pytest.mark.parametrize("agent", ["codex", "cursor"])
def test_admit_backstop_refuses_project_root_after_bind_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = SimpleNamespace(
        agent=agent,
        project_root=str(repo),
        cwd=None,
        worktree="HEAD",
        read_only=False,
        in_place=False,
        dispatch_id="root-backstop",
        capacity_wait_s=0,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        dispatch_warnings=[],
    )
    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", lambda _args: None)
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_worktree_occupancy",
        lambda _args: None,
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused, match="project root"
    ):
        goalflight_dispatch._admit_dispatch_worktree(args)

    assert args._worktree_seat_refused is True
    assert args._worktree_admission_refused is True
    assert not (repo / "worktrees").exists()


def test_bind_project_root_cwd_without_in_place_refuses_writer(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    args = SimpleNamespace(
        worktree="HEAD",
        project_root=str(repo),
        cwd=str(repo),
        read_only=False,
        in_place=False,
        dispatch_id="root-cwd-without-in-place",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused, match="project root"
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)


def test_bind_inherited_lock_fd_outside_managed_seat_refuses_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = SimpleNamespace(
        worktree="HEAD",
        project_root=str(repo),
        cwd=None,
        read_only=False,
        in_place=False,
        dispatch_id="inherited-root-bind",
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
    )
    inherited_fd_path = tmp_path / "unrelated-open-fd"
    with inherited_fd_path.open("w", encoding="utf-8") as inherited_fd:
        monkeypatch.setenv(
            goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
            str(inherited_fd.fileno()),
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeCwdRefused, match="project root"
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)


def test_admit_refuses_writer_subdirectory_inside_checkout_with_worktree_off(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    source_dir = repo / "src"
    source_dir.mkdir()
    alias = tmp_path / "src-alias"
    alias.symlink_to(source_dir, target_is_directory=True)
    args = SimpleNamespace(
        agent="codex-acp",
        shape="acp",
        worker=[],
        project_root=str(repo),
        cwd=str(alias),
        worktree="off",
        read_only=False,
        in_place=False,
        dispatch_id="acp-checkout-subdir",
        capacity_wait_s=0,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        dispatch_warnings=[],
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="inside the project checkout",
    ):
        goalflight_dispatch._admit_dispatch_worktree(args)


def test_admit_restores_capacity_lease_flag_when_bind_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = SimpleNamespace(
        capacity_wait_s=0,
        _worktree_capacity_lease_active=False,
    )

    def refuse(_args):
        raise goalflight_worktree_pool.WorktreeCwdRefused("test refusal")

    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", refuse)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="test refusal",
    ):
        goalflight_dispatch._admit_dispatch_worktree(args)

    assert args._worktree_capacity_lease_active is False
    assert not hasattr(args, "_worktree_capacity_deadline")


def test_direct_acp_runner_emits_permanent_worktree_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import io
    import goalflight_acp_run

    repo = _make_repo(tmp_path)
    source_dir = repo / "src"
    source_dir.mkdir()

    async def refuse_from_admission(cfg):
        try:
            goalflight_dispatch._admit_dispatch_worktree(cfg)
        except goalflight_worktree_pool.WorktreeCwdRefused as exc:
            return {
                "dispatch_id": cfg.dispatch_id,
                "state": "failed_worktree",
                "error": str(exc),
            }
        raise AssertionError("worktree-off writer inside checkout was admitted")

    def run_immediately(coro):
        try:
            coro.send(None)
        except StopIteration as result:
            return result.value
        raise AssertionError("refusal path unexpectedly awaited ACP work")

    monkeypatch.setattr(goalflight_acp_run, "run_acp_dispatch", refuse_from_admission)
    monkeypatch.setattr(asyncio, "run", run_immediately)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = goalflight_acp_run.main(
            [
                "--agent",
                "codex",
                "--cwd",
                str(source_dir),
                "--dispatch-id",
                "direct-acp-subdir-refused",
                "--prompt-text",
                "test",
                "--json",
            ]
        )

    stdout = output.getvalue()
    assert result == 64, stdout
    refusal = next(
        line for line in stdout.splitlines() if line.startswith("DISPATCH-REFUSED ")
    )
    assert '"permanent": true' in refusal, refusal
    assert "inside the project checkout" in refusal, refusal


def test_acp_in_place_nested_cwd_is_rejected_with_inherited_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    args = SimpleNamespace(
        agent="codex-acp",
        shape="acp",
        read_only=False,
        worker=[],
        project_root=str(repo),
        cwd=str(nested),
        worktree="off",
        worktree_root=None,
        dispatch_id="acp-nested-in-place",
        controller_label=None,
        skip_seat_reset=False,
        in_place=True,
        from_queue=False,
        capacity_wait_s=0,
        dispatch_warnings=[],
        _worktree_seat=None,
    )

    inherited_path = tmp_path / "inherited-lock-fd"
    with inherited_path.open("w+", encoding="utf-8") as inherited_fd:
        monkeypatch.setenv(
            goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
            str(inherited_fd.fileno()),
        )
        with pytest.raises(
            goalflight_worktree_pool.WorktreeCwdRefused,
            match="--in-place",
        ):
            goalflight_dispatch._admit_dispatch_worktree(args)


def test_read_only_resume_pins_checkout_without_worker_cwd_before_waiting(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-parent",
            "state": "blocked",
            "worker_cwd": str(checkout),
            "worktree_path": str(checkout),
            "worktree_base": base,
        }
    )
    old_ns = 1_000_000_000
    os.utime(checkout, ns=(old_ns, old_ns))
    args = SimpleNamespace(
        parent_dispatch_id="readonly-parent",
        dispatch_id="readonly-child",
        agent="codex",
        shape="bash",
        read_only=True,
        cwd=str(checkout),
    )

    goalflight_dispatch._prepare_read_only_resume_binding(args, repo)

    assert args._worktree_path == str(checkout)
    assert args._worktree_id == checkout.name
    assert args._worktree_base_commit == base
    assert checkout.stat().st_mtime_ns > old_ns
    assert goalflight_dispatch._ledger_worker_cwd(args, "waiting_capacity") is None


def test_read_only_resume_rejects_dirty_fallback_before_binding(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-parent",
            "state": "blocked",
            "worker_cwd": str(checkout),
            "worktree_path": str(checkout),
            "worktree_base": base,
        }
    )
    dirty_file = checkout / "resume-dirty.txt"
    dirty_file.write_text("keep me\n", encoding="utf-8")
    args = SimpleNamespace(
        parent_dispatch_id="readonly-parent",
        dispatch_id="readonly-child",
        agent="codex",
        shape="bash",
        read_only=True,
        cwd=str(checkout),
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="read-only resume checkout .* is not clean",
    ):
        goalflight_dispatch._prepare_read_only_resume_binding(args, repo)

    assert dirty_file.read_text(encoding="utf-8") == "keep me\n"
    assert not hasattr(args, "_worktree_path")


def test_read_only_resume_rejects_missing_recorded_base(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-legacy-parent",
            "state": "blocked",
            "worker_cwd": str(checkout),
            "worktree_path": str(checkout),
        }
    )
    args = SimpleNamespace(
        parent_dispatch_id="readonly-legacy-parent",
        dispatch_id="readonly-child",
        agent="codex",
        shape="bash",
        read_only=True,
        cwd=str(checkout),
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="without a resolvable review base",
    ):
        goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
    assert not hasattr(args, "_worktree_path")


def test_read_only_resume_touches_checkout_under_allocation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    goalflight_ledger.write_record(
        {
            "dispatch_id": "readonly-parent",
            "state": "blocked",
            "worker_cwd": str(checkout),
            "worktree_path": str(checkout),
            "worktree_base": base,
        }
    )
    held: list[bool] = []
    real_lock = goalflight_worktree_pool._read_only_allocation_lock

    @contextlib.contextmanager
    def observed_lock(project_root: Path, **kwargs):
        with real_lock(project_root, **kwargs):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    real_utime = goalflight_dispatch.os.utime

    def checked_utime(*args, **kwargs):
        assert held, "resume protection must hold the allocation lock while touching"
        return real_utime(*args, **kwargs)

    monkeypatch.setattr(
        goalflight_worktree_pool, "_read_only_allocation_lock", observed_lock
    )
    monkeypatch.setattr(goalflight_dispatch.os, "utime", checked_utime)
    args = SimpleNamespace(
        parent_dispatch_id="readonly-parent",
        dispatch_id="readonly-child",
        agent="codex",
        shape="bash",
        read_only=True,
        cwd=str(checkout),
    )

    goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
    assert args._worktree_path == str(checkout)


def test_read_only_resume_rejects_dirty_checkout_after_preparation(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "readonly-dirty-parent",
        "state": "blocked",
        "terminal_state": "blocked",
        "project_root": str(repo),
        "worker_cwd": str(checkout),
        "worktree_path": str(checkout),
        "worktree_base": base,
    }
    goalflight_ledger.write_record(record)
    args = SimpleNamespace(
        parent_dispatch_id="readonly-dirty-parent",
        dispatch_id="readonly-dirty-child",
        agent="codex",
        shape="bash",
        read_only=True,
        cwd=str(checkout),
    )

    goalflight_dispatch._prepare_read_only_resume_binding(args, repo)
    (checkout / "late-untracked.txt").write_text("changed after preparation\n")

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="read-only resume checkout .* is not clean",
    ):
        goalflight_dispatch._revalidate_read_only_resume_worktree(
            args,
            resume_plan={"source": {"record": record}},
        )


def test_occupancy_refusal_releases_bound_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    args = SimpleNamespace(
        agent="test-dispatch",
        project_root=str(repo),
        cwd=None,
        worktree="HEAD",
        worktree_root=None,
        dispatch_id="occupancy-refused",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        from_queue=False,
        capacity_wait_s=0,
        dispatch_warnings=[],
        _worktree_seat=None,
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_worktree_incumbent_reason",
        lambda _args: ("live incumbent", None, "starting"),
    )

    with pytest.raises(goalflight_dispatch.DispatchUsageError, match="live incumbent"):
        goalflight_dispatch._admit_dispatch_worktree(args)
    assert args._worktree_seat is None

    record_finished_holder("occupancy-refused")
    _rewrite_test_seat_lock_identity_dead(repo, "s-1")
    replacement = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "after-occupancy-refusal"
    )
    replacement.release()
@pytest.mark.parametrize("legacy", [False, True], ids=["recorded", "legacy"])
@pytest.mark.parametrize("read_only", [False, True], ids=["writable", "read-only"])
def test_resume_refuses_exact_seat_on_foreign_branch_before_skip_reset_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
    read_only: bool,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent"
    child_id = "resume-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    recorded_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "worktree/foreign", recorded_head)
    _git(seat, "checkout", "worktree/foreign")
    parent.release()
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": parent_id,
        "agent": "grok-code",
        "engine": "grok",
        "shape": "bash",
        "state": "blocked",
        "terminal_state": "blocked",
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": f"worktree/{parent_id}",
        "worktree_head": recorded_head,
        "engine_session_id": "00000000-0000-4000-8000-000000000033",
        "dispatch_argv": [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--cwd",
            str(seat),
        ],
    }
    if read_only:
        record["dispatch_argv"].append("--read-only")
    if legacy:
        record.pop("worktree_branch")
        record.pop("worktree_head")
    goalflight_ledger.write_record(record)
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue the worker.\n", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID_SEED", child_id)
    monkeypatch.setattr(goalflight_dispatch, "grok_selected_account", lambda _args: None)
    launched: list[list[str]] = []
    monkeypatch.setattr(
        goalflight_dispatch,
        "main",
        lambda argv=None, **_kwargs: launched.append(list(argv or [])) or 0,
    )

    assert goalflight_dispatch._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 64
    error = capsys.readouterr().err
    assert "actual branch worktree/foreign" in error
    assert f"expected branch worktree/{parent_id}" in error
    if not legacy:
        assert recorded_head in error
    assert launched == []


def test_read_only_resume_revalidates_after_capacity_wait_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(tmp_path / "codex-state"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "readonly-capacity-parent"
    child_id = "readonly-capacity-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    recorded_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "worktree/readonly-foreign", recorded_head)
    parent.release()
    account = "readonly-capacity-seat"
    session_id = "00000000-0000-4000-8000-000000000033"
    _write_grok_resume_source(account, seat, session_id)
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": parent_id,
        "agent": "grok-code",
        "engine": "grok",
        "shape": "bash",
        "account": account,
        "effective_account": account,
        "state": "blocked",
        "terminal_state": "blocked",
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": f"worktree/{parent_id}",
        "worktree_head": recorded_head,
        "engine_session_id": session_id,
        "dispatch_argv": [
            "--agent",
            "grok-code",
            "--shape",
            "bash",
            "--cwd",
            str(seat),
            "--read-only",
        ],
    }
    goalflight_ledger.write_record(record)
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue the worker.\n", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID_SEED", child_id)
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True)
    claim = queue_dir / f"{child_id}.json.claimed-1"
    queue_token = "readonly-capacity-queue-token"
    claim.write_text(
        json.dumps(
            {
                "schema": goalflight_dispatch.DISPATCH_QUEUE_SCHEMA,
                "dispatch_id": child_id,
                "dispatch_argv": [
                    "--agent",
                    "grok-code",
                    "--shape",
                    "bash",
                    "--cwd",
                    str(seat),
                    "--read-only",
                ],
                "queue_launch_token": queue_token,
                "created_at": goalflight_ledger.utc_now(),
                "project_root": str(repo),
                "request": {"cwd": str(seat), "tail": str(tmp_path / "tail")},
            }
        ),
        encoding="utf-8",
    )

    def acquire_capacity(*_args, **_kwargs):
        _git(seat, "checkout", "worktree/readonly-foreign")
        return "lease-readonly-capacity"

    monkeypatch.setattr(goalflight_dispatch, "_account_engine", lambda _agent: None)
    monkeypatch.setattr(goalflight_dispatch, "_validate_before_side_effects", lambda *_a, **_k: {})
    monkeypatch.setattr(goalflight_dispatch, "_validate_os_sandbox_boundary", lambda _args: None)
    monkeypatch.setattr(goalflight_dispatch, "_resolve_launch_account_env", lambda _args: {})
    monkeypatch.setattr(goalflight_dispatch, "_wrap_grok_read_only_os_sandbox", lambda argv, _args: argv)
    monkeypatch.setattr(goalflight_dispatch, "_acquire_capacity", acquire_capacity)
    monkeypatch.setattr(
        goalflight_dispatch.goalflight_capacity,
        "mark_lease_spawning",
        lambda _lease_id: True,
    )
    monkeypatch.setattr(
        goalflight_dispatch.goalflight_capacity,
        "mark_lease_spawn_failed",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(goalflight_dispatch, "_stamp_controller_session", lambda *_a, **_k: {})
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_controller_registration",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(goalflight_dispatch, "_reap_quota_stuck_before_bash_launch", lambda: None)
    def nonpersisting_record_ledger(*_args, **_kwargs):
        return None

    setattr(
        nonpersisting_record_ledger,
        "_goalflight_nonpersisting_seam",
        True,
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_record_ledger",
        nonpersisting_record_ledger,
    )
    monkeypatch.setattr(goalflight_dispatch, "_finish_ledger", lambda *_a, **_k: None)
    monkeypatch.setattr(goalflight_dispatch, "_release_capacity", lambda *_a, **_k: None)
    monkeypatch.setattr(
        goalflight_dispatch.goalflight_cursor,
        "cleanup_dispatch_data",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_attempt_claiming_worker_argv",
        lambda _root, _dispatch_id, argv: (argv, False),
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_materialize_steer_prompt",
        lambda path, *_a, **_k: Path(path),
    )
    real_main = goalflight_dispatch.main

    def main_with_queue_claim(argv=None, **kwargs):
        resume_plan = kwargs["resume_plan"]
        resume_plan["args"].from_queue = True
        resume_plan["args"].queue_claim_path = str(claim)
        resume_plan["args"].queue_launch_token = queue_token
        return real_main(argv, **kwargs)

    monkeypatch.setattr(goalflight_dispatch, "main", main_with_queue_claim)
    spawned: list[object] = []
    monkeypatch.setattr(
        goalflight_dispatch,
        "_spawn_daemonized_process",
        lambda *_a, **_k: spawned.append(True) or 42001,
    )

    assert goalflight_dispatch._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 64
    error = capsys.readouterr().err
    assert "actual branch worktree/readonly-foreign" in error
    assert f"expected branch worktree/{parent_id}" in error
    assert spawned == []
    restored_entry = json.loads(claim.read_text(encoding="utf-8"))
    assert not restored_entry.get("queue_worker_spawn_intent")
    for key in (
        "queue_launch_started",
        "queue_launch_started_at",
        "queue_launcher_pid",
        "queue_launcher_identity",
    ):
        restored_entry.pop(key, None)
    claim.write_text(json.dumps(restored_entry), encoding="utf-8")
    restored, _decision = goalflight_dispatch._bounded_restore_claim(
        claim, restored_entry, queue_dir
    )
    assert restored is True
    assert (queue_dir / f"{child_id}.json").exists()
    assert not claim.exists()


def test_resume_validates_unmanaged_git_checkout_identity_and_head(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    checkout = tmp_path / "readonly-checkout"
    _git(repo, "worktree", "add", "--detach", str(checkout), "HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    record = {"project_root": str(repo), "worktree_base": head}

    goalflight_dispatch._validate_resume_worktree_source(
        "unmanaged-resume", record, checkout
    )
    _git(checkout, "checkout", "-b", "foreign")
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match=r"actual branch foreign.*expected branch HEAD",
    ):
        goalflight_dispatch._validate_resume_worktree_source(
            "unmanaged-resume", record, checkout
        )


def test_resume_in_place_accepts_recorded_linked_worktree_without_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    monkeypatch.chdir(repo)

    args = SimpleNamespace(
        worktree=None,
        parent_dispatch_id="in-place-parent",
        dispatch_id="in-place-child",
        cwd=str(linked),
        skip_seat_reset=True,
        in_place=True,
        controller_label=None,
        _worktree_seat=None,
    )

    assert goalflight_dispatch._bind_dispatch_worktree(args) is None
    assert args._worktree_seat is None
    assert Path(args.cwd).resolve() == linked.resolve()
    assert not (repo / "worktrees").exists()

    args.skip_seat_reset = False
    with pytest.raises(goalflight_worktree_pool.WorktreeCwdRefused):
        goalflight_dispatch._bind_dispatch_worktree(args)

    other_root = tmp_path / "other"
    other_root.mkdir()
    other_repo = _make_repo(other_root)
    args.cwd = str(other_repo)
    args.skip_seat_reset = True
    monkeypatch.setattr(goalflight_dispatch, "_project_root", lambda _args: repo)
    with pytest.raises(goalflight_worktree_pool.WorktreeCwdRefused):
        goalflight_dispatch._bind_dispatch_worktree(args)


def test_resume_refuses_a_recorded_seat_reclaimed_by_another_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    finish_seat_holder(parent)
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-parent",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": "worktree/resume-parent",
            "worktree_head": _git(repo, "rev-parse", "worktree/resume-parent"),
        }
    )
    try:
        args.capacity_wait_s = 0.05
        # A pinned seat waits through the positive budget before refusing.
        clock = _drive_dispatch_deadline(monkeypatch)
        bind_probes = _record_bind_probe_durations(monkeypatch)
        with pytest.raises(
            goalflight_dispatch.DispatchUsageError,
            match=r"worktree seat s-1; wait for a seat: holder=reclaimer;.*holder_liveness=unknown",
        ):
            goalflight_dispatch._admit_dispatch_worktree(args)
        assert len(bind_probes) == 2
        assert len(clock.requested_sleeps) == 1
        assert sum(clock.requested_sleeps) == pytest.approx(args.capacity_wait_s)
        assert sum(clock.requested_sleeps) <= args.capacity_wait_s
        assert clock.elapsed == pytest.approx(args.capacity_wait_s)
    finally:
        reclaimer.release()

    _write_seat_holder_record(
        "reclaimer",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id="resume-parent",
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert resumed.path == seat
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/resume-parent"
        )
        assert _git(seat, "rev-parse", "HEAD") == _git(
            repo, "rev-parse", "worktree/resume-parent"
        )
    finally:
        resumed.release()


def test_resume_does_not_adopt_terminal_non_parent_from_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, seat)

    first_resume = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "resume-one",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    first_resume.release()
    _write_terminal_seat_record(
        "resume-one", repo, seat, parent_dispatch_id="resume-parent"
    )
    dirty_file = seat / "non-parent-terminal-dirty.txt"
    dirty_file.write_text("leave the unrelated terminal checkout intact\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    claim = tmp_path / "resume-two.claimed"
    claim.write_text(
        json.dumps(
            {
                "dispatch_id": "resume-two",
                "queue_launch_token": "resume-two-token",
                "state": "claimed",
                "dispatch_argv": ["--agent", "test-dispatch"],
            }
        ),
        encoding="utf-8",
    )
    claim_before = claim.read_bytes()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-two",
        cwd=str(seat.relative_to(repo)),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        worktree_pin_holder="resume-parent",
        from_queue=True,
        queue_claim_path=str(claim),
        queue_launch_token="resume-two-token",
        capacity_wait_s=0.2,
    )
    cwd_before = args.cwd
    pin_before = args.worktree_pin_holder
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    with pytest.raises(goalflight_worktree_pool.WorktreeSeatReclaimed):
        goalflight_dispatch._admit_dispatch_worktree(args)
    assert args.cwd == cwd_before
    assert args.worktree_pin_holder == pin_before
    assert claim.read_bytes() == claim_before
    assert _lock_dispatch_id(lock_path) == "resume-one"
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "leave the unrelated terminal checkout intact\n"


def test_resume_refuses_unresolvable_terminal_holder_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    finish_seat_holder(parent)
    _write_terminal_seat_record("resume-parent", repo, seat)
    unresolved = goalflight_worktree_pool.acquire_worktree_seat(repo, "missing-row")
    unresolved.release()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        capacity_wait_s=600,
    )
    clock = _drive_dispatch_deadline(monkeypatch)
    bind_probes = _record_bind_probe_durations(monkeypatch)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatReclaimed,
        match=r"reclaimed by missing-row; expected recorded holder resume-parent",
    ) as exc_info:
        goalflight_dispatch._admit_dispatch_worktree(args)
    # Terminal reclaim mismatches are non-retryable despite a positive budget.
    assert clock.requested_sleeps == []
    assert sum(clock.requested_sleeps) <= args.capacity_wait_s
    assert len(bind_probes) == 1
    assert exc_info.value.flock_held is False
    assert unresolved.keep_ref in str(exc_info.value)


def test_resume_parent_validation_rejects_unreadable_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(_parent: str):
        return {"state": "unreadable"}

    monkeypatch.setattr(goalflight_ledger, "read_record", unreadable)
    assert not goalflight_dispatch._resume_parent_is_terminal_and_exited(
        "resume-parent"
    )


def test_resume_unresolvable_lineage_holder_never_reuses_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "unresolvable-holder",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
        }
    )
    assert not goalflight_dispatch._resume_parent_is_terminal_and_exited(
        "unresolvable-holder"
    )


def test_resume_never_steals_live_holder_in_same_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    finish_seat_holder(parent)
    _write_terminal_seat_record("resume-parent", repo, seat)
    live = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "resume-one",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    identity = goalflight_worktree_pool.goalflight_compat.process_start_identity(
        os.getpid()
    )
    assert identity and identity.get("start_token")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-one",
            "parent_dispatch_id": "resume-parent",
            "state": "running",
            "project_root": str(repo),
            "worker_pid": os.getpid(),
            "worker_identity": identity,
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_id": seat.name,
            "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
            "worktree_head": _git(seat, "rev-parse", "HEAD"),
        }
    )

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-two",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        capacity_wait_s=0.05,
    )
    # The pinned-seat retry sleeps min(0.05, remaining) at goalflight_dispatch.py:4181,
    # so one 50 ms budget gives one retry before the final refusal probe.
    clock = _drive_dispatch_deadline(monkeypatch)
    bind_probes = _record_bind_probe_durations(monkeypatch)
    flock_operations = _record_flock_operations(monkeypatch)
    try:
        with pytest.raises(
            goalflight_dispatch.DispatchUsageError,
            match=r"worktree seat s-1; wait for a seat: holder=resume-one;.*holder_liveness=live",
        ):
            goalflight_dispatch._admit_dispatch_worktree(args)
        assert len(bind_probes) == 2
        _assert_nonblocking_flock_operations(flock_operations)
        assert len(clock.requested_sleeps) == 1
        assert sum(clock.requested_sleeps) == pytest.approx(args.capacity_wait_s)
        assert sum(clock.requested_sleeps) <= args.capacity_wait_s
        assert clock.elapsed == pytest.approx(args.capacity_wait_s)
    finally:
        live.release()


@pytest.mark.parametrize("use_resume_context", [False, True])
def test_resume_ignores_broken_codex_lineage_with_different_valid_session(
    tmp_path: Path,
    use_resume_context: bool,
) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    repo = _make_repo(source_parent)
    session_id = "00000000-0000-4000-8000-000000000025"
    source = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "resume-source",
        "agent": "codex",
        "engine": "codex",
        "state": "complete",
        "terminal_state": "complete",
        "project_root": str(repo),
        "codex_session_id": session_id,
    }
    unrelated = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "unrelated-live-candidate",
        "parent_dispatch_id": "example-resume-1",
        "agent": "codex",
        "engine": "codex",
        "state": "running",
        "project_root": str(repo),
        "codex_session_id": "00000000-0000-4000-8000-000000000024",
    }
    goalflight_ledger.write_record(source)
    goalflight_ledger.write_record(unrelated)

    def assert_resume_is_not_poisoned() -> None:
        records = goalflight_ledger.read_records()
        resume_context = (
            goalflight_dispatch._resume_validation_context(records)
            if use_resume_context else None
        )
        goalflight_dispatch._assert_no_nonterminal_codex_resume_child(
            dispatch_id="resume-source",
            session_id=session_id,
            resume_context=resume_context,
        )

    assert_resume_is_not_poisoned()


@pytest.mark.parametrize("candidate_session_id", [None, "not-a-session"])
@pytest.mark.parametrize("use_resume_context", [False, True])
def test_resume_fails_closed_for_broken_lineage_without_valid_session(
    tmp_path: Path,
    candidate_session_id: str | None,
    use_resume_context: bool,
) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    repo = _make_repo(source_parent)
    session_id = "00000000-0000-4000-8000-000000000025"
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-source",
            "agent": "codex",
            "engine": "codex",
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(repo),
            "codex_session_id": session_id,
        }
    )
    candidate = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "unknown-session-candidate",
        "parent_dispatch_id": "missing-ancestor",
        "agent": "codex",
        "engine": "codex",
        "state": "running",
        "project_root": str(repo),
    }
    if candidate_session_id is not None:
        candidate["codex_session_id"] = candidate_session_id
    goalflight_ledger.write_record(candidate)
    records = goalflight_ledger.read_records()
    resume_context = (
        goalflight_dispatch._resume_validation_context(records)
        if use_resume_context else None
    )

    with pytest.raises(
        (goalflight_worktree_pool.WorktreeCwdRefused,
         goalflight_dispatch.DispatchUsageError),
        match="missing or unreadable lineage ancestor",
    ):
        goalflight_dispatch._assert_no_nonterminal_codex_resume_child(
            dispatch_id="resume-source",
            session_id=session_id,
            resume_context=resume_context,
        )


@pytest.mark.parametrize("use_resume_context", [False, True])
def test_resume_still_blocks_broken_codex_candidate_with_same_session(
    tmp_path: Path,
    use_resume_context: bool,
) -> None:
    source_parent = tmp_path / "source"
    source_parent.mkdir()
    repo = _make_repo(source_parent)
    other_parent = tmp_path / "other"
    other_parent.mkdir()
    other_repo = _make_repo(other_parent)
    session_id = "00000000-0000-4000-8000-000000000025"
    source = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "resume-source",
        "agent": "codex",
        "engine": "codex",
        "state": "complete",
        "terminal_state": "complete",
        "project_root": str(repo),
        "codex_session_id": session_id,
    }
    candidate = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": "same-session-candidate",
        "parent_dispatch_id": "missing-ancestor",
        "agent": "codex",
        "engine": "codex",
        "state": "running",
        "project_root": str(other_repo),
        "codex_session_id": session_id,
    }
    goalflight_ledger.write_record(source)
    goalflight_ledger.write_record(candidate)
    records = goalflight_ledger.read_records()
    resume_context = (
        goalflight_dispatch._resume_validation_context(records)
        if use_resume_context else None
    )

    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="already has non-terminal resume child same-session-candidate",
    ):
        goalflight_dispatch._assert_no_nonterminal_codex_resume_child(
            dispatch_id="resume-source",
            session_id=session_id,
            resume_context=resume_context,
        )


def test_worktree_wait_announces_retry_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cwd = tmp_path / "s-1"
    cwd.mkdir()
    args = SimpleNamespace(
        cwd=str(cwd),
        parent_dispatch_id="expected-parent",
        worktree_pin_holder=None,
        capacity_wait_s=1,
        tail=str(tmp_path / "dispatch.tail"),
        dispatch_warnings=[],
        _worktree_occupancy_checked_path=str(cwd.resolve()),
        _worktree_occupancy_warning=None,
        _worktree_seat=None,
        in_place=True,
    )
    attempts = 0

    def bind(_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise goalflight_worktree_pool.WorktreeSeatUnavailable(
                "worktree s-1 is held: s-1=current-holder"
            )
        return None

    monkeypatch.setattr(goalflight_dispatch, "_bind_dispatch_worktree", bind)
    monkeypatch.setattr(goalflight_dispatch.time, "sleep", lambda _seconds: None)

    try:
        assert goalflight_dispatch._admit_dispatch_worktree(args) is None
        notice = capsys.readouterr().err
        assert "waiting for worktree seat s-1" in notice
        assert "current_holder=current-holder" in notice
        assert "expected_holder=expected-parent" in notice
        assert "deadline=" in notice
        assert "waiting for worktree seat s-1" in (
            tmp_path / "dispatch.tail"
        ).read_text(encoding="utf-8")
    finally:
        goalflight_dispatch._release_worktree_occupancy_lock(args)


def test_capacity_lease_signal_guard_releases_on_operator_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, object] = {}
    signal_calls: list[tuple[int, object]] = []
    released: list[tuple[str, str, str]] = []

    def fake_signal(signum: int, handler: object) -> object:
        signal_calls.append((signum, handler))
        installed.setdefault(signum, handler)
        return signal.SIG_DFL

    monkeypatch.setattr(goalflight_dispatch.signal, "signal", fake_signal)
    monkeypatch.setattr(
        goalflight_dispatch,
        "_release_capacity",
        lambda lease_id, state, reason: released.append((lease_id, state, reason)),
    )

    restore = goalflight_dispatch._install_capacity_lease_signal_guard("lease-1")
    assert restore is not None
    for signum in (signal.SIGTERM, signal.SIGINT):
        with pytest.raises(SystemExit) as exc_info:
            installed[signum](signum, None)
        assert exc_info.value.code == 128 + signum

    assert released == [
        ("lease-1", "failed", "signal-SIGTERM"),
        ("lease-1", "failed", "signal-SIGINT"),
    ]
    restore()
    assert len(signal_calls) == 4


def test_resume_unknown_lock_refuses_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-unknown-parent"
    child_id = "resume-unknown-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    head = _git(repo, "rev-parse", "worktree/resume-unknown-parent")
    parent.release()
    goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name).write_text(
        "\n", encoding="utf-8"
    )
    session_id = "00000000-0000-4000-8000-000000000034"
    _write_grok_resume_source("old-seat", seat, session_id)
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "agent": "grok-code",
            "engine": "grok",
            "shape": "bash",
            "account": "old-seat",
            "effective_account": "old-seat",
            "engine_session_id": session_id,
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": "worktree/resume-unknown-parent",
            "worktree_head": head,
            "dispatch_argv": [
                "--agent",
                "grok-code",
                "--shape",
                "bash",
                "--cwd",
                str(seat),
                "--worktree",
                "HEAD",
                "--account",
                "old-seat",
            ],
        }
    )
    prompt = tmp_path / "resume-unknown.md"
    prompt.write_text("Continue without resetting the seat.\n", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID_SEED", child_id)
    monkeypatch.setattr(goalflight_dispatch, "_resolve_launch_account_env", lambda _args: {})
    monkeypatch.setattr(goalflight_dispatch, "_acquire_capacity", lambda *_a, **_k: "lease-unknown")
    monkeypatch.setattr(goalflight_dispatch, "_release_capacity", lambda *_a, **_k: None)
    monkeypatch.setattr(goalflight_dispatch, "_stamp_controller_session", lambda *_a, **_k: {})
    monkeypatch.setattr(
        goalflight_dispatch,
        "_prepare_attempt_controller_registration",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(goalflight_dispatch, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(goalflight_dispatch, "_mark_queue_claim_launch_started", lambda _args: None)

    rc = goalflight_dispatch._cmd_resume(
        [
            parent_id,
            "--prompt-file",
            str(prompt),
            "--account",
            "old-seat",
            "--unregistered-forced",
        ]
    )

    assert rc == 2
    assert "unknown ownership" in capsys.readouterr().err
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        "worktree/resume-unknown-parent"
    )
    assert (seat / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_resume_reseats_a_recycled_worktree_on_the_parent_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    committed_head = _commit_in(old_seat, "worker committed before stopping")
    assert committed_head != recorded_head
    parent.release()
    record_finished_holder("resume-parent")
    _rewrite_test_seat_lock_identity_dead(repo, old_seat.name)
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "resume-parent-other"
    )
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, old_seat)
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        _assert_pinned_resume_refusal(
            args, repo, old_seat, "resume-parent-other", monkeypatch
        )
        assert _git(old_seat, "rev-parse", "HEAD") != committed_head
    finally:
        reclaimer.release()


def test_resume_recycled_branch_divergence_is_refused_before_new_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    parent.release()
    _write_terminal_seat_record("resume-parent", repo, old_seat)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo),
        input="diverged\n",
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "mktree"],
        cwd=str(repo),
        input=f"100644 blob {blob}\tdiverged.txt\n",
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    diverged = _git(repo, "commit-tree", tree)
    _git(
        repo,
        "update-ref",
        "refs/heads/worktree/resume-parent",
        diverged,
        recorded_head,
    )
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "reclaimer",
        occupy_path=old_seat,
        reset=False,
        expected_prior_dispatch_id="resume-parent",
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        _assert_pinned_resume_refusal(args, repo, old_seat, "reclaimer", monkeypatch)
    finally:
        reclaimer.release()


def test_resume_recycled_branch_accepts_root_resume_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    root = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-root")
    old_seat = root.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-root")
    root.release()
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-root",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
        }
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-child",
            "parent_dispatch_id": "resume-root",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_id": old_seat.name,
            "worktree_path": str(old_seat),
            "worktree_branch": "worktree/resume-root",
            "worktree_head": recorded_head,
        }
    )
    _rewrite_test_seat_lock_identity_dead(repo, old_seat.name)
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-child",
        dispatch_id="resume-grandchild",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    try:
        _assert_pinned_resume_refusal(args, repo, old_seat, "reclaimer", monkeypatch)
    finally:
        reclaimer.release()


@pytest.mark.parametrize(
    "case, expected",
    [
        ("cycle", "lineage cycle"),
        ("missing", "missing or unreadable lineage ancestor"),
        ("missing-project-root", "missing a project root"),
    ],
)
def test_resume_rejects_inconsistent_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)

    def record(dispatch_id: str, *, parent: str | None = None, root: Path = repo, branch: str | None = None) -> dict:
        value = {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(root),
        }
        if parent:
            value["parent_dispatch_id"] = parent
        if branch:
            value["worktree_branch"] = branch
        return value

    if case == "cycle":
        goalflight_ledger.write_record(record("lineage-child", parent="lineage-root"))
        goalflight_ledger.write_record(record("lineage-root", parent="lineage-child"))
    elif case == "missing":
        goalflight_ledger.write_record(record("lineage-child", parent="missing"))
    elif case == "missing-project-root":
        goalflight_ledger.write_record(record("lineage-child", parent="lineage-root"))
        root_record = record("lineage-root")
        root_record.pop("project_root")
        goalflight_ledger.write_record(root_record)
    else:
        goalflight_ledger.write_record(
            record("lineage-child", parent="lineage-root")
        )
        goalflight_ledger.write_record(
            record("lineage-root", branch="worktree/not-the-root")
        )

    with pytest.raises(goalflight_worktree_pool.WorktreeCwdRefused, match=expected):
        goalflight_dispatch._resume_lineage_dispatch_ids("lineage-child")


@pytest.mark.parametrize(
    "case, expected",
    [
        ("different-project-root", "different project root"),
        ("wrong-root-branch", "does not belong to root dispatch lineage-root"),
    ],
)
def test_resume_command_rejects_inconsistent_lineage_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "lineage-parent"
    root_id = "lineage-root"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    head = _git(repo, "rev-parse", f"worktree/{parent_id}")
    parent.release()
    if case == "different-project-root":
        other_root = tmp_path / "other"
        other_root.mkdir()
        root_project = _make_repo(other_root)
        root_branch = f"worktree/{root_id}"
    else:
        root_project = repo
        root_branch = "worktree/not-the-root"
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": root_id,
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(root_project),
            "worktree_branch": root_branch,
        }
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "parent_dispatch_id": root_id,
            "agent": "grok-code",
            "engine": "grok",
            "shape": "bash",
            "account": "default",
            "engine_session_id": "00000000-0000-4000-8000-000000000034",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_branch": f"worktree/{root_id}",
            "worktree_head": head,
            "dispatch_argv": [
                "--agent",
                "grok-code",
                "--shape",
                "bash",
                "--cwd",
                str(seat),
                "--worktree",
                "HEAD",
            ],
        }
    )
    prompt = tmp_path / "lineage-resume.md"
    prompt.write_text("Continue the existing worker.\n", encoding="utf-8")
    child_id = f"{case}-resume-child"
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID_SEED", child_id)
    monkeypatch.setattr(goalflight_dispatch, "_validate_before_side_effects", lambda *_args: {})
    monkeypatch.setattr(goalflight_dispatch, "grok_selected_account", lambda _args: None)

    assert goalflight_dispatch._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 64
    assert expected in capsys.readouterr().err
    assert not goalflight_ledger.record_path(child_id).exists()
    assert not (
        goalflight_dispatch._dispatch_base_dir() / ".dispatch-ids" / f"{child_id}.json"
    ).exists()


@pytest.mark.parametrize("reclaimed_for", ["resume-parent", "other-parent"])
def test_resume_recycled_dirty_state_uses_reclaim_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reclaimed_for: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    (old_seat / "unfinished.txt").write_text("preserve this\n", encoding="utf-8")
    parent.release()
    record_finished_holder("resume-parent")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    assert reclaimer.quarantine_branch
    if reclaimed_for == "other-parent":
        _git(repo, "update-ref", "-d", f"refs/heads/{reclaimer.quarantine_branch}")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-parent",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_path": str(old_seat),
            "worktree_branch": "worktree/resume-parent",
            "worktree_head": recorded_head,
        }
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "reclaimer",
            "agent": "codex",
            "engine": "codex",
            "state": "starting",
            "terminal_state": "unknown",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_path": str(old_seat),
            "worktree_quarantine_ref": reclaimer.quarantine_branch,
            "worktree_reclaimed_dispatch_id": reclaimed_for,
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        _assert_pinned_resume_refusal(args, repo, old_seat, "reclaimer", monkeypatch)
        assert (old_seat / "unfinished.txt").exists() is False
    finally:
        reclaimer.release()


def test_resume_restores_recycled_dirty_tree_in_place_without_moving_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    original_head = _git(seat, "rev-parse", "HEAD")
    tracked_bytes = b"dirty\x00tracked\r\n"
    untracked_bytes = b"new\x00untracked\xff\n"
    (seat / "tracked.txt").write_bytes(tracked_bytes)
    untracked = seat / "new-directory" / "untracked.bin"
    untracked.parent.mkdir()
    untracked.write_bytes(untracked_bytes)
    original_status = _git(
        seat, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    parent.release()
    _write_seat_holder_record(
        parent_id,
        repo,
        parent,
        state="blocked",
        ended_at="2026-01-01T00:00:00+00:00",
    )

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    assert reclaimer.path == seat
    assert reclaimer.keep_ref
    assert reclaimer.quarantine_branch
    assert reclaimer.reclaimed_dispatch_id == parent_id
    quarantine_ref = f"refs/heads/{reclaimer.quarantine_branch}"
    keep_oid = _git(repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}")
    quarantine_oid = _git(repo, "rev-parse", "--verify", f"{quarantine_ref}^{{commit}}")
    _write_seat_holder_record(
        "reclaimer",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert resumed.path == seat
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{parent_id}"
        )
        assert _git(seat, "rev-parse", "HEAD") == original_head
        assert (seat / "tracked.txt").read_bytes() == tracked_bytes
        assert untracked.read_bytes() == untracked_bytes
        assert _git(seat, "status", "--porcelain=v1", "-z", "--untracked-files=all") == (
            original_status
        )
        assert _git(repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}") == keep_oid
        assert _git(repo, "rev-parse", "--verify", f"{quarantine_ref}^{{commit}}") == quarantine_oid
        assert not (seat.parent / "s-2").exists()
    finally:
        resumed.release()


def test_resume_uses_latest_reclaimer_refs_over_older_lineage_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    root_id = "resume-root"
    parent_id = "resume-child"
    root = goalflight_worktree_pool.acquire_worktree_seat(repo, root_id)
    seat = root.path
    original_head = _git(seat, "rev-parse", "HEAD")
    root.release()
    _write_seat_holder_record(
        root_id,
        repo,
        root,
        state="complete",
        ended_at="2026-01-01T00:00:00+00:00",
    )

    stale_keep_ref = f"refs/goalflight/keep/{root_id}/head"
    stale_quarantine_ref = (
        f"refs/heads/goalflight/quarantine/s-1-{root_id}-"
        "20000101T000000000000Z"
    )
    (seat / "stale-snapshot.txt").write_text("old snapshot\n", encoding="utf-8")
    _git(seat, "add", "stale-snapshot.txt")
    stale_tree = _git(seat, "write-tree")
    stale_commit = _git(
        repo, "commit-tree", stale_tree, "-p", original_head, "-m", "stale snapshot"
    )
    _git(repo, "update-ref", stale_keep_ref, original_head)
    _git(repo, "update-ref", stale_quarantine_ref, stale_commit)
    _git(seat, "reset", "--hard", original_head)

    child = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        parent_id,
        base=original_head,
        branch=f"worktree/{root_id}",
        reset=False,
        occupy_path=seat,
        expected_prior_dispatch_id=root_id,
    )
    tracked_bytes = b"latest tracked\x00bytes\r\n"
    untracked_bytes = b"latest\x00untracked\xff\n"
    (seat / "tracked.txt").write_bytes(tracked_bytes)
    untracked = seat / "latest-directory" / "latest.bin"
    untracked.parent.mkdir()
    untracked.write_bytes(untracked_bytes)
    original_status = _git(
        seat, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    child.release()
    _write_seat_holder_record(
        parent_id,
        repo,
        child,
        state="blocked",
        ended_at="2026-01-02T00:00:00+00:00",
        parent_dispatch_id=root_id,
        worktree_keep_ref=stale_keep_ref,
        worktree_quarantine_ref=stale_quarantine_ref,
    )

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    assert reclaimer.path == seat
    assert reclaimer.keep_ref
    assert reclaimer.quarantine_branch
    assert reclaimer.reclaimed_dispatch_id == parent_id
    current_quarantine_ref = f"refs/heads/{reclaimer.quarantine_branch}"
    current_keep_oid = _git(
        repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}"
    )
    current_quarantine_oid = _git(
        repo, "rev-parse", "--verify", f"{current_quarantine_ref}^{{commit}}"
    )
    stale_keep_oid = _git(repo, "rev-parse", "--verify", f"{stale_keep_ref}^{{commit}}")
    stale_quarantine_oid = _git(
        repo, "rev-parse", "--verify", f"{stale_quarantine_ref}^{{commit}}"
    )
    _write_seat_holder_record(
        "reclaimer",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-grandchild",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert resumed.path == seat
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{root_id}"
        )
        assert _git(seat, "rev-parse", "HEAD") == original_head
        assert (seat / "tracked.txt").read_bytes() == tracked_bytes
        assert untracked.read_bytes() == untracked_bytes
        assert not (seat / "stale-snapshot.txt").exists()
        assert _git(
            seat, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ) == original_status
        assert _git(repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}") == current_keep_oid
        assert _git(repo, "rev-parse", "--verify", f"{current_quarantine_ref}^{{commit}}") == current_quarantine_oid
        assert _git(repo, "rev-parse", "--verify", f"{stale_keep_ref}^{{commit}}") == stale_keep_oid
        assert _git(repo, "rev-parse", "--verify", f"{stale_quarantine_ref}^{{commit}}") == stale_quarantine_oid
    finally:
        resumed.release()


def test_resume_refuses_live_recycled_seat_and_names_recovery_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    original_head = _git(seat, "rev-parse", "HEAD")
    (seat / "unfinished.txt").write_text("keep in quarantine\n", encoding="utf-8")
    parent.release()
    _write_seat_holder_record(
        parent_id,
        repo,
        parent,
        state="blocked",
        ended_at="2026-01-01T00:00:00+00:00",
    )

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    assert reclaimer.path == seat
    assert reclaimer.quarantine_branch
    quarantine_ref = f"refs/heads/{reclaimer.quarantine_branch}"
    keep_oid = _git(repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}")
    quarantine_oid = _git(repo, "rev-parse", "--verify", f"{quarantine_ref}^{{commit}}")
    _write_seat_holder_record(
        "reclaimer",
        repo,
        reclaimer,
        state="running",
        live=True,
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    lock_before = lock_path.read_bytes()
    current_branch = _git(seat, "rev-parse", "--abbrev-ref", "HEAD")
    current_head = _git(seat, "rev-parse", "HEAD")
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        capacity_wait_s=0.01,
    )

    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        message = str(exc_info.value)
        assert "holder=reclaimer" in message
        assert "flock_liveness=held" in message
        assert reclaimer.keep_ref in message
        assert quarantine_ref in message
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == current_branch
        assert _git(seat, "rev-parse", "HEAD") == current_head
        assert current_head != original_head or current_branch != f"worktree/{parent_id}"
        assert lock_path.read_bytes() == lock_before
        assert _git(repo, "rev-parse", "--verify", f"{reclaimer.keep_ref}^{{commit}}") == keep_oid
        assert _git(repo, "rev-parse", "--verify", f"{quarantine_ref}^{{commit}}") == quarantine_oid
        assert not (seat.parent / "s-2").exists()
    finally:
        reclaimer.release()


def test_resume_recycled_dirty_state_with_unresolved_reclaimer_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    (old_seat / "unfinished.txt").write_text("preserve this\n", encoding="utf-8")
    parent.release()
    record_finished_holder("resume-parent")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    reclaimer_keep_ref = reclaimer.keep_ref
    reclaimer_quarantine_ref = f"refs/heads/{reclaimer.quarantine_branch}"
    reclaimer.release()
    # Simulate a crash after the reclaim ref was published but before the
    # reclaimer could publish its ledger row.
    old = "2000-01-01T00:00:00+00:00"
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-parent",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_path": str(old_seat),
            "worktree_branch": "worktree/resume-parent",
            "worktree_head": recorded_head,
            "ended_at": old,
            "updated_at": old,
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
        capacity_wait_s=0.2,
    )

    clock = _drive_dispatch_deadline(monkeypatch)
    bind_probes = _record_bind_probe_durations(monkeypatch)
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatReclaimed,
        match=r"reclaimed by reclaimer; expected recorded holder resume-parent",
    ) as exc_info:
        goalflight_dispatch._admit_dispatch_worktree(args)
    # An unresolved recovery proof refuses immediately despite positive budget.
    assert clock.requested_sleeps == []
    assert sum(clock.requested_sleeps) <= args.capacity_wait_s
    assert len(bind_probes) == 1
    assert exc_info.value.flock_held is False
    assert reclaimer_keep_ref in str(exc_info.value)
    assert reclaimer_quarantine_ref in str(exc_info.value)


def test_resume_reseats_when_recorded_worktree_checkout_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing checkout whose git admin dir still points at it is not recreated.

    Resume must not prune that admin dir and add the slot again, and it must
    not check the same branch out in a sibling seat.
    """
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    parent.release()
    # Remove the checkout while retaining the git admin dir that points at it.
    shutil.rmtree(old_seat)
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-parent",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_id": old_seat.name,
            "worktree_path": str(old_seat),
            "worktree_branch": "worktree/resume-parent",
            "worktree_head": recorded_head,
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match=r"refusing to create missing --cwd",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert not old_seat.exists()
    assert not (old_seat.parent / "s-2").exists()


def test_pinned_resume_missing_checkout_never_relocates_while_flock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "missing-seat-parent"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    recorded_head = _git(repo, "rev-parse", f"worktree/{parent_id}")
    initial.release()
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": f"worktree/{parent_id}",
            "worktree_head": recorded_head,
        }
    )
    shutil.rmtree(seat)

    args, claim, claim_before = _queue_retry_args(
        tmp_path, repo, seat, parent_id, wait_s=0.12
    )
    args.parent_dispatch_id = parent_id
    args.cwd = str(seat.relative_to(repo))
    cwd_before = args.cwd
    pin_before = args.worktree_pin_holder
    monkeypatch.setattr(
        goalflight_dispatch,
        "_resume_recycled_worktree_in_place",
        lambda *_args, **_kwargs: pytest.fail(
            "pinned resume attempted worktree recovery while the path is held"
        ),
    )
    proc, release, lock_path = _start_flock_holder(
        tmp_path, repo, seat, "missing-seat-holder", record_ledger=False
    )
    lock_before = lock_path.read_bytes()
    clock = _drive_dispatch_deadline(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        message = str(exc_info.value)
        assert clock.elapsed < 0.12
        assert message.count("missing-seat-holder") == 1
        assert "flock_liveness=held" in message
        assert "holder_liveness=unknown" in message
        assert args.cwd == cwd_before
        assert args.worktree_pin_holder == pin_before
        assert claim.read_bytes() == claim_before
        assert lock_path.read_bytes() == lock_before
        assert _lock_dispatch_id(lock_path) == "missing-seat-holder"
        assert not seat.exists()
        assert not (seat.parent / "s-2").exists()
        assert not getattr(args, "_resume_relocated_worktree", False)
    finally:
        release.touch()
        proc.wait(timeout=5)


def test_resume_refuses_recorded_branch_owned_by_another_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    parent.release()
    record_finished_holder("resume-parent")
    _git(repo, "branch", "worktree/other-dispatch", recorded_head)
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "reclaimer")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "resume-parent",
            "agent": "codex",
            "engine": "codex",
            "state": "blocked",
            "terminal_state": "blocked",
            "project_root": str(repo),
            "worker_cwd": str(old_seat),
            "worktree_id": old_seat.name,
            "worktree_path": str(old_seat),
            "worktree_branch": "worktree/other-dispatch",
            "worktree_head": recorded_head,
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        _assert_pinned_resume_refusal(args, repo, old_seat, "reclaimer", monkeypatch)
    finally:
        reclaimer.release()


def test_resume_keeps_a_seat_held_by_the_original_dispatch_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="resume-parent",
        dispatch_id="resume-child",
        cwd=str(parent.path),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=r"s-1=resume-parent",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
        assert _git(parent.path, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/resume-parent"
        )
        assert not (repo / "worktrees" / "s-2").exists()
    finally:
        parent.release()


def test_dispatch_quarantines_dirty_seat_instead_of_destroying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
    (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
    (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
    finish_seat_holder(abandoned)

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
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
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
    assert "s-1" in branches[0]
    assert _git(repo, "show", f"{branches[0]}:abandoned.txt") == "preserve me"


def test_cwd_without_worktree_does_not_acquire_a_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "cwd-only"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    command = _dispatch_cmd(tmp_path, repo, "cwd-only", sys.executable, "-c", worker)
    command[command.index("--") : command.index("--")] = ["--cwd", str(repo)]
    proc = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 64, combined
    assert "project root" in combined.lower(), combined
    assert "DISPATCH-REFUSED " in proc.stdout
    assert not marker.exists()
    assert not (repo / "worktrees").exists()


def test_explicit_in_place_writer_runs_in_project_root(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "explicit-in-place-worker-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    command = _dispatch_cmd(
        tmp_path,
        repo,
        "explicit-in-place-writer",
        sys.executable,
        "-c",
        worker,
    )
    command.insert(command.index("--"), "--in-place")
    proc = subprocess.run(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert Path(marker.read_text(encoding="utf-8")).resolve() == repo.resolve()
    assert not (repo / "worktrees").exists()


def test_admit_read_only_at_root_with_leaked_seat_fd_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "readonly-inherited-seat"
    )
    dirty_file = holder.path / "tracked.txt"
    dirty_file.write_text("preserve this edit\n", encoding="utf-8")
    head_before = _git(holder.path, "rev-parse", "HEAD")
    args = SimpleNamespace(
        agent="codex-acp",
        shape="acp",
        worker=[],
        project_root=str(repo),
        cwd=str(repo),
        worktree="off",
        read_only=True,
        in_place=False,
        dispatch_id="read-only-inherited-seat",
        capacity_wait_s=0,
        controller_label=None,
        worktree_root=None,
        skip_seat_reset=False,
        parent_dispatch_id=None,
        from_queue=False,
        _worktree_seat=None,
        dispatch_warnings=[],
    )
    acquired: list[bool] = []

    def refuse_reset(*_args, **kwargs):
        acquired.append(bool(kwargs.get("reset")))
        raise AssertionError("inherited lock reached seat acquisition")

    monkeypatch.setattr(goalflight_worktree_pool, "acquire_worktree_seat", refuse_reset)
    monkeypatch.setenv(
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV,
        str(holder.fileno()),
    )
    try:
        assert goalflight_dispatch._admit_dispatch_worktree(args) is None
        assert acquired == []
        assert dirty_file.read_text(encoding="utf-8") == "preserve this edit\n"
        assert _git(holder.path, "rev-parse", "HEAD") == head_before
    finally:
        holder.release()


def test_waiting_capacity_replay_does_not_insert_project_root_cwd(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, "wait-row-holder")
    dispatch_id = "waiting-row-replay-cwd"
    marker = tmp_path / "waiting-row-worker-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    command = _dispatch_cmd(
        tmp_path,
        repo,
        dispatch_id,
        sys.executable,
        "-c",
        worker,
    )
    command[command.index("--") : command.index("--")] = [
        "--worktree",
        "HEAD",
        "--capacity-wait-s",
        "10",
    ]
    proc = subprocess.Popen(
        command,
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    record_path = goalflight_ledger.record_path(dispatch_id)
    record = None
    try:
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            if record_path.exists():
                candidate = json.loads(record_path.read_text(encoding="utf-8"))
                if candidate.get("state") == "waiting_capacity":
                    record = candidate
                    break
            time.sleep(0.02)
        assert record is not None, "dispatch did not write its pre-admit waiting row"
        assert "--cwd" not in record.get("dispatch_argv", []), record
    finally:
        finish_seat_holder(holder)
        stdout, stderr = proc.communicate(timeout=30)

    combined = stdout + stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert Path(marker.read_text(encoding="utf-8")).resolve() != repo.resolve()


def test_replay_without_admitted_cwd_does_not_use_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = SimpleNamespace(
        _original_argv=None,
        agent="codex",
        dispatch_id="fallback-replay-cwd",
        shape="bash",
        priority="normal",
        billing="sub",
        poll_secs=1.0,
        max_idle_secs=30.0,
        cwd=None,
        _worktree_path=None,
        prompt_file=None,
        prompt=None,
        task_ids=[],
        force=False,
        model=None,
        os_sandbox=None,
        read_only=False,
        fast=False,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=False,
        no_orientation=False,
        worktree="HEAD",
        worktree_base=None,
        worktree_root=None,
        in_place=False,
        skip_seat_reset=False,
        worktree_pin_holder=None,
        capacity_wait_s=None,
        account=None,
        interactive=False,
        permission_mode=None,
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        permission_allow_tool_title_pattern=[],
        controller_label=None,
        controller_session_id=None,
        _controller_beacon_pid=None,
        engine_session_id=None,
        codex_session_id=None,
        parent_dispatch_id=None,
        unregistered_forced=True,
        occupied_worktree_forced=False,
    )

    argv = goalflight_dispatch._canonical_replay_argv(
        args,
        [],
        tail=tmp_path / "fallback.tail",
        status_json=tmp_path / "fallback.status.json",
    )

    assert "--cwd" not in argv


def test_sidecar_env_drops_closed_worktree_lock_fd() -> None:
    import goalflight_dispatch as D

    env = {
        "PATH": "/usr/bin",
        goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV: "5",
        goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV: "7",
    }
    sidecar = D._sidecar_env(env)
    assert goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV not in sidecar
    assert goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV not in sidecar
    assert env[goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV] == "5"
    assert env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] == "7"
    assert sidecar["PATH"] == "/usr/bin"


def test_worktree_launch_does_not_fail_caffeinate_on_stale_lock_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "caf-ready"
    worker = (
        "from pathlib import Path; import os; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ok'); "
        "print('COMPLETE: caffeinate-sidecar — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "caf-sidecar",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert "does not name an open descriptor" not in combined
    assert '"step": "caffeinate"' not in combined or "WorktreeSeatError" not in combined
    launched = _launched_payload(proc.stdout)
    if sys.platform == "darwin":
        reason = _caffeinate_unavailable_reason()
        if reason is None:
            assert launched.get("caffeinate_pid"), combined
        else:
            pytest.skip(f"caffeinate sidecar assertion: {reason}")


def _caffeinate_unavailable_reason() -> str | None:
    executable = shutil.which("caffeinate")
    if executable is None:
        return "caffeinate is not installed on this host"
    try:
        result = subprocess.run(
            [executable, "-d", "-t", "1"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except PermissionError:
        return "sandbox denies exec of caffeinate"
    except FileNotFoundError:
        raise AssertionError("caffeinate disappeared after PATH resolution")
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("caffeinate capability probe timed out") from exc
    except OSError as exc:
        raise AssertionError(f"caffeinate capability probe failed: {exc}") from exc
    if result.returncode == 0:
        return None
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "operation not permitted" in output or "permission denied" in output:
        return "sandbox denies exec of caffeinate"
    raise AssertionError(
        "caffeinate capability probe failed without a sandbox refusal: "
        f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_two_worktree_launches_do_not_serialize_on_occupancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pooled seats are distinct trees; occupancy must not lock the project root."""
    if goalflight_capacity.profile().get("operating_cap", 1) < 2:
        pytest.skip("host capacity profile cannot run two dispatches concurrently")
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    release = tmp_path / "release-both"
    marker_a = tmp_path / "ready-a"
    marker_b = tmp_path / "ready-b"

    def worker(marker: Path) -> str:
        return (
            "import os, time\n"
            "from pathlib import Path\n"
            "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD']))\n"
            "os.fstat(int(os.environ['GOALFLIGHT_OCCUPANCY_LOCK_FD']))\n"
            f"Path({str(marker)!r}).write_text(os.getcwd(), encoding='utf-8')\n"
            f"release = Path({str(release)!r})\n"
            # Keep workers alive past the controller's bounded rendezvous wait,
            # so a slow second launch fails at the assertion below instead of
            # racing the worker's own timeout.
            "deadline = time.monotonic() + 60\n"
            "while not release.exists():\n"
            "    if time.monotonic() >= deadline:\n"
            "        raise TimeoutError('release')\n"
            "    time.sleep(0.05)\n"
            "print('COMPLETE: wt-conc — ok', flush=True)\n"
        )

    pa = subprocess.Popen(
        _dispatch_cmd(tmp_path, repo, "wt-conc-a", sys.executable, "-c", worker(marker_a)),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pb = subprocess.Popen(
        _dispatch_cmd(tmp_path, repo, "wt-conc-b", sys.executable, "-c", worker(marker_b)),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (
            marker_a.exists() and marker_b.exists()
        ):
            time.sleep(0.05)
        assert marker_a.exists() and marker_b.exists(), (
            "pooled launches did not overlap: both workers must reach the rendezvous "
            "while holding their seats before either is released",
            pa.poll(),
            pb.poll(),
        )
        cwd_a = marker_a.read_text(encoding="utf-8").strip()
        cwd_b = marker_b.read_text(encoding="utf-8").strip()
        assert cwd_a != cwd_b, (cwd_a, cwd_b)
        assert Path(cwd_a).name.startswith("s-")
        assert Path(cwd_b).name.startswith("s-")
        release.write_text("go", encoding="utf-8")
        out_a, err_a = pa.communicate(timeout=30)
        out_b, err_b = pb.communicate(timeout=30)
    finally:
        if pa.poll() is None:
            pa.kill()
            pa.wait(timeout=5)
        if pb.poll() is None:
            pb.kill()
            pb.wait(timeout=5)
    assert pa.returncode == 0, out_a + err_a
    assert pb.returncode == 0, out_b + err_b
    assert 64 not in {pa.returncode, pb.returncode}


def test_raw_worker_process_cwd_is_the_leased_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "raw-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "raw-seat-cwd",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    launched = _launched_payload(proc.stdout)
    seat = Path(launched["worktree_path"]).resolve()
    assert Path(marker.read_text(encoding="utf-8")).resolve() == seat


def _commit_in(worktree: Path, message: str, text: str = "unique work\n") -> str:
    (worktree / "tracked.txt").write_text(text, encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD")


def _payloads(stdout: str) -> tuple[dict, dict]:
    started: dict = {}
    launched: dict = {}
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-START "):
            started = json.loads(line[len("DISPATCH-START ") :])
        elif line.startswith("DISPATCH-LAUNCHED "):
            launched = json.loads(line[len("DISPATCH-LAUNCHED ") :])
    return started, launched


def test_acquire_checks_out_named_seat_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "named-one")
    try:
        abbrev = _git(lease.path, "rev-parse", "--abbrev-ref", "HEAD")
        assert abbrev == "worktree/named-one", abbrev
        status = _git(lease.path, "status", "--branch", "--porcelain=v1")
        assert "HEAD (no branch)" not in status, status
        assert status.splitlines()[0].startswith("## worktree/named-one"), status
        assert lease.branch == "worktree/named-one"
    finally:
        lease.release()


def test_dispatch_status_retains_worktree_identity_after_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=1)
    marker = tmp_path / "branch-ready"
    worker = (
        "from pathlib import Path; import os; "
        "os.fstat(int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])); "
        f"Path({str(marker)!r}).write_text('ok'); "
        "print('!COMPLETE: report-branch — ok', flush=True)"
    )
    proc = subprocess.run(
        _dispatch_cmd(
            tmp_path,
            repo,
            "report-branch",
            sys.executable,
            "-c",
            worker,
        ),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    started, launched = _payloads(proc.stdout)
    assert started.get("worktree_branch") == "worktree/report-branch", started
    assert launched.get("worktree_branch") == "worktree/report-branch", launched
    assert launched.get("worktree_seat") == "s-1", launched
    assert launched.get("worktree_id") == launched.get("worktree_seat"), launched
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    status_path = tmp_path / "report-branch.status.json"
    deadline = time.time() + 10
    status = {}
    while time.time() < deadline:
        if status_path.exists():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if status.get("state") == "complete":
                break
        time.sleep(0.05)
    assert status.get("state") == "complete", status
    assert status.get("worktree_path") == launched["worktree_path"], status
    assert status.get("worktree_seat") == launched["worktree_seat"], status


@pytest.mark.parametrize("resolution", ["null", "empty", "error"])
def test_dispatch_refuses_unresolved_seat_base_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolution: str
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    marker = tmp_path / "must-not-launch"
    # Fault the prepared seat's SHA probe, leaving real acquisition and launch
    # in place: an unknown base must never reach the worker.
    driver = f"""
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
import goalflight_dispatch as dispatch
import goalflight_worktree_pool as pool
original_git = pool._git
def git(cwd, *args, **kwargs):
    if cwd.name == 's-1' and args == ('rev-parse', '--verify', 'HEAD^{{commit}}'):
        if {resolution!r} == 'error':
            raise pool.WorktreeSeatError('cannot read prepared HEAD')
        return None if {resolution!r} == 'null' else ''
    return original_git(cwd, *args, **kwargs)
pool._git = git
raise SystemExit(dispatch.main(sys.argv[1:]))
"""
    cmd = _dispatch_cmd(
        tmp_path, repo, "unknown-base", sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('launched')",
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver, *cmd[2:]],
        cwd=str(repo), env=_env(tmp_path, seats=1), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, combined
    assert "s-1" in combined, combined
    assert "base SHA" in combined, combined
    assert "--at" in combined and "omitted" in combined, combined
    assert "--at <ref>" in combined, combined
    assert not _launched_payload(proc.stdout), combined
    assert not marker.exists(), combined
    # The failed launch leaves a reclaimable high-water slot absent. At
    # capacity, acquisition can rebuild that same slot after holder validation.
    slot = goalflight_worktree_pool.repository_worktree_root(repo) / "s-1"
    assert not slot.exists()
    held, _holder = goalflight_worktree_pool._probe_worktree_seat_flock(
        repo, slot
    )
    assert held is False
    lock_root = goalflight_worktree_pool._seat_lock_root(repo)
    assert goalflight_worktree_pool._ring_state_path(lock_root).is_file()
    assert goalflight_worktree_pool._read_ring_hwm(lock_root) == 1
    with goalflight_worktree_pool.acquire_worktree_seat(
        repo, "after-refusal"
    ) as recreated:
        assert recreated.path == slot
    assert slot.is_dir()


@pytest.mark.parametrize("explicit_base", [False, True])
def test_dispatch_records_resolved_base_and_launches_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_base: bool
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    # Reuse a seat left at an older commit, after the project's base advances.
    previous = goalflight_worktree_pool.acquire_worktree_seat(repo, "previous-base")
    finish_seat_holder(previous)
    base = _commit_in(repo, "advance base", "new base\n")
    marker = tmp_path / "launched-base"
    worker = (
        "from pathlib import Path; import subprocess; "
        f"Path({str(marker)!r}).write_text("
        "subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()); "
        "print('COMPLETE: resolved-base — ok', flush=True)"
    )
    cmd = _dispatch_cmd(tmp_path, repo, "resolved-base", sys.executable, "-c", worker)
    if explicit_base:
        cmd[2:2] = ["--at", "main"]
    proc = subprocess.run(
        cmd, cwd=str(repo), env=_env(tmp_path, seats=1), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert marker.read_text() == base
    started, launched = _payloads(proc.stdout)
    assert started.get("worktree_base") == base, started
    assert launched.get("worktree_base") == base, launched


def test_pin_and_reset_detached_ahead_of_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unique detached commit is pinned before acquire-time reset."""
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "detached-ahead")
    _git(lease.path, "checkout", "--detach")
    sha = _commit_in(lease.path, "unique detached commit")
    short = _git(lease.path, "rev-parse", "--short", "HEAD")
    finish_seat_holder(lease)

    nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "next-occupant")
    try:
        assert _git(nxt.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
        keep_refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/keep/").splitlines()
        assert any(short in _git(repo, "rev-parse", ref) or sha == _git(repo, "rev-parse", ref) for ref in keep_refs)
    finally:
        nxt.release()


def test_detached_ahead_seat_is_skipped_for_a_free_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "keep-me")
    _git(first.path, "checkout", "--detach")
    sha = _commit_in(first.path, "do not clobber")
    finish_seat_holder(first)

    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "use-wt-2")
    try:
        assert second.path.name == "s-2"
        assert second.branch == "worktree/use-wt-2"
        assert _git(second.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
        assert _git(first.path, "rev-parse", "HEAD") == sha
    finally:
        second.release()


def test_reuse_keeps_prior_named_branch_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    first = goalflight_worktree_pool.acquire_worktree_seat(repo, "worker-a")
    sha = _commit_in(first.path, "worker a finished")
    assert _git(first.path, "rev-parse", "--abbrev-ref", "HEAD") == "worktree/worker-a"
    finish_seat_holder(first)

    second = goalflight_worktree_pool.acquire_worktree_seat(repo, "worker-b")
    try:
        assert second.branch == "worktree/worker-b"
        assert _git(second.path, "rev-parse", "--abbrev-ref", "HEAD") == "worktree/worker-b"
        assert _git(repo, "rev-parse", "refs/heads/worktree/worker-a") == sha
        assert _git(repo, "show", "worktree/worker-a:tracked.txt") == "unique work"
    finally:
        second.release()


def test_same_dispatch_branch_refuses_reuse_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")
    sha = _commit_in(lease.path, "retry must not rewind this branch")
    seat = lease.path
    finish_seat_holder(lease)

    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatError,
        match=r"dispatch_branch_in_use:s-1",
    ):
        goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")

    assert _git(seat, "rev-parse", "HEAD") == sha
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == "worktree/same-id"


def test_saved_detached_commit_does_not_block_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "already-saved")
    _git(lease.path, "checkout", "--detach")
    sha = _commit_in(lease.path, "saved elsewhere")
    _git(lease.path, "branch", "rescue/already-saved")
    finish_seat_holder(lease)

    reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "after-rescue")
    try:
        assert _git(reused.path, "rev-parse", "--abbrev-ref", "HEAD") == "worktree/after-rescue"
        assert _git(repo, "rev-parse", "refs/heads/rescue/already-saved") == sha
    finally:
        reused.release()


def test_claude_preset_has_no_cwd_flag_seat_is_process_cwd() -> None:
    import argparse
    import goalflight_dispatch as D

    argv, _stdin = D.build_worker(
        argparse.Namespace(
            agent="claude",
            cwd="/repo/worktrees/wt-1",
            model=None,
            parent_dispatch_id=None,
        ),
        "/tmp/prompt.md",
        [],
    )
    assert argv[:1] == ["claude"]
    assert "--cwd" not in argv
    assert "-C" not in argv


def test_default_dispatch_acquires_captive_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    marker = tmp_path / "default-cwd"
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(marker)!r}).write_text(os.getcwd())"
    )
    proc = subprocess.run(
        _dispatch_cmd(tmp_path, repo, "default-seat", sys.executable, "-c", worker),
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    launched = _launched_payload(proc.stdout)
    assert launched.get("worktree_seat") == "s-1", launched
    seat = Path(launched["worktree_path"]).resolve()
    assert seat.name == "s-1"
    assert seat.parent.name == "worktrees"
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), combined
    assert Path(marker.read_text(encoding="utf-8")).resolve() == seat
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(seat) in listed
    assert "bt-" not in listed


def test_explicit_project_root_cwd_allocates_captive_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "captive-seed")
    seat = seed.path
    seed.release()
    _write_terminal_seat_record("captive-seed", repo, seat)
    args = SimpleNamespace(
        agent="test-dispatch",
        project_root=str(repo),
        cwd=str(repo),
        worktree="create",
        worktree_root=None,
        dispatch_id="explicit-root-cwd",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        from_queue=False,
        capacity_wait_s=0,
        dispatch_warnings=[],
        _worktree_seat=None,
    )
    goalflight_dispatch._prepare_explicit_worktree_occupancy(args)
    assert args._worktree_occupancy_lock.path == repo.resolve()
    # Keep this regression focused on lock handoff; the source checkout lock
    # is not a seat admin dir in the pool cleanliness inventory.
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_repo_clean_report",
        lambda project_root: goalflight_worktree_pool._RepoCleanReport(
            [], repo=project_root
        ),
    )
    lease = None
    try:
        lease = goalflight_dispatch._bind_dispatch_worktree(args)
        assert lease.path != repo.resolve()
        assert lease.path.parent == repo / "worktrees"
        assert lease.path.name.startswith("s-")
    finally:
        if lease is not None:
            lease.release()
        goalflight_dispatch._release_worktree_occupancy_lock(args)


def _assert_acp_resume_admission_keeps_parent_occupancy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    detached_child: bool,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.delenv(goalflight_worktree_pool.WORKTREE_LOCK_FD_ENV, raising=False)
    monkeypatch.delenv(goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV, raising=False)
    repo = _make_repo(tmp_path)
    seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "acp-resume-seed")
    seat = seed.path
    seed.release()
    _write_terminal_seat_record("acp-resume-seed", repo, seat)
    dispatch_id = f"acp-resume-{('detached' if detached_child else 'foreground')}"
    args = SimpleNamespace(
        agent="codex-acp",
        model=None,
        prompt_file=None,
        cwd=str(seat),
        project_root=str(repo),
        worktree="create",
        worktree_base="HEAD",
        worktree_root=None,
        controller_label=None,
        worktree_pin_holder=None,
        read_only=False,
        in_place=False,
        prompt="COMPLETE: no-op",
        max_idle_secs="300",
        poll_secs="0.1",
        dispatch_id=dispatch_id,
        parent_dispatch_id="acp-resume-seed",
        status_json=None,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        billing="sub",
        tail=None,
        priority="normal",
        capacity_wait_s=0,
        skip_seat_reset=False,
        from_queue=False,
        dispatch_warnings=[],
        _worktree_seat=None,
    )
    monkeypatch.setattr(
        goalflight_dispatch,
        "_persist_acp_watcher_prompt",
        lambda **_kwargs: tmp_path / f"{dispatch_id}.prompt",
    )

    inherited_fd = None
    if detached_child:
        parent_lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            seat, dispatch_id
        )
        inherited_fd = os.dup(parent_lock.fileno())
        os.set_inheritable(inherited_fd, True)
        monkeypatch.setenv(
            goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV,
            str(inherited_fd),
        )
        goalflight_dispatch._prepare_explicit_worktree_occupancy(args)
    else:
        parent_lock = None
        goalflight_dispatch._prepare_explicit_worktree_occupancy(args)
        parent_lock = args._worktree_occupancy_lock

    lease = None
    cfg = None
    try:
        cfg = goalflight_dispatch._build_acp_cfg(
            args,
            status_json=tmp_path / f"{dispatch_id}.status.json",
            base=tmp_path,
        )
        if not detached_child:
            assert cfg._worktree_occupancy_lock is parent_lock
        lease = goalflight_dispatch._admit_dispatch_worktree(cfg)
        assert lease.path == seat
        assert isinstance(
            cfg._worktree_occupancy_lock,
            goalflight_worktree_pool.WorktreePathLock,
        )
        assert cfg._worktree_occupancy_lock.dispatch_id == dispatch_id
        if detached_child:
            assert cfg._worktree_occupancy_lock.fileno() == inherited_fd
    finally:
        if lease is not None:
            lease.release()
        if detached_child and cfg is not None:
            goalflight_dispatch._release_worktree_occupancy_lock(cfg)
        elif not detached_child:
            goalflight_dispatch._release_worktree_occupancy_lock(args)
        if detached_child and parent_lock is not None:
            parent_lock.release()


def test_acp_foreground_resume_hands_occupancy_lock_to_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_acp_resume_admission_keeps_parent_occupancy(
        tmp_path, monkeypatch, detached_child=False
    )


def test_acp_detached_resume_revalidates_inherited_occupancy_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_acp_resume_admission_keeps_parent_occupancy(
        tmp_path, monkeypatch, detached_child=True
    )


def test_sequential_default_dispatch_uses_unused_seats_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "4")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=4)
    paths = []
    for name in ("seq-a", "seq-b"):
        marker = tmp_path / f"{name}.cwd"
        worker = (
            "from pathlib import Path; import os, time; time.sleep(0.5); "
            f"Path({str(marker)!r}).write_text(os.getcwd()); "
            f"print('COMPLETE: {name} — ok', flush=True)"
        )
        command = _dispatch_cmd(tmp_path, repo, name, sys.executable, "-c", worker)
        command.remove("--launch-detached")
        command.insert(command.index("--"), "--foreground")
        proc = subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        launched = _launched_payload(proc.stdout)
        paths.append(Path(launched["worktree_path"]).resolve())
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        status_path = tmp_path / f"{name}.status.json"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                state = json.loads(status_path.read_text(encoding="utf-8")).get("state")
            except (FileNotFoundError, json.JSONDecodeError):
                state = None
            if state in {"complete", "failed", "worker_dead", "worker_error"}:
                break
            time.sleep(0.05)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                leases = json.loads((tmp_path / "state" / "capacity.json").read_text(encoding="utf-8")).get("leases", {})
            except (FileNotFoundError, json.JSONDecodeError):
                leases = {}
            if not leases:
                break
            time.sleep(0.05)
    assert paths[0] != paths[1]
    assert {path.name for path in paths} == {"s-1", "s-2"}
    assert not (paths[0].parent / "s-3").exists()


def test_cwd_to_cache_worktree_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _env(tmp_path, seats=2)
    cache = repo / ".cache" / "worktrees" / "foo"
    cache.mkdir(parents=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH),
            "--unregistered-forced",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            "cwd-refuse",
            "--cwd",
            str(cache),
            "--launch-detached",
            "--tail",
            str(tmp_path / "cwd-refuse.tail"),
            "--status-json",
            str(tmp_path / "cwd-refuse.status.json"),
            "--",
            sys.executable,
            "-c",
            "print('nope')",
        ],
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "not a seat" in combined or "refusing" in combined.lower()
    assert not (cache / ".git").exists()
    assert not (repo / "worktrees").exists()


def test_resume_injects_skip_seat_reset(tmp_path: Path) -> None:
    import argparse
    import goalflight_dispatch as D

    worktree = tmp_path / "historical-bt"
    worktree.mkdir()
    prompt = tmp_path / "resume.md"
    prompt.write_text("continue\n", encoding="utf-8")
    source = {
        "engine": "grok",
        "agent": "grok-code",
        "session_id": "sess",
        "shape": "bash",
        "record": {
            "worker_cwd": str(worktree),
            "dispatch_argv": [
                "--agent",
                "grok-code",
                "--cwd",
                str(worktree),
            ],
        },
    }
    resume_args = argparse.Namespace(
        dispatch_id="parent-resume",
        cwd=None,
        unregistered_forced=True,
        controller_label=None,
        controller_pid=None,
        controller_session_id=None,
    )
    argv = D._resume_launch_argv(
        source,
        child_dispatch_id="child-resume",
        prompt_path=prompt,
        resume_args=resume_args,
    )
    assert "--skip-seat-reset" in argv
    cwd = D._option_value_before_worker_remainder(argv, "--cwd")
    assert Path(str(cwd)).resolve() == worktree.resolve()


def test_queue_retry_carrier_refuses_checked_out_dispatch_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    claim = tmp_path / "queued.claimed"
    dispatch_id = "queue-pin-retry"
    goalflight_dispatch._write_json_atomic(
        claim,
        {
            "dispatch_id": dispatch_id,
            "queue_launch_token": "queue-token",
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--worktree",
                "HEAD",
            ],
        },
    )
    monkeypatch.setattr(goalflight_dispatch, "_project_root", lambda _args: repo)
    monkeypatch.setattr(
        goalflight_dispatch,
        "_controller_ring_label",
        lambda *_args: "controller",
    )
    calls: list[dict] = []
    real_acquire = goalflight_worktree_pool.acquire_worktree_seat

    def acquire(*args, **kwargs):
        calls.append(dict(kwargs))
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(goalflight_worktree_pool, "acquire_worktree_seat", acquire)
    first_args = SimpleNamespace(
        agent="test-dispatch",
        dispatch_id=dispatch_id,
        cwd=None,
        worktree="HEAD",
        in_place=False,
        skip_seat_reset=False,
        from_queue=True,
        queue_claim_path=str(claim),
        queue_launch_token="queue-token",
        controller_label="controller",
        parent_dispatch_id=None,
        _worktree_seat=None,
    )
    first = goalflight_dispatch._bind_dispatch_worktree(first_args)
    assert first is not None
    seat = first.path
    first.release()

    carrier = json.loads(claim.read_text(encoding="utf-8"))
    assert carrier["worktree_path"] == str(seat)
    assert carrier["worktree_seat"] == seat.name
    assert "--skip-seat-reset" in carrier["dispatch_argv"]
    assert carrier["dispatch_argv"][carrier["dispatch_argv"].index("--cwd") + 1] == str(seat)

    retry_args = SimpleNamespace(
        **{
            **vars(first_args),
            "cwd": str(seat),
            "skip_seat_reset": True,
            "_worktree_seat": None,
        }
    )
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatError,
        match=r"dispatch_branch_in_use:",
    ):
        goalflight_dispatch._bind_dispatch_worktree(retry_args)
    assert calls[-1]["reset"] is False
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{dispatch_id}"
    )


def test_pinned_queue_free_flock_does_not_treat_lock_json_as_live_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder_id = "released-queue-pin-holder"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    _write_terminal_seat_record(holder_id, repo, seat)
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    assert _lock_dispatch_id(lock_path) == holder_id
    args, _claim, _claim_before = _queue_retry_args(
        tmp_path, repo, seat, holder_id, wait_s=0.2
    )

    lease = goalflight_dispatch._admit_dispatch_worktree(args)
    try:
        assert lease is not None
        assert lease.path == seat
        assert _lock_dispatch_id(lock_path) == args.dispatch_id
        assert not (seat.parent / "s-2").exists()
    finally:
        if lease is not None:
            lease.release()
        goalflight_dispatch._release_worktree_occupancy_lock(args)


def test_live_dispatch_metadata_on_other_seats_keeps_pool_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "3")
    repo = _make_repo(tmp_path)
    seeds = [
        goalflight_worktree_pool.acquire_worktree_seat(repo, f"seed-seat-{index}")
        for index in range(1, 4)
    ]
    seats = [lease.path for lease in seeds]
    for lease in seeds:
        _write_terminal_seat_record(lease.dispatch_id, repo, lease.path)
        lease.release()
    markers = []
    for seat in seats[:2]:
        marker = seat / "preserve-live-occupant.txt"
        marker.write_text("retain live metadata seat\n", encoding="utf-8")
        markers.append(marker)

    live_id = "live-holder-on-another-seat"
    live = goalflight_worktree_pool.acquire_worktree_seat(
        repo, live_id, occupy_path=seats[2]
    )
    _write_seat_holder_record(live_id, repo, live, state="running", live=True)
    active_occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
        live.path, live_id
    )
    read_record = goalflight_ledger.read_record
    monkeypatch.setattr(
        goalflight_ledger,
        "read_record",
        lambda dispatch_id: (
            None if dispatch_id == live_id else read_record(dispatch_id)
        ),
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [(os.getpid(), str(live.path))],
    )

    def stamp_stale_metadata(seat: Path) -> None:
        occupancy = goalflight_worktree_pool.try_acquire_worktree_path_lock(
            seat, live_id
        )
        occupancy.release()
        lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
            repo, seat.name
        )
        with lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            goalflight_worktree_pool._write_occupant(
                handle, seat_name=seat.name, dispatch_id=live_id
            )
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    try:
        stamp_stale_metadata(seats[0])
        stamp_stale_metadata(seats[1])
        monkeypatch.setattr(
            goalflight_worktree_pool, "_process_cwd_snapshot", lambda *, timeout_s: None
        )
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "unknown-cwd-census")
        monkeypatch.setattr(
            goalflight_worktree_pool,
            "_process_cwd_snapshot",
            lambda *, timeout_s: [],
        )
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "live-census-clear")
        assert all(
            marker.read_text(encoding="utf-8") == "retain live metadata seat\n"
            for marker in markers
        )
        assert [_lock_dispatch_id(
            goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
        ) for seat in seats[:2]] == [live_id, live_id]
    finally:
        active_occupancy.release()
        live.release()


def test_terminal_native_unknown_identity_keeps_live_cwd_seat(
    tmp_path: Path,
    isolated_pool_state: Path,
) -> None:
    repo = _make_repo(tmp_path)
    holder_id = "terminal-native-unknown"
    holder = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = holder.path
    marker = seat / "preserve-me.txt"
    marker.write_text("still in use\n", encoding="utf-8")
    holder.release()

    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": holder_id,
            "agent": "codex",
            "engine": "codex",
            "state": "complete",
            "terminal_state": "complete",
            "lock_holder": "native",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
            "worktree_head": _git(seat, "rev-parse", "HEAD"),
        }
    )

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=seat,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert worker.poll() is None
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "native-reclaimer")

        assert marker.read_text(encoding="utf-8") == "still in use\n"
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{holder_id}"
        )
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def test_pinned_queue_seat_live_flock_refuses_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder_id = "queue-parent-live"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "keep-me.txt"
    dirty_file.write_text("dirty checkout\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    proc, release, lock_path = _start_flock_holder(
        tmp_path, repo, seat, holder_id
    )
    args, claim, claim_before = _queue_retry_args(
        tmp_path, repo, seat, holder_id, wait_s=0.12
    )
    lock_before = lock_path.read_bytes()
    clock = _drive_dispatch_deadline(monkeypatch)
    probe_durations = _record_bind_probe_durations(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        message = str(exc_info.value)
        assert clock.elapsed == pytest.approx(args.capacity_wait_s)
        assert len(probe_durations) > 1
        assert message.count(holder_id) == 1
        assert "flock_liveness=held" in message
        assert "holder_liveness=live" in message
        assert args.cwd == str(seat)
        assert args.worktree_pin_holder == holder_id
        assert args.from_queue is True
        assert args.queue_claim_path == str(claim)
        assert claim.read_bytes() == claim_before
        assert lock_path.read_bytes() == lock_before
        assert _git(seat, "rev-parse", "HEAD") == head_before
        assert dirty_file.read_text(encoding="utf-8") == "dirty checkout\n"
        assert not (seat.parent / "s-2").exists()
    finally:
        release.touch()
        proc.wait(timeout=5)


def test_pinned_worktree_inspection_refusal_uses_pinned_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder_id = "queue-parent-unobserved"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "keep-me.txt"
    dirty_file.write_text("dirty checkout\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    args, claim, claim_before = _queue_retry_args(
        tmp_path, repo, seat, holder_id, wait_s=0.2
    )
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    lock_before = lock_path.read_bytes()
    clock = _drive_dispatch_deadline(monkeypatch)
    inspections = 0
    real_prepare = goalflight_worktree_pool._prepare_claimed_seat_locked

    def refuse_uninspectable_claim(**kwargs):
        nonlocal inspections
        real_path_presence = goalflight_worktree_pool._path_presence

        def unknown_presence(_path: Path) -> str:
            nonlocal inspections
            inspections += 1
            return "unknown"

        goalflight_worktree_pool._path_presence = unknown_presence
        try:
            return real_prepare(**kwargs)
        finally:
            goalflight_worktree_pool._path_presence = real_path_presence

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_prepare_claimed_seat_locked",
        refuse_uninspectable_claim,
    )
    with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
        goalflight_dispatch._admit_dispatch_worktree(args)

    message = str(exc_info.value)
    assert clock.elapsed == pytest.approx(args.capacity_wait_s)
    assert inspections > 1
    assert message.count(holder_id) == 1
    assert "flock_liveness=unknown" in message
    assert args.cwd == str(seat)
    assert args.worktree_pin_holder == holder_id
    assert args.from_queue is True
    assert args.queue_claim_path == str(claim)
    assert claim.read_bytes() == claim_before
    assert lock_path.read_bytes() == lock_before
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "dirty checkout\n"
    assert not (seat.parent / "s-2").exists()


def test_pinned_queue_seat_proceeds_when_flock_frees_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder_id = "queue-parent-live"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    proc, release, _lock_path = _start_flock_holder(
        tmp_path, repo, seat, holder_id, record_ledger=False
    )
    _write_terminal_seat_record(holder_id, repo, seat)
    args, _claim, _claim_before = _queue_retry_args(
        tmp_path, repo, seat, holder_id, wait_s=0.8
    )
    bind_attempts: list[bool] = []
    original_bind = goalflight_dispatch._bind_dispatch_worktree

    def release_after_first_busy_bind(bind_args):
        bind_attempts.append(True)
        try:
            return original_bind(bind_args)
        except goalflight_worktree_pool.WorktreeSeatUnavailable:
            if len(bind_attempts) == 1:
                release.touch()
                proc.wait(timeout=5)
            raise

    monkeypatch.setattr(
        goalflight_dispatch, "_bind_dispatch_worktree", release_after_first_busy_bind
    )
    clock = _drive_dispatch_deadline(monkeypatch)
    lease = None
    try:
        lease = goalflight_dispatch._admit_dispatch_worktree(args)
        assert lease is not None
        assert lease.path == seat
        assert len(bind_attempts) == 2
        assert len(clock.requested_sleeps) == 1
        assert clock.elapsed == pytest.approx(0.05)
        assert sum(clock.requested_sleeps) <= args.capacity_wait_s
    finally:
        release.touch()
        proc.wait(timeout=5)
        if lease is not None:
            lease.release()


def test_pinned_resume_retries_allocation_lock_without_relocating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-allocation-parent"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "allocation-parent-dirty.txt"
    dirty_file.write_text("keep the recorded checkout\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    _write_terminal_seat_record(parent_id, repo, seat)
    proc, release, _lock_path = _start_allocation_flock_holder(tmp_path, repo)
    real_acquire_allocation_lock = goalflight_worktree_pool._acquire_allocation_lock
    released_after_miss = False

    def acquire_allocation_lock(*args, **kwargs):
        nonlocal released_after_miss
        try:
            return real_acquire_allocation_lock(*args, **kwargs)
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            if not released_after_miss and "worktree allocation lock" in str(exc):
                released_after_miss = True
                release.touch()
                proc.wait(timeout=5)
            raise

    monkeypatch.setattr(
        goalflight_worktree_pool, "_acquire_allocation_lock", acquire_allocation_lock
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-allocation-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=0.6,
    )
    lease = None
    try:
        lease = goalflight_dispatch._admit_dispatch_worktree(args)
        assert lease is not None
        assert lease.path == seat
        assert not getattr(args, "_resume_relocated_worktree", False)
        assert args.cwd == str(seat)
        assert _git(seat, "rev-parse", "HEAD") == head_before
        assert dirty_file.read_text(encoding="utf-8") == "keep the recorded checkout\n"
        assert not (seat.parent / "s-2").exists()
    finally:
        release.touch()
        if proc.poll() is None:
            proc.wait(timeout=5)
        if lease is not None:
            lease.release()


def test_pinned_allocation_lock_misses_use_cap_and_report_unobserved_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    assert goalflight_dispatch.PINNED_SEAT_WAIT_MAX_S == 120.0
    monkeypatch.setattr(goalflight_dispatch, "PINNED_SEAT_WAIT_MAX_S", 0.12)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    holder_id = "allocation-blocked-seat-holder"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "allocation-held-dirty.txt"
    dirty_file.write_text("do not reset\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    seat_proc, seat_release, seat_lock = _start_flock_holder(
        tmp_path, repo, seat, holder_id
    )
    allocation_proc, allocation_release, _allocation_lock = (
        _start_allocation_flock_holder(tmp_path, repo)
    )
    args, claim, claim_before = _queue_retry_args(
        tmp_path, repo, seat, holder_id, wait_s=1800.0
    )
    seat_lock_before = seat_lock.read_bytes()
    clock = _drive_dispatch_deadline(monkeypatch)
    probe_durations = _record_bind_probe_durations(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        message = str(exc_info.value)
        assert clock.elapsed == pytest.approx(0.12)
        assert len(probe_durations) > 1
        assert message.count(holder_id) == 1
        assert "flock_liveness=unknown" in message
        assert "wait for a seat" in message
        assert args.cwd == str(seat)
        assert args.worktree_pin_holder == holder_id
        assert args.from_queue is True
        assert args.queue_claim_path == str(claim)
        assert claim.read_bytes() == claim_before
        assert seat_lock.read_bytes() == seat_lock_before
        assert _git(seat, "rev-parse", "HEAD") == head_before
        assert dirty_file.read_text(encoding="utf-8") == "do not reset\n"
        assert not (seat.parent / "s-2").exists()
    finally:
        allocation_release.touch()
        seat_release.touch()
        allocation_proc.wait(timeout=5)
        seat_proc.wait(timeout=5)


def test_pinned_queue_refusal_is_retryable_by_drain_after_holder_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    dispatch_id = "drain-pinned-seat-retry"
    holder_id = "drain-pinned-seat-holder"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, holder_id)
    seat = initial.path
    initial.release()
    proc, release, _lock_path = _start_flock_holder(
        tmp_path, repo, seat, holder_id, record_ledger=False
    )
    held, _holder = goalflight_worktree_pool._probe_worktree_seat_flock(
        repo, seat
    )
    assert held is True
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True)
    queue_path = queue_dir / f"{dispatch_id}.json"
    marker = tmp_path / "drain-pinned-worker-ran"
    tail = tmp_path / f"{dispatch_id}.tail"
    status = tmp_path / f"{dispatch_id}.status.json"
    entry = {
        "schema": goalflight_dispatch.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(repo),
        "process_cwd": str(repo),
        "created_at": goalflight_ledger.utc_now(),
        "updated_at": goalflight_ledger.utc_now(),
        "queue_path": str(queue_path),
        "worktree_seat": seat.name,
        "worktree_path": str(seat),
        "worktree_pin_holder": holder_id,
        "dispatch_argv": [
            "--agent",
            "test-dispatch",
            "--unregistered-forced",
            "--dispatch-id",
            dispatch_id,
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--poll-secs",
            "0.1",
            "--max-idle-secs",
            "10",
            "--worktree",
            "HEAD",
            "--cwd",
            str(seat),
            "--skip-seat-reset",
            "--worktree-pin-holder",
            holder_id,
            "--capacity-wait-s",
            "0",
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran'); "
                f"print('COMPLETE: {dispatch_id} — retry ran')"
            ),
        ],
        "request": {"cwd": str(seat), "tail": str(tail), "status_json": str(status)},
    }
    goalflight_dispatch._write_json_atomic(queue_path, entry)
    env = _env(tmp_path, seats=2)

    def drain() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "drain",
                "--capacity-wait-s",
                "0",
                "--json",
            ],
            cwd=str(repo),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )

    try:
        refused = drain()
        assert refused.returncode == 0, (refused.stdout, refused.stderr)
        first_payload = json.loads(refused.stdout)
        assert first_payload["launched"] == 0, first_payload
        assert first_payload["left_queued"] == 1, json.dumps(
            first_payload, sort_keys=True
        )[:5000]
        first_detail = first_payload["details"][0]
        assert "launch_attempt_class" in first_detail, (
            first_payload,
            refused.stderr,
        )
        assert first_detail["launch_attempt_class"] == (
            goalflight_dispatch.LAUNCH_ATTEMPT_CLASS_PROVEN_TRANSIENT
        ), first_detail
        assert "launch_backoff_until" not in first_detail, first_detail
        assert queue_path.is_file()
        queued = json.loads(queue_path.read_text(encoding="utf-8"))
        assert queued["worktree_path"] == str(seat)
        assert queued["worktree_seat"] == seat.name
        assert queued["worktree_pin_holder"] == holder_id
        assert "launch_backoff_until" not in queued
    finally:
        release.touch()
        proc.wait(timeout=15)
    held, _holder = goalflight_worktree_pool._probe_worktree_seat_flock(
        repo, seat
    )
    assert held is False
    _write_terminal_seat_record(holder_id, repo, seat)

    retried = drain()
    assert retried.returncode == 0, (retried.stdout, retried.stderr)
    second_payload = json.loads(retried.stdout)
    assert second_payload["launched"] == 1, second_payload
    assert not queue_path.exists()
    deadline = time.monotonic() + 30
    final_status: dict = {}
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text(encoding="utf-8") == "ran":
            try:
                final_status = json.loads(status.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                final_status = {}
            if final_status.get("state") == "complete" and final_status.get("worker_alive") is not True:
                break
        time.sleep(0.05)
    assert marker.exists() and marker.read_text(encoding="utf-8") == "ran"
    assert final_status.get("state") == "complete", final_status


def test_pinned_resume_with_live_parent_flock_uses_bounded_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent-live"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "resume-live-dirty.txt"
    dirty_file.write_text("leave intact\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    proc, release, lock_path = _start_flock_holder(
        tmp_path, repo, seat, parent_id
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=0.12,
    )
    lock_before = lock_path.read_bytes()
    clock = _drive_dispatch_deadline(monkeypatch)
    probe_durations = _record_bind_probe_durations(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        message = str(exc_info.value)
        assert clock.elapsed == pytest.approx(args.capacity_wait_s)
        assert len(probe_durations) > 1
        assert message.count(parent_id) == 1
        assert "holder_liveness=live" in message
        assert args.cwd == str(seat)
        assert lock_path.read_bytes() == lock_before
        assert _git(seat, "rev-parse", "HEAD") == head_before
        assert dirty_file.read_text(encoding="utf-8") == "leave intact\n"
        assert not (seat.parent / "s-2").exists()
    finally:
        release.touch()
        proc.wait(timeout=5)


def test_rejected_resume_preserves_relative_cwd_pin_and_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent-terminal"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    initial.release()
    _write_terminal_seat_record(parent_id, repo, seat)
    proc, release, _lock_path = _start_flock_holder(
        tmp_path, repo, seat, "resume-occupancy-holder", occupancy=True
    )
    args, claim, claim_before = _queue_retry_args(
        tmp_path, repo, seat, parent_id, wait_s=0.12
    )
    args.parent_dispatch_id = parent_id
    args.cwd = str(seat.relative_to(repo))
    cwd_before = args.cwd
    pin_before = args.worktree_pin_holder
    clock = _drive_dispatch_deadline(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._admit_dispatch_worktree(args)
        assert clock.elapsed == pytest.approx(0.12)
        assert "resume-occupancy-holder" in str(exc_info.value)
        assert args.cwd == cwd_before
        assert args.worktree_pin_holder == pin_before
        assert claim.read_bytes() == claim_before
        assert not (seat.parent / "s-2").exists()
    finally:
        release.touch()
        proc.wait(timeout=5)


def test_pinned_parent_with_unknown_worker_liveness_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent-unknown"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    initial.release()
    dirty_file = seat / "unknown-parent-dirty.txt"
    dirty_file.write_text("leave intact\n", encoding="utf-8")
    head_before = _git(seat, "rev-parse", "HEAD")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
            "worktree_head": head_before,
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(seat.relative_to(repo)),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        worktree_pin_holder=parent_id,
        from_queue=True,
        queue_claim_path=str(tmp_path / "resume-unknown.claimed"),
        queue_launch_token="resume-unknown-token",
        capacity_wait_s=0.2,
    )
    claim = Path(args.queue_claim_path)
    claim.write_bytes(b'{"dispatch_id":"resume-child","state":"claimed"}\n')
    claim_before = claim.read_bytes()
    cwd_before = args.cwd
    pin_before = args.worktree_pin_holder
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="terminal ledger record with proven-exited worker liveness",
    ):
        goalflight_dispatch._admit_dispatch_worktree(args)
    assert args.cwd == cwd_before
    assert args.worktree_pin_holder == pin_before
    assert claim.read_bytes() == claim_before
    assert _lock_dispatch_id(lock_path) == parent_id
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "leave intact\n"
    assert not (seat.parent / "s-2").exists()


def test_pinned_parent_with_exited_worker_but_nonterminal_state_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "resume-parent-running"
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = initial.path
    initial.release()
    head_before = _git(seat, "rev-parse", "HEAD")
    dirty_file = seat / "nonterminal-parent-dirty.txt"
    dirty_file.write_text("leave intact\n", encoding="utf-8")
    status_path = tmp_path / "parent-status.json"
    status_path.write_text(
        json.dumps(
            {
                "dispatch_id": parent_id,
                "state": "complete",
                "worker_pid": 2147483647,
                "expected_worker_identity": {
                    "pid": 2147483647,
                    "start_token": "exited-test-worker",
                },
            }
        ),
        encoding="utf-8",
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "state": "running",
            "terminal_state": "unknown",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_id": seat.name,
            "worktree_path": str(seat),
            "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
            "worktree_head": head_before,
            "status_path": str(status_path),
        }
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="resume-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=0.2,
    )
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="terminal ledger record with proven-exited worker liveness",
    ):
        goalflight_dispatch._admit_dispatch_worktree(args)
    assert _lock_dispatch_id(lock_path) == parent_id
    assert _git(seat, "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "leave intact\n"
    assert not (seat.parent / "s-2").exists()


def test_pinned_occupancy_flock_refuses_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, "seat-owner")
    seat = initial.path
    initial.release()
    proc, release, _lock_path = _start_flock_holder(
        tmp_path, repo, seat, "occupancy-holder", occupancy=True
    )
    args = SimpleNamespace(
        agent="codex-acp",
        shape="acp",
        read_only=False,
        worktree="HEAD",
        parent_dispatch_id=None,
        dispatch_id="pinned-occupancy",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        from_queue=True,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=0.12,
    )
    clock = _drive_dispatch_deadline(monkeypatch)
    probe_durations = _record_path_probe_durations(monkeypatch)
    try:
        with pytest.raises(goalflight_dispatch.DispatchUsageError) as exc_info:
            goalflight_dispatch._prepare_attempt_worktree_occupancy(args)
        message = str(exc_info.value)
        assert clock.elapsed == pytest.approx(args.capacity_wait_s)
        assert probe_durations
        assert message.count("occupancy-holder") == 1
        assert "holder_liveness=live" in message
        assert args.cwd == str(seat)
    finally:
        release.touch()
        proc.wait(timeout=5)


def test_pinned_occupancy_flock_proceeds_when_released_within_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_pinned_test_env(monkeypatch)
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    initial = goalflight_worktree_pool.acquire_worktree_seat(repo, "seat-owner")
    seat = initial.path
    initial.release()
    proc, release, _lock_path = _start_flock_holder(
        tmp_path, repo, seat, "occupancy-holder", occupancy=True
    )
    args = SimpleNamespace(
        agent="codex-acp",
        shape="acp",
        read_only=False,
        worktree="HEAD",
        parent_dispatch_id=None,
        dispatch_id="pinned-occupancy",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        from_queue=True,
        controller_label=None,
        worktree_root=None,
        _worktree_seat=None,
        capacity_wait_s=0.8,
    )
    path_lock_attempts: list[bool] = []
    original_probe = goalflight_worktree_pool.try_acquire_worktree_path_lock

    def release_after_first_busy_probe(*probe_args, **probe_kwargs):
        path_lock_attempts.append(True)
        try:
            return original_probe(*probe_args, **probe_kwargs)
        except goalflight_worktree_pool.WorktreePathLockBusy:
            if len(path_lock_attempts) == 1:
                release.touch()
                proc.wait(timeout=5)
            raise

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "try_acquire_worktree_path_lock",
        release_after_first_busy_probe,
    )
    clock = _drive_dispatch_deadline(monkeypatch)
    try:
        assert goalflight_dispatch._prepare_attempt_worktree_occupancy(args) is None
        assert len(path_lock_attempts) == 2
        assert len(clock.requested_sleeps) == 1
        assert clock.elapsed == pytest.approx(0.05)
        assert sum(clock.requested_sleeps) <= args.capacity_wait_s
    finally:
        release.touch()
        proc.wait(timeout=5)
        goalflight_dispatch._release_worktree_occupancy_lock(args)


def _completion_refusal_env(tmp_path: Path) -> dict[str, str]:
    """Launch env that cannot touch the operator's ledger, journal, or tasks."""
    env = _env(tmp_path, seats=2)
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp_path / "task-store")
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "4"
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_DISPATCH_SCRIPT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_PROCESS_ROLE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
    ):
        env.pop(key, None)
    return env


def _apply_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_DISPATCH_SCRIPT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_PROCESS_ROLE",
        "GOALFLIGHT_WORKTREE_LOCK_FD",
        "GOALFLIGHT_OCCUPANCY_LOCK_FD",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
    ):
        if key not in env:
            monkeypatch.delenv(key, raising=False)


def _lock_dispatch_id(lock_path: Path) -> str:
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    return str(payload.get("dispatch_id") or "")


def _assert_pool_admission_refuses_occupied_path(
    repo: Path, seat: Path, dispatch_id: str
) -> None:
    try:
        admitted = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            dispatch_id,
            occupy_path=seat,
            reset=False,
        )
    except goalflight_worktree_pool.WorktreeSeatUnavailable:
        return
    admitted.release()
    pytest.fail("pool admission accepted an unregistered or foreign occupancy lock")


def test_pool_admission_reuses_preclaimed_occupancy_lock(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "registered-seed")
    seat = seed.path
    seed.release()
    _write_terminal_seat_record("registered-seed", repo, seat)
    dispatch_id = "preclaimed-child"
    preclaimed = goalflight_worktree_pool.try_acquire_worktree_path_lock(
        seat, dispatch_id
    )
    lease = None
    try:
        lease = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            dispatch_id,
            occupy_path=seat,
            reset=False,
            _preclaimed_occupancy_lock=preclaimed,
        )
        assert lease.path == seat
    finally:
        if lease is not None:
            lease.release()
        preclaimed.release()


@pytest.mark.parametrize(
    "holder_metadata",
    ["stale", "written-by-holder"],
    ids=["unlocked-same-inode-fd", "metadata-written-by-other-holder"],
)
def test_pool_admission_refuses_forged_inherited_occupancy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    holder_metadata: str,
) -> None:
    repo = _make_repo(tmp_path)
    seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "registered-seed")
    seat = seed.path
    seed.release()
    child_id = "resume-child"
    lock_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
    inherited_fd = (
        os.open(lock_path, os.O_RDWR) if holder_metadata == "stale" else None
    )
    holder_id = "foreign-holder" if holder_metadata == "stale" else child_id
    holder, release, _seat_lock_path = _start_flock_holder(
        tmp_path,
        repo,
        seat,
        holder_id,
        occupancy=True,
        record_ledger=False,
    )
    try:
        if inherited_fd is None:
            inherited_fd = os.open(lock_path, os.O_RDWR)
        if holder_metadata == "stale":
            with lock_path.open("r+", encoding="utf-8") as lock_file:
                goalflight_worktree_pool._write_occupant(
                    lock_file, seat_name=seat.name, dispatch_id=child_id
                )
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        assert metadata.get("dispatch_id") == child_id
        if holder_metadata == "written-by-holder":
            assert metadata.get("pid") != os.getpid()
        monkeypatch.setenv(
            goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV,
            str(inherited_fd),
        )
        _assert_pool_admission_refuses_occupied_path(repo, seat, child_id)
    finally:
        release.touch()
        holder_out, holder_err = holder.communicate(timeout=10)
        assert holder.returncode == 0, (holder.returncode, holder_out, holder_err)
        if inherited_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(inherited_fd, fcntl.LOCK_UN)
            os.close(inherited_fd)


def test_pool_admission_refuses_replaced_registered_occupancy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    seed = goalflight_worktree_pool.acquire_worktree_seat(repo, "registered-seed")
    seat = seed.path
    seed.release()
    child_id = "resume-child"
    registered_path = goalflight_worktree_pool.worktree_path_lock_path(seat)
    registered_identity = registered_path.stat()
    holder, release, _seat_lock_path = _start_flock_holder(
        tmp_path,
        repo,
        seat,
        "foreign-holder",
        occupancy=True,
        record_ledger=False,
    )
    inherited_fd = None
    try:
        registered_path.unlink()
        inherited_fd = os.open(
            registered_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
        )
        fcntl.flock(inherited_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with os.fdopen(os.dup(inherited_fd), "r+", encoding="utf-8") as lock_file:
            goalflight_worktree_pool._write_occupant(
                lock_file, seat_name=seat.name, dispatch_id=child_id
            )
        replacement_identity = os.fstat(inherited_fd)
        assert (registered_identity.st_dev, registered_identity.st_ino) != (
            replacement_identity.st_dev,
            replacement_identity.st_ino,
        )
        monkeypatch.setenv(
            goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV,
            str(inherited_fd),
        )
        _assert_pool_admission_refuses_occupied_path(repo, seat, child_id)
    finally:
        release.touch()
        holder_out, holder_err = holder.communicate(timeout=10)
        assert holder.returncode == 0, (holder.returncode, holder_out, holder_err)
        if inherited_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(inherited_fd, fcntl.LOCK_UN)
            os.close(inherited_fd)


@pytest.mark.parametrize("shape", ["bash", "acp"])
def test_completion_refusal_leaves_seat_occupant_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """A launch the completion gate refuses must not rewrite the seat occupant.

    Both launch shapes bind the seat before they refuse today, so a rejected
    child becomes the lock-file occupant and the next resume of the real
    holder is told the seat was reclaimed.
    """
    import goalflight_ledger as ledger
    import goalflight_task

    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    env = _completion_refusal_env(tmp_path)
    _apply_env(monkeypatch, env)
    monkeypatch.chdir(repo)

    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "seat-parent")
    seat = parent.path
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
        repo,
        parent.seat_name,
        controller_label=goalflight_worktree_pool.default_controller_ring_label(
            None, project_root=repo
        ),
    )
    parent.release()
    (seat / "keep-me.txt").write_text("still here\n", encoding="utf-8")
    assert _lock_dispatch_id(lock_path) == "seat-parent"

    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "blocker-dead",
            "agent": "test-dispatch",
            "state": "worker_dead",
            "terminal_state": "worker_dead",
            "project_root": str(repo),
            "task_ids": ["t-370"],
            "started_at": "2026-01-01T00:00:00+00:00",
            "worker_cwd": str(seat),
        }
    )
    store = goalflight_task.TaskStore(repo)
    store.docs_dir.mkdir(parents=True, exist_ok=True)
    store.tasks_path.write_text(
        json.dumps(
            {
                "id": "t-370",
                "kind": "task",
                "title": "held by a dead attempt",
                "blocked_by": [],
                "links": [],
                "tags": [],
                "done": False,
                "created_at": "2026-01-01T00:00:00+00:00",
                "created_by": "test",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    child_id = f"refused-child-{shape}"
    argv = [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--shape",
        shape,
        "--dispatch-id",
        child_id,
        "--task",
        "t-370",
        "--cwd",
        str(seat),
        "--launch-detached",
    ]
    if shape == "acp":
        prompt = tmp_path / "prompt.md"
        prompt.write_text("continue the held task\n", encoding="utf-8")
        argv.extend(["--agent", "claude", "--prompt-file", str(prompt)])
    else:
        argv.extend(
            [
                "--agent",
                "test-dispatch",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit('should-not-spawn')",
            ]
        )

    proc = subprocess.run(
        argv,
        cwd=str(repo),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 64, combined
    assert "partial_task_supersession" in combined, combined
    assert _lock_dispatch_id(lock_path) == "seat-parent", combined
    assert child_id not in _lock_dispatch_id(lock_path)
    assert (seat / "keep-me.txt").read_text(encoding="utf-8") == "still here\n"


def test_skipped_worktree_seats_are_visible_on_stderr_and_status() -> None:
    """Skipped seats are printed and stored on the prelaunch status document."""
    rows = (("s-1", "stale_registration"),)
    args = SimpleNamespace(
        dispatch_id="skip-visible",
        agent="test",
        shape="bash",
        dispatch_warnings=[],
    )
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        goalflight_dispatch._note_skipped_worktree_seats(args, rows)
        goalflight_dispatch._emit_dispatch_warnings(args.dispatch_warnings)
        failure = goalflight_worktree_pool.WorktreeSeatUnavailable(
            "pool exhausted",
            skipped_slots=rows,
        )
        goalflight_dispatch._emit_skipped_worktree_seat_failure(failure)
    text = buf.getvalue()
    payloads = [
        json.loads(line)
        for line in text.splitlines()
        if line.startswith("{")
    ]
    assert payloads == [
        {"skipped_worktree_seats": [{"reason": "stale_registration", "seat": "s-1"}]},
        {"skipped_worktree_seats": [{"reason": "stale_registration", "seat": "s-1"}]},
    ]
    assert (
        "goalflight_dispatch: skipped worktree seats: s-1=stale_registration"
        in text
    )
    metadata = goalflight_dispatch._prelaunch_status_metadata(args)
    assert metadata["skipped_worktree_seats"] == [
        {"seat": "s-1", "reason": "stale_registration"}
    ]


def test_recovery_keep_and_quarantine_refs_include_nanosecond_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("*.local\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore local files")
    monkeypatch.chdir(repo)

    parent_id = "stamped-recovery-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    (parent.path / "unfinished.local").write_bytes(b"unfinished work\n")
    _git(parent.path, "add", "-f", "unfinished.local")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "stamped-recovery-child"
    )
    try:
        assert reclaimer.keep_ref and reclaimer.quarantine_branch
        keep_name = reclaimer.keep_ref.rsplit("/", 1)[-1]
        assert keep_name.startswith("head-")
        assert keep_name.split("-", 2)[1].isdecimal()
        assert reclaimer.quarantine_branch.rsplit("-", 2)[-2].isdecimal()
    finally:
        reclaimer.release()


@pytest.mark.parametrize("archive_fails", [False, True])
@pytest.mark.parametrize("case_variant", [False, True])
def test_resume_archives_and_removes_ignored_occupant_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_fails: bool,
    case_variant: bool,
) -> None:
    if case_variant:
        upper = tmp_path / "ResumeCaseProbe"
        lower = tmp_path / "resumecaseprobe"
        upper.write_text("probe", encoding="utf-8")
        try:
            try:
                case_insensitive = lower.exists() and os.path.samefile(upper, lower)
            except OSError:
                case_insensitive = False
        finally:
            upper.unlink()
        if not case_insensitive:
            pytest.skip("temporary filesystem is case-sensitive")

    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    archive_dir = tmp_path / "seat-archive"
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(archive_dir))
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("*.local\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore local files")
    monkeypatch.chdir(repo)

    parent_id = "ignored-resume-parent"
    parent_bytes = b"parent recovery\x00bytes\n"
    occupant_bytes = b"current occupant\x00bytes\xff\n"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent_name = "Foo.local" if case_variant else "shared.local"
    occupant_name = "foo.local" if case_variant else "shared.local"
    collision = seat / parent_name
    collision.write_bytes(parent_bytes)
    ignored_parent = seat / "parent-only.local"
    ignored_parent.write_bytes(b"ignored parent bytes\x00\xff\n")
    _git(seat, "add", "-f", parent_name)
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "ignored-reclaimer")
    assert reclaimer.quarantine_branch
    occupant_collision = seat / occupant_name
    occupant_collision.write_bytes(occupant_bytes)
    ignored_link = None
    outside_target = None
    if not case_variant:
        outside_target = tmp_path / "ignored-link-target"
        outside_target.write_bytes(b"outside target remains unchanged\n")
        ignored_link = seat / "ignored-link.local"
        ignored_link.symlink_to(outside_target)
    _write_seat_holder_record(
        "ignored-reclaimer",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()

    if archive_fails:
        monkeypatch.setattr(
            goalflight_worktree_gc,
            "_archive_ignored_payload",
            lambda *_args, **_kwargs: (None, "simulated archive failure"),
        )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="ignored-resume-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = None
    try:
        if archive_fails:
            with pytest.raises(
                goalflight_worktree_pool.WorktreeSeatResetRefused,
                match="ignored content could not be preserved",
            ):
                resumed = goalflight_dispatch._bind_dispatch_worktree(args)
            assert occupant_collision.read_bytes() == occupant_bytes
            if ignored_link is not None:
                assert ignored_link.is_symlink()
            assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
                "worktree/ignored-reclaimer"
            )
            return

        resumed = goalflight_dispatch._bind_dispatch_worktree(args)
        assert resumed.path == seat
        assert collision.read_bytes() == parent_bytes
        assert ignored_parent.read_bytes() == b"ignored parent bytes\x00\xff\n"
        if ignored_link is not None:
            assert ignored_link.is_symlink()
            assert os.readlink(ignored_link) == str(outside_target)
            assert outside_target.read_bytes() == b"outside target remains unchanged\n"
        assert Path(args._worktree_ignored_archive).is_file()
        archives = list(archive_dir.glob("*.tar.gz"))
        assert len(archives) == 1
        notice = args._resume_ignored_restore_notice
        assert str(archives[0]) in notice
        assert occupant_name in notice
        import tarfile

        with tarfile.open(archives[0], "r:gz") as archive:
            member = next(
                item for item in archive.getmembers()
                if Path(item.name).name == occupant_name
            )
            archived_file = archive.extractfile(member)
            assert archived_file is not None
            assert archived_file.read() == occupant_bytes
    finally:
        if resumed is not None:
            resumed.release()


def test_resume_refuses_when_current_occupant_quarantine_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(tmp_path / "seat-archive"))
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("*.local\n", encoding="utf-8")
    (repo / "staged.txt").write_text("base staged\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "staged.txt")
    _git(repo, "commit", "-m", "add ignored and staged test paths")
    monkeypatch.chdir(repo)

    parent_id = "quarantine-failure-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    (seat / "shared.local").write_bytes(b"parent payload\n")
    _git(seat, "add", "-f", "shared.local")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "quarantine-failure-current")
    (seat / "shared.local").write_bytes(b"ignored current payload\n")
    (seat / "staged.txt").write_text("staged payload\n", encoding="utf-8")
    _git(seat, "add", "staged.txt")
    (seat / "staged.txt").write_text("different working payload\n", encoding="utf-8")
    _write_seat_holder_record(
        "quarantine-failure-current",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="quarantine-failure-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatResetRefused,
        match="separate staged and working versions",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert (seat / "staged.txt").read_text(encoding="utf-8") == (
        "different working payload\n"
    )
    assert (seat / "shared.local").read_bytes() == b"ignored current payload\n"
    archives = list((tmp_path / "seat-archive").glob("*.tar.gz"))
    assert len(archives) == 1


def test_resume_restores_deleted_and_renamed_paths_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_SEAT_ARCHIVE_DIR", str(tmp_path / "seat-archive"))
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text(
        "deleted.txt\nrename-old.txt\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore deleted fixture paths")
    (repo / "deleted.txt").write_bytes(b"must stay deleted\x00\xff\n")
    (repo / "rename-old.txt").write_bytes(b"renamed bytes\x00\xff\n")
    _git(repo, "add", "-f", "deleted.txt", "rename-old.txt")
    _git(repo, "commit", "-m", "add delete and rename fixtures")
    monkeypatch.chdir(repo)

    parent_id = "exact-restore-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    original_head = _git(seat, "rev-parse", "HEAD")
    (seat / "deleted.txt").unlink()
    _git(seat, "mv", "rename-old.txt", "rename-new.txt")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, "exact-restore-reclaimer")
    _write_seat_holder_record(
        "exact-restore-reclaimer",
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="exact-restore-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert _git(seat, "rev-parse", "HEAD") == original_head
        assert not (seat / "deleted.txt").exists()
        assert not (seat / "rename-old.txt").exists()
        assert (seat / "rename-new.txt").read_bytes() == b"renamed bytes\x00\xff\n"
    finally:
        resumed.release()


def test_resume_selects_newest_quarantine_snapshot_by_ref_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "snapshot-order-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    head = _git(seat, "rev-parse", "HEAD")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    keep_ref = f"refs/goalflight/keep/{parent_id}/head"
    stale_ref = (
        f"refs/heads/goalflight/quarantine/s-1-{parent_id}-"
        "20000101T000000000000Z"
    )
    latest_ref = (
        f"refs/heads/goalflight/quarantine/s-1-{parent_id}-"
        "20300101T000000000000Z"
    )
    _git(repo, "update-ref", keep_ref, head)
    base_tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    stale_commit = _git(
        repo, "commit-tree", base_tree, "-p", head, "-m", "older snapshot"
    )
    index_path = tmp_path / "latest.index"
    index_env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"latest snapshot\n",
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.decode().strip()
    subprocess.run(
        ["git", "read-tree", head], cwd=repo, env=index_env, check=True
    )
    subprocess.run(
        [
            "git", "update-index", "--add", "--cacheinfo",
            f"100644,{blob},latest.txt",
        ],
        cwd=repo,
        env=index_env,
        check=True,
    )
    latest_tree = subprocess.run(
        ["git", "write-tree"], cwd=repo, env=index_env,
        stdout=subprocess.PIPE, check=True,
    ).stdout.decode().strip()
    latest_commit = _git(
        repo, "commit-tree", latest_tree, "-p", head, "-m", "latest snapshot"
    )
    _git(repo, "update-ref", stale_ref, stale_commit)
    _git(repo, "update-ref", latest_ref, latest_commit)

    reclaimer_id = "z-latest-reclaimer"
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, reclaimer_id)
    _write_seat_holder_record(
        reclaimer_id,
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=keep_ref,
        worktree_quarantine_ref=latest_ref,
    )
    reclaimer.release()
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": "a-older-reclaimer",
            "agent": "codex",
            "engine": "codex",
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(repo),
            "worktree_reclaimed_dispatch_id": parent_id,
            "worktree_keep_ref": keep_ref,
            "worktree_quarantine_ref": stale_ref,
        }
    )

    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="snapshot-order-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert (seat / "latest.txt").read_bytes() == b"latest snapshot\n"
        assert _git(seat, "rev-parse", "HEAD") == head
        assert _git(repo, "rev-parse", "--verify", f"{stale_ref}^{{commit}}") == stale_commit
        assert _git(repo, "rev-parse", "--verify", f"{latest_ref}^{{commit}}") == latest_commit
    finally:
        resumed.release()


def test_failed_recycled_restore_leaves_seat_allocatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "failed-restore-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    (seat / "tracked.txt").write_text("parent bytes\n", encoding="utf-8")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer_id = "failed-restore-reclaimer"
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, reclaimer_id)
    (seat / "reclaimer-commit.txt").write_text("saved occupant head\n", encoding="utf-8")
    _git(seat, "add", "reclaimer-commit.txt")
    _git(seat, "commit", "-m", "save current occupant head")
    reclaimer_head = _git(seat, "rev-parse", "HEAD")
    _write_seat_holder_record(
        reclaimer_id,
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=reclaimer.keep_ref,
        worktree_quarantine_ref=reclaimer.quarantine_branch,
    )
    reclaimer.release()

    def fail_after_partial_restore(path: Path, **_kwargs) -> None:
        (path / "partial-restore.txt").write_text("partial\n", encoding="utf-8")
        raise goalflight_worktree_pool.WorktreeSeatResetRefused("restore failed")

    restore_quarantined_worktree = (
        goalflight_worktree_pool._restore_quarantined_worktree
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_restore_quarantined_worktree",
        fail_after_partial_restore,
    )
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="failed-restore-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    with pytest.raises(
        goalflight_worktree_pool.WorktreeSeatResetRefused, match="restore failed"
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
        f"worktree/{reclaimer_id}"
    )
    assert _git(seat, "rev-parse", "HEAD") == reclaimer_head
    reclaimer_keep_refs = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/goalflight/keep/{reclaimer_id}/",
    ).splitlines()
    assert any(
        ref.rsplit("/", 1)[-1].startswith("head-")
        and _git(repo, "rev-parse", ref) == reclaimer_head
        for ref in reclaimer_keep_refs
    )
    assert not _git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/goalflight/restored/{parent_id}/",
    )

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_restore_quarantined_worktree",
        restore_quarantined_worktree,
    )
    retry = goalflight_worktree_pool.acquire_worktree_seat(repo, "after-failed-restore")
    try:
        assert retry.path == seat
        assert retry.quarantine_branch
    finally:
        retry.release()


def test_restored_recovery_snapshot_does_not_block_later_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "consumed-snapshot-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    saved_head = _git(seat, "rev-parse", "HEAD")
    (seat / "preserved.txt").write_text("recovered work\n", encoding="utf-8")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    reclaimer_id = "consumed-snapshot-reclaimer"
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(repo, reclaimer_id)
    keep_ref = reclaimer.keep_ref
    quarantine_ref = reclaimer.quarantine_branch
    assert keep_ref and quarantine_ref
    _write_seat_holder_record(
        reclaimer_id,
        repo,
        reclaimer,
        state="complete",
        worktree_reclaimed_dispatch_id=parent_id,
        worktree_keep_ref=keep_ref,
        worktree_quarantine_ref=quarantine_ref,
    )
    reclaimer.release()

    restore_verified = False
    restore = goalflight_worktree_pool._restore_quarantined_worktree
    git = goalflight_worktree_pool._git
    git_proc = goalflight_worktree_pool._git_proc

    def verify_before_restore_marker(path: Path, **kwargs) -> None:
        nonlocal restore_verified
        restore(path, **kwargs)
        restore_verified = True

    def create_marker_only_after_restore(path: Path, *args, **kwargs):
        if args and args[0] == "update-ref" and "refs/goalflight/restored/" in str(args[1]):
            assert restore_verified
            assert args[-1] == ""
        return git(path, *args, **kwargs)

    def fail_marker_post_write_verification(path: Path, *args, **kwargs):
        if args and args[0] == "rev-parse" and any(
            "refs/goalflight/restored/" in str(arg) for arg in args
        ):
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                stdout="",
                stderr="simulated marker verification failure",
            )
        return git_proc(path, *args, **kwargs)

    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_restore_quarantined_worktree",
        verify_before_restore_marker,
    )
    monkeypatch.setattr(
        goalflight_worktree_pool, "_git", create_marker_only_after_restore
    )
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_git_proc",
        fail_marker_post_write_verification,
    )

    first_args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id=parent_id,
        dispatch_id="consumed-snapshot-child",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    first = goalflight_dispatch._bind_dispatch_worktree(first_args)
    try:
        marker_refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            f"refs/goalflight/restored/{parent_id}/",
        ).splitlines()
        assert len(marker_refs) == 1
        assert marker_refs[0].rsplit("/", 1)[-1].split("-", 1)[0].isdecimal()
        assert _git(seat, "rev-parse", "HEAD") == saved_head
        assert (seat / "preserved.txt").read_text(encoding="utf-8") == "recovered work\n"
        (seat / "child-commit.txt").write_text("new child work\n", encoding="utf-8")
        _git(seat, "add", "child-commit.txt")
        _git(seat, "commit", "-m", "commit after recovery")
        child_head = _git(seat, "rev-parse", "HEAD")
    finally:
        first.release()

    _write_seat_holder_record(
        "consumed-snapshot-child",
        repo,
        first,
        state="blocked",
        parent_dispatch_id=parent_id,
    )
    second_args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="consumed-snapshot-child",
        dispatch_id="consumed-snapshot-grandchild",
        project_root=str(repo),
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )
    child_record = goalflight_ledger.read_record("consumed-snapshot-child")
    assert child_record is not None
    goalflight_dispatch._validate_resume_worktree_source(
        "consumed-snapshot-child", child_record, seat
    )
    second = goalflight_dispatch._bind_dispatch_worktree(second_args)
    try:
        assert second.path == seat
        assert _git(seat, "rev-parse", "HEAD") == child_head
        assert _git(repo, "rev-parse", f"{keep_ref}^{{commit}}") == saved_head
        assert _git(repo, "rev-parse", f"{quarantine_ref}^{{commit}}")
    finally:
        second.release()


def test_keep_snapshot_order_uses_nanosecond_stamps_within_one_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "ref-time-parent"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    head = _git(seat, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    def commit_at(date: str, message: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", head, "-m", message],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()

    stale_commit = commit_at("2030-01-01T00:00:00+0000", "newer commit, older ref")
    current_commit = commit_at("2000-01-01T00:00:00+0000", "older commit, newer ref")
    same_second = 1_900_000_000_000_000_000
    stale_stamp = same_second + 100_000_000
    restored_stamp = same_second + 500_000_000
    current_stamp = same_second + 800_000_000
    assert stale_stamp // 1_000_000_000 == current_stamp // 1_000_000_000
    stale_ref = (
        f"refs/goalflight/keep/{parent_id}/head-{stale_stamp}-{stale_commit[:12]}"
    )
    current_ref = (
        f"refs/goalflight/keep/{parent_id}/head-{current_stamp}-{current_commit[:12]}"
    )
    restore_marker = (
        f"refs/goalflight/restored/{parent_id}/{restored_stamp}-{head[:12]}"
    )
    for ref, commit in (
        (stale_ref, stale_commit),
        (current_ref, current_commit),
        (restore_marker, head),
    ):
        _git(repo, "update-ref", ref, commit, "")

    refs = goalflight_dispatch._resume_recovery_refs_for_parent(repo, parent_id)
    assert refs[0] == current_ref
    assert stale_ref not in refs


def test_branch_refusal_does_not_clean_untracked_files_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    owner = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "branch-refusal-owner", base="HEAD"
    )
    seat = owner.path
    base = _git(seat, "rev-parse", "HEAD")
    target_branch = "worktree/branch-refusal-parent"
    _git(repo, "branch", target_branch, base)
    external = tmp_path / "external-checkout"
    _git(repo, "worktree", "add", str(external), target_branch)
    preserved = seat / "untracked-before-refusal.txt"
    preserved.write_bytes(b"must remain before branch refusal\x00\xff\n")
    try:
        with pytest.raises(goalflight_worktree_pool.WorktreeSeatError):
            goalflight_worktree_pool._prepare_seat_checkout(
                seat,
                branch=target_branch,
                base_commit=base,
                keep_id="branch-refusal-resume-child",
            )
        assert preserved.read_bytes() == b"must remain before branch refusal\x00\xff\n"
    finally:
        owner.release()
        _git(repo, "worktree", "remove", "--force", str(external))


def test_reclaimed_read_only_resume_prompt_names_the_new_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    old_path = tmp_path / "reclaimed-read-only-seat"
    new_path, _selected, hold = goalflight_worktree_pool.bind_read_only_worktree(
        repo, "readonly-prompt-seed", base=base
    )
    prompt = tmp_path / "resume-prompt.md"
    prompt.write_text("Continue the review.\n", encoding="utf-8")
    args = SimpleNamespace(
        cwd=str(old_path),
        parent_dispatch_id="readonly-prompt-parent",
        dispatch_id="readonly-prompt-child",
        read_only=True,
    )
    try:
        goalflight_dispatch._record_shared_read_only_worktree(
            args, new_path, base, hold
        )
        assembled = goalflight_dispatch._materialize_steer_prompt(
            str(prompt),
            tmp_path,
            args.dispatch_id,
            agent="codex",
            resume_cwd_notice=args._resume_cwd_notice,
        )
        text = Path(assembled).read_text(encoding="utf-8")
        assert f"Your previous checkout path {old_path} was reclaimed" in text
        assert f"you are now in {new_path}" in text
        assert f"do not read {old_path}" in text
    finally:
        if hold is not None:
            hold.release()


def test_resume_discovers_recovery_refs_without_reclaimer_ledger_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "crash-before-reclaim-record"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    head = _git(seat, "rev-parse", "HEAD")
    parent.release()
    _write_seat_holder_record(parent_id, repo, parent, state="blocked")

    keep_ref = f"refs/goalflight/keep/{parent_id}/head"
    quarantine_ref = (
        f"refs/heads/goalflight/quarantine/s-1-{parent_id}-"
        "20261016T120000000000Z"
    )
    commit = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", f"{head}^{{tree}}"),
        "-p",
        head,
        "-m",
        "recovery without ledger row",
    )
    _git(repo, "update-ref", keep_ref, head)
    _git(repo, "update-ref", quarantine_ref, commit)

    refs = goalflight_dispatch._resume_recovery_refs_for_parent(repo, parent_id)
    assert keep_ref in refs
    assert quarantine_ref in refs


def test_raw_read_only_command_gets_a_writer_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = SimpleNamespace(
        agent="codex",
        shape="bash",
        read_only=True,
        os_sandbox=None,
        worker=["--", "sh", "-c", "touch writable.txt"],
        project_root=str(repo),
        cwd=None,
        worktree=None,
        worktree_root=None,
        dispatch_id="raw-read-only-writer",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        _worktree_seat=None,
    )

    lease = goalflight_dispatch._bind_dispatch_worktree(args)
    assert lease is not None
    try:
        assert args.cwd == str(lease.path)
        assert lease.path.parent == goalflight_worktree_pool.repository_worktree_root(repo)
        assert lease.path.parent.name != goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR
    finally:
        lease.release_unlaunched()


def test_s7_prelaunch_child_attribution_uses_resume_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "7")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    fillers = [
        goalflight_worktree_pool.acquire_worktree_seat(repo, f"filler-{index}")
        for index in range(6)
    ]
    parent_id = "example-dispatch-1"
    resume_id = "example-resume-2"
    child_id = "example-resume-3"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    assert seat.name == "s-7"
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)

    resume = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        resume_id,
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id=parent_id,
    )
    _write_terminal_seat_record(
        resume_id, repo, seat, parent_dispatch_id=parent_id
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "parent_dispatch_id": resume_id,
            "project_root": str(repo),
            "state": "failed",
            "terminal_state": "error",
            "prelaunch_failure": True,
            "worker_pid": None,
            "worker_identity": None,
            "codex_session_id": "inherited-session",
            "engine_session_id": "inherited-session",
        }
    )
    goalflight_worktree_pool._write_occupant(
        resume._lock_file,
        seat_name=seat.name,
        dispatch_id=child_id,
    )

    try:
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable
        ) as busy:
            goalflight_worktree_pool.acquire_worktree_seat(
                repo,
                "resume-while-live-holder",
                occupy_path=seat,
                reset=False,
                expected_prior_dispatch_id=resume_id,
            )
        assert not isinstance(busy.value, goalflight_worktree_pool.WorktreeSeatReclaimed)
    finally:
        resume.release()

    _rewrite_test_seat_lock_identity_dead(
        repo, seat.name
    )

    resumed = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "resume-after-prelaunch-child",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id=resume_id,
    )
    try:
        assert resumed.path == seat
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{parent_id}"
    finally:
        resumed.release()
        for lease in fillers:
            lease.release()


@pytest.mark.parametrize("lock_identity", ["live", "unknown", "dead"])
def test_prelaunch_attribution_keeps_raw_lock_owner_for_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_identity: str,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "raw-lock-parent"
    resume_id = "raw-lock-resume"
    child_id = "raw-lock-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [],
    )

    resume = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        resume_id,
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id=parent_id,
    )
    _write_terminal_seat_record(
        resume_id, repo, seat, parent_dispatch_id=parent_id
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "parent_dispatch_id": resume_id,
            "project_root": str(repo),
            "state": "failed",
            "terminal_state": "error",
            "prelaunch_failure": True,
            "worker_pid": None,
            "worker_identity": None,
        }
    )
    goalflight_worktree_pool._write_occupant(
        resume._lock_file,
        seat_name=seat.name,
        dispatch_id=child_id,
    )
    assert goalflight_worktree_pool._attribute_prelaunch_seat_holder(
        seat,
        child_id,
        project_root=repo,
        lock_metadata=goalflight_worktree_pool._lock_metadata(resume._lock_file),
    ) == resume_id
    if lock_identity == "unknown":
        metadata = goalflight_worktree_pool._lock_metadata(resume._lock_file)
        metadata.update(pid=None, start_token=None)
        resume._lock_file.seek(0)
        resume._lock_file.truncate()
        json.dump(metadata, resume._lock_file, sort_keys=True)
        resume._lock_file.write("\n")
        resume._lock_file.flush()
    elif lock_identity == "dead":
        _mark_test_seat_lock_identity_dead(resume._lock_file)
    resume.release()

    census_calls: list[bool] = []

    def clear_census(*, timeout_s: float):
        census_calls.append(True)
        return []

    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", clear_census
    )
    if lock_identity == "dead":
        reclaimed = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "raw-lock-retry",
            occupy_path=seat,
            reset=True,
        )
        assert reclaimed.path == seat
        reclaimed.release()
        return

    with pytest.raises(goalflight_worktree_pool.WorktreeSeatUnavailable) as retained:
        goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "raw-lock-retry",
            occupy_path=seat,
            reset=True,
        )

    expected_reason = (
        "identity_live" if lock_identity == "live" else "identity_unknown"
    )
    assert retained.value.reason == expected_reason
    assert census_calls == []


def test_prelaunch_attribution_refuses_a_seat_reset_to_another_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "7")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    fillers = [
        goalflight_worktree_pool.acquire_worktree_seat(repo, f"filler-{index}")
        for index in range(6)
    ]
    branch_owner_id = "example-resume-4"
    resume_parent_id = "example-resume-2"
    child_id = "example-resume-3"
    unrelated_id = "unrelated-reset-dispatch"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, branch_owner_id)
    seat = parent.path
    assert seat.name == "s-7"
    parent.release()
    _write_terminal_seat_record(branch_owner_id, repo, seat)
    resume_parent = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        resume_parent_id,
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id=branch_owner_id,
    )
    _write_terminal_seat_record(
        resume_parent_id, repo, seat, parent_dispatch_id=branch_owner_id
    )
    resume_parent.release()
    _rewrite_test_seat_lock_identity_dead(
        repo, seat.name
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "parent_dispatch_id": resume_parent_id,
            "project_root": str(repo),
            "state": "failed",
            "terminal_state": "error",
            "prelaunch_failure": True,
        }
    )
    reset = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        unrelated_id,
        occupy_path=seat,
        reset=True,
        expected_prior_dispatch_id=resume_parent_id,
    )
    reset.release()
    _write_terminal_seat_record(unrelated_id, repo, seat)
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    with lock_path.open("r+", encoding="utf-8") as handle:
        goalflight_worktree_pool._write_occupant(
            handle, seat_name=seat.name, dispatch_id=child_id
        )
        _mark_test_seat_lock_identity_dead(handle)

    try:
        try:
            admitted = goalflight_worktree_pool.acquire_worktree_seat(
                repo,
                "resume-after-unrelated-reset",
                occupy_path=seat,
                reset=False,
                expected_prior_dispatch_id=resume_parent_id,
            )
        except goalflight_worktree_pool.WorktreeSeatError:
            pass
        else:
            admitted.release()
            pytest.fail("resume adopted a seat reset to an unrelated branch")
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            f"worktree/{unrelated_id}"
        )
        assert _lock_dispatch_id(lock_path) == child_id
    finally:
        for lease in fillers:
            lease.release()


@pytest.mark.parametrize("evidence", ["identity", "cwd", "spawn"])
def test_prelaunch_attribution_refuses_worker_evidence_on_child(
    evidence: str, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    parent_id = "live-evidence-parent"
    child_id = "live-evidence-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)
    worker_evidence = {
        "identity": {
            "worker_pid": os.getpid(),
            "worker_identity": goalflight_ledger.process_identity(os.getpid()),
        },
        "cwd": {"worker_cwd": str(seat)},
        "spawn": {"worker_spawn_attempted": True},
    }[evidence]
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "parent_dispatch_id": parent_id,
            "project_root": str(repo),
            "state": "failed",
            "terminal_state": "error",
            "prelaunch_failure": True,
            **worker_evidence,
        }
    )
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    with lock_path.open("r+", encoding="utf-8") as handle:
        goalflight_worktree_pool._write_occupant(
            handle, seat_name=seat.name, dispatch_id=child_id
        )

    try:
        admitted = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "resume-live-child",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id=parent_id,
        )
    except goalflight_worktree_pool.WorktreeSeatError:
        pass
    else:
        admitted.release()
        pytest.fail(f"resume adopted a seat with {evidence} evidence on its child")
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{parent_id}"
    assert _lock_dispatch_id(lock_path) == child_id


def test_prelaunch_row_after_reset_cannot_resume_onto_old_parent_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    parent_id = "reset-lineage-parent"
    child_id = "reset-lineage-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)
    child = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        child_id,
        occupy_path=seat,
        reset=True,
        expected_prior_dispatch_id=parent_id,
    )
    child.release_unlaunched()
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": child_id,
            "parent_dispatch_id": parent_id,
            "project_root": str(repo),
            "state": "failed",
            "terminal_state": "error",
            "prelaunch_failure": True,
            "worktree_path": str(seat),
            "worktree_branch": f"worktree/{child_id}",
        }
    )

    try:
        admitted = goalflight_worktree_pool.acquire_worktree_seat(
            repo,
            "resume-reset-lineage-parent",
            occupy_path=seat,
            reset=False,
            expected_prior_dispatch_id=parent_id,
        )
    except goalflight_worktree_pool.WorktreeSeatError:
        pass
    else:
        admitted.release()
        pytest.fail("resume adopted a child branch as its old parent's checkout")
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{child_id}"
    assert _lock_dispatch_id(
        goalflight_worktree_pool.worktree_seat_lock_path(repo, seat.name)
    ) == child_id


def test_prelaunch_withdrawal_clears_child_lock_via_parent_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    parent_id = "withdraw-parent"
    child_id = "withdraw-prelaunch-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)
    child = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": child_id,
        "parent_dispatch_id": parent_id,
        "project_root": str(repo),
        "state": "failed",
        "terminal_state": "error",
        "prelaunch_failure": True,
        "worker_pid": None,
        "worker_identity": None,
        "engine_session_id": "inherited-session",
    }
    goalflight_ledger.write_record(child)
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
        repo,
        seat.name,
        controller_label=goalflight_worktree_pool.default_controller_ring_label(
            None, project_root=repo
        ),
    )
    with lock_path.open("r+", encoding="utf-8") as handle:
        goalflight_worktree_pool._write_occupant(
            handle, seat_name=seat.name, dispatch_id=child_id
        )
    monkeypatch.setattr(
        goalflight_worktree_pool.goalflight_compat,
        "process_identity_matches",
        lambda *_args, **_kwargs: False,
    )

    goalflight_dispatch._release_withdrawn_worktree(child, child_id)

    assert _lock_dispatch_id(lock_path) == parent_id
    resumed = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        "withdraw-parent-resume",
        occupy_path=seat,
        reset=False,
        expected_prior_dispatch_id=parent_id,
    )
    resumed.release()


def test_successful_reset_keeps_lock_text_on_new_branch_when_unlaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "reset-parent"
    child_id = "reset-prelaunch-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    parent.release()
    _write_terminal_seat_record(parent_id, repo, seat)

    (repo / "tracked.txt").write_text("advanced base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "advance base")
    new_base = _git(repo, "rev-parse", "HEAD")
    lease = goalflight_worktree_pool.acquire_worktree_seat(
        repo,
        child_id,
        occupy_path=seat,
        base=new_base,
        reset=True,
        expected_prior_dispatch_id=parent_id,
    )
    lock_path = goalflight_worktree_pool.worktree_seat_lock_path(
        repo,
        seat.name,
        controller_label=goalflight_worktree_pool.default_controller_ring_label(
            None, project_root=repo
        ),
    )
    lease.release_unlaunched()

    assert _lock_dispatch_id(lock_path) == child_id
    assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == f"worktree/{child_id}"


def test_dirty_shared_review_checkout_returns_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    base = _git(repo, "rev-parse", "HEAD")
    shared, _base_commit = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    (shared / "dirty-review.txt").write_text("uncommitted\n", encoding="utf-8")
    args = SimpleNamespace(
        agent="codex",
        shape="bash",
        read_only=True,
        os_sandbox=None,
        worker=[],
        project_root=str(repo),
        cwd=None,
        worktree="HEAD",
        worktree_root=None,
        dispatch_id="dirty-shared-review",
        controller_label=None,
        skip_seat_reset=False,
        in_place=False,
        _worktree_seat=None,
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match="read-only review checkout unavailable",
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)
    assert not (repo / "worktrees" / "s-1").exists()


def _make_release_seat_dispatch(tmp_path: Path, dispatch_id: str):
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    finish_seat_holder(lease)
    _rewrite_seat_holder_as_dead(repo, seat.name, dispatch_id)
    authority = goalflight_journal.open_or_create_journal(repo)
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label="owner",
        owner_session_nonce="release-seat-test",
    )
    assert prepared.committed and prepared.value is not None
    terminal = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete"},
    )
    assert terminal.committed
    identity = {"pid": 2147483647, "start_token": "exited-test-worker"}
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "state": "complete",
        "terminal_state": "complete",
        "controller_label": "owner",
        "worker_pid": identity["pid"],
        "worker_identity": identity,
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_id": seat.name,
        "worktree_path": str(seat),
        "worktree_branch": _git(seat, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktree_head": _git(seat, "rev-parse", "HEAD"),
    }
    goalflight_ledger.write_record(record)
    goalflight_dispatch._queue_entry_path(dispatch_id).parent.mkdir(
        parents=True, exist_ok=True
    )
    lock_path = goalflight_worktree_pool._seat_lock_root(repo) / f"{seat.name}.lock"
    return repo, seat, record, lock_path


def _release_seat_command(dispatch_id: str, *options: str) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = goalflight_dispatch.main(
            ["release-seat", dispatch_id, "--controller-label", "owner", "--json", *options]
        )
    return code, json.loads(output.getvalue())


def test_release_seat_clean_integrated_dispatch_releases_and_allows_reuse(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-clean"
    )

    code, result = _release_seat_command("release-clean")

    assert code == 0 and result["status"] == "released", result
    assert result["integrated_into"] == "main"
    assert goalflight_ledger.read_record("release-clean")["seat_release"]["status"] == "released"
    assert lock_path.read_text(encoding="utf-8") == ""
    assert goalflight_worktree_gc._resumable_terminal_hold_reason(
        goalflight_ledger.read_record("release-clean")
    ) is None
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="no longer resumable because its seat was released",
    ):
        goalflight_dispatch._validate_resume_source("release-clean")

    next_lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "release-reuse")
    try:
        assert next_lease.path == seat
    finally:
        next_lease.release()


def test_release_seat_serializes_with_codex_resume_claim_revalidation(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, _seat, record, _lock_path = _make_release_seat_dispatch(
        tmp_path, "release-resume-race"
    )
    session_id = "00000000-0000-4000-8000-000000000007"
    home = goalflight_dispatch._codex_dispatch_homes_dir() / "release-resume-race"
    rollout = (
        home
        / "sessions"
        / "2026"
        / "01"
        / "01"
        / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    record.update(
        codex_session_id=session_id,
        codex_home=str(home),
        codex_home_owner_dispatch_id="release-resume-race",
    )
    goalflight_ledger.write_record(record)

    source = goalflight_dispatch._validate_resume_source("release-resume-race")
    assert source["session_id"] == session_id
    release_result: list[tuple[int, dict]] = []
    release_started = threading.Event()
    release_done = threading.Event()

    def release() -> None:
        release_started.set()
        release_result.append(_release_seat_command("release-resume-race"))
        release_done.set()

    with goalflight_dispatch._codex_resume_lock(home, session_id):
        thread = threading.Thread(target=release)
        thread.start()
        assert release_started.wait(5), "release worker did not start"
        assert not release_done.wait(0.1), "release must wait for resume claim lock"
        goalflight_dispatch._revalidate_codex_resume_claim(
            parent_dispatch_id="release-resume-race",
            expected_home=home,
            expected_session_id=session_id,
            expected_home_owner_dispatch_id="release-resume-race",
        )
    assert release_done.wait(5), "release did not resume after claim lock was released"
    thread.join(timeout=5)
    assert release_result[0][0] == 0 and release_result[0][1]["status"] == "released"
    with pytest.raises(
        goalflight_dispatch.DispatchUsageError,
        match="no longer resumable because its seat was released",
    ):
        goalflight_dispatch._revalidate_codex_resume_claim(
            parent_dispatch_id="release-resume-race",
            expected_home=home,
            expected_session_id=session_id,
            expected_home_owner_dispatch_id="release-resume-race",
        )


def test_release_seat_refuses_tracked_changes(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-dirty"
    )
    (seat / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    code, result = _release_seat_command("release-dirty")

    assert code == 1 and result["reason_code"] == "tracked_changes", result
    assert (seat / "tracked.txt").read_text(encoding="utf-8") == "dirty\n"
    assert goalflight_ledger.read_record("release-dirty").get("seat_release") is None
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == "release-dirty"


@pytest.mark.parametrize("identity", ("missing", "dead"))
def test_release_refuses_live_seat_cwd_without_live_lock_identity(
    tmp_path: Path,
    isolated_pool_state: Path,
    identity: str,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"release-live-cwd-{identity}"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    lease.release()
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    if identity == "missing":
        payload.pop("pid", None)
        payload.pop("start_token", None)
    else:
        payload.update(pid=2147483647, start_token="exited-test-worker")
    lock_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
        }
    )
    marker = seat / "written-after-quarantine.txt"
    marker.write_text("preserve active work\n", encoding="utf-8")
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=seat,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert worker.poll() is None
        callback_calls: list[Path] = []
        released, reason = goalflight_worktree_pool.release_worktree_for_dispatch(
            repo,
            seat,
            dispatch_id,
            before_release=lambda path: (callback_calls.append(path), marker.unlink()),
        )

        assert not released
        assert (
            "identity_unknown" if identity == "missing" else "worker_cwd_live"
        ) in reason
        assert callback_calls == []
        assert marker.read_text(encoding="utf-8") == "preserve active work\n"
    finally:
        worker.terminate()
        worker.wait(timeout=5)


@pytest.mark.parametrize("census_failure", ("raises", "unknown"))
def test_release_refuses_destructive_callback_when_cwd_census_is_unknown(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    census_failure: str,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = f"release-unknown-census-{census_failure}"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    lease.release()
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload.update(pid=2147483647, start_token="exited-test-worker")
    lock_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
        }
    )
    marker = seat / "preserve-on-unknown-census.txt"
    marker.write_text("preserve active work\n", encoding="utf-8")

    if census_failure == "raises":
        def fail_census(*, timeout_s: float):
            raise OSError("injected incomplete process census")

        monkeypatch.setattr(goalflight_worktree_pool, "_process_cwd_snapshot", fail_census)
    else:
        monkeypatch.setattr(
            goalflight_worktree_pool,
            "_process_cwd_snapshot",
            lambda *, timeout_s: None,
        )

    callback_calls: list[Path] = []
    released, reason = goalflight_worktree_pool.release_worktree_for_dispatch(
        repo,
        seat,
        dispatch_id,
        before_release=lambda path: (callback_calls.append(path), marker.unlink()),
    )

    assert not released
    assert "worker_cwd_unknown" in reason
    assert callback_calls == []
    assert marker.read_text(encoding="utf-8") == "preserve active work\n"


def test_release_seat_missing_identity_refuses_before_checkout_callback(
    tmp_path: Path,
    isolated_pool_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-missing-identity"
    )
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_payload.pop("pid", None)
    lock_payload.pop("start_token", None)
    lock_path.write_text(json.dumps(lock_payload, sort_keys=True) + "\n", encoding="utf-8")
    (seat / "must-not-reset.txt").write_text("retain this work\n", encoding="utf-8")
    callback_calls: list[Path] = []
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_prepare_seat_checkout",
        lambda path, **_kwargs: callback_calls.append(path),
    )

    code, result = _release_seat_command("release-missing-identity")

    assert code == 1 and result["reason_code"] == "seat_holder_unavailable", result
    assert "identity_unknown" in result["reason"]
    assert callback_calls == []
    assert (seat / "must-not-reset.txt").read_text(encoding="utf-8") == "retain this work\n"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == (
        "release-missing-identity"
    )


def test_release_seat_native_dead_holder_with_occupied_census_skips_callback(
    tmp_path: Path,
    isolated_pool_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-native-occupied-census"
    )
    (seat / "must-not-reset.txt").write_text("retain this work\n", encoding="utf-8")
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_process_cwd_snapshot",
        lambda *, timeout_s: [(os.getpid(), str(seat))],
    )
    callback_calls: list[Path] = []
    monkeypatch.setattr(
        goalflight_worktree_pool,
        "_prepare_seat_checkout",
        lambda path, **_kwargs: callback_calls.append(path),
    )

    code, result = _release_seat_command("release-native-occupied-census")

    assert code == 1 and result["reason_code"] == "seat_holder_unavailable", result
    assert "worker_cwd_live" in result["reason"]
    assert callback_calls == []
    assert (seat / "must-not-reset.txt").read_text(encoding="utf-8") == "retain this work\n"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == (
        "release-native-occupied-census"
    )


def test_release_keeps_seat_flock_during_census(
    tmp_path: Path,
    isolated_pool_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    dispatch_id = "release-flock-census-race"
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    lease.release()
    lock_path = goalflight_worktree_pool._candidate_lock_path(
        repo, seat, managed_root=seat.parent
    )
    lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_payload.update(pid=2147483647, start_token="exited-test-worker")
    lock_path.write_text(
        json.dumps(lock_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
        }
    )
    original_metadata = lock_path.read_text(encoding="utf-8")
    marker = seat / "preserve-during-census-race.txt"
    marker.write_text("preserve active work\n", encoding="utf-8")
    worker_processes: list[subprocess.Popen] = []
    claimant_acquired: list[bool] = []

    def census_with_claimant(*, timeout_s: float) -> list[tuple[int, str]]:
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=seat,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        worker_processes.append(worker)
        assert worker.poll() is None
        claimant = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, sys\n"
                    "with open(sys.argv[1], 'r+') as lock:\n"
                    " try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    " except BlockingIOError: sys.exit(3)\n"
                    " fcntl.flock(lock.fileno(), fcntl.LOCK_UN)\n"
                ),
                str(lock_path),
            ],
            cwd=seat,
            capture_output=True,
            text=True,
            timeout=5,
        )
        claimant_acquired.append(claimant.returncode == 0)
        return [(worker.pid, str(seat))]

    monkeypatch.setattr(
        goalflight_worktree_pool, "_process_cwd_snapshot", census_with_claimant
    )
    try:
        released, reason = goalflight_worktree_pool.release_worktree_for_dispatch(
            repo,
            seat,
            dispatch_id,
            before_release=lambda path: marker.unlink(),
        )

        assert claimant_acquired == [False]
        assert not released
        assert "worker_cwd_live" in reason
        assert marker.read_text(encoding="utf-8") == "preserve active work\n"
        assert lock_path.read_text(encoding="utf-8") == original_metadata
    finally:
        for worker in worker_processes:
            worker.terminate()
            worker.wait(timeout=5)


def test_release_seat_refuses_unmerged_branch(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-unmerged"
    )
    (seat / "branch-only.txt").write_text("not integrated\n", encoding="utf-8")
    _git(seat, "add", "branch-only.txt")
    _git(seat, "commit", "-m", "unmerged work")

    code, result = _release_seat_command("release-unmerged")

    assert code == 1 and result["reason_code"] == "branch_not_integrated", result
    assert (seat / "branch-only.txt").exists()
    assert goalflight_ledger.read_record("release-unmerged").get("seat_release") is None
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == "release-unmerged"


def test_release_seat_refuses_non_owner(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _seat, _record, _lock_path = _make_release_seat_dispatch(
        tmp_path, "release-not-owner"
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = goalflight_dispatch.main(
            [
                "release-seat",
                "release-not-owner",
                "--controller-label",
                "other",
                "--json",
            ]
        )
    result = json.loads(output.getvalue())

    assert code == 1 and result["reason_code"] == "not_owner", result
    assert goalflight_ledger.read_record("release-not-owner").get("seat_release") is None


def test_release_seat_quarantines_untracked_files_before_release(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-untracked"
    )
    (seat / "user-work.txt").write_text("keep me\n", encoding="utf-8")

    code, result = _release_seat_command("release-untracked")

    assert code == 0 and result["status"] == "released", result
    quarantine_ref = result["quarantine_ref"]
    assert _git(repo, "show", f"{quarantine_ref}:user-work.txt") == "keep me"
    assert not (seat / "user-work.txt").exists()
    assert goalflight_ledger.read_record("release-untracked")["worktree_quarantine_ref"] == quarantine_ref
    assert lock_path.read_text(encoding="utf-8") == ""


def test_release_seat_refuses_parent_preserved_seat_without_cleaning_it(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, child, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-preserved-child"
    )
    parent_id = "release-preserved-parent"
    parent = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": parent_id,
        "agent": "codex",
        "engine": "codex",
        "state": "complete",
        "terminal_state": "complete",
        "controller_label": "owner",
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_path": str(seat),
        "worktree_branch": child["worktree_branch"],
    }
    goalflight_ledger.write_record(parent)
    child.update(
        parent_dispatch_id=parent_id,
        prelaunch_failure=True,
        worker_pid=None,
        worker_identity=None,
        worker_cwd=None,
    )
    goalflight_ledger.write_record(child)
    user_file = seat / "shared-user-data.txt"
    user_file.write_text("keep this shared checkout data\n", encoding="utf-8")

    code, result = _release_seat_command("release-preserved-child")

    assert code == 1 and result["reason_code"] == "seat_preserved_for_parent", result
    assert user_file.read_text(encoding="utf-8") == "keep this shared checkout data\n"
    record = goalflight_ledger.read_record("release-preserved-child")
    assert record.get("seat_release") is None
    assert record.get("worktree_quarantine_ref") is None
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == parent_id


def test_release_seat_midway_refusal_leaves_resume_available(
    tmp_path: Path,
    isolated_pool_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    repo, seat, record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-midway-refusal"
    )
    session_id = "00000000-0000-4000-8000-000000000035"
    home = goalflight_dispatch._codex_dispatch_homes_dir() / "release-midway-refusal"
    rollout = (
        home
        / "sessions"
        / "2026"
        / "01"
        / "01"
        / f"rollout-2026-01-01T00-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    record.update(
        codex_session_id=session_id,
        codex_home=str(home),
        codex_home_owner_dispatch_id="release-midway-refusal",
    )
    goalflight_ledger.write_record(record)
    user_file = seat / "user-work.txt"
    user_file.write_text("preserve after a failed release\n", encoding="utf-8")

    def fail_checkout_reset(*_args, **_kwargs):
        raise goalflight_worktree_pool.WorktreeSeatError("injected reset refusal")

    monkeypatch.setattr(
        goalflight_worktree_pool, "_prepare_seat_checkout", fail_checkout_reset
    )

    code, result = _release_seat_command("release-midway-refusal")

    assert code == 1 and result["reason_code"] == "quarantine_failed", result
    latest = goalflight_ledger.read_record("release-midway-refusal")
    assert latest.get("seat_release") is None
    quarantine_ref = latest["worktree_quarantine_ref"]
    assert _git(repo, "show", f"{quarantine_ref}:user-work.txt") == "preserve after a failed release"
    assert user_file.read_text(encoding="utf-8") == "preserve after a failed release\n"
    assert json.loads(lock_path.read_text(encoding="utf-8"))["dispatch_id"] == "release-midway-refusal"
    source = goalflight_dispatch._validate_resume_source("release-midway-refusal")
    assert source["session_id"] == session_id


def test_release_seat_repeat_is_noop(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-repeat"
    )
    code, first = _release_seat_command("release-repeat")
    assert code == 0 and first["status"] == "released", first
    record_bytes = goalflight_ledger.record_path("release-repeat").read_bytes()
    lock_bytes = lock_path.read_bytes()

    code, second = _release_seat_command("release-repeat")

    assert code == 0 and second["status"] == "already released", second
    assert goalflight_ledger.record_path("release-repeat").read_bytes() == record_bytes
    assert lock_path.read_bytes() == lock_bytes


def test_release_seat_dry_run_writes_nothing(
    tmp_path: Path, isolated_pool_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, seat, _record, lock_path = _make_release_seat_dispatch(
        tmp_path, "release-dry-run"
    )
    untracked = seat / "preview.txt"
    untracked.write_text("preserve on preview\n", encoding="utf-8")
    ledger_path = goalflight_ledger.record_path("release-dry-run")
    record_bytes = ledger_path.read_bytes()
    lock_bytes = lock_path.read_bytes()
    refs_before = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/goalflight/quarantine")

    code, result = _release_seat_command("release-dry-run", "--dry-run")

    assert code == 0 and result["status"] == "dry-run", result
    assert result["quarantine_paths"] == ["preview.txt"]
    assert ledger_path.read_bytes() == record_bytes
    assert lock_path.read_bytes() == lock_bytes
    assert untracked.read_text(encoding="utf-8") == "preserve on preview\n"
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/goalflight/quarantine") == refs_before
