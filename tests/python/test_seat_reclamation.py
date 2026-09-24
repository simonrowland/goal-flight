"""Seat reuse requires terminal state and a dead worker generation."""

import json
import asyncio
import fcntl
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_dispatch_worktree_pool import _make_repo, _git, _env, _dispatch_cmd
import goalflight_dispatch as dispatch
import goalflight_ledger as ledger
import goalflight_worktree_pool as pool


@pytest.fixture
def holder(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    repo = _make_repo(tmp_path)
    lease = pool.acquire_worktree_seat(repo, "old", controller_label="owner")
    path = lease.path
    lease.release()
    row = {"dispatch_id": "old", "state": "complete", "controller_label": "owner",
           "worker_pid": 34567, "worker_identity": {"pid": 34567, "start_token": "old-token"}}
    monkeypatch.setattr(ledger, "read_record", lambda ident: row if ident == "old" else None)
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: False)
    return repo, path, row


def test_terminal_holder_pins_head_and_dirty_files(holder):
    repo, path, row = holder
    (path / "tracked.txt").write_text("committed\n")
    _git(path, "commit", "-am", "worker change")
    head = _git(path, "rev-parse", "HEAD")
    (path / "tracked.txt").write_text("dirty\n")
    (path / "new.txt").write_text("untracked\n")
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path
        assert _git(repo, "rev-parse", "refs/goalflight/keep/old/head") == head
        refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/keep/old/dirty-*").splitlines()
        assert len(refs) == 1
        assert _git(repo, "show", refs[0] + ":tracked.txt") == "dirty"
        assert _git(repo, "show", refs[0] + ":new.txt") == "untracked"
        assert _git(path, "status", "--porcelain") == ""


@pytest.mark.parametrize("live", [True, None])
def test_terminal_live_or_unknown_holder_retained(holder, monkeypatch, live):
    repo, path, row = holder
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: live)
    before = _git(path, "rev-parse", "HEAD")
    (path / "new.txt").write_text("untouched")
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "rev-parse", "HEAD") == before
    assert _git(path, "branch", "--show-current") == "worktree/old"
    assert (path / "new.txt").read_text() == "untouched"


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_nonterminal_holder_retained(holder, state):
    repo, path, row = holder
    row["state"] = state
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")


def test_quarantine_failure_preserves_real_index(holder, monkeypatch):
    repo, path, row = holder
    (path / ".gitignore").write_text("*.bin\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore binary files")
    (path / "valuable.bin").write_text("staged ignored\n")
    _git(path, "add", "-f", "valuable.bin")
    (path / "tracked.txt").write_text("unstaged\n")
    original = _git(path, "diff", "--cached")
    real_git = pool._git
    def fail_ref(cwd, *args, **kwargs):
        if args[0] == "update-ref" and "dirty-" in args[1]:
            raise pool.WorktreeSeatError("pin failed")
        return real_git(cwd, *args, **kwargs)
    monkeypatch.setattr(pool, "_git", fail_ref)
    with pytest.raises(pool.WorktreeSeatError):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "diff", "--cached") == original
    assert (path / "tracked.txt").read_text() == "unstaged\n"


def test_quarantine_preserves_force_staged_ignored_file(holder):
    repo, path, row = holder
    (path / ".gitignore").write_text("*.bin\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore binary files")
    (path / "valuable.bin").write_text("keep this\n")
    _git(path, "add", "-f", "valuable.bin")
    (path / "tracked.txt").write_text("ordinary edit\n")

    with pool.acquire_worktree_seat(repo, "next") as lease:
        refs = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/goalflight/keep/old/dirty-*",
        ).splitlines()
        assert len(refs) == 1
        assert _git(repo, "show", refs[0] + ":valuable.bin") == "keep this"


