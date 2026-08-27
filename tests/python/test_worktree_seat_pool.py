#!/usr/bin/env python3
"""Acceptance tests for the fixed local worktree seat pool."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree seat leases require POSIX fcntl locks")

import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_worktree_pool


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def git(repo: Path, *args: str) -> str:
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


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "goalflight-test@example.invalid")
    git(repo, "config", "user.name", "Goal Flight Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    return repo


@contextlib.contextmanager
def seat_limit(limit: int):
    name = goalflight_worktree_pool.WORKTREE_SEATS_ENV
    prior = os.environ.get(name)
    os.environ[name] = str(limit)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def pooled_worktrees(repo: Path) -> list[Path]:
    root = repo.resolve() / "worktrees"
    return sorted(path for path in root.glob("wt-*") if path.is_dir())


def start_seat_holder(repo: Path, dispatch_id: str) -> tuple[subprocess.Popen[str], Path]:
    child_code = "\n".join(
        [
            "import signal, sys",
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
            "from pathlib import Path",
            "import goalflight_worktree_pool as pool",
            "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), sys.argv[2])",
            "print(lease.path, flush=True)",
            "signal.pause()",
        ]
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code, str(repo), dispatch_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    assert proc.stdout is not None
    held_path_text = proc.stdout.readline().strip()
    if not held_path_text:
        assert proc.stderr is not None
        raise AssertionError(
            f"holder {dispatch_id} failed before acquire: {proc.stderr.read()}"
        )
    return proc, Path(held_path_text)


def test_hard_ceiling_is_lazy_and_reuses_seats() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))

        first = goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-one")
        assert_true("first seat name", first.path.name == "wt-1")
        assert_true("one lazy checkout", [p.name for p in pooled_worktrees(repo)] == ["wt-1"])

        second = goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-two")
        assert_true("second seat name", second.path.name == "wt-2")
        assert_true("distinct concurrent seats", first.path != second.path)
        assert_true(
            "pool grows with concurrency",
            [p.name for p in pooled_worktrees(repo)] == ["wt-1", "wt-2"],
        )

        try:
            goalflight_worktree_pool.acquire_worktree_seat(repo, "dispatch-three")
        except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            message = str(exc)
            assert_true(
                "ceiling names first occupant",
                "wt-1" in message and "dispatch-one" in message,
            )
            assert_true(
                "ceiling names second occupant",
                "wt-2" in message and "dispatch-two" in message,
            )
        else:
            raise AssertionError("third concurrent dispatch exceeded a two-seat ceiling")
        assert_true("no seat N+1", not (repo.resolve() / "worktrees" / "wt-3").exists())

        first.release()
        second.release()

        # Acceptance property: task count is unbounded but checkout count is not.
        # Three times N sequential dispatches must reuse the existing range.
        for index in range(6):
            lease = goalflight_worktree_pool.acquire_worktree_seat(
                repo, f"sequential-{index}"
            )
            assert_true("sequential reuse chooses existing seat", lease.path.name == "wt-1")
            lease.release()
            assert_true("sequential count stays bounded", len(pooled_worktrees(repo)) <= 2)
        assert_true("ceiling remains exact", not (repo.resolve() / "worktrees" / "wt-3").exists())


def test_process_concurrency_gets_distinct_seats_and_names_all_occupants() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(2):
        repo = make_repo(Path(td))
        holders: list[subprocess.Popen[str]] = []
        try:
            first, first_path = start_seat_holder(repo, "process-one")
            holders.append(first)
            second, second_path = start_seat_holder(repo, "process-two")
            holders.append(second)
            assert_true("processes receive distinct seats", first_path != second_path)
            assert_true(
                "process concurrency fills exact range",
                {first_path.name, second_path.name} == {"wt-1", "wt-2"},
            )
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "process-three")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                detail = str(exc)
                assert_true(
                    "failure names process one",
                    "wt-1" in detail and "process-one" in detail,
                )
                assert_true(
                    "failure names process two",
                    "wt-2" in detail and "process-two" in detail,
                )
            else:
                raise AssertionError("third process exceeded a two-seat ceiling")
            assert_true(
                "process pressure creates no wt-3",
                not (repo / "worktrees" / "wt-3").exists(),
            )
        finally:
            for proc in holders:
                if proc.poll() is None:
                    os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_dirty_seat_is_quarantined_then_reset_on_acquire() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        abandoned = goalflight_worktree_pool.acquire_worktree_seat(repo, "abandoned")
        (abandoned.path / "tracked.txt").write_text("abandoned edit\n", encoding="utf-8")
        (abandoned.path / "abandoned.txt").write_text("preserve me\n", encoding="utf-8")
        abandoned.release()

        reused = goalflight_worktree_pool.acquire_worktree_seat(repo, "next")
        try:
            assert_true("same seat reused", reused.path.name == "wt-1")
            assert_true(
                "tracked file reset to base",
                (reused.path / "tracked.txt").read_text(encoding="utf-8") == "base\n",
            )
            assert_true("untracked file cleaned", not (reused.path / "abandoned.txt").exists())
            assert_true("seat clean after acquire", git(reused.path, "status", "--porcelain") == "")
            branches = git(
                repo,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads/goalflight/quarantine/",
            ).splitlines()
            assert_true("one visible quarantine branch", len(branches) == 1)
            branch = branches[0]
            assert_true("branch names seat", "wt-1" in branch)
            assert_true(
                "quarantine diff non-empty",
                bool(git(repo, "diff", "--name-only", f"main..{branch}")),
            )
            assert_true(
                "tracked edit preserved",
                git(repo, "show", f"{branch}:tracked.txt") == "abandoned edit",
            )
            assert_true(
                "untracked file preserved",
                git(repo, "show", f"{branch}:abandoned.txt") == "preserve me",
            )
        finally:
            reused.release()


def test_sigkill_releases_kernel_lease_without_cleanup() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        child_code = "\n".join(
            [
                "import os, signal, sys, time",
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                "from pathlib import Path",
                "import goalflight_worktree_pool as pool",
                "lease = pool.acquire_worktree_seat(Path(sys.argv[1]), 'killed-worker')",
                "print(lease.path, flush=True)",
                "signal.pause()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(repo)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            assert proc.stdout is not None
            held_path = Path(proc.stdout.readline().strip())
            assert_true("child acquired seat", held_path.name == "wt-1")
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "blocked-worker")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                assert_true("live occupant diagnosed", "killed-worker" in str(exc))
            else:
                raise AssertionError("live child did not hold the kernel lease")

            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)

            replacement = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "replacement-worker"
            )
            try:
                assert_true("killed worker frees same seat", replacement.path == held_path)
            finally:
                replacement.release()
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_parent_release_keeps_inherited_worker_lease_until_worker_dies() -> None:
    with tempfile.TemporaryDirectory() as td, seat_limit(1):
        repo = make_repo(Path(td))
        lease = goalflight_worktree_pool.acquire_worktree_seat(repo, "inherited-worker")
        lock_fd = lease.fileno()
        child_code = "\n".join(
            [
                "import os, signal, sys",
                "os.fstat(int(sys.argv[1]))",
                "print('ready', flush=True)",
                "signal.pause()",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock_fd)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(lock_fd,),
        )
        try:
            assert proc.stdout is not None
            assert_true("worker inherited open lock fd", proc.stdout.readline().strip() == "ready")
            lease.release()
            try:
                goalflight_worktree_pool.acquire_worktree_seat(repo, "premature-reuse")
            except goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
                assert_true("inherited worker remains occupant", "inherited-worker" in str(exc))
            else:
                raise AssertionError("parent release unlocked a live inherited worker seat")

            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            replacement = goalflight_worktree_pool.acquire_worktree_seat(
                repo, "after-inherited-worker-kill"
            )
            replacement.release()
        finally:
            lease.release()
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)


def test_default_seat_count_is_not_a_per_controller_cap() -> None:
    assert_true(
        "default is the documented checkout ceiling",
        goalflight_worktree_pool.DEFAULT_WORKTREE_SEATS == 24,
    )
    assert_true(
        "wt-1 is a pool seat",
        goalflight_worktree_pool.is_pool_seat_path("/repo/worktrees/wt-1"),
    )
    assert_true(
        "ad-hoc task tree is not a pool seat",
        not goalflight_worktree_pool.is_pool_seat_path("/repo/worktrees/t-353-live"),
    )


def main() -> None:
    tests = [
        test_hard_ceiling_is_lazy_and_reuses_seats,
        test_process_concurrency_gets_distinct_seats_and_names_all_occupants,
        test_dirty_seat_is_quarantined_then_reset_on_acquire,
        test_sigkill_releases_kernel_lease_without_cleanup,
        test_parent_release_keeps_inherited_worker_lease_until_worker_dies,
        test_default_seat_count_is_not_a_per_controller_cap,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
