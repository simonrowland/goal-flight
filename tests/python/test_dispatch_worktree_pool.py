#!/usr/bin/env python3
"""Dispatch --worktree acquires a pooled seat; exhaustion refuses; fd is inherited."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
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
import goalflight_worktree_pool  # noqa: E402
from test_worktree_seat_pool import finish_seat_holder, record_finished_holder


@pytest.fixture(autouse=True)
def pool_ledger_matches_child(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))


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


def test_resume_reacquires_exact_seat_and_blocks_fresh_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    seat = parent.path
    parent.release()

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
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match="resume-child",
        ):
            goalflight_worktree_pool.acquire_worktree_seat(repo, "fresh-dispatch")
        assert not (seat.parent / "s-2").exists()
    finally:
        resumed.release()


@pytest.mark.parametrize("shape", ["bash", "acp"])
def test_read_only_bind_uses_clean_pooled_seat_for_bash_and_acp(
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
        assert hold is not None
        assert Path(args.cwd).resolve() == seat.resolve()
        assert not (repo / "worktrees" / goalflight_worktree_pool.READ_ONLY_WORKTREE_DIR).exists()
    finally:
        goalflight_dispatch._release_read_only_worktree_hold(args)


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


def test_acp_in_place_nested_cwd_is_rejected_during_admission(tmp_path: Path) -> None:
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

    with pytest.raises(goalflight_worktree_pool.WorktreeCwdRefused, match="--in-place"):
        goalflight_dispatch._admit_dispatch_worktree(args)


def test_read_only_resume_records_and_touches_checkout_before_waiting(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
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
    assert goalflight_dispatch._ledger_worker_cwd(args, "waiting_capacity") == str(checkout)


def test_read_only_resume_rejects_dirty_fallback_before_binding(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
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


def test_read_only_resume_touches_checkout_under_allocation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    checkout, _selected = goalflight_worktree_pool.shared_read_only_worktree(
        repo, base=base
    )
    held: list[bool] = []
    real_lock = goalflight_worktree_pool._read_only_allocation_lock

    @contextlib.contextmanager
    def observed_lock(project_root: Path):
        with real_lock(project_root):
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
        "engine_session_id": "12345678-1234-4abc-8def-1234567890ab",
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
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent_id = "readonly-capacity-parent"
    child_id = "readonly-capacity-child"
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, parent_id)
    seat = parent.path
    recorded_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "worktree/readonly-foreign", recorded_head)
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
        "engine_session_id": "12345678-1234-4abc-8def-1234567890ab",
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
    queue_dir = tmp_path / "dispatch-queue"
    queue_dir.mkdir()
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
    monkeypatch.setattr(goalflight_dispatch, "_record_ledger", lambda *_a, **_k: None)
    monkeypatch.setattr(goalflight_dispatch, "_finish_ledger", lambda *_a, **_k: None)
    monkeypatch.setattr(goalflight_dispatch, "_release_capacity", lambda *_a, **_k: None)
    monkeypatch.setattr(goalflight_dispatch, "_terminal_worktree_gc", lambda *_a, **_k: None)
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
        with pytest.raises(
            goalflight_worktree_pool.WorktreeSeatUnavailable,
            match=r"(?=.*1/1 worktrees busy)(?=.*s-1=reclaimer)(?=.*worker identity unknown)",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
    finally:
        reclaimer.release()

    record_finished_holder("reclaimer")
    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert resumed.path == seat
        assert _git(seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/resume-parent"
        )
    finally:
        resumed.release()


def test_resume_unknown_lock_refuses_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
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
    goalflight_ledger.write_record(
        {
            "schema": goalflight_ledger.SCHEMA,
            "dispatch_id": parent_id,
            "agent": "grok-code",
            "engine": "grok",
            "shape": "bash",
            "account": "old-seat",
            "effective_account": "old-seat",
            "engine_session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
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
    monkeypatch.setattr(goalflight_dispatch, "_terminal_worktree_gc", lambda *_a, **_k: None)

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
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
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
    reclaimer = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "resume-parent-other"
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
        cwd=str(old_seat),
        skip_seat_reset=True,
        in_place=False,
        controller_label=None,
        _worktree_seat=None,
    )

    try:
        resumed = goalflight_dispatch._bind_dispatch_worktree(args)
        try:
            assert resumed.path != old_seat
            assert resumed.path.name == "s-2"
            assert _git(resumed.path, "rev-parse", "--abbrev-ref", "HEAD") == (
                "worktree/resume-parent"
            )
            assert _git(resumed.path, "rev-parse", "HEAD") == committed_head
            assert args._worktree_branch == "worktree/resume-parent"
            assert args._worktree_head == committed_head
        finally:
            resumed.release()
        assert _git(old_seat, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/resume-parent-other"
        )
    finally:
        reclaimer.release()


def test_resume_recycled_branch_divergence_is_refused_before_new_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
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
    parent.release()
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
        with pytest.raises(
            goalflight_worktree_pool.WorktreeCwdRefused,
            match=r"worktree/resume-parent.*diverged.*recorded head",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
        assert not (repo / "worktrees" / "s-2").exists()
    finally:
        reclaimer.release()


def test_resume_recycled_branch_accepts_root_resume_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
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
        resumed = goalflight_dispatch._bind_dispatch_worktree(args)
        try:
            assert resumed.path.name == "s-2"
            assert resumed.branch == "worktree/resume-root"
        finally:
            resumed.release()
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
            "engine_session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
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
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
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
        if reclaimed_for == "resume-parent":
            with pytest.raises(
                goalflight_worktree_pool.WorktreeCwdRefused,
                match=reclaimer.quarantine_branch,
            ) as exc_info:
                goalflight_dispatch._bind_dispatch_worktree(args)
            assert "git worktree add <recovery-path>" in str(exc_info.value)
            assert not (repo / "worktrees" / "s-2").exists()
        else:
            resumed = goalflight_dispatch._bind_dispatch_worktree(args)
            try:
                assert resumed.path.name == "s-2"
                assert resumed.branch == "worktree/resume-parent"
            finally:
                resumed.release()
    finally:
        reclaimer.release()


def test_resume_recycled_dirty_state_finds_archived_reclaimer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
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
    recovery_ref = reclaimer.quarantine_branch
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
    )

    with pytest.raises(
        goalflight_worktree_pool.WorktreeCwdRefused,
        match=recovery_ref,
    ):
        goalflight_dispatch._bind_dispatch_worktree(args)


def test_resume_reseats_when_recorded_worktree_checkout_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    parent = goalflight_worktree_pool.acquire_worktree_seat(repo, "resume-parent")
    old_seat = parent.path
    recorded_head = _git(repo, "rev-parse", "worktree/resume-parent")
    parent.release()
    # Remove the checkout while retaining the managed seat registration. This
    # is the missing-path variant of a recycled recorded seat.
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

    resumed = goalflight_dispatch._bind_dispatch_worktree(args)
    try:
        assert resumed is not None
        assert _git(resumed.path, "rev-parse", "--abbrev-ref", "HEAD") == (
            "worktree/resume-parent"
        )
    finally:
        resumed.release()


def test_resume_refuses_recorded_branch_owned_by_another_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "2")
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
        with pytest.raises(
            goalflight_worktree_pool.WorktreeCwdRefused,
            match=r"worktree/other-dispatch.*resume-parent",
        ):
            goalflight_dispatch._bind_dispatch_worktree(args)
        assert not (repo / "worktrees" / "s-2").exists()
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
        cwd=str(repo),
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
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        assert launched.get("caffeinate_pid"), combined


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
            "deadline = time.monotonic() + 20\n"
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
        deadline = time.time() + 20
        while time.time() < deadline and not (marker_a.exists() and marker_b.exists()):
            time.sleep(0.05)
        assert marker_a.exists() and marker_b.exists(), (
            "pooled launches did not overlap",
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


def test_dispatch_payload_includes_worktree_branch(
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
        "print('COMPLETE: seat-branch — ok', flush=True)"
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
    # A refused launch must release its seat, including on a failed SHA probe.
    lease = goalflight_worktree_pool.acquire_worktree_seat(
        repo, "after-refusal"
    )
    lease.release()


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
        assert second.path.name == "s-1"
        assert second.branch == "worktree/use-wt-2"
        assert _git(second.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
        keep_refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/keep/").splitlines()
        assert any(_git(repo, "rev-parse", ref) == sha for ref in keep_refs)
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


def test_pin_and_reset_when_same_branch_uniquely_holds_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")
    repo = _make_repo(tmp_path)
    lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")
    sha = _commit_in(lease.path, "retry must not rewind this branch")
    short = _git(lease.path, "rev-parse", "--short", "HEAD")
    finish_seat_holder(lease)

    nxt = goalflight_worktree_pool.acquire_worktree_seat(repo, "same-id")
    try:
        assert _git(nxt.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "main")
        keep_refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/goalflight/keep/").splitlines()
        assert any(_git(repo, "rev-parse", ref) == sha for ref in keep_refs)
    finally:
        nxt.release()


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


def test_sequential_default_dispatch_reuses_one_seat(
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
    assert paths[0] == paths[1]
    assert paths[0].name == "s-1"
    assert not (paths[0].parent / "s-2").exists()


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


def test_queue_retry_carrier_pins_exact_seat_without_reset(
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
    retry = goalflight_dispatch._bind_dispatch_worktree(retry_args)
    assert retry is not None
    try:
        assert retry.path == seat
        assert calls[-1]["reset"] is False
    finally:
        retry.release()


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