def test_quarantine_refuses_staged_goalflight_entry(holder):
    repo, path, row = holder
    (path / ".gitignore").write_text(".goal-flight/\n")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "ignore goal-flight")
    notes = path / ".goal-flight" / "seat" / "memory.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("keep this\n")
    _git(path, "add", "-f", ".goal-flight/seat/memory.md")

    with pytest.raises(pool.WorktreeSeatResetRefused, match="staged .goal-flight"):
        pool.acquire_worktree_seat(repo, "next")
    assert notes.read_text() == "keep this\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_quarantine_refuses_clean_filter_bytes(holder, monkeypatch):
    repo, path, row = holder
    payload = path / "valuable.dat"
    payload.write_text("base worktree bytes\n")
    _git(path, "add", "valuable.dat")
    _git(path, "commit", "-m", "track filter candidate")
    (path / ".gitattributes").write_text("*.dat filter=pointer\n")
    _git(path, "add", ".gitattributes")
    _git(path, "commit", "-m", "configure pointer filter")
    clean_filter = path / "clean-filter.sh"
    clean_filter.write_text("#!/bin/sh\nprintf 'LFS_POINTER\\n'\n")
    clean_filter.chmod(0o755)
    _git(path, "config", "filter.pointer.clean", str(clean_filter))
    _git(path, "config", "filter.pointer.smudge", "cat")
    _git(path, "config", "filter.pointer.required", "true")
    payload.write_text("raw worktree bytes\n")
    assert _git(path, "check-attr", "filter", "--", "valuable.dat").endswith(
        "filter: pointer"
    )
    real_git = pool._git
    def clean_status(cwd, *args, **kwargs):
        if args[:2] == ("status", "--porcelain=v1"):
            return ""
        return real_git(cwd, *args, **kwargs)
    monkeypatch.setattr(pool, "_git", clean_status)
    with pytest.raises(pool.WorktreeSeatResetRefused, match="active clean"):
        pool.acquire_worktree_seat(repo, "next")
    assert payload.read_text() == "raw worktree bytes\n"
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_quarantine_failure_skips_candidate_and_reuses_next(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    repo = _make_repo(tmp_path)
    bad = pool.acquire_worktree_seat(repo, "bad")
    good = pool.acquire_worktree_seat(repo, "good")
    nested = bad.path / "nested"
    nested.mkdir()
    _git(nested, "init")
    bad_path = bad.path
    good_path = good.path
    bad.release()
    good.release()

    records = {
        ident: {
            "dispatch_id": ident,
            "state": "complete",
            "worker_pid": 34567,
            "worker_identity": {"pid": 34567, "start_token": f"{ident}-token"},
        }
        for ident in ("bad", "good")
    }
    monkeypatch.setattr(ledger, "read_record", lambda ident: records.get(ident))
    monkeypatch.setattr(
        pool.goalflight_compat,
        "process_identity_matches",
        lambda pid, token: False,
    )

    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == good_path
    assert nested.exists()
    assert bad_path != good_path


def test_quarantine_refuses_separate_staged_and_working_versions(holder):
    repo, path, row = holder
    (path / "tracked.txt").write_text("staged\n")
    _git(path, "add", "tracked.txt")
    (path / "tracked.txt").write_text("working\n")
    with pytest.raises(pool.WorktreeSeatResetRefused, match="separate staged"):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"
    assert _git(path, "diff", "--cached")
    assert (path / "tracked.txt").read_text() == "working\n"


def test_terminal_explicit_prelaunch_failure_reclaims_without_identity(holder):
    repo, path, row = holder
    row.pop("worker_pid")
    row.pop("worker_identity")
    row["prelaunch_failure"] = True
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_terminal_missing_identity_without_prelaunch_proof_is_retained(holder):
    repo, path, row = holder
    row.pop("worker_pid")
    row.pop("worker_identity")
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")


def test_allocation_lock_wait_honors_deadline(holder):
    repo, path, row = holder
    lock_path = pool._seat_lock_root(repo) / "allocation.lock"
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(pool.WorktreeSeatUnavailable, match="allocation lock"):
            pool.acquire_worktree_seat(
                repo,
                "next",
                capacity_deadline=started + 0.05,
            )
        assert time.monotonic() - started < 1.0
    finally:
        lock_file.close()


def test_message_uses_worker_and_ledger_owner(holder, monkeypatch):
    repo, path, row = holder
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: True)
    row["wrapper_pid"] = 45678
    message = pool._busy_worktree_message(repo, 1, [(str(path), {"dispatch_id": "old", "pid": 123})])
    assert "controller=owner" in message
    assert "state=complete" in message
    assert "worker_pid=34567" in message
    assert "wrapper_pid=45678" in message
    assert "pid=123" not in message


def test_local_config_cap_overrides_env(tmp_path, monkeypatch):
    config = tmp_path / "capacity.json"
    config.write_text(json.dumps({"worktrees_per_repo": 20}))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    assert pool.configured_worktree_seats() == 20
    config.write_text("{}")
    assert pool.configured_worktree_seats() == 2
    monkeypatch.delenv("GOALFLIGHT_WORKTREES_PER_REPO")
    monkeypatch.delenv("GOALFLIGHT_WORKTREE_SEATS", raising=False)
    assert pool.configured_worktree_seats() == 15


