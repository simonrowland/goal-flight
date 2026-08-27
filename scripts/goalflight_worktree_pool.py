#!/usr/bin/env python3
"""Fixed, kernel-leased pool of reusable local Git worktrees."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import TextIO


WORKTREE_SEATS_ENV = "GOALFLIGHT_WORKTREE_SEATS"
WORKTREE_LOCK_FD_ENV = "GOALFLIGHT_WORKTREE_LOCK_FD"
# Seats bound how many worktrees EXIST, not how much work may run: seats are
# reused, so N seats sustains N CONCURRENT workers per project indefinitely
# rather than N total dispatches. 4 was therefore acting as a de-facto
# per-controller worker cap, which was never intended -- several controllers
# share one project root (battery-tool-v2 currently has five), so four seats
# starved the whole project between them.
#
# Derivation. The binding constraints are RAM and the machine concurrency cap,
# not disk: a seat is one git worktree, and the checkout is ~40MB here, so 24
# seats is under 1GB per project. The machine cap is 120 concurrent workers
# across ~5 active projects, i.e. ~24 per project if every project ran flat out
# simultaneously -- which is the number that stops seats from binding before
# the real capacity gate does. Sanity check: today's busiest project ran ~12
# concurrent workers, so 24 leaves 2x headroom and still cannot, by itself,
# reach the 120 machine cap.
DEFAULT_WORKTREE_SEATS = 24
WORKTREE_SEAT_PREFIX = "wt-"
QUARANTINE_REF_PREFIX = "goalflight/quarantine"


class WorktreeSeatError(RuntimeError):
    """Base error for managed worktree seat acquisition."""


class WorktreeSeatUnavailable(WorktreeSeatError):
    """Raised when every configured seat is held."""


class WorktreeSeatLease:
    """A worktree seat whose ownership is exactly one kernel lock."""

    def __init__(
        self,
        *,
        path: Path,
        seat_name: str,
        dispatch_id: str,
        lock_file: TextIO,
        quarantine_branch: str | None,
    ) -> None:
        self.path = path
        self.seat_name = seat_name
        self.dispatch_id = dispatch_id
        self.quarantine_branch = quarantine_branch
        self._lock_file: TextIO | None = lock_file

    def fileno(self) -> int:
        if self._lock_file is None:
            raise WorktreeSeatError(f"worktree seat lease already released: {self.seat_name}")
        return self._lock_file.fileno()

    def release(self) -> None:
        """Drop this process's descriptor; the kernel unlocks after the last holder."""
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        # Do not call LOCK_UN: the worker may hold an inherited descriptor for
        # this same open file description after the runner exits.
        lock_file.close()

    def __enter__(self) -> "WorktreeSeatLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def configured_worktree_seats() -> int:
    raw = os.environ.get(WORKTREE_SEATS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_WORKTREE_SEATS
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorktreeSeatError(
            f"{WORKTREE_SEATS_ENV} must be a positive integer, got {raw!r}"
        ) from exc
    if value < 1:
        raise WorktreeSeatError(
            f"{WORKTREE_SEATS_ENV} must be a positive integer, got {raw!r}"
        )
    return value


def inherited_worktree_lock_fds() -> tuple[int, ...]:
    """Return validated inherited seat-lock descriptors, if this is a child."""
    raw = os.environ.get(WORKTREE_LOCK_FD_ENV, "").strip()
    if not raw:
        return ()
    try:
        fd = int(raw)
        os.fstat(fd)
    except (ValueError, OSError) as exc:
        raise WorktreeSeatError(
            f"{WORKTREE_LOCK_FD_ENV} does not name an open descriptor: {raw!r}"
        ) from exc
    return (fd,)


def _git(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeSeatError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout.strip()


def _git_common_dir(cwd: Path) -> Path:
    raw = Path(_git(cwd, "rev-parse", "--git-common-dir"))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def _git_dir(cwd: Path) -> Path:
    raw = Path(_git(cwd, "rev-parse", "--git-dir"))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def worktree_seat_lock_path(project_root: Path, seat_name: str) -> Path:
    """Return the per-repository lock path for an already-named seat."""
    return _git_dir(project_root.resolve()) / "goalflight-worktree-seat-locks" / f"{seat_name}.lock"


def _verify_project_root(project_root: Path) -> None:
    top = Path(_git(project_root, "rev-parse", "--show-toplevel")).resolve()
    if top != project_root:
        raise WorktreeSeatError(f"--cwd must be the git repository root: {project_root}")


def _verify_existing_seat(project_root: Path, worktree_path: Path) -> None:
    if worktree_path.is_symlink():
        raise WorktreeSeatError(f"managed worktree path must not be a symlink: {worktree_path}")
    if not worktree_path.is_dir():
        raise WorktreeSeatError(f"managed worktree path is not a directory: {worktree_path}")
    top = Path(_git(worktree_path, "rev-parse", "--show-toplevel")).resolve()
    if top != worktree_path.resolve():
        raise WorktreeSeatError(
            f"managed worktree path is not a Git worktree root: {worktree_path}"
        )
    if _git_common_dir(worktree_path) != _git_common_dir(project_root):
        raise WorktreeSeatError(f"managed worktree belongs to another repository: {worktree_path}")


def _write_occupant(lock_file: TextIO, *, seat_name: str, dispatch_id: str) -> None:
    payload = {
        "seat": seat_name,
        "dispatch_id": dispatch_id,
        "pid": os.getpid(),
        "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(payload, lock_file, sort_keys=True)
    lock_file.write("\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _lock_metadata(lock_file: TextIO) -> dict:
    try:
        lock_file.seek(0)
        payload = json.load(lock_file)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _occupant_description(lock_file: TextIO, seat_name: str) -> str:
    payload = _lock_metadata(lock_file)
    dispatch_id = str(payload.get("dispatch_id") or "unknown-dispatch")
    pid = payload.get("pid")
    suffix = f" pid={pid}" if isinstance(pid, int) else ""
    return f"{seat_name}={dispatch_id}{suffix}"


def _quarantine_dirty_worktree(
    worktree_path: Path,
    *,
    seat_name: str,
    abandoned_dispatch_id: str,
) -> str | None:
    dirty = _git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all")
    if not dirty:
        return None

    _git(worktree_path, "add", "-A")
    tree = _git(worktree_path, "write-tree")
    parent = _git(worktree_path, "rev-parse", "HEAD")
    parent_tree = _git(worktree_path, "rev-parse", "HEAD^{tree}")
    if tree == parent_tree:
        raise WorktreeSeatError(
            f"dirty seat {seat_name} cannot be represented by a branch commit; refusing reset"
        )

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    branch = f"{QUARANTINE_REF_PREFIX}/{seat_name}-{stamp}"
    message = (
        f"quarantine abandoned {seat_name}\n\n"
        f"Previous dispatch: {abandoned_dispatch_id}\n"
    )
    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": "Goal Flight Quarantine",
            "GIT_AUTHOR_EMAIL": "goal-flight-quarantine@invalid",
            "GIT_COMMITTER_NAME": "Goal Flight Quarantine",
            "GIT_COMMITTER_EMAIL": "goal-flight-quarantine@invalid",
        }
    )
    commit = _git(
        worktree_path,
        "commit-tree",
        tree,
        "-p",
        parent,
        input_text=message,
        env=commit_env,
    )
    changed = _git(
        worktree_path,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        parent,
        commit,
    )
    if not changed:
        raise WorktreeSeatError(
            f"quarantine commit for dirty seat {seat_name} is empty; refusing reset"
        )
    _git(worktree_path, "update-ref", f"refs/heads/{branch}", commit, "")
    return branch


def acquire_worktree_seat(
    project_root: Path,
    dispatch_id: str,
    *,
    base: str = "HEAD",
    managed_root: Path | None = None,
) -> WorktreeSeatLease:
    """Acquire, prepare, and return one seat without ever exceeding the range."""
    project_root = project_root.resolve()
    _verify_project_root(project_root)
    seat_limit = configured_worktree_seats()
    base_commit = _git(project_root, "rev-parse", "--verify", f"{base}^{{commit}}")

    if managed_root is not None:
        managed_root = managed_root.expanduser()
        if managed_root.is_symlink():
            raise WorktreeSeatError(
                f"managed worktree root must not be a symlink: {managed_root}"
            )
        managed_root = managed_root.resolve(strict=False)
    else:
        managed_root = project_root / "worktrees"
    if managed_root.is_symlink():
        raise WorktreeSeatError(f"managed worktree root must not be a symlink: {managed_root}")
    if managed_root.exists() and not managed_root.is_dir():
        raise WorktreeSeatError(f"managed worktree root is not a directory: {managed_root}")
    managed_root.mkdir(parents=True, exist_ok=True)

    lock_root = _git_dir(project_root) / "goalflight-worktree-seat-locks"
    if lock_root.is_symlink():
        raise WorktreeSeatError(f"worktree seat lock root must not be a symlink: {lock_root}")
    if lock_root.exists() and not lock_root.is_dir():
        raise WorktreeSeatError(f"worktree seat lock root is not a directory: {lock_root}")
    lock_root.mkdir(parents=True, exist_ok=True)

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    allocation_lock_path = lock_root / "allocation.lock"
    try:
        allocation_fd = os.open(allocation_lock_path, flags, 0o600)
    except OSError as exc:
        raise WorktreeSeatError(
            f"cannot open worktree allocation lock {allocation_lock_path}: {exc}"
        ) from exc
    allocation_file = os.fdopen(allocation_fd, "r+", encoding="utf-8")
    try:
        # Serialize the short acquire/reset transaction. This is not seat
        # ownership; it only ensures a contender never reads an occupant's old
        # diagnostic metadata between that occupant's flock and metadata write.
        fcntl.flock(allocation_file.fileno(), fcntl.LOCK_EX)
        occupants: list[str] = []
        for slot in range(1, seat_limit + 1):
            seat_name = f"{WORKTREE_SEAT_PREFIX}{slot}"
            worktree_path = managed_root / seat_name
            lock_path = lock_root / f"{seat_name}.lock"
            try:
                lock_fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise WorktreeSeatError(
                    f"cannot open worktree seat lock {lock_path}: {exc}"
                ) from exc
            lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                occupants.append(_occupant_description(lock_file, seat_name))
                lock_file.close()
                continue

            try:
                prior_dispatch_id = str(
                    _lock_metadata(lock_file).get("dispatch_id") or "unknown-dispatch"
                )
                _write_occupant(lock_file, seat_name=seat_name, dispatch_id=dispatch_id)
                if worktree_path.exists() or worktree_path.is_symlink():
                    _verify_existing_seat(project_root, worktree_path)
                else:
                    _git(
                        project_root,
                        "worktree",
                        "add",
                        "--detach",
                        str(worktree_path),
                        base_commit,
                    )
                    _verify_existing_seat(project_root, worktree_path)

                quarantine_branch = _quarantine_dirty_worktree(
                    worktree_path,
                    seat_name=seat_name,
                    abandoned_dispatch_id=prior_dispatch_id,
                )
                _git(worktree_path, "checkout", "-f", base_commit)
                _git(worktree_path, "clean", "-fd")
                remaining = _git(
                    worktree_path,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                )
                if remaining:
                    raise WorktreeSeatError(
                        f"worktree seat {seat_name} is not clean after acquire-time reset"
                    )
                return WorktreeSeatLease(
                    path=worktree_path,
                    seat_name=seat_name,
                    dispatch_id=dispatch_id,
                    lock_file=lock_file,
                    quarantine_branch=quarantine_branch,
                )
            except BaseException:
                lock_file.close()
                raise

        detail = ", ".join(occupants)
        raise WorktreeSeatUnavailable(
            f"all {seat_limit} worktree seats are held: {detail}"
        )
    finally:
        allocation_file.close()