@pytest.mark.parametrize("value", [0, -1, True, 2.5, "2"])
def test_invalid_local_config_cap_fails_loudly(tmp_path, monkeypatch, value):
    config = tmp_path / "capacity.json"
    config.write_text(json.dumps({"worktrees_per_repo": value}))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    with pytest.raises(pool.WorktreeSeatError, match="worktrees_per_repo"):
        pool.configured_worktree_seats()


def test_unparseable_local_config_cap_fails_loudly(tmp_path, monkeypatch):
    config = tmp_path / "capacity.json"
    config.write_text("not json")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", str(config))
    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "2")
    with pytest.raises(pool.WorktreeSeatError, match="invalid JSON"):
        pool.configured_worktree_seats()


def test_seat_wait_retries_without_terminal_record(monkeypatch):
    args = SimpleNamespace(capacity_wait_s=10)
    lease = object()
    bind = Mock(side_effect=[pool.WorktreeSeatUnavailable("full"), lease])
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", bind)
    monkeypatch.setattr(dispatch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(dispatch, "_prepare_attempt_worktree_occupancy", lambda args: None)
    assert dispatch._admit_dispatch_worktree(args) is lease
    assert bind.call_count == 2


def test_seat_wait_expiry_is_admission_refusal(monkeypatch):
    args = SimpleNamespace(capacity_wait_s=0)
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", Mock(side_effect=pool.WorktreeSeatUnavailable("full")))
    with pytest.raises(pool.WorktreeSeatUnavailable):
        dispatch._admit_dispatch_worktree(args)
    assert args._worktree_seat_refused


@pytest.mark.parametrize("state", ["failed", "error", "cancelled", "worker_dead", "blocked_capacity", "blocked_task_breadcrumb"])
def test_terminal_dead_states_reuse(holder, state):
    repo, path, row = holder
    row["state"] = state
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_dead_status_keeps_launch_identity(holder, tmp_path):
    repo, path, row = holder
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"dispatch_id": "old", "state": "complete", "worker_pid": 34567,
                                  "worker_identity": {}, "expected_worker_identity": row["worker_identity"]}))
    row["status_path"] = str(status)
    with pool.acquire_worktree_seat(repo, "next") as lease:
        assert lease.path == path


def test_path_lock_prevents_reset(holder):
    repo, path, row = holder
    with pool.try_acquire_worktree_path_lock(path, "other"):
        with pytest.raises(pool.WorktreeSeatUnavailable):
            pool.acquire_worktree_seat(repo, "next")
        assert _git(path, "branch", "--show-current") == "worktree/old"


@pytest.mark.parametrize("wait_s", [None, 0.2])
def test_dispatch_refusal_leaves_no_row_and_emits_once(tmp_path, monkeypatch, wait_s):
    env = _env(tmp_path, seats=1)
    for key, value in env.items():
        if key.startswith("GOALFLIGHT_"):
            monkeypatch.setenv(key, value)
    repo = _make_repo(tmp_path)
    with pool.acquire_worktree_seat(repo, "held") as holder:
        command = _dispatch_cmd(tmp_path, repo, "waiting", sys.executable, "-c", "raise AssertionError('launched')")
        if wait_s is not None:
            command[command.index("--"):command.index("--")] = ["--capacity-wait-s", str(wait_s)]
        result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stderr.count("1/1 worktrees busy") == 1
        assert "DISPATCH-END" not in result.stdout
        assert "DISPATCH-BLOCKED" not in result.stdout
        assert ledger.read_record("waiting") is None
        assert not (tmp_path / "waiting.status.json").exists()
        from test_worktree_seat_pool import finish_seat_holder
        finish_seat_holder(holder)
        retried = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=30)
        assert retried.returncode == 0, retried.stdout + retried.stderr
        assert "DISPATCH-LAUNCHED" in retried.stdout


def test_missing_holder_record_retains_tree(holder, monkeypatch):
    repo, path, row = holder
    monkeypatch.setattr(ledger, "read_record", lambda ident: None)
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"


def test_live_resume_holder_retains_old_branch(holder, monkeypatch):
    repo, path, row = holder
    resumed = {**row, "dispatch_id": "resumed", "worker_pid": 67890,
               "worker_identity": {"pid": 67890, "start_token": "live"}}
    pool.worktree_lock_path_for_path(repo, path).write_text(json.dumps({"dispatch_id": "resumed"}))
    monkeypatch.setattr(ledger, "read_record", lambda ident: resumed if ident == "resumed" else row)
    monkeypatch.setattr(pool.goalflight_compat, "process_identity_matches", lambda pid, token: pid == 67890)
    with pytest.raises(pool.WorktreeSeatUnavailable):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "branch", "--show-current") == "worktree/old"
    message = pool._busy_worktree_message(repo, 1, [(str(path), {"dispatch_id": "resumed"})])
    assert "lock holder: s-1=resumed" in message
    assert "worker_pid=67890" in message


def test_saved_head_is_never_overwritten(holder):
    repo, path, row = holder
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", "refs/goalflight/keep/old/head", base)
    (path / "tracked.txt").write_text("new commit\n")
    _git(path, "commit", "-am", "another commit")
    head = _git(path, "rev-parse", "HEAD")
    with pytest.raises(pool.WorktreeSeatResetRefused):
        pool.acquire_worktree_seat(repo, "next")
    assert _git(path, "rev-parse", "HEAD") == head
    assert _git(path, "rev-parse", "refs/goalflight/keep/old/head") == base


def test_full_pool_waits_then_launches_same_id(tmp_path, monkeypatch):
    env = _env(tmp_path, seats=1)
    for key, value in env.items():
        if key.startswith("GOALFLIGHT_"):
            monkeypatch.setenv(key, value)
    repo = _make_repo(tmp_path)
    holder = pool.acquire_worktree_seat(repo, "held")
    # This generation is intentionally gone, even though its PID was reused.
    ledger.record_path("held").write_text(json.dumps({"dispatch_id": "held", "state": "complete",
        "worker_pid": os.getpid(), "worker_identity": {"pid": os.getpid(), "start_token": "previous-generation"}}))
    marker = tmp_path / "launched"
    command = _dispatch_cmd(tmp_path, repo, "waiting", sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('launched')")
    command[command.index("--"):command.index("--")] = ["--capacity-wait-s", "10"]
    proc = subprocess.Popen(command, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while ledger.read_record("waiting") is None and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is None
        time.sleep(0.2)
        assert not marker.exists()
        assert ledger.read_record("waiting")["state"] == "waiting_capacity"
        holder.release()
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, stdout + stderr
        assert stdout.count("DISPATCH-LAUNCHED") == 1
        assert "DISPATCH-BLOCKED" not in stdout
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
    finally:
        holder.release()
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=5)


@pytest.mark.parametrize("free_seat", [False, True])
def test_acp_uses_same_seat_wait_and_refusal(tmp_path, monkeypatch, free_seat):
    import goalflight_acp_run as acp
    from test_worktree_dispatch import runner_args, FakeProc, FakeConn, fake_run_prompt
    from test_worktree_seat_pool import record_finished_holder

    monkeypatch.setenv("GOALFLIGHT_WORKTREES_PER_REPO", "1")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    repo = _make_repo(tmp_path)
    holder = pool.acquire_worktree_seat(repo, "acp-held")
    record_finished_holder("acp-held")
    args = runner_args(repo, "acp-waiter", tmp_path / "acp.status.json")
    args.capacity_wait_s = 3 if free_seat else 0.05
    spawned = []

    async def spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return FakeProc(), FakeConn()

    monkeypatch.setattr(acp, "agent_command", lambda *_args, **kwargs: ("fake-agent", []))
    monkeypatch.setattr(acp, "validate_acp_dispatch_readiness", lambda *args: None)
    monkeypatch.setattr(acp, "validate_os_sandbox_request", lambda *args: None)
    monkeypatch.setattr(acp, "preflight_os_sandbox", lambda *args: None)
    monkeypatch.setattr(acp, "cleanup_ghosts", lambda: None)
    monkeypatch.setattr(acp, "spawn_and_handshake_with_retry", spawn)
    monkeypatch.setattr(acp, "run_prompt", fake_run_prompt)
    timer = threading.Timer(0.5, holder.release) if free_seat else None
    if timer:
        timer.start()
    try:
        payload = asyncio.run(acp.run(args))
        if free_seat:
            assert payload["state"] == "complete", payload
            assert len(spawned) == 1
            assert Path(spawned[0]["cwd"]) == holder.path
        else:
            assert not spawned
            assert payload["state"] == "failed_worktree"
            assert "seat wait expired" in payload["error"]
            assert ledger.read_record("acp-waiter") is None
    finally:
        if timer:
            timer.cancel()
            timer.join()
        holder.release()
