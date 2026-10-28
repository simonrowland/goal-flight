#!/usr/bin/env python3
"""Fixed, kernel-leased pool of reusable local Git worktrees."""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, TextIO

import goalflight_compat
import goalflight_dispatch_paths
import goalflight_ledger
import goalflight_journal
import goalflight_subprocess


WORKTREES_PER_REPO_ENV = "GOALFLIGHT_WORKTREES_PER_REPO"
# Deprecated compatibility alias. Keep the symbol and on-disk lock names for
# mixed-version controllers during ``goalflight update`` windows.
WORKTREE_SEATS_ENV = "GOALFLIGHT_WORKTREE_SEATS"
WORKTREE_LOCK_FD_ENV = "GOALFLIGHT_WORKTREE_LOCK_FD"
OCCUPANCY_LOCK_FD_ENV = "GOALFLIGHT_OCCUPANCY_LOCK_FD"
READ_ONLY_ALLOCATION_LOCK_FD_ENV = "GOALFLIGHT_READ_ONLY_ALLOCATION_LOCK_FD"
OCCUPANCY_LOCK_NAME = "goalflight-worktree.lock"
# Documented fallback when no --controller-label and no live lease label is
# available. Stable across dispatches; do not invent a per-launch name.
UNLABELED_CONTROLLER_RING = "unlabeled"
SEAT_NOTES_NAMESPACE = ".goal-flight/seat"
# Per-repository checkout ceiling, NOT a per-controller worker cap. There is no
# such cap in this codebase and none is wanted. The old default of 4 became a
# de-facto fan-out limit and pushed every extra dispatch onto ad-hoc
# `git worktree add`, which is how the bypass (SC-06) became the main road:
# 358 worktrees fleet-wide, 210 of one repo's 211 ad-hoc, 202GB, and a machine
# at 100% disk. Worktrees are REUSED, so N worktrees sustains N CONCURRENT workers per
# project indefinitely rather than N total dispatches. Several controllers
# share one project root, so four worktrees starved the whole project between them.
#
# Derivation. The binding constraints are RAM and the machine concurrency cap,
# not disk: a worktree is one git checkout, ~40MB here, so 15 worktrees is
# under 1GB per project. The machine cap is 120 concurrent workers across ~5
# active projects. Fifteen worktrees leaves room for provider capacity to govern
# concurrency without allowing checkout sprawl. Sanity check: the busiest
# project observed ~12 concurrent workers, so 15 leaves operational headroom.
#
# Raise via GOALFLIGHT_WORKTREES_PER_REPO when one repo needs more concurrent
# worktrees. GOALFLIGHT_WORKTREE_SEATS remains a deprecated alias.
# NEVER lower this default to "shape" concurrency -- that is what made 4 behave
# as a worker cap.
DEFAULT_WORKTREE_SEATS = 15
WORKTREE_SEAT_PREFIX = "wt-"
CAPTIVE_SEAT_PREFIX = "s-"
SEAT_BRANCH_PREFIX = "seat"  # legacy branch prefix, retained for readers
WORKTREE_BRANCH_PREFIX = "worktree"
QUARANTINE_REF_PREFIX = "goalflight/quarantine"
KEEP_REF_PREFIX = "goalflight/keep"
READ_ONLY_WORKTREE_DIR = ".goalflight-readonly"
READ_ONLY_WORKTREE_KEEP = 4
READ_ONLY_WORKTREE_GRACE_S = 60 * 60
WORKTREE_COMPLETE_RETENTION_GRACE_S_ENV = (
    "GOALFLIGHT_WORKTREE_COMPLETE_RETENTION_GRACE_S"
)
DEFAULT_WORKTREE_COMPLETE_RETENTION_GRACE_S = 2 * 60 * 60
READ_ONLY_GIT_TIMEOUT_S = 30.0
SEAT_RESET_GIT_TIMEOUT_S = 30.0
READ_ONLY_REAP_TIMEOUT_S = 30.0
# The host-wide cwd census was measured at roughly one second for thousands of
# process rows under heavy load. Keep a 30x margin while bounding seat recovery;
# recursive lsof +D scans have different costs and do not inform this budget.
PROCESS_CWD_SNAPSHOT_TIMEOUT_S = 30.0


def _worktree_complete_retention_grace_s() -> float:
    raw = os.environ.get(WORKTREE_COMPLETE_RETENTION_GRACE_S_ENV)
    if raw is None:
        return DEFAULT_WORKTREE_COMPLETE_RETENTION_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WORKTREE_COMPLETE_RETENTION_GRACE_S
    if not math.isfinite(value) or value < 0:
        return DEFAULT_WORKTREE_COMPLETE_RETENTION_GRACE_S
    return value


def _read_only_allocation_wait_s(default: float = 540.0) -> float:
    raw = os.environ.get("GOALFLIGHT_READONLY_ALLOCATION_WAIT_S")
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    # inf would wait forever and NaN compares false everywhere; neither is a budget.
    return value if math.isfinite(value) and value >= 0 else default


# Wait budget for the read-only allocation mutex itself, separate from the work
# done while holding it. The mutex serialises allocation so two dispatches can
# never be handed the same checkout, so it cannot be widened to N holders. A
# holder was measured at 80-140 s of CPU under fleet load (b-473), so a 30 s
# wait made concurrent dispatches fail instead of queueing; waiters now queue.
# The default stays under the ACP parent's 620 s launch watch, so a parent
# never reports failure while its child is still queued for this lock.
READ_ONLY_ALLOCATION_WAIT_S = _read_only_allocation_wait_s()
_LOCK_REGISTRY_NAME = "goalflight-worktree-lock-registry.json"
_LOCK_REGISTRY_MUTEX_NAME = "goalflight-worktree-lock-registry.mutex"
_SAFE_RING_LABEL = re.compile(r"[A-Za-z0-9._-]+")


def _deadline_timeout(deadline: float | None, *, cap: float = READ_ONLY_GIT_TIMEOUT_S) -> float | None:
    """Return one bounded Git timeout from an absolute transaction deadline."""
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - time.monotonic()))

# Three-state verdicts, same shape as goalflight_worktree_gc.py. UNKNOWN always
# retains (refuses reset). Do not collapse "could not tell" into a green light.
YES = "yes"
NO = "no"
UNKNOWN = "unknown"

_GIT_MUTATING_COMMANDS = frozenset(
    {
        "add",
        "apply",
        "checkout",
        "clean",
        "commit",
        "cherry-pick",
        "merge",
        "mv",
        "read-tree",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "sparse-checkout",
        "switch",
        "update-index",
    }
)
_GIT_WORKTREE_TARGET_COMMANDS = frozenset({"add", "lock", "move", "remove", "unlock"})


class WorktreeSeatError(RuntimeError):
    """Base error for managed worktree acquisition (legacy class name)."""


class WorktreeSeatUnavailable(WorktreeSeatError):
    """Raised when every configured worktree is held."""

    def __init__(
        self,
        message: str,
        *,
        seat_name: str | None = None,
        current_holder: str | None = None,
        flock_held: bool = False,
        reason: str | None = None,
        skipped_slots: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.seat_name = seat_name
        self.current_holder = current_holder
        self.flock_held = flock_held
        self.reason = reason
        self.skipped_slots = tuple(skipped_slots)


class WorktreeReadOnlyLockTimeout(WorktreeSeatUnavailable):
    """Raised when the detached-checkout allocation lock cannot be acquired."""


class WorktreeSeatReclaimed(WorktreeSeatUnavailable):
    """Raised when a resume's recorded seat belongs to another dispatch."""

    def __init__(
        self,
        message: str,
        *,
        seat_name: str | None = None,
        current_holder: str | None = None,
        expected_holder: str | None = None,
        terminal_reclaimed: bool = False,
        flock_held: bool = False,
    ) -> None:
        super().__init__(
            message,
            seat_name=seat_name,
            current_holder=current_holder,
            flock_held=flock_held,
        )
        self.expected_holder = expected_holder
        self.terminal_reclaimed = terminal_reclaimed


class WorktreeSeatResetRefused(WorktreeSeatError):
    """Raised when resetting a free worktree would lose unique or undetermined work."""

    def __init__(
        self,
        message: str,
        *,
        skipped_slots: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.skipped_slots = tuple(skipped_slots)


class WorktreePathLockBusy(WorktreeSeatError):
    """Raised when the exclusive worktree-path lock is already held."""

    def __init__(self, message: str, *, occupant_id: str | None = None) -> None:
        super().__init__(message)
        self.occupant_id = occupant_id


class WorktreePathLockUnknown(WorktreeSeatError):
    """Raised when the worktree-path lock cannot be evaluated."""


class WorktreeCwdRefused(WorktreeSeatError):
    """Raised when ``--cwd`` names a path the controller is not allowed to mint."""


class WorktreePathLock:
    """Kernel lock on an arbitrary worktree path.

    Ownership is the open file description: close the descriptor (or die) and
    the kernel releases the claim. Do not LOCK_UN while a worker may still
    hold an inherited descriptor for the same description.
    """

    def __init__(self, *, path: Path, lock_file: TextIO, dispatch_id: str) -> None:
        self.path = path
        self.dispatch_id = dispatch_id
        self._lock_file: TextIO | None = lock_file

    def fileno(self) -> int:
        if self._lock_file is None:
            raise WorktreeSeatError(
                f"worktree path lock already released: {self.path}"
            )
        return self._lock_file.fileno()

    def release(self) -> None:
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        lock_file.close()

    def __enter__(self) -> "WorktreePathLock":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class WorktreeSeatLease:
    """A worktree whose ownership is exactly one kernel lock."""

    def __init__(
        self,
        *,
        path: Path,
        seat_name: str,
        dispatch_id: str,
        lock_file: TextIO,
        quarantine_branch: str | None,
        branch: str,
        keep_ref: str | None = None,
        reclaimed_dispatch_id: str | None = None,
        controller_label: str | None = None,
        created: bool = False,
        skipped_slots: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
        previous_lock_contents: str | None = "",
    ) -> None:
        self.path = path
        self.seat_name = seat_name
        self.dispatch_id = dispatch_id
        self.quarantine_branch = quarantine_branch
        self.branch = branch
        self.keep_ref = keep_ref
        self.reclaimed_dispatch_id = reclaimed_dispatch_id
        self.controller_label = controller_label
        self.created = created
        self.skipped_slots = tuple(skipped_slots)
        self._previous_lock_contents = previous_lock_contents
        self._lock_file: TextIO | None = lock_file

    def fileno(self) -> int:
        if self._lock_file is None:
            raise WorktreeSeatError(f"worktree lease already released: {self.seat_name}")
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

    def release_unlaunched(self) -> None:
        """Restore old lock text only when the checkout still has its old branch."""
        lock_file = self._lock_file
        if lock_file is None:
            return
        try:
            if _known_lock_dispatch_id(lock_file) != self.dispatch_id:
                raise WorktreeSeatError(
                    f"cannot restore prelaunch occupant for {self.seat_name}; "
                    "the lock no longer names this dispatch"
                )
            if self._previous_lock_contents is not None:
                lock_file.seek(0)
                lock_file.write(self._previous_lock_contents)
                lock_file.truncate()
                lock_file.flush()
                os.fsync(lock_file.fileno())
        finally:
            self.release()

    def __enter__(self) -> "WorktreeSeatLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class WorktreeReadOnlySeatLease:
    """A shared kernel lock held for a read-only review's lifetime."""

    def __init__(
        self,
        *,
        path: Path,
        seat_name: str,
        dispatch_id: str,
        lock_file: TextIO,
        path_lock: WorktreePathLock | None = None,
    ) -> None:
        self.path = path
        self.seat_name = seat_name
        self.dispatch_id = dispatch_id
        self._lock_file: TextIO | None = lock_file
        self._path_lock = path_lock

    def fileno(self) -> int:
        if self._lock_file is None:
            raise WorktreeSeatError(
                f"read-only worktree lease already released: {self.seat_name}"
            )
        return self._lock_file.fileno()

    def release(self) -> None:
        """Drop this process's descriptor; the shared lock then disappears."""
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        path_lock = self._path_lock
        self._path_lock = None
        if path_lock is not None:
            path_lock.release()
        lock_file.close()

    def __enter__(self) -> "WorktreeReadOnlySeatLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def _load_local_overrides() -> tuple[Path, dict]:
    raw_conf_path = os.environ.get("GOALFLIGHT_CAPACITY_CONF", "").strip()
    conf_path = (
        Path(raw_conf_path).expanduser()
        if raw_conf_path
        else Path.home() / ".goal-flight" / "capacity.local.json"
    )
    if conf_path == Path(os.devnull):
        local_overrides = {}
    else:
        try:
            local_overrides = json.loads(conf_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            local_overrides = {}
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorktreeSeatError(
                f"capacity override {conf_path} is unreadable or invalid JSON: {exc}"
            ) from exc
        if not isinstance(local_overrides, dict):
            raise WorktreeSeatError(
                f"capacity override {conf_path} must contain a JSON object"
            )
    return conf_path, local_overrides


def configured_worktree_seats() -> int:
    conf_path, local_overrides = _load_local_overrides()
    if "worktrees_per_repo" in local_overrides:
        configured = local_overrides["worktrees_per_repo"]
        if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
            return configured
        raise WorktreeSeatError(
            f"{conf_path}: worktrees_per_repo must be a positive integer, "
            f"got {configured!r}"
        )
    raw = os.environ.get(WORKTREES_PER_REPO_ENV)
    env_name = WORKTREES_PER_REPO_ENV
    if raw is None or not raw.strip():
        raw = os.environ.get(WORKTREE_SEATS_ENV)
        env_name = WORKTREE_SEATS_ENV
    if raw is None or not raw.strip():
        return DEFAULT_WORKTREE_SEATS
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorktreeSeatError(
            f"{env_name} must be a positive integer, got {raw!r}"
        ) from exc
    if value < 1:
        raise WorktreeSeatError(
            f"{env_name} must be a positive integer, got {raw!r}"
        )
    return value


def _configured_seat_sparse_pattern(project_root: Path) -> str | None:
    _, local_overrides = _load_local_overrides()
    configured = local_overrides.get("seat_sparse_pattern_files")
    if configured is None:
        return None
    if not isinstance(configured, dict):
        print(
            "goalflight seat sparse: seat_sparse_pattern_files must be an object; "
            "creating a full seat",
            file=sys.stderr,
        )
        return None
    raw_path = configured.get(str(project_root))
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path:
        print(
            f"goalflight seat sparse: invalid pattern path for {project_root}; "
            "creating a full seat",
            file=sys.stderr,
        )
        return None
    pattern_path = Path(raw_path).expanduser()
    if not pattern_path.is_absolute():
        print(
            f"goalflight seat sparse: pattern path {raw_path!r} for {project_root} "
            "is not absolute; creating a full seat",
            file=sys.stderr,
        )
        return None
    try:
        return pattern_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(
            f"goalflight seat sparse: cannot read pattern {pattern_path}: {exc}; "
            "creating a full seat",
            file=sys.stderr,
        )
        return None


def _sparse_pattern_has_root(pattern: str) -> bool:
    return any(line.strip() == "/*" for line in pattern.splitlines())


def _resolved_config_key(key: object) -> Path | None:
    if not isinstance(key, str) or not key:
        return None
    try:
        return Path(key).expanduser().resolve()
    except OSError:
        return None


def seat_cow_enabled(project_root: Path) -> bool:
    """Per-repository copy-on-write opt-in; absent or invalid values stay off."""
    _conf_path, overrides = _load_local_overrides()
    configured = overrides.get("seat_cow_templates")
    if configured is None:
        return False
    if not isinstance(configured, dict):
        print(
            "goalflight seat cow: seat_cow_templates must be an object; "
            "leaving copy-on-write off",
            file=sys.stderr,
        )
        return False
    resolved = project_root.resolve()
    matches = [
        value
        for key, value in configured.items()
        if _resolved_config_key(key) == resolved
    ]
    if not matches:
        return False
    first = matches[0]
    if any(value != first or type(value) is not type(first) for value in matches[1:]):
        print(
            f"goalflight seat cow: seat_cow_templates has conflicting values for "
            f"{project_root}; leaving copy-on-write off",
            file=sys.stderr,
        )
        return False
    if isinstance(first, bool):
        return first
    print(
        f"goalflight seat cow: invalid seat_cow_templates value for "
        f"{project_root}; leaving copy-on-write off",
        file=sys.stderr,
    )
    return False


def _cow_profile_pattern(project_root: Path) -> str:
    pattern = _configured_seat_sparse_pattern(project_root)
    if pattern is None or not _sparse_pattern_has_root(pattern):
        return "/*\n"
    return pattern


def _cow_prepare_outside_locks(
    project_root: Path, base_commit: str
) -> tuple[bool, str | None, str | None]:
    """Prepare an opted-in template before taking pool allocation locks."""
    if not seat_cow_enabled(project_root):
        return False, None, None
    pattern = _cow_profile_pattern(project_root)
    import goalflight_seat_cow as cow

    try:
        reason = cow.prepare_for_new_seat(
            project_root, base_commit=base_commit, pattern=pattern
        )
    except Exception as exc:
        print(
            f"goalflight seat cow: {project_root}: template preparation failed: {exc}",
            file=sys.stderr,
        )
        return True, f"template-build-failed: {exc}", pattern
    return True, reason, pattern


def inherited_worktree_lock_fds() -> tuple[int, ...]:
    """Return validated inherited worktree-lock and occupancy-lock descriptors."""
    fds: list[int] = []
    errors: list[str] = []
    for env_name in (
        WORKTREE_LOCK_FD_ENV,
        OCCUPANCY_LOCK_FD_ENV,
        READ_ONLY_ALLOCATION_LOCK_FD_ENV,
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            fd = int(raw)
            os.fstat(fd)
        except (ValueError, OSError) as exc:
            # A closed occupancy fd is leftover ended state, not a missing
            # seat. The occupancy prepare path treats EBADF the same way.
            if (
                env_name == OCCUPANCY_LOCK_FD_ENV
                and isinstance(exc, OSError)
                and exc.errno == errno.EBADF
            ):
                os.environ.pop(OCCUPANCY_LOCK_FD_ENV, None)
                continue
            errors.append(f"{env_name} does not name an open descriptor: {raw!r}")
            continue
        if fd not in fds:
            fds.append(fd)
    if errors:
        raise WorktreeSeatError("; ".join(errors))
    return tuple(fds)


def pass_worktree_lock_fds(env: dict[str, str] | None = None) -> tuple[int, ...]:
    """Descriptors a child must inherit to keep holding this process's locks.

    ``inherited_worktree_lock_fds`` reads this process's ``os.environ`` for
    the pooled-worktree, occupancy, and detached-read-only-allocation fds. A
    parent that acquired a *new* worktree puts the fd in the child env dict
    without exporting it on itself; that fd still has to be in ``pass_fds`` or
    the helper exec closes it and the seat frees while the worker runs.
    Occupancy and detached-resume locks are usually exported on the parent;
    the env-dict lookup also covers a child env that names a fd the parent has
    not exported.

    Callers that must not hold occupancy (watcher, caffeinate, redact
    sidecars) strip that fd after this returns; passing the combined set
    unchanged to those processes would keep the tree occupied after the
    worker dies.
    """
    fds: list[int] = []
    seen: set[int] = set()
    for fd in inherited_worktree_lock_fds():
        if fd not in seen:
            fds.append(fd)
            seen.add(fd)
    if env is None:
        return tuple(fds)
    for env_name in (
        WORKTREE_LOCK_FD_ENV,
        OCCUPANCY_LOCK_FD_ENV,
        READ_ONLY_ALLOCATION_LOCK_FD_ENV,
    ):
        raw = str(env.get(env_name) or "").strip()
        if not raw:
            continue
        try:
            fd = int(raw)
            os.fstat(fd)
        except (ValueError, OSError):
            continue
        if fd not in seen:
            fds.append(fd)
            seen.add(fd)
    return tuple(fds)


def sanitize_controller_ring_label(label: str | None) -> str:
    """Return a filesystem-safe, stable ring label.

    Empty or unsafe labels collapse to ``unlabeled``. The fallback is
    documented and must stay the same across dispatches.
    """
    raw = str(label or "").strip()
    if not raw:
        return UNLABELED_CONTROLLER_RING
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    if not cleaned or cleaned in {".", ".."} or not _SAFE_RING_LABEL.fullmatch(cleaned):
        return UNLABELED_CONTROLLER_RING
    return cleaned[:64]


def default_controller_ring_label(
    explicit_label: str | None = None,
    *,
    project_root: Path | None = None,
) -> str:
    """Resolve the ring label: explicit, else repo-name fallback, else unlabeled."""
    if explicit_label is not None and str(explicit_label).strip():
        return sanitize_controller_ring_label(explicit_label)
    if project_root is not None:
        name = Path(project_root).name.strip()
        if name:
            return sanitize_controller_ring_label(name)
    return UNLABELED_CONTROLLER_RING


def default_seat_base(
    project_root: Path, *, timeout: float | None = None
) -> str:
    """Project default ref: ``origin/main`` when it exists, else ``HEAD``."""
    proc = _git_proc(
        project_root,
        "rev-parse",
        "--verify",
        "origin/main^{commit}",
        timeout=timeout,
    )
    if proc is not None and proc.returncode == 0:
        return "origin/main"
    return "HEAD"


def _slot_from_seat_name(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    if rest.isdigit() and int(rest) >= 1:
        return int(rest)
    return None


def pool_seat_name(path: str | Path) -> str | None:
    """Return ``s-N`` or legacy ``wt-N`` when the basename matches a worktree pattern.

    A matching name is necessary but not sufficient for a maintained worktree.
    Ad-hoc worktrees can be named ``s-5`` or ``wt-5``; ask
    ``registered_pool_seat_verdict``.
    """
    name = Path(path).name
    if _slot_from_seat_name(name, CAPTIVE_SEAT_PREFIX) is not None:
        return name
    if _slot_from_seat_name(name, WORKTREE_SEAT_PREFIX) is not None:
        return name
    return None


def is_pool_seat_path(path: str | Path) -> bool:
    """True when the basename looks like ``s-N`` or ``wt-N``. Not a registration check."""
    return pool_seat_name(path) is not None


def controller_ring_root(project_root: Path, controller_label: str | None) -> Path:
    """Return ``{repo}/worktrees/{label}`` for a controller's captive ring."""
    label = default_controller_ring_label(controller_label, project_root=project_root)
    return Path(project_root).resolve() / "worktrees" / label


def repository_worktree_root(project_root: Path) -> Path:
    """Return the single repository-wide managed worktree directory."""
    return Path(project_root).resolve() / "worktrees"


def _exclude_managed_worktrees(project_root: Path, *, timeout: float | None = None) -> None:
    project_root = Path(project_root).resolve()
    managed_root = repository_worktree_root(project_root)
    pattern = "/" + managed_root.relative_to(project_root).as_posix().rstrip("/") + "/"
    exclude_value = _git(
        project_root, "rev-parse", "--git-path", "info/exclude", timeout=timeout,
    )
    exclude_path = Path(exclude_value)
    if not exclude_path.is_absolute():
        exclude_path = project_root / exclude_path
    git_dir = Path(_git(
        project_root, "rev-parse", "--absolute-git-dir", timeout=timeout,
    )).resolve()

    def checked_parent() -> tuple[Path | None, str | None]:
        if exclude_path.is_symlink():
            return None, "exclude file is a symlink"
        if exclude_path.parent.is_symlink():
            return None, "exclude directory is a symlink"
        try:
            resolved_parent = exclude_path.parent.resolve(strict=False)
            resolved_exclude = exclude_path.resolve(strict=False)
        except OSError as exc:
            return None, f"cannot resolve exclude path: {exc}"
        if not resolved_parent.is_relative_to(git_dir):
            return None, "exclude directory resolves outside the Git directory"
        if not resolved_exclude.is_relative_to(git_dir):
            return None, "exclude file resolves outside the Git directory"
        return resolved_parent, None

    def warn_and_skip(reason: str) -> None:
        print(
            f"goalflight worktree pool: skipping unsafe Git exclude {exclude_path}: {reason}",
            file=sys.stderr,
        )

    resolved_parent, unsafe_reason = checked_parent()
    if unsafe_reason:
        warn_and_skip(unsafe_reason)
        return
    pattern_bytes = pattern.encode("utf-8")
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent, unsafe_reason = checked_parent()
        if unsafe_reason or resolved_parent is None:
            warn_and_skip(unsafe_reason or "cannot resolve exclude directory")
            return
        parent_fd = os.open(
            resolved_parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            exclude_fd = os.open(
                exclude_path.name,
                os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0),
                0o666,
                dir_fd=parent_fd,
            )
            try:
                exclude_stat = os.fstat(exclude_fd)
                if not stat.S_ISREG(exclude_stat.st_mode):
                    warn_and_skip("exclude path is not a regular file")
                    return
                if exclude_stat.st_nlink > 1:
                    warn_and_skip("exclude file has multiple hard links")
                    return
                handle = os.fdopen(exclude_fd, "a+b")
                exclude_fd = -1
                with handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    handle.seek(0)
                    contents = handle.read()
                    if pattern_bytes in contents.splitlines():
                        return
                    separator = b"" if not contents or contents.endswith(b"\n") else b"\n"
                    handle.seek(0, os.SEEK_END)
                    handle.write(separator + pattern_bytes + b"\n")
                    handle.flush()
            finally:
                if exclude_fd >= 0:
                    os.close(exclude_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
            warn_and_skip(f"exclude path became unsafe before it could be opened: {exc}")
            return
        raise WorktreeSeatError(
            f"cannot update local Git exclude file {exclude_path}: {exc}"
        ) from exc


def read_only_worktree_root(project_root: Path) -> Path:
    """Return the shared detached-checkout directory."""
    return repository_worktree_root(project_root) / READ_ONLY_WORKTREE_DIR


def read_only_allocation_lock_path(
    project_root: Path, *, timeout: float | None = None
) -> Path:
    """Return the lock shared by detached-checkout allocation and GC."""
    return _seat_lock_root(project_root, timeout=timeout) / "readonly-allocation.lock"


def read_only_worktree_path_verdict(
    path: str | Path, *, project_root: Path
) -> tuple[str, str]:
    """Classify a read-only path without following a symlink into the pool."""
    root = read_only_worktree_root(project_root)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(project_root).resolve() / candidate
    try:
        if root.is_symlink():
            return UNKNOWN, f"read-only root is a symlink: {root}"
        root_real = root.resolve(strict=False)
        if root_real != root:
            return UNKNOWN, f"read-only root resolves through a symlink: {root}"
        relative = candidate.relative_to(root)
    except ValueError:
        return NO, f"{candidate} is not under {root}"
    except OSError as exc:
        return UNKNOWN, f"{candidate} could not be classified ({exc})"
    if len(relative.parts) != 1:
        return NO, f"{candidate} is not a direct read-only checkout"
    managed_verdict, managed_reason = managed_worktree_path_verdict(
        candidate, project_root=project_root
    )
    if managed_verdict == UNKNOWN:
        return UNKNOWN, managed_reason
    if is_pool_seat_path(candidate) or managed_verdict == YES:
        return UNKNOWN, f"{candidate} is also a pooled worktree path"
    if candidate.is_symlink():
        return UNKNOWN, f"read-only checkout is a symlink: {candidate}"
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return UNKNOWN, f"read-only checkout could not be resolved ({exc})"
    if resolved.parent != root_real:
        return UNKNOWN, f"read-only checkout resolves outside {root}"
    return YES, f"direct child of {root}"


def is_read_only_worktree_path(path: str | Path, *, project_root: Path) -> bool:
    """True for a safe direct child of the shared detached-checkout directory."""
    verdict, _reason = read_only_worktree_path_verdict(path, project_root=project_root)
    return verdict == YES


def is_managed_worktree_path(path: str | Path, *, project_root: Path) -> bool:
    """Recognize a path; callers needing safety reasons must use the tri-state helper."""
    verdict, _reason = managed_worktree_path_verdict(path, project_root=project_root)
    return verdict == YES


def managed_worktree_path_verdict(
    path: str | Path, *, project_root: Path
) -> tuple[str, str]:
    """Classify a managed-looking path without turning inspection errors into NO."""
    try:
        resolved = Path(path).resolve()
        root = repository_worktree_root(project_root).resolve()
        rel = resolved.relative_to(root)
    except ValueError:
        return NO, f"{path} is not under the managed worktree root"
    except OSError as exc:
        return UNKNOWN, f"{path} could not be classified ({exc})"
    parts = rel.parts
    if len(parts) == 1:
        if is_captive_seat_name(parts[0]):
            return YES, f"{path} is a managed pooled worktree"
        return NO, f"{path} is not a managed pooled worktree"
    if len(parts) == 2 and is_captive_seat_name(parts[1]):
        return YES, f"{path} is a managed pooled worktree"
    return NO, f"{path} is not a managed pooled worktree"


def is_controller_ring_seat(
    path: str | Path,
    *,
    project_root: Path,
    controller_label: str | None,
) -> bool:
    """True when ``path`` is ``worktrees/{this-controller}/s-N``."""
    try:
        resolved = Path(path).resolve()
        ring = controller_ring_root(project_root, controller_label).resolve()
    except OSError:
        return False
    if resolved.parent != ring:
        return False
    return _slot_from_seat_name(resolved.name, CAPTIVE_SEAT_PREFIX) is not None


def is_reserved_seat_notes_path(relpath: str) -> bool:
    """True when a porcelain path is inside the reserved seat-notes namespace."""
    text = relpath.replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    return text == SEAT_NOTES_NAMESPACE or text.startswith(SEAT_NOTES_NAMESPACE + "/")


def _registered_pool_seat_lock_info(
    path: str | Path,
    *,
    project_root: Path,
    deadline: float | None = None,
    git_args_prefix: tuple[str, ...] = (),
) -> tuple[str, str, Path | None, os.stat_result | None]:
    """Return registration plus the lock identity used for that verdict."""
    try:
        root = project_root.resolve()
    except OSError as exc:
        return "unknown", f"project root unresolvable ({exc})", None, None
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        return "unknown", f"worktree path unresolvable ({exc})", None, None

    managed_root = root / "worktrees"
    try:
        managed_root = managed_root.resolve()
    except OSError as exc:
        return "unknown", f"managed worktree root unresolvable ({exc})", None, None

    try:
        rel = resolved.relative_to(managed_root)
    except ValueError:
        return (
            "no",
            f"{resolved} is not under the managed worktree root {managed_root}",
            None,
            None,
        )
    except OSError as exc:
        return (
            "unknown",
            f"managed worktree path could not be compared ({exc})",
            None,
            None,
        )

    parts = rel.parts
    lock_subdir: str | None = None
    if len(parts) == 1:
        seat_name = pool_seat_name(parts[0])
        prefix = (
            CAPTIVE_SEAT_PREFIX
            if seat_name and is_captive_seat_name(seat_name)
            else WORKTREE_SEAT_PREFIX
        )
        if seat_name is None or _slot_from_seat_name(seat_name, prefix) is None:
            return "no", f"{resolved.name} is not a pool worktree name", None, None
    elif len(parts) == 2:
        seat_name = pool_seat_name(parts[1])
        prefix = CAPTIVE_SEAT_PREFIX
        if seat_name is None or _slot_from_seat_name(seat_name, prefix) is None:
            return "no", f"{resolved.name} is not a pool worktree name", None, None
        lock_subdir = parts[0]
    else:
        return (
            "no",
            f"{resolved} is not a managed worktree path under {managed_root}",
            None,
            None,
        )

    try:
        seat_limit = configured_worktree_seats()
    except WorktreeSeatError as exc:
        return "unknown", f"worktree configuration unreadable ({exc})", None, None

    slot = _slot_from_seat_name(seat_name, prefix)
    if slot is None:
        return (
            "no",
            f"{seat_name} is not a valid managed worktree id",
            None,
            None,
        )

    try:
        registry_root = _git_common_dir(
            root,
            git_args_prefix=git_args_prefix,
            timeout=(
                None
                if deadline is None
                else _deadline_timeout(deadline)
            ),
            deadline=deadline,
        )
        lock_root = registry_root / "goalflight-worktree-seat-locks"
    except WorktreeSeatError as exc:
        if deadline is not None and (
            "timed out" in str(exc).lower() or time.monotonic() >= deadline
        ):
            return (
                "unknown",
                "worktree lock directory deadline expired; retaining checkout",
                None,
                None,
            )
        return "unknown", f"worktree lock directory unreadable ({exc})", None, None
    if lock_subdir:
        lock_root = lock_root / lock_subdir

    lock_path = lock_root / f"{seat_name}.lock"
    try:
        if lock_root.is_symlink():
            return (
                "unknown",
                f"worktree lock root is a symlink ({lock_root})",
                None,
                None,
            )
    except FileNotFoundError:
        return (
            "no",
            f"no worktree lock for {seat_name}; path is not a registered pool worktree",
            None,
            None,
        )
    except OSError as exc:
        return (
            "unknown",
            f"worktree lock unreadable for {seat_name} ({exc})",
            None,
            None,
        )

    try:
        current = os.lstat(lock_path)
    except FileNotFoundError:
        try:
            registered = _registered_lock_identity(
                lock_path, registry_root=registry_root
            )
        except OSError as exc:
            return "unknown", str(exc), None, None
        if registered is not None:
            return "unknown", f"registered worktree lock disappeared ({lock_path})", None, None
        return (
            "no",
            f"no worktree lock for {seat_name}; path is not a registered pool worktree",
            None,
            None,
        )
    except OSError as exc:
        return "unknown", f"worktree lock unreadable for {seat_name} ({exc})", None, None
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        return "unknown", f"worktree lock is not a regular file ({lock_path})", None, None
    try:
        registered = _registered_lock_identity(
            lock_path, registry_root=registry_root
        )
    except OSError as exc:
        return "unknown", str(exc), None, None
    if registered is None:
        return (
            "unknown",
            f"worktree lock identity is not registered ({lock_path})",
            lock_path,
            current,
        )
    if (
        current.st_dev != registered.st_dev
        or current.st_ino != registered.st_ino
    ):
        return "unknown", f"worktree lock identity changed ({lock_path})", None, None
    return "yes", f"registered pool worktree {seat_name}", lock_path, registered


def registered_pool_seat_verdict(
    path: str | Path,
    *,
    project_root: Path,
    deadline: float | None = None,
) -> tuple[str, str]:
    """Ask the pool whether ``path`` is a registered worktree.

    Returns ``("yes"|"no"|"unknown", reason)``. Name is irrelevant unless the
    path is a managed seat — either the captive
    ``<project>/worktrees/<label>/s-N`` ring or the legacy
    ``<project>/worktrees/wt-N`` layout — *and* the matching lock file exists.
    A missing lock is "not registered" (de-registered and ad-hoc trees are
    litter). If registration cannot be determined, the verdict is unknown so
    a deleter retains.
    """
    verdict, reason, _lock_path, _lock_stat = _registered_pool_seat_lock_info(
        path, project_root=project_root, deadline=deadline
    )
    return verdict, reason


def _git(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
) -> str:
    kwargs = {"input_text": input_text, "env": env, "timeout": timeout}
    if deadline is not None:
        kwargs["deadline"] = deadline
    result = _git_proc(cwd, *args, **kwargs)
    if result is None:
        raise WorktreeSeatError(f"git {' '.join(args)} could not run in {cwd}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeSeatError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout.strip()


def _git_nul(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
) -> str:
    """Run a Git command whose NUL-delimited output must keep path bytes."""
    kwargs = {"input_text": input_text, "env": env, "timeout": timeout}
    if deadline is not None:
        kwargs["deadline"] = deadline
    result = _git_proc(cwd, *args, **kwargs)
    if result is None:
        raise WorktreeSeatError(f"git {' '.join(args)} could not run in {cwd}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeSeatError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout


def _git_identity(
    cwd: Path, *, timeout: float | None = None, deadline: float | None = None
) -> tuple[str, str, str] | None:
    """Return realpath git-dir, common-dir, and worktree top-level."""
    try:
        result = goalflight_subprocess.run_with_deadline(
            [
                "git",
                "-C",
                str(cwd),
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
                "--show-toplevel",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            deadline=deadline,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(values) != 3:
        return None
    return (
        os.path.realpath(values[0]),
        os.path.realpath(values[1]),
        os.path.realpath(values[2]),
    )


def _git_worktree_target_args(args: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return worktree paths, or None when a mutating target is ambiguous."""
    if len(args) < 2 or args[0] != "worktree":
        return ()
    subcommand = args[1]
    if subcommand not in _GIT_WORKTREE_TARGET_COMMANDS:
        return ()
    values: list[str] = []
    index = 2
    while index < len(args):
        value = args[index]
        if value == "--":
            values.extend(args[index + 1 :])
            break
        if value in {"-b", "-B", "--branch", "--orphan", "--reason"}:
            index += 2
            continue
        if (
            value.startswith("--branch=")
            or value.startswith("--orphan=")
            or value.startswith("--reason=")
        ):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        values.append(value)
        index += 1
    required = 2 if subcommand == "move" else 1
    if len(values) < required:
        return None
    return tuple(values[:required])


def guard_worktree_mutation(
    cwd: Path, *args: str, timeout: float | None = None,
    deadline: float | None = None,
) -> str | None:
    """Refuse worktree mutations whose target is the repository main checkout."""
    if not args:
        return None
    command = args[0]
    if command == "worktree":
        targets = _git_worktree_target_args(args)
        if targets is None:
            return f"refusing git {' '.join(args)}: cannot determine target worktree"
        if not targets:
            return None
    elif command == "stash":
        if len(args) > 1 and args[1] in {"list", "show"}:
            return None
        targets = (str(cwd),)
    elif command in _GIT_MUTATING_COMMANDS:
        targets = (str(cwd),)
    else:
        return None

    if deadline is not None:
        remaining = _deadline_timeout(deadline)
        timeout = remaining if timeout is None else min(timeout, remaining)
    started = time.monotonic() if timeout is not None else None

    def remaining_timeout() -> float | None:
        if started is None:
            return None
        return max(0.0, timeout - (time.monotonic() - started))

    identity_kwargs = {"deadline": deadline} if deadline is not None else {}
    source = _git_identity(
        cwd, timeout=remaining_timeout(), **identity_kwargs,
    )
    if source is None:
        return f"refusing git {' '.join(args)}: cannot verify repository identity"
    if command != "worktree":
        if source[0] == source[1]:
            return (
                f"refusing git {' '.join(args)}: target {source[2]} is the "
                "repository main worktree"
            )
        return None
    source_main = source[1]
    source_main = os.path.realpath(str(Path(source_main).parent))
    for raw_target in targets:
        target = Path(raw_target).expanduser()
        if not target.is_absolute():
            target = cwd / target
        target_real = os.path.realpath(str(target))
        if target_real == source_main:
            return (
                f"refusing git {' '.join(args)}: target {target_real} is the "
                "repository main worktree"
            )
        target_identity = (
            _git_identity(
                Path(target_real), timeout=remaining_timeout(), **identity_kwargs,
            )
            if Path(target_real).is_dir()
            else None
        )
        if target_identity is None:
            if command == "worktree":
                continue
            return (
                f"refusing git {' '.join(args)}: cannot verify target worktree "
                f"{target_real}"
            )
        target_git, target_common, target_top = target_identity
        if target_git == target_common or target_top == source_main:
            return (
                f"refusing git {' '.join(args)}: target {target_real} is the "
                "repository main worktree"
            )
    return None


def _git_proc(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    if deadline is not None:
        remaining = _deadline_timeout(deadline)
        timeout = remaining if timeout is None else min(timeout, remaining)
    started = time.monotonic() if timeout is not None else None
    guard_kwargs = {"timeout": timeout}
    if deadline is not None:
        guard_kwargs["deadline"] = deadline
    guard_error = guard_worktree_mutation(cwd, *args, **guard_kwargs)
    if guard_error is not None:
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", guard_error
        )
    command_timeout = timeout
    if started is not None:
        command_timeout = max(0.0, timeout - (time.monotonic() - started))
        if command_timeout <= 0.0:
            return subprocess.CompletedProcess(
                ["git", *args], 124, "", "git command timed out"
            )
    try:
        run_kwargs = {
            "cwd": str(cwd),
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "check": False,
            "timeout": command_timeout,
        }
        if deadline is not None:
            run_kwargs["deadline"] = deadline
        if input_text is None:
            return goalflight_subprocess.run_with_deadline(["git", *args], **run_kwargs)
        # Do not make communicate() write a potentially unbounded input string
        # to a pipe while Git's captured output pipes can fill.
        with tempfile.TemporaryFile(mode="w+b") as input_file:
            input_file.write(input_text.encode("utf-8", errors="replace"))
            input_file.flush()
            input_file.seek(0)
            return goalflight_subprocess.run_with_deadline(
                ["git", *args], stdin=input_file, **run_kwargs
            )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args], 124, "", "git command timed out"
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _condition(verdict: str, reason: str) -> dict[str, str]:
    return {"verdict": verdict, "reason": reason}


def seat_branch_name(dispatch_id: str) -> str:
    """Return the current branch name for a managed worktree."""
    return worktree_branch_name(dispatch_id)


def worktree_branch_name(dispatch_id: str) -> str:
    """Return the new branch prefix; readers continue to accept ``seat/*``."""
    raw = str(dispatch_id).strip()
    if not raw:
        raise WorktreeSeatError("dispatch id is empty; cannot name a worktree branch")
    return f"{WORKTREE_BRANCH_PREFIX}/{raw}"


def is_worktree_branch(branch: str | None) -> bool:
    """Accept both the new and legacy managed branch prefixes."""
    value = str(branch or "")
    return value.startswith(WORKTREE_BRANCH_PREFIX + "/") or value.startswith(
        SEAT_BRANCH_PREFIX + "/"
    )


def _git_common_dir(
    cwd: Path,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
    git_args_prefix: tuple[str, ...] = (),
) -> Path:
    if timeout is None and deadline is None:
        raw = Path(_git(cwd, *git_args_prefix, "rev-parse", "--git-common-dir"))
    else:
        git_kwargs = {
            "timeout": _deadline_timeout(deadline) if timeout is None else timeout,
        }
        if deadline is not None:
            git_kwargs["deadline"] = deadline
        raw = Path(_git(
            cwd, *git_args_prefix, "rev-parse", "--git-common-dir", **git_kwargs,
        ))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def _git_dir(cwd: Path) -> Path:
    raw = Path(_git(cwd, "rev-parse", "--git-dir"))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def is_captive_seat_name(name: str) -> bool:
    """True when ``name`` is a captive ``s-N`` seat."""
    return _slot_from_seat_name(name, CAPTIVE_SEAT_PREFIX) is not None


def _seat_lock_root(
    project_root: Path,
    *,
    controller_label: str | None = None,
    timeout: float | None = None,
) -> Path:
    root = (
        _git_common_dir(project_root.resolve(), timeout=timeout)
        / "goalflight-worktree-seat-locks"
    )
    if controller_label is None:
        return root
    return root / sanitize_controller_ring_label(controller_label)


def worktree_seat_lock_path(
    project_root: Path,
    seat_name: str,
    *,
    controller_label: str | None = None,
) -> Path:
    """Return the per-repository lock path for an already-named worktree.

    Captive ``s-N`` worktrees store locks under ``{lock_root}/{label}/s-N.lock``.
    Legacy ``wt-N`` worktrees keep ``{lock_root}/wt-N.lock`` so in-flight
    workers are not evicted during the overlap window.
    """
    if controller_label is not None and is_captive_seat_name(seat_name):
        # New allocations are repository-scoped even when an older caller
        # still supplies its controller label.  Keep resolving an existing
        # label-ring lock for mixed-version workers during migration.
        direct = repository_worktree_root(project_root) / seat_name
        legacy = (
            repository_worktree_root(project_root)
            / sanitize_controller_ring_label(controller_label)
            / seat_name
        )
        direct_state = _path_presence(direct)
        legacy_state = _path_presence(legacy)
        if "unknown" in {direct_state, legacy_state}:
            raise WorktreeSeatError(
                f"cannot determine pooled seat path while resolving {worktree_path}"
            )
        if direct_state == "present" or legacy_state == "absent":
            return _seat_lock_root(project_root) / f"{seat_name}.lock"
        return _seat_lock_root(project_root, controller_label=controller_label) / f"{seat_name}.lock"
    return _seat_lock_root(project_root) / f"{seat_name}.lock"


def _ring_state_path(lock_root: Path) -> Path:
    return lock_root / "ring.json"


def _lock_registry_path(registry_root: Path) -> Path:
    return Path(registry_root) / _LOCK_REGISTRY_NAME


def _lock_registry_key(lock_path: Path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(lock_path)))


def _read_lock_registry(registry_root: Path) -> dict:
    path = _lock_registry_path(registry_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"locks": {}}
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise OSError(errno.EIO, f"lock registration is unreadable: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("locks", {}), dict):
        raise OSError(errno.EIO, f"lock registration is invalid: {path}")
    return payload


def _registered_lock_identity(
    lock_path: Path, *, registry_root: Path
) -> os.stat_result | None:
    payload = _read_lock_registry(registry_root)
    key = _lock_registry_key(lock_path)
    record = payload["locks"].get(key)
    if record is not None:
        return _registered_stat_from_record(lock_path, record)
    try:
        current = os.lstat(lock_path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(current.st_mode):
        return None
    # Legacy label rings can move the registered lock inode from the global
    # path to a label-local path. A moved inode is still identified by the
    # durable registration; the pathname is only used to locate that inode.
    matches = []
    for registered_path, candidate in payload["locks"].items():
        if registered_path == key:
            continue
        registered = _registered_stat_from_record(Path(registered_path), candidate)
        if (
            registered.st_dev == current.st_dev
            and registered.st_ino == current.st_ino
        ):
            matches.append(registered)
    if matches:
        return matches[0]
    return None


def _registered_stat_from_record(
    lock_path: Path, record: object
) -> os.stat_result:
    if not isinstance(record, dict):
        raise OSError(errno.EIO, f"lock registration entry is invalid: {lock_path}")
    try:
        device = int(record["st_dev"])
        inode = int(record["st_ino"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OSError(errno.EIO, f"lock registration entry is invalid: {lock_path}") from exc
    if device < 0 or inode <= 0:
        raise OSError(errno.EIO, f"lock registration entry is invalid: {lock_path}")
    return os.stat_result((stat.S_IFREG, inode, device, 1, 0, 0, 0, 0, 0, 0))


def _flock_exclusive_until(
    fd: int, *, deadline: float | None, description: str
) -> None:
    """Take an exclusive flock, bounded when a transaction supplies a deadline."""
    if deadline is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OSError(
                    errno.ETIMEDOUT,
                    f"{description} wait expired",
                )
            time.sleep(min(0.05, remaining))


def _record_lock_identity(
    lock_path: Path,
    fd: int,
    *,
    registry_root: Path,
    deadline: float | None = None,
) -> os.stat_result:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(errno.ELOOP, f"lock path is not a regular file: {lock_path}")
    registry_path = _lock_registry_path(registry_root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    mutex_path = registry_path.with_name(_LOCK_REGISTRY_MUTEX_NAME)
    key = _lock_registry_key(lock_path)
    with mutex_path.open("a+", encoding="utf-8") as mutex:
        _flock_exclusive_until(
            mutex.fileno(),
            deadline=deadline,
            description="lock registry mutex",
        )
        payload = _read_lock_registry(registry_root)
        existing = payload["locks"].get(key)
        if existing is not None:
            registered = _registered_stat_from_record(lock_path, existing)
            if (
                registered.st_dev != opened.st_dev
                or registered.st_ino != opened.st_ino
            ):
                raise OSError(
                    errno.EAGAIN,
                    f"lock identity changed while registering: {lock_path}",
                )
            return registered
        payload["locks"][key] = {
            "st_dev": int(opened.st_dev),
            "st_ino": int(opened.st_ino),
        }
        temporary = registry_path.with_name(
            f"{registry_path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(registry_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
    return os.stat_result(
        (stat.S_IFREG, opened.st_ino, opened.st_dev, 1, 0, 0, 0, 0, 0, 0)
    )


def _occupancy_registry_keys(payload: dict, lock_path: Path) -> list[str]:
    """Registry keys that name this occupancy lock, including realpath aliases."""
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        return []
    key = _lock_registry_key(lock_path)
    wanted = os.path.normpath(os.path.realpath(str(lock_path)))
    found: list[str] = []
    for registered in locks:
        if registered == key:
            found.append(registered)
            continue
        try:
            same = os.path.normpath(os.path.realpath(registered)) == wanted
        except OSError:
            same = False
        if same:
            found.append(registered)
    return found


def _drop_occupancy_registration(
    lock_path: Path,
    *,
    registry_root: Path,
    deadline: float | None = None,
    require_absent: bool = True,
) -> None:
    """Drop occupancy-lock registry keys for ``lock_path`` under the mutex.

    ``require_absent`` refuses to forget a key whose file is still on disk.
    Callers that are about to remove that file pass ``require_absent=False``
    and only match a key whose inode is still the file's inode.
    """
    if lock_path.name != OCCUPANCY_LOCK_NAME:
        raise OSError(
            errno.EPERM,
            f"refusing to drop a non-occupancy lock registration: {lock_path}",
        )
    registry_path = _lock_registry_path(registry_root)
    try:
        registry_exists = registry_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(registry_exists.st_mode):
        raise OSError(errno.EPERM, f"lock registration is not a regular file: {registry_path}")
    mutex_path = registry_path.with_name(_LOCK_REGISTRY_MUTEX_NAME)
    with mutex_path.open("a+", encoding="utf-8") as mutex:
        _flock_exclusive_until(
            mutex.fileno(),
            deadline=deadline,
            description="lock registry mutex",
        )
        try:
            current = os.lstat(lock_path)
        except FileNotFoundError:
            current = None
        if current is not None and require_absent:
            return
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise OSError(errno.EPERM, f"lock path is not a regular file: {lock_path}")
        payload = _read_lock_registry(registry_root)
        keys = _occupancy_registry_keys(payload, lock_path)
        if not keys:
            return
        if current is not None:
            for key in keys:
                registered = _registered_stat_from_record(lock_path, payload["locks"][key])
                if (
                    registered.st_dev != current.st_dev
                    or registered.st_ino != current.st_ino
                ):
                    raise OSError(
                        errno.EAGAIN,
                        f"lock identity changed while dropping registration: {lock_path}",
                    )
        for key in keys:
            payload["locks"].pop(key, None)
        temporary = registry_path.with_name(
            f"{registry_path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(registry_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _adopt_exclusive_lock(
    lock_path: Path,
    fd: int,
    *,
    registry_root: Path,
    deadline: float | None = None,
) -> os.stat_result:
    """Register a legacy inode after its caller has acquired LOCK_EX."""
    registered = _registered_lock_identity(lock_path, registry_root=registry_root)
    if registered is not None:
        if not _lock_fd_matches_identity(fd, registered):
            raise OSError(errno.EAGAIN, f"lock identity changed while adopting: {lock_path}")
        return registered
    return _record_lock_identity(
        lock_path,
        fd,
        registry_root=registry_root,
        deadline=deadline,
    )


def _read_ring_hwm(lock_root: Path) -> int:
    path = _ring_state_path(lock_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        hwm = int(payload.get("hwm") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, hwm)


def _write_ring_hwm(lock_root: Path, hwm: int) -> None:
    path = _ring_state_path(lock_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"hwm": int(hwm)}, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_porcelain_z(output: str) -> list[tuple[str, tuple[str, ...]]]:
    fields = output.split("\0")
    records: list[tuple[str, tuple[str, ...]]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise WorktreeSeatResetRefused(
                "malformed NUL-delimited Git status; refusing reset"
            )
        status = record[:2]
        paths = [record[3:]]
        if any(code in {"R", "C"} for code in status):
            if index >= len(fields) or not fields[index]:
                raise WorktreeSeatResetRefused(
                    "malformed NUL-delimited rename status; refusing reset"
                )
            paths.append(fields[index])
            index += 1
        records.append((status, tuple(paths)))
    return records


def _status_records(
    worktree_path: Path,
    *,
    untracked: str = "all",
    timeout: float | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    output = _git_nul(
        worktree_path,
        "status",
        "--porcelain=v1",
        "-z",
        f"--untracked-files={untracked}",
        "--ignore-submodules=none",
        timeout=timeout,
    )
    return _parse_porcelain_z(output)


def _status_tree_paths(record: tuple[str, tuple[str, ...]]) -> tuple[str, ...]:
    status, paths = record
    if "D" in status:
        return ()
    if "R" in status:
        return paths[:1]
    return paths


def _status_is_product(record: tuple[str, tuple[str, ...]]) -> bool:
    status, paths = record
    if not paths:
        return bool(status.strip())
    if status != "??":
        return True
    return any(not is_reserved_seat_notes_path(path) for path in paths)


def _nul_paths(output: str) -> tuple[str, ...]:
    return tuple(path for path in output.split("\0") if path)


def classify_dispatch_cwd(
    cwd: Path,
    *,
    project_root: Path,
    controller_label: str | None,
    managed_root: Path | None = None,
) -> str:
    """Classify ``--cwd`` as ``in-place``, ``ring-seat``, or ``refuse``.

    In-place is the project root of *this* dispatch:

    - ``cwd`` is that path's git toplevel and equals ``project_root``, or
    - ``cwd`` is not inside any git checkout and equals ``project_root``
      (a non-git project identity, the same fallback
      ``resolve_project_root`` uses).

    A nested path under another git checkout — ``.cache/worktrees/foo``,
    an ad-hoc linked worktree of the same repo, ``/tmp`` clones — is not
    in-place. Those are the sprawl paths: refuse unless they are a managed
    worktree in this repository's pool.
    """
    try:
        resolved = Path(cwd).expanduser().resolve()
        root = Path(project_root).resolve()
    except OSError:
        return "refuse"
    proc = _git_proc(resolved, "rev-parse", "--show-toplevel")
    git_top: Path | None = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        try:
            git_top = Path(proc.stdout.strip()).resolve()
        except OSError:
            git_top = None
    if git_top is not None:
        if git_top == resolved and git_top == root:
            return "in-place"
    elif resolved == root:
        return "in-place"
    if managed_root is not None:
        try:
            ring_root = Path(managed_root).expanduser().resolve(strict=False)
            if resolved.parent == ring_root and is_captive_seat_name(resolved.name):
                return "ring-seat"
        except OSError:
            pass
    elif is_managed_worktree_path(resolved, project_root=root):
        return "ring-seat"
    return "refuse"


def _verify_project_root(
    project_root: Path, *, timeout: float | None = None
) -> None:
    if timeout is None:
        raw_top = _git(project_root, "rev-parse", "--show-toplevel")
    else:
        raw_top = _git(
            project_root, "rev-parse", "--show-toplevel", timeout=timeout
        )
    top = Path(raw_top).resolve()
    if top != project_root:
        raise WorktreeSeatError(f"--cwd must be the git repository root: {project_root}")


def _verify_existing_seat(
    project_root: Path,
    worktree_path: Path,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> None:
    if worktree_path.is_symlink():
        raise WorktreeSeatError(f"managed worktree path must not be a symlink: {worktree_path}")
    if not worktree_path.is_dir():
        raise WorktreeSeatError(f"managed worktree path is not a directory: {worktree_path}")
    if deadline is None and timeout is None:
        raw_top = _git(worktree_path, "rev-parse", "--show-toplevel")
    else:
        raw_top = _git(
            worktree_path,
            "rev-parse",
            "--show-toplevel",
            timeout=_deadline_timeout(
                deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S
            ),
        )
    top = Path(raw_top).resolve()
    if top != worktree_path.resolve():
        raise WorktreeSeatError(
            f"managed worktree path is not a Git worktree root: {worktree_path}"
        )
    if deadline is None and timeout is None:
        worktree_common = _git_common_dir(worktree_path)
        project_common = _git_common_dir(project_root)
    else:
        worktree_common = _git_common_dir(
            worktree_path,
            timeout=_deadline_timeout(
                deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S
            ),
        )
        project_common = _git_common_dir(
            project_root,
            timeout=_deadline_timeout(
                deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S
            ),
        )
    if worktree_common != project_common:
        raise WorktreeSeatError(f"managed worktree belongs to another repository: {worktree_path}")


def read_only_worktree_usage(
    path: str | Path,
    *,
    ledger_dir: Path | None = None,
    project_root: Path | None = None,
    ledger_index=None,
    identity_deadline: float | None = None,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
) -> dict[str, str]:
    """Return the shared fail-closed in-use verdict for a detached checkout.

    The GC owns the ledger predicate for both normal and read-only worktrees;
    keep this import lazy because the GC imports this module for Git helpers.
    """
    import goalflight_worktree_gc

    return goalflight_worktree_gc.check_unowned(
        str(path),
        ledger_dir or goalflight_ledger.runs_dir(create=False),
        project_root=project_root,
        ledger_index=ledger_index,
        identity_deadline=identity_deadline,
        **(
            {"process_cwd_census": process_cwd_census}
            if process_cwd_census is not None else {}
        ),
    )


def _registered_worktree_paths(
    project_root: Path,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
    git_args_prefix: tuple[str, ...] = (),
) -> list[Path]:
    git_timeout = (
        READ_ONLY_GIT_TIMEOUT_S
        if timeout is None
        else max(0.0, timeout)
    )
    git_kwargs = {"timeout": git_timeout}
    if deadline is not None:
        git_kwargs["deadline"] = deadline
    output = _git(
        project_root, *git_args_prefix, "worktree", "list", "--porcelain",
        **git_kwargs,
    )
    paths: list[Path] = []
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree ") :].strip()
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        paths.append(candidate)
    return paths


def _read_only_registered_worktrees(
    project_root: Path, *, deadline: float | None = None
) -> list[Path]:
    if deadline is not None and time.monotonic() >= deadline:
        raise WorktreeSeatError("read-only registration scan deadline expired")
    timeout = _deadline_timeout(deadline)
    paths: list[Path] = []
    for candidate in _registered_worktree_paths(
        project_root, timeout=timeout, deadline=deadline,
    ):
        if deadline is not None and time.monotonic() >= deadline:
            raise WorktreeSeatError("read-only registration scan deadline expired")
        verdict, _reason = read_only_worktree_path_verdict(
            candidate, project_root=project_root
        )
        if deadline is not None and time.monotonic() >= deadline:
            raise WorktreeSeatError("read-only registration scan deadline expired")
        if verdict == YES:
            paths.append(candidate)
    return paths


def _read_only_reap_candidates(
    registered: list[Path],
    *,
    requested: Path | None,
    project_root: Path,
    ledger_index,
    deadline: float,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
) -> tuple[list[tuple[int, Path]], list[str], bool]:
    """Collect fully checked old candidates; an expired pass is discarded."""
    import goalflight_worktree_gc

    now_ns = time.time_ns()
    candidates: list[tuple[int, Path]] = []
    blocked_reasons: list[str] = []
    for path in registered:
        if time.monotonic() >= deadline:
            blocked_reasons.append(
                "read-only candidate scan deadline expired; retaining checkout"
            )
            return [], blocked_reasons, True
        if requested is not None and path.resolve(strict=False) == requested:
            continue
        usage = read_only_worktree_usage(
            path,
            project_root=project_root,
            ledger_index=ledger_index,
            identity_deadline=deadline,
            **(
                {"process_cwd_census": process_cwd_census}
                if process_cwd_census is not None else {}
            ),
        )
        if usage["verdict"] != YES:
            reason = usage.get("reason")
            if reason and reason not in blocked_reasons:
                blocked_reasons.append(reason)
            continue
        cleanliness = goalflight_worktree_gc.check_clean(
            str(path), directory_state="present", deadline=deadline
        )
        if cleanliness["verdict"] != YES:
            reason = cleanliness.get("reason")
            if reason and reason not in blocked_reasons:
                blocked_reasons.append(reason)
            continue
        if time.monotonic() >= deadline:
            blocked_reasons.append(
                "read-only candidate scan deadline expired; retaining checkout"
            )
            return [], blocked_reasons, True
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if time.monotonic() >= deadline:
            blocked_reasons.append(
                "read-only candidate scan deadline expired; retaining checkout"
            )
            return [], blocked_reasons, True
        if now_ns - mtime >= READ_ONLY_WORKTREE_GRACE_S * 1_000_000_000:
            candidates.append((mtime, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates, blocked_reasons, False


def _ledger_directory_stamp(ledger_dir: Path) -> tuple[int, int, int] | None:
    """Cheap change detector for the atomic ledger directory writers."""
    try:
        stat_result = ledger_dir.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_mtime_ns),
    )


def _registered_pool_worktrees(
    project_root: Path,
    *,
    deadline: float | None = None,
    git_args_prefix: tuple[str, ...] = (),
) -> list[Path]:
    """Return pooled seats whose lock identity can be opened or adopted."""
    paths: list[Path] = []
    for candidate in _registered_worktree_paths(
        project_root,
        timeout=_deadline_timeout(deadline),
        deadline=deadline,
        git_args_prefix=git_args_prefix,
    ):
        if deadline is not None and time.monotonic() >= deadline:
            raise WorktreeSeatError("read-only candidate scan deadline expired")
        managed_verdict, _managed_reason = managed_worktree_path_verdict(
            candidate, project_root=project_root
        )
        if managed_verdict != YES:
            continue
        verdict, _reason, lock_path, lock_stat = _registered_pool_seat_lock_info(
            candidate,
            project_root=project_root,
            deadline=deadline,
            git_args_prefix=git_args_prefix,
        )
        if verdict == YES or (verdict == UNKNOWN and lock_path is not None and lock_stat is not None):
            paths.append(candidate)
    return paths


def _read_only_seat_matches(
    path: Path, base_commit: str, *, deadline: float | None = None
) -> bool:
    """Require the exact review base and an entirely clean checkout."""
    if deadline is not None and time.monotonic() >= deadline:
        return False
    try:
        if _git(
            path,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            timeout=_deadline_timeout(deadline),
            deadline=deadline,
        ) != base_commit:
            return False
    except WorktreeSeatError:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    return check_seat_cleanliness(
        path, timeout=_deadline_timeout(deadline), deadline=deadline,
    )["verdict"] == YES


def _try_acquire_shared_read_only_seat(
    project_root: Path,
    path: Path,
    base_commit: str,
    dispatch_id: str,
    *,
    deadline: float | None = None,
) -> WorktreeReadOnlySeatLease | None:
    """Take a non-blocking shared hold, then close the HEAD/status race."""
    try:
        if deadline is None:
            lock_info = _registered_pool_seat_lock_info(
                path, project_root=project_root
            )
        else:
            lock_info = _registered_pool_seat_lock_info(
                path, project_root=project_root, deadline=deadline
            )
        verdict, _reason, lock_path, expected_stat = lock_info
        if (
            verdict == NO
            or lock_path is None
            or expected_stat is None
        ):
            return None
        flags = _lock_open_flags() & ~os.O_CREAT
        registry_root = _git_common_dir(
            project_root, timeout=_deadline_timeout(deadline), deadline=deadline,
        )
        if deadline is not None and time.monotonic() >= deadline:
            return None
        lock_fd = _open_registered_lock(
            lock_path,
            flags,
            registry_root=registry_root,
            allow_unregistered=verdict == UNKNOWN,
            registration_deadline=deadline,
        )
    except (OSError, WorktreeSeatError):
        return None
    lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
    path_lock: WorktreePathLock | None = None
    try:
        if verdict == UNKNOWN:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _adopt_exclusive_lock(
                    lock_path,
                    lock_file.fileno(),
                    registry_root=registry_root,
                    deadline=deadline,
                )
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            except (BlockingIOError, OSError):
                lock_file.close()
                return None
        else:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                return None
        try:
            path_lock = _try_acquire_worktree_path_lock(
                path, dispatch_id, shared=True, deadline=deadline
            )
        except (WorktreePathLockBusy, WorktreePathLockUnknown):
            lock_file.close()
            return None
        lease = WorktreeReadOnlySeatLease(
            path=path,
            seat_name=path.name,
            dispatch_id=dispatch_id,
            lock_file=lock_file,
            path_lock=path_lock,
        )
        if deadline is not None and time.monotonic() >= deadline:
            lease.release()
            return None
        if not _read_only_seat_matches(path, base_commit, deadline=deadline):
            lease.release()
            return None
        return lease
    except BaseException:
        lock_file.close()
        raise


def try_acquire_read_only_pool_seat(
    project_root: Path,
    path: Path,
    dispatch_id: str,
    *,
    base_commit: str | None = None,
    deadline: float | None = None,
) -> WorktreeReadOnlySeatLease | None:
    """Hold a specific pooled seat when it still matches a read-only review."""
    if deadline is None:
        deadline = time.monotonic() + READ_ONLY_REAP_TIMEOUT_S
    project_root = project_root.resolve()
    path = path.expanduser().resolve(strict=False)
    managed_verdict, _managed_reason = managed_worktree_path_verdict(
        path, project_root=project_root
    )
    if managed_verdict != YES:
        return None
    if base_commit is None:
        return None
    if not _read_only_seat_matches(path, base_commit, deadline=deadline):
        return None
    return _try_acquire_shared_read_only_seat(
        project_root, path, base_commit, dispatch_id, deadline=deadline
    )


def bind_read_only_worktree(
    project_root: Path,
    dispatch_id: str,
    *,
    base: str | None = None,
    reap: bool = True,
) -> tuple[Path, str, WorktreeReadOnlySeatLease | None]:
    """Bind to the detached shared checkout without taking a writer-seat lock."""
    try:
        path, base_commit = shared_read_only_worktree(
            project_root, base=base, reap=reap
        )
    except WorktreeReadOnlyLockTimeout:
        raise
    except (OSError, subprocess.SubprocessError, WorktreeSeatError) as exc:
        raise WorktreeCwdRefused(
            f"read-only review checkout unavailable: {exc}"
        ) from exc
    return path, base_commit, None


def _reap_read_only_worktrees(
    project_root: Path,
    *,
    root: Path,
    requested_path: Path | None,
    deadline: float | None = None,
    shared_deadline: bool = False,
    ledger_index=None,
    ledger_stamp: tuple[int, int, int] | None = None,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
    fresh_process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
) -> None:
    """Remove only old, clean, unowned registered read-only checkouts."""
    if deadline is None:
        deadline = time.monotonic() + READ_ONLY_REAP_TIMEOUT_S
    import goalflight_worktree_gc

    if ledger_index is None:
        # Direct test/tool callers do not own the allocator lock. Snapshot the
        # ledger under StateLock, then release it before any pin/remove Git
        # call; final removal rechecks below reacquire it briefly.
        ledger_lock = goalflight_ledger.StateLock.try_acquire(deadline)
        if ledger_lock is None:
            return
        try:
            snapshot = goalflight_worktree_gc.ledger_index_for_dir(
                goalflight_ledger.runs_dir(create=False), deadline=deadline
            )
            ledger_stamp = _ledger_directory_stamp(
                goalflight_ledger.runs_dir(create=False)
            )
        finally:
            ledger_lock.release()
        return _reap_read_only_worktrees(
            project_root,
            root=root,
            requested_path=requested_path,
            deadline=deadline,
            shared_deadline=shared_deadline,
            ledger_index=snapshot,
            ledger_stamp=ledger_stamp,
            process_cwd_census=process_cwd_census,
            fresh_process_cwd_census=fresh_process_cwd_census,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print(
            "goalflight read-only reaper: retained checkout(s); "
            "read-only candidate scan deadline expired; retaining checkout",
            file=sys.stderr,
        )
        return
    try:
        registered = _read_only_registered_worktrees(
            project_root, deadline=deadline
        )
    except (OSError, subprocess.SubprocessError, WorktreeSeatError) as exc:
        if (
            "deadline" in str(exc).lower()
            or "timed out" in str(exc).lower()
            or time.monotonic() >= deadline
        ):
            print(
                "goalflight read-only reaper: retained checkout(s); "
                "read-only candidate scan deadline expired; retaining checkout",
                file=sys.stderr,
            )
        return
    requested = (
        requested_path.resolve(strict=False) if requested_path is not None else None
    )
    candidates, blocked_reasons, scan_expired = _read_only_reap_candidates(
        registered,
        requested=requested,
        project_root=project_root,
        ledger_index=ledger_index,
        deadline=deadline,
        process_cwd_census=process_cwd_census,
    )
    if scan_expired and not shared_deadline:
        # A slow ownership scan must not consume the entire cleanup budget and
        # leave known over-cap entries untouched. Retry the bounded snapshot
        # once with a fresh interval; incomplete snapshots remain fail-closed.
        deadline = time.monotonic() + READ_ONLY_REAP_TIMEOUT_S
        candidates, retry_blockers, scan_expired = _read_only_reap_candidates(
            registered,
            requested=requested,
            project_root=project_root,
            ledger_index=ledger_index,
            deadline=deadline,
            process_cwd_census=process_cwd_census,
        )
        blocked_reasons = retry_blockers
    if scan_expired:
        print(
            "goalflight read-only reaper: retained checkout(s); "
            "read-only candidate scan deadline expired; retaining checkout",
            file=sys.stderr,
        )
        return

    # Candidate discovery and pin/removal are separate bounded phases. Every
    # successful scan, including a retry, gets a fresh removal interval.
    if not shared_deadline:
        deadline = time.monotonic() + READ_ONLY_REAP_TIMEOUT_S
    pinned: list[Path] = []
    for _mtime, path in candidates[READ_ONLY_WORKTREE_KEEP:]:
        if deadline - time.monotonic() <= 0:
            blocked_reasons.append(
                "read-only removal deadline expired; retaining checkout"
            )
            break
        pin_timeout = _deadline_timeout(deadline)
        if pin_timeout <= 0:
            blocked_reasons.append(
                "read-only pin deadline expired; retaining checkout"
            )
            break
        _keep_ref, pin_error = pin_worktree_head_before_remove(
            project_root, path, timeout=pin_timeout, deadline=deadline
        )
        if pin_error is not None:
            if "timed out" in pin_error.lower() or "deadline" in pin_error.lower():
                blocked_reasons.append(
                    f"read-only pin deadline expired; retaining checkout ({pin_error})"
                )
            continue
        pinned.append(path)
        if time.monotonic() >= deadline:
            blocked_reasons.append(
                "read-only removal deadline expired after pinning; retaining checkout"
            )
            break

    # All keep refs are pinned before the global ledger lock is acquired. One
    # fresh snapshot covers every candidate, so the lock is held only for the
    # final ownership re-check and removals, not for any unbounded Git call.
    if pinned:
        ledger_lock = goalflight_ledger.StateLock.try_acquire(deadline)
        if ledger_lock is None:
            blocked_reasons.append(
                "read-only final ledger re-check deadline expired; retaining checkout"
            )
        else:
            with ledger_lock:
                ledger_dir = goalflight_ledger.runs_dir(create=False)
                if ledger_stamp == _ledger_directory_stamp(ledger_dir):
                    fresh_index = ledger_index
                else:
                    fresh_index = goalflight_worktree_gc.ledger_index_for_dir(
                        ledger_dir, deadline=deadline
                    )
                for path in pinned:
                    if time.monotonic() >= deadline:
                        blocked_reasons.append(
                            "read-only final re-check deadline expired; retaining checkout"
                        )
                        break
                    fresh_usage = read_only_worktree_usage(
                        path,
                        project_root=project_root,
                        ledger_index=fresh_index,
                        identity_deadline=deadline,
                        **(
                            {"process_cwd_census": fresh_process_cwd_census}
                            if fresh_process_cwd_census is not None
                            else (
                                {"process_cwd_census": process_cwd_census}
                                if process_cwd_census is not None else {}
                            )
                        ),
                    )
                    if fresh_usage["verdict"] != YES:
                        reason = fresh_usage.get("reason") or "ownership re-check unknown"
                        blocked_reasons.append(
                            f"read-only final ownership re-check retained checkout: {reason}"
                        )
                        continue
                    cleanliness = goalflight_worktree_gc.check_clean(
                        str(path), directory_state="present", deadline=deadline
                    )
                    if cleanliness["verdict"] != YES:
                        blocked_reasons.append(
                            "read-only final cleanliness re-check retained checkout: "
                            + cleanliness["reason"]
                        )
                        continue
                    _archive_path, archive_error = goalflight_worktree_gc._archive_ignored_payload(
                        path, deadline=deadline
                    )
                    if archive_error is not None:
                        blocked_reasons.append(
                            "read-only ignored payload could not be preserved; "
                            f"retaining checkout ({archive_error})"
                        )
                        continue
                    if time.monotonic() >= deadline:
                        cwd_snapshot = None
                    elif fresh_process_cwd_census is not None:
                        cwd_snapshot = fresh_process_cwd_census()
                    else:
                        cwd_snapshot = _process_cwd_snapshot(
                            timeout_s=min(
                                PROCESS_CWD_SNAPSHOT_TIMEOUT_S,
                                deadline - time.monotonic(),
                            ),
                            deadline=deadline,
                        )
                    cwd_use = _process_cwd_under_seat(cwd_snapshot, path)
                    if cwd_use is not False:
                        blocked_reasons.append(
                            "read-only pre-removal process cwd census is "
                            + ("occupied" if cwd_use else "unknown")
                            + "; retaining checkout"
                        )
                        continue
                    try:
                        _git(
                            project_root,
                            "worktree",
                            "remove",
                            str(path),
                            timeout=_deadline_timeout(deadline),
                        deadline=deadline,
                        )
                    except WorktreeSeatError as exc:
                        if "timed out" in str(exc).lower() or "deadline" in str(exc).lower():
                            blocked_reasons.append(
                                f"read-only removal deadline expired; retaining checkout ({exc})"
                            )
                            break
                        # Dirty or otherwise refused trees remain registered and intact.
                        continue
                    except (OSError, subprocess.SubprocessError):
                        continue
    if blocked_reasons:
        detail = "; ".join(blocked_reasons[:3])
        if len(blocked_reasons) > 3:
            detail += f"; and {len(blocked_reasons) - 3} more blockers"
        print(
            "goalflight read-only reaper: retained checkout(s); " + detail,
            file=sys.stderr,
        )


def _write_occupant(
    lock_file: TextIO,
    *,
    seat_name: str,
    dispatch_id: str,
    controller_label: str | None = None,
) -> None:
    identity = goalflight_compat.process_start_identity(os.getpid())
    payload = {
        "seat": seat_name,
        "worktree_id": seat_name,
        "dispatch_id": dispatch_id,
        "pid": os.getpid(),
        "start_token": identity.get("start_token") if identity else None,
        "controller_label": controller_label,
        "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    lock_file.seek(0)
    previous = lock_file.read()
    try:
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(payload, lock_file, sort_keys=True)
        lock_file.write("\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
    except BaseException:
        # A failed replacement must not erase the previous diagnostic owner.
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(previous)
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except OSError:
            pass
        raise


def _lock_metadata(lock_file: TextIO) -> dict:
    try:
        lock_file.seek(0)
        payload = json.load(lock_file)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unregistered_lock_metadata(lock_path: Path) -> dict:
    """Read diagnostic metadata while treating an unregistered lock as busy."""
    try:
        fd = _open_lock_path_safely(
            lock_path,
            _lock_open_flags() & ~os.O_CREAT,
            expected_stat=_lock_path_identity(lock_path),
        )
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r+", encoding="utf-8") as lock_file:
            return _lock_metadata(lock_file)
    except OSError:
        return {}


def _known_lock_dispatch_id(lock_file: TextIO) -> str | None:
    value = _lock_metadata(lock_file).get("dispatch_id")
    text = str(value or "").strip()
    return text or None


def _bounded_journal_attempt_state(project_root: Path, dispatch_id: str) -> str:
    """Read one attempt without the journal reader's startup validation path."""
    connection = None
    deadline = time.monotonic() + 0.2
    try:
        journal_path = goalflight_journal.resolve_journal_path(project_root)
        if not journal_path.is_file():
            return "unreadable"
        journal_stat = journal_path.stat()
        with journal_path.open("rb") as journal_file:
            header = journal_file.read(20)
        wal_mode = len(header) >= 20 and header[18:20] == b"\x02\x02"
        wal_path = Path(f"{journal_path}-wal")
        shm_path = Path(f"{journal_path}-shm")
        wal_exists = wal_path.exists()
        shm_exists = shm_path.exists()
        if wal_mode and wal_exists != shm_exists:
            return "unreadable"
        if wal_mode and not wal_exists:
            # No WAL means the main database is a safe point-in-time source:
            # attempt lifecycle states never move back from TERMINAL.
            query = "?mode=ro&immutable=1"
        else:
            # Rollback-journal mode honors writer locks. In WAL mode, both
            # sidecars must already exist so this read-only open cannot create
            # them as a side effect.
            query = "?mode=ro"
        connection = sqlite3.connect(
            journal_path.resolve().as_uri() + query,
            uri=True,
            timeout=0.025,
        )
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 100
        )
        connection.execute("PRAGMA busy_timeout = 25")
        rows = connection.execute(
            "SELECT lifecycle_state FROM dispatch_attempts "
            "WHERE dispatch_id = ? LIMIT 2",
            (dispatch_id,),
        ).fetchall()
        if time.monotonic() >= deadline:
            return "unreadable"
        if wal_mode and not wal_exists:
            current_stat = journal_path.stat()
            with journal_path.open("rb") as journal_file:
                current_header = journal_file.read(20)
            if (
                wal_path.exists()
                or current_header[18:20] != b"\x02\x02"
                or (current_stat.st_size, current_stat.st_mtime_ns)
                != (journal_stat.st_size, journal_stat.st_mtime_ns)
            ):
                return "unreadable"
        if not rows:
            return "missing"
        if len(rows) != 1:
            return "unreadable"
        state = rows[0][0]
        if state not in {
            goalflight_journal.ATTEMPT_PREPARED,
            goalflight_journal.ATTEMPT_STARTING,
            goalflight_journal.ATTEMPT_RUNNING,
            goalflight_journal.ATTEMPT_TERMINAL,
            goalflight_journal.ATTEMPT_ABANDONED,
        }:
            return "unreadable"
        return str(state)
    except Exception:
        return "unreadable"
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _occupant_description(
    lock_file: TextIO,
    seat_name: str,
    *,
    project_root: Path | None = None,
    journal_state_cache: dict[str, str] | None = None,
) -> str:
    payload = _lock_metadata(lock_file)
    return _holder_description(
        seat_name,
        payload,
        project_root=project_root,
        journal_state_cache=journal_state_cache,
    )


def _lock_identity_liveness(lock_metadata: dict | None) -> bool | None:
    if not isinstance(lock_metadata, dict):
        return None
    pid = lock_metadata.get("pid")
    token = lock_metadata.get("start_token")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(token, str)
        or not token
    ):
        return None
    return goalflight_compat.process_identity_matches(pid, token)


def _holder_record(
    dispatch_id: str,
    *,
    project_root: Path | None = None,
    lock_metadata: dict | None = None,
    journal_state_cache: dict[str, str] | None = None,
    probe_journal: bool = True,
) -> tuple[dict, bool | None]:
    """Read worker evidence, using journal holders only with lock identity."""
    record = goalflight_ledger.read_record(dispatch_id)
    status = None
    if record is None:
        status_path = goalflight_dispatch_paths.dispatch_base_dir() / (
            f"{goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)}.status.json"
        )
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if project_root is None:
                return {}, None
            holder_metadata = (
                lock_metadata
                if isinstance(lock_metadata, dict)
                and lock_metadata.get("dispatch_id") == dispatch_id
                else None
            )
            lock_identity_live = None
            lock_pid_status = "unknown"
            if holder_metadata is not None:
                lock_identity_live = _lock_identity_liveness(holder_metadata)
                lock_pid_status = (
                    "live"
                    if lock_identity_live is True
                    else "absent_or_reused"
                    if lock_identity_live is False
                    else "unknown"
                )
            elif lock_metadata is not None:
                lock_pid_status = "not_applicable"
            if journal_state_cache is not None and dispatch_id in journal_state_cache:
                journal_state = journal_state_cache[dispatch_id]
            elif not probe_journal:
                journal_state = "unreadable"
            else:
                journal_state = _bounded_journal_attempt_state(
                    project_root, dispatch_id
                )
                if journal_state_cache is not None:
                    journal_state_cache[dispatch_id] = journal_state
            state = (
                journal_state
                if journal_state not in {"unreadable", "missing"}
                else None
            )
            return {
                "dispatch_id": dispatch_id,
                "_holder_source": "journal",
                "_lock_identity_live": lock_identity_live,
                "_lock_pid_status": lock_pid_status,
                "controller_label": (
                    holder_metadata.get("controller_label")
                    if holder_metadata is not None
                    else None
                ),
                "journal_state": journal_state,
                "state": state,
            }, None
        except (OSError, ValueError):
            return {}, None
        if not isinstance(status, dict) or status.get("dispatch_id") != dispatch_id:
            return {}, None
        identity = status.get("expected_worker_identity") or status.get("worker_identity")
        if not isinstance(identity, dict):
            return {}, None
        pid = identity.get("pid")
        token = identity.get("start_token")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(token, str)
        ):
            return {}, None
        status_pid = status.get("worker_pid")
        if status_pid is not None and status_pid != pid:
            return {}, None
        if goalflight_compat.is_linux():
            valid_token = bool(
                re.fullmatch(
                    r"linux:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:[1-9][0-9]*",
                    token,
                )
            )
        elif goalflight_compat.is_macos():
            match = re.fullmatch(r"darwin:([1-9][0-9]*):([0-9]+)", token)
            valid_token = bool(match and int(match.group(2)) < 1_000_000)
        elif goalflight_compat.is_windows():
            valid_token = bool(re.fullmatch(r"windows:[1-9][0-9]*", token))
        else:
            valid_token = False
        if not valid_token:
            return {}, None
        record = {"worker_pid": pid, "worker_identity": identity}
    elif goalflight_ledger.record_is_unreadable(record):
        return {}, None
    else:
        record = dict(record)
        status_path = record.get("status_path")
        if status_path:
            try:
                status = json.loads(Path(status_path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                return record, None
            else:
                if not isinstance(status, dict) or status.get("dispatch_id") != dispatch_id:
                    return record, None
    if status:
        for key in ("state", "worker_pid", "wrapper_pid", "lock_holder"):
            if status.get(key) is not None:
                if key == "state" and status[key] != "cancelled" and goalflight_ledger.terminal_state_for(status[key]) == "unknown":
                    continue
                record[key] = status[key]
        # Watchers publish the current process snapshot (empty after exit, or
        # a reused PID). Only the launch generation can prove this worker dead.
        expected = status.get("expected_worker_identity") or record.get("worker_identity")
        if expected:
            record["worker_identity"] = expected
        elif status.get("worker_identity"):
            record["worker_identity"] = status["worker_identity"]
    identity = record.get("worker_identity") or {}
    if not isinstance(identity, dict):
        return record, _holder_effective_liveness(record, None)
    pid = record.get("worker_pid")
    token = identity.get("start_token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not token:
        return record, _holder_effective_liveness(record, None)
    if identity.get("pid", pid) != pid:
        return record, _holder_effective_liveness(record, None)
    live = goalflight_compat.process_identity_matches(pid, token)
    return record, _holder_effective_liveness(record, live)


def _process_cwd_snapshot(
    *, timeout_s: float, deadline: float | None = None,
) -> list[tuple[int, str]] | None:
    """Return one complete host-wide lsof cwd snapshot, or None if unknown."""
    try:
        result = goalflight_subprocess.run_with_deadline(
            ["lsof", "-nP", "-d", "cwd", "-Fpfn"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            deadline=deadline,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None

    stdout = result.stdout or ""
    if result.returncode != 0:
        return None
    if not goalflight_subprocess.lsof_stderr_is_mount_stat_warnings_only(result.stderr or ""):
        return None
    if not stdout or not stdout.endswith("\n"):
        return None

    rows: list[tuple[int, str]] = []
    pending_pid: int | None = None
    pending_cwd_fd = False
    for line in stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            if pending_pid is not None:
                return None
            pending_pid = int(line[1:])
            if pending_pid <= 0:
                return None
        elif line == "fcwd" and pending_pid is not None and not pending_cwd_fd:
            pending_cwd_fd = True
        elif line.startswith("n") and pending_pid is not None and pending_cwd_fd:
            path = line[1:]
            if not path:
                return None
            if path.endswith(" (deleted)"):
                path = path[: -len(" (deleted)")]
            rows.append((pending_pid, path))
            pending_pid = None
            pending_cwd_fd = False
        else:
            return None
    if pending_pid is not None or pending_cwd_fd:
        return None
    return rows or None


def _process_cwd_under_seat(
    snapshot: list[tuple[int, str]] | None,
    worktree_path: Path,
) -> bool | None:
    """Return whether a live process cwd is in this seat; None means unknown."""
    if snapshot is None:
        return None
    try:
        seat_path = os.path.realpath(worktree_path)
        for pid, raw_cwd in snapshot:
            if not raw_cwd:
                return None
            process_cwd = os.path.realpath(raw_cwd)
            try:
                if os.path.commonpath((seat_path, process_cwd)) != seat_path:
                    continue
            except ValueError:
                continue
            try:
                os.getpgid(pid)
            except ProcessLookupError:
                continue
            except OSError:
                return None
            return True
    except (OSError, RuntimeError, ValueError):
        return None
    return False


def _holder_record_is_terminal(record: dict) -> bool:
    if record.get("_holder_source") == "journal":
        return record.get("journal_state") == goalflight_journal.ATTEMPT_TERMINAL
    state = str(record.get("state") or "")
    terminal = goalflight_ledger.terminal_state_for(
        state, record.get("reason") or record.get("error")
    )
    return state == "cancelled" or terminal not in {"", "unknown", "watcher_stopped"}


def _holder_effective_liveness(record: dict, live: bool | None) -> bool | None:
    """Treat terminal proof that no worker launched as a proven absent worker."""
    if (
        live is None
        and _holder_record_is_terminal(record)
        and goalflight_ledger.prelaunch_without_worker_evidence(record)
    ):
        return False
    return live


def _holder_is_terminal(
    dispatch_id: str,
    *,
    project_root: Path | None = None,
    lock_metadata: dict | None = None,
    journal_state_cache: dict[str, str] | None = None,
) -> bool:
    """Return true when the terminal holder is safe to classify as reclaimed."""
    record, live = _holder_record(
        dispatch_id,
        project_root=project_root,
        lock_metadata=lock_metadata,
        journal_state_cache=journal_state_cache,
    )
    if not record or not _holder_record_is_terminal(record):
        return False
    if isinstance(lock_metadata, dict) and lock_metadata.get("dispatch_id") == dispatch_id:
        lock_live = _lock_identity_liveness(lock_metadata)
        if live is True or lock_live is True:
            return False
        if live is None or lock_live is None:
            return False
        live = False
    return live is False


def _holder_description(
    name: str,
    metadata: dict,
    *,
    project_root: Path | None = None,
    journal_state_cache: dict[str, str] | None = None,
    probe_journal: bool = True,
    deadline: float | None = None,
) -> str:
    dispatch_id = str(metadata.get("dispatch_id") or "unknown-dispatch")
    if Path(name).is_absolute() and Path(name).is_dir():
        branch = _git_proc(
            Path(name),
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            timeout=_deadline_timeout(deadline),
            deadline=deadline,
        )
        if branch is not None and branch.returncode == 0:
            for prefix in (WORKTREE_BRANCH_PREFIX, SEAT_BRANCH_PREFIX):
                if branch.stdout.strip().startswith(prefix + "/"):
                    branch_id = branch.stdout.strip()[len(prefix) + 1:]
                    if branch_id != dispatch_id:
                        return (
                            _holder_description(
                                Path(name).name,
                                {"dispatch_id": branch_id},
                                project_root=project_root,
                                journal_state_cache=journal_state_cache,
                                probe_journal=probe_journal,
                                deadline=deadline,
                            )
                            + " / lock holder: "
                            + _holder_description(
                                Path(name).name,
                                metadata,
                                project_root=project_root,
                                journal_state_cache=journal_state_cache,
                                probe_journal=probe_journal,
                                deadline=deadline,
                            )
                        )
                    break
    record, live = _holder_record(
        dispatch_id,
        project_root=project_root,
        lock_metadata=metadata,
        journal_state_cache=journal_state_cache,
        probe_journal=probe_journal,
    )
    if record.get("_holder_source") == "journal":
        journal_state = str(record.get("journal_state") or "unreadable")
        lock_pid_status = record.get("_lock_pid_status") or "unknown"
        return (
            f"{Path(name).name}={dispatch_id} "
            f"controller={record.get('controller_label') or 'unknown'} "
            f"state={record.get('state') or 'unknown'} "
            f"journal={journal_state} lock_pid={lock_pid_status}"
        )
    detail = (
        f"{Path(name).name}={dispatch_id} "
        f"controller={record.get('controller_label') or 'unknown'} "
        f"state={record.get('state') or 'unknown'}"
    )
    if live is True:
        detail += f" worker_pid={record['worker_pid']}"
    elif live is False:
        detail += " worker exited"
    elif record.get("prelaunch_failure") is True:
        detail += " worker never launched"
    else:
        detail += " worker identity unknown"
    if record.get("wrapper_pid"):
        detail += f" wrapper_pid={record['wrapper_pid']}"
    return detail


def _holder_validation_reason(
    worktree_path: Path,
    record: dict,
    live: bool | None,
    *,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
    require_census_for_prelaunch: bool = False,
    deadline: float | None = None,
) -> str | None:
    """Apply the shared terminal, identity, and cwd-retention rule."""
    if not _holder_record_is_terminal(record):
        return "state_not_terminal"
    if live is None:
        return "identity_unknown"
    if live is True:
        return "identity_live"
    prelaunch_proof = goalflight_ledger.prelaunch_without_worker_evidence(record)
    if prelaunch_proof and not require_census_for_prelaunch:
        return None
    if live is not False and not prelaunch_proof:
        return "identity_unknown"
    try:
        snapshot = (
            process_cwd_census()
            if process_cwd_census is not None
            else _process_cwd_snapshot(
                timeout_s=_deadline_timeout(deadline) or READ_ONLY_GIT_TIMEOUT_S,
                **({"deadline": deadline} if deadline is not None else {}),
            )
        )
    except Exception:
        snapshot = None
    has_live_cwd = _process_cwd_under_seat(snapshot, worktree_path)
    if has_live_cwd is True:
        return "worker_cwd_live"
    if has_live_cwd is None:
        return "worker_cwd_unknown"
    return None


def _validate_holder(
    worktree_path: Path,
    prior_dispatch_id: str,
    *,
    project_root: Path | None = None,
    lock_metadata: dict | None = None,
    journal_state_cache: dict[str, str] | None = None,
    probe_journal: bool = True,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
    require_census_for_prelaunch: bool = False,
    deadline: float | None = None,
) -> str:
    """Existing branch names identify holders; directory labels never do."""
    metadata_dispatch_id = prior_dispatch_id
    presence = _path_presence(worktree_path)
    if presence == "unknown":
        raise WorktreeSeatUnavailable(
            f"worktree {worktree_path} could not be inspected; holder is unknown "
            "(identity_unknown)",
            seat_name=worktree_path.name,
            reason="identity_unknown",
        )
    if presence == "present":
        try:
            branch = _git(
                worktree_path,
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                timeout=_deadline_timeout(deadline),
                deadline=deadline,
            )
        except WorktreeSeatError as exc:
            if "git command timed out" not in str(exc):
                raise
            raise WorktreeSeatUnavailable(
                f"worktree {worktree_path} branch probe timed out "
                "(branch_probe_timeout)",
                seat_name=worktree_path.name,
                reason="branch_probe_timeout",
            ) from exc
        for prefix in (WORKTREE_BRANCH_PREFIX, SEAT_BRANCH_PREFIX):
            if branch.startswith(prefix + "/"):
                prior_dispatch_id = branch[len(prefix) + 1:]
                break
    lock_dispatch_id = None
    if isinstance(lock_metadata, dict) and isinstance(
        lock_metadata.get("dispatch_id"), str
    ):
        lock_dispatch_id = lock_metadata["dispatch_id"].strip()
    holders = tuple(
        dict.fromkeys(
            holder
            for holder in (lock_dispatch_id, metadata_dispatch_id, prior_dispatch_id)
            if holder and holder != "unknown-dispatch"
        )
    )
    if not holders:
        raise WorktreeSeatUnavailable(
            f"worktree {worktree_path} has unknown ownership (identity_unknown)",
            seat_name=worktree_path.name,
            reason="identity_unknown",
        )
    for holder in holders:
        holder_metadata = (
            lock_metadata
            if isinstance(lock_metadata, dict)
            and lock_metadata.get("dispatch_id") == holder
            else None
        )
        record, live = _holder_record(
            holder,
            project_root=project_root,
            lock_metadata=holder_metadata,
            journal_state_cache=journal_state_cache,
            probe_journal=probe_journal,
        )
        # Before worker attribution, the lock owner is the only liveness
        # evidence. Once a worker identity is recorded, use that identity so a
        # still-running controller cannot pin a seat after its worker exits.
        if (
            holder_metadata is not None
            and goalflight_ledger.prelaunch_without_worker_evidence(record)
            and any(
                record.get(key)
                for key in ("parent_dispatch_id", "worktree_reclaimed_dispatch_id")
            )
            and record.get("_holder_source") != "journal"
        ):
            lock_live = _lock_identity_liveness(holder_metadata)
            if live is True or lock_live is True:
                live = True
            elif live is None or lock_live is None:
                live = None
            else:
                live = False
        reason = _holder_validation_reason(
            worktree_path,
            record,
            live,
            process_cwd_census=process_cwd_census,
            require_census_for_prelaunch=require_census_for_prelaunch,
            deadline=deadline,
        )
        if reason is not None:
            description_metadata = (
                holder_metadata
                if holder_metadata is not None
                else {"dispatch_id": holder}
            )
            description = _holder_description(
                worktree_path.name,
                description_metadata,
                project_root=project_root,
                journal_state_cache=journal_state_cache,
                probe_journal=probe_journal,
                deadline=deadline,
            )
            raise WorktreeSeatUnavailable(
                f"{description}; retained ({reason})",
                seat_name=worktree_path.name,
                current_holder=holder,
                reason=reason,
            )
    # The branch owner is the durable code line. The lock record identifies
    # the dispatch that actually occupied this seat for the reclaim attempt.
    return metadata_dispatch_id


def _attribute_prelaunch_seat_holder(
    worktree_path: Path,
    dispatch_id: str | None,
    *,
    project_root: Path | None = None,
    lock_metadata: dict | None = None,
    journal_state_cache: dict[str, str] | None = None,
) -> str | None:
    """Replace a never-launched child with the durable occupant it inherited."""
    if not dispatch_id:
        return None
    holder = dispatch_id
    seen: set[str] = set()
    while holder and holder not in seen:
        seen.add(holder)
        record, _live = _holder_record(
            holder,
            project_root=project_root,
            lock_metadata=lock_metadata,
            journal_state_cache=journal_state_cache,
            probe_journal=False,
        )
        if (
            not _holder_record_is_terminal(record)
            or not goalflight_ledger.prelaunch_without_worker_evidence(record)
        ):
            return holder
        recorded_path = record.get("worktree_path") or record.get("worker_cwd")
        if isinstance(recorded_path, str) and Path(recorded_path).resolve(
            strict=False
        ) != worktree_path.resolve(strict=False):
            return holder
        parent = next(
            (
                str(record.get(key) or "").strip()
                for key in ("parent_dispatch_id", "worktree_reclaimed_dispatch_id")
                if str(record.get(key) or "").strip() not in {"", holder}
            ),
            None,
        )
        if parent is None:
            break
        holder = parent
    return None


def _refnames(
    cwd: Path, *, timeout: float | None = None
) -> tuple[list[str] | None, str]:
    proc = _git_proc(
        cwd, "for-each-ref", "--format=%(refname)", timeout=timeout
    )
    if proc is None:
        return None, "git for-each-ref could not run"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return None, detail
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()], ""


def _rev_list_unique_commits(
    cwd: Path,
    *,
    start: str,
    base_commit: str,
    refs: list[str],
    moving_ref: str | None,
    oneline: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Enumerate ``start`` minus the exact reset keep-set through stdin.

    The keep-set is every ref under ``refs/`` except ``moving_ref``, plus the
    reset base. ``--stdin`` keeps the unbounded ref list out of argv; unlike
    ``--all``, it does not add HEAD or another worktree's detached HEAD.
    """
    revisions = [start, "--not", base_commit]
    revisions.extend(ref for ref in refs if not moving_ref or ref != moving_ref)
    args = ["rev-list"]
    if oneline:
        args.append("--oneline")
    args.append("--stdin")
    return _git_proc(
        cwd,
        *args,
        input_text="\n".join(revisions) + "\n",
        timeout=timeout,
    )


def check_reset_preserves_commits(
    cwd: Path,
    *,
    start: str,
    base_commit: str,
    moving_ref: str | None,
    timeout: float | None = None,
) -> dict[str, str]:
    """YES if moving ``start`` to ``base_commit`` would not lose unique commits.

    ``moving_ref`` is the full refname that ``checkout -B`` / ``worktree add -B``
    will force-move. That ref is excluded from the keep-set. Detached HEAD
    passes ``moving_ref=None``: nothing currently names those commits.
    UNKNOWN retains — losing a commit is irreversible.
    """
    refs, err = _refnames(cwd, timeout=timeout)
    if refs is None:
        return _condition(
            UNKNOWN,
            f"cannot list refs ({err}); unique commits are unknown",
        )
    proc = _rev_list_unique_commits(
        cwd,
        start=start,
        base_commit=base_commit,
        refs=refs,
        moving_ref=moving_ref,
        oneline=True,
        timeout=timeout,
    )
    if proc is None:
        return _condition(
            UNKNOWN, "git rev-list could not run; unique commits are unknown"
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return _condition(UNKNOWN, f"cannot enumerate unique commits ({detail})")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return _condition(YES, "no unique commits would become unreachable")
    shown = ", ".join(lines[:8])
    extra = f" (+{len(lines) - 8} more)" if len(lines) > 8 else ""
    target = moving_ref or "detached HEAD"
    return _condition(
        NO,
        f"{target} has commits not reachable from the new base or any other ref; "
        f"reset would lose: {shown}{extra}",
    )


def check_seat_cleanliness(
    worktree_path: Path, *, timeout: float | None = None,
    deadline: float | None = None,
) -> dict[str, str]:
    """YES clean / NO dirty / UNKNOWN. Same three-state as worktree GC check_clean."""
    proc = _git_proc(
        worktree_path,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        timeout=timeout,
        deadline=deadline,
    )
    if proc is None:
        return _condition(
            UNKNOWN, "git status could not run, so cleanliness is unknown"
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return _condition(
            UNKNOWN, f"git status failed ({detail}), so cleanliness is unknown"
        )
    try:
        dirty = [record for record in _parse_porcelain_z(proc.stdout) if _status_is_product(record)]
    except WorktreeSeatResetRefused as exc:
        return _condition(UNKNOWN, str(exc))
    if dirty:
        return _condition(
            NO,
            f"worktree has uncommitted or untracked files ({len(dirty)} entries)",
        )
    return _condition(YES, "worktree is clean")


def pin_unique_commits(
    worktree_path: Path,
    *,
    base_commit: str,
    moving_ref: str | None,
    worktree_id: str,
    timeout: float | None = None,
) -> dict[str, str | None]:
    """Durably pin commits that a reset would otherwise make unreachable."""
    refs, err = _refnames(worktree_path, timeout=timeout)
    if refs is None:
        return {"verdict": UNKNOWN, "reason": f"cannot list refs ({err})", "keep_ref": None}
    head_proc = _git_proc(
        worktree_path, "rev-parse", "HEAD^{commit}", timeout=timeout
    )
    if head_proc is None or head_proc.returncode != 0:
        return {"verdict": UNKNOWN, "reason": "cannot resolve worktree HEAD", "keep_ref": None}
    head = head_proc.stdout.strip()
    unique = _rev_list_unique_commits(
        worktree_path,
        start=head,
        base_commit=base_commit,
        refs=refs,
        moving_ref=moving_ref,
        timeout=timeout,
    )
    if unique is None or unique.returncode != 0:
        return {
            "verdict": UNKNOWN,
            "reason": "cannot enumerate unique commits",
            "keep_ref": None,
        }
    if not [line for line in unique.stdout.splitlines() if line.strip()]:
        return {"verdict": YES, "reason": "no unique commits need pinning", "keep_ref": None}

    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(worktree_id)).strip(".-") or "worktree"
    stamp = str(time.time_ns())
    keep_ref = f"refs/{KEEP_REF_PREFIX}/{stamp}-{safe_id}-{head[:12]}"
    updated = _git_proc(
        worktree_path,
        "update-ref",
        "--create-reflog",
        keep_ref,
        head,
        "",
        timeout=timeout,
    )
    if updated is None or updated.returncode != 0:
        return {
            "verdict": UNKNOWN,
            "reason": "cannot durably create keep ref",
            "keep_ref": None,
        }
    verified = _git_proc(
        worktree_path,
        "rev-parse",
        "--verify",
        f"{keep_ref}^{{commit}}",
        timeout=timeout,
    )
    if verified is None or verified.returncode != 0 or verified.stdout.strip() != head:
        return {
            "verdict": UNKNOWN,
            "reason": "keep ref did not verify after creation",
            "keep_ref": None,
        }
    return {
        "verdict": YES,
        "reason": f"unique commits pinned at {keep_ref}",
        "keep_ref": keep_ref,
    }


def keep_ref_containing_commit(
    project_root: Path,
    commit: str,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> dict[str, str | None]:
    """Find a goal-flight keep ref that already preserves ``commit``."""
    proc = _git_proc(
        project_root,
        "for-each-ref",
        f"--contains={commit}",
        "--format=%(refname)",
        "refs/goalflight/keep/",
        "refs/keep/",
        timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
        deadline=deadline,
    )
    if proc is None or proc.returncode != 0:
        detail = (
            (proc.stderr or proc.stdout).strip()
            if proc is not None
            else "git for-each-ref could not run"
        )
        return {
            "verdict": UNKNOWN,
            "reason": f"keep-ref reachability could not be checked ({detail})",
            "keep_ref": None,
        }
    keep_ref = next(
        (
            line.strip()
            for line in proc.stdout.splitlines()
            if line.startswith(("refs/goalflight/keep/", "refs/keep/"))
        ),
        None,
    )
    if keep_ref is None:
        return {
            "verdict": NO,
            "reason": "commit is not reachable from a goal-flight keep ref",
            "keep_ref": None,
        }
    return {
        "verdict": YES,
        "reason": f"commit is preserved by {keep_ref}",
        "keep_ref": keep_ref,
    }


def pin_worktree_head_before_remove(
    project_root: Path,
    worktree_path: str | Path,
    *,
    dispatch_id: str | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
) -> tuple[str | None, str | None]:
    """Pin a detached worktree HEAD before its administrative entry is removed."""
    path = Path(worktree_path)
    try:
        if deadline is not None and time.monotonic() >= deadline:
            return None, "pin deadline expired before resolving checkout HEAD"
        head = _git(
            path,
            "rev-parse",
            "HEAD^{commit}",
            timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
            deadline=deadline,
        )
        existing = keep_ref_containing_commit(
            project_root,
            head,
            timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
            deadline=deadline,
        )
        if existing["verdict"] == YES:
            return str(existing["keep_ref"]), None
        if existing["verdict"] != NO:
            return None, str(existing["reason"])

        safe_id = re.sub(
            r"[^A-Za-z0-9._-]+", "-", str(dispatch_id or "")
        ).strip(".-")
        stamp = str(time.time_ns())
        if safe_id:
            keep_ref = f"refs/{KEEP_REF_PREFIX}/{safe_id}/head-{stamp}-{head[:12]}"
        else:
            safe_path = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip(".-") or "worktree"
            keep_ref = f"refs/{KEEP_REF_PREFIX}/gc-{stamp}-{safe_path}-{head[:12]}"
        previous = _git_proc(
            project_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{keep_ref}^{{commit}}",
            timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
            deadline=deadline,
        )
        if previous is None or previous.returncode not in (0, 1):
            return None, "cannot inspect dispatch keep ref before pinning"
        if previous.returncode == 0:
            if previous.stdout.strip() == head:
                return keep_ref, None
            return None, "cannot create unique dispatch keep ref"
        if deadline is not None and time.monotonic() >= deadline:
            return None, "pin deadline expired before creating keep ref"
        created = _git_proc(
            project_root,
            "update-ref",
            "--create-reflog",
            keep_ref,
            head,
            "",
            timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
            deadline=deadline,
        )
        if created is None or created.returncode != 0:
            # Another safe pin may have won the race after the reachability
            # check. Reuse it when possible; otherwise fail closed.
            existing = keep_ref_containing_commit(
                project_root,
                head,
                timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
                deadline=deadline,
            )
            if existing["verdict"] == YES:
                return str(existing["keep_ref"]), None
            return None, "cannot durably create keep ref"
        if deadline is not None and time.monotonic() >= deadline:
            return None, "pin deadline expired before verifying keep ref"
        if _git(
            project_root,
            "rev-parse",
            "--verify",
            f"{keep_ref}^{{commit}}",
            timeout=_deadline_timeout(deadline, cap=timeout or READ_ONLY_GIT_TIMEOUT_S),
            deadline=deadline,
        ) != head:
            return None, "keep ref did not verify after creation"
        return keep_ref, None
    except WorktreeSeatError as exc:
        return None, str(exc)


def _pin_existing_target_branch(
    cwd: Path,
    *,
    branch: str,
    base_commit: str,
    keep_id: str,
) -> str | None:
    """Pin an existing target branch before a forced checkout can move it."""
    target_ref = f"refs/heads/{branch}"
    target = _git_proc(
        cwd, "rev-parse", "--verify", "--quiet", f"{target_ref}^{{commit}}"
    )
    if target is None or target.returncode not in (0, 1):
        raise WorktreeSeatResetRefused(
            f"cannot inspect target branch {branch}; refusing reset"
        )
    if target.returncode == 1:
        return None
    target_tip = target.stdout.strip()
    if not target_tip:
        raise WorktreeSeatResetRefused(
            f"target branch {branch} has no readable tip; refusing reset"
        )

    from_base = _git_proc(
        cwd, "merge-base", "--is-ancestor", target_tip, base_commit
    )
    if from_base is None or from_base.returncode not in (0, 1):
        raise WorktreeSeatResetRefused(
            f"cannot compare target branch {branch} with reset base; refusing reset"
        )
    if from_base.returncode == 0:
        return None

    kept = _git_proc(
        cwd,
        "for-each-ref",
        "--contains",
        target_tip,
        "--format=%(refname)",
        f"refs/{KEEP_REF_PREFIX}/",
    )
    if kept is None or kept.returncode != 0:
        raise WorktreeSeatResetRefused(
            f"cannot inspect keep refs for target branch {branch}; refusing reset"
        )
    if any(line.strip() for line in kept.stdout.splitlines()):
        return None

    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(keep_id)).strip(".-") or "worktree"
    stamp = str(time.time_ns())
    keep_ref = (
        f"refs/{KEEP_REF_PREFIX}/{safe_id}/prior-target-"
        f"{stamp}-{target_tip[:12]}"
    )
    existing = _git_proc(
        cwd, "rev-parse", "--verify", "--quiet", f"{keep_ref}^{{commit}}"
    )
    if existing is None or existing.returncode not in (0, 1):
        raise WorktreeSeatResetRefused(
            f"cannot inspect saved target branch {branch}; refusing reset"
        )
    if existing.returncode == 0:
        if existing.stdout.strip() != target_tip:
            raise WorktreeSeatResetRefused(
                f"saved target branch {branch} pin points elsewhere; refusing reset"
            )
        return keep_ref
    updated = _git_proc(
        cwd, "update-ref", "--create-reflog", keep_ref, target_tip, ""
    )
    if updated is None or updated.returncode != 0:
        raise WorktreeSeatResetRefused(
            f"cannot pin target branch {branch}; refusing reset"
        )
    verified = _git_proc(cwd, "rev-parse", "--verify", f"{keep_ref}^{{commit}}")
    if verified is None or verified.returncode != 0 or verified.stdout.strip() != target_tip:
        raise WorktreeSeatResetRefused(
            f"target branch {branch} pin did not verify; refusing reset"
        )
    return keep_ref


def evaluate_seat_reset_safety(
    worktree_path: Path,
    *,
    base_commit: str,
    new_branch: str,
) -> dict:
    """Decide whether an existing worktree may be reset onto ``new_branch`` at base.

    Conjunction, same shape as ``goalflight_worktree_gc.classify``: every
    conjunct that we cannot prove must retain. Unique commits (detached and
    ahead, or a branch we would force-move) are a hard retain. Cleanliness
    UNKNOWN is a hard retain. Dirty (NO) is not: acquire still quarantines
    uncommitted files onto ``goalflight/quarantine/...`` before checkout, which
    is the pool guarantee this helper must not replace.

    Could share ``check_clean`` with GC: GC already imports this module, so
    extracting cleanliness into a third module would be the cycle-free share.
    Unique-commits-vs-other-refs is a different question than GC's
    merged-into-integration, so that conjunct stays here.
    """
    timeout = SEAT_RESET_GIT_TIMEOUT_S
    abbrev_proc = _git_proc(
        worktree_path,
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        timeout=timeout,
    )
    if abbrev_proc is None or abbrev_proc.returncode != 0:
        detail = "git rev-parse --abbrev-ref HEAD could not run"
        if abbrev_proc is not None:
            detail = (abbrev_proc.stderr or abbrev_proc.stdout or "").strip() or detail
        return {
            "decision": "retain",
            "reason": f"current branch unknown ({detail}); refusing reset",
            "conditions": {},
        }
    abbrev = abbrev_proc.stdout.strip() or "HEAD"
    detached = abbrev == "HEAD"

    head_proc = _git_proc(worktree_path, "rev-parse", "HEAD", timeout=timeout)
    if head_proc is None or head_proc.returncode != 0:
        detail = "cannot resolve HEAD"
        if head_proc is not None:
            detail = (head_proc.stderr or head_proc.stdout or "").strip() or detail
        return {
            "decision": "retain",
            "reason": f"{detail}; refusing reset",
            "conditions": {},
        }
    head = head_proc.stdout.strip()

    if detached:
        commits = check_reset_preserves_commits(
            worktree_path,
            start=head,
            base_commit=base_commit,
            moving_ref=None,
            timeout=timeout,
        )
    elif abbrev == new_branch:
        commits = check_reset_preserves_commits(
            worktree_path,
            start=head,
            base_commit=base_commit,
            moving_ref=f"refs/heads/{new_branch}",
            timeout=timeout,
        )
    else:
        commits = _condition(
            YES,
            f"branch {abbrev!r} remains after checkout of {new_branch!r}",
        )

    clean = check_seat_cleanliness(worktree_path, timeout=timeout)
    conditions = {"commits_preserved": commits, "cleanliness": clean}
    blockers: list[str] = []
    if commits["verdict"] != YES:
        blockers.append(commits["reason"])
    if clean["verdict"] == UNKNOWN:
        blockers.append(clean["reason"])
    if any(condition["verdict"] == UNKNOWN for condition in conditions.values()):
        return {
            "decision": "retain",
            "reason": "; ".join(blockers),
            "conditions": conditions,
            "head": head,
            "moving_ref": f"refs/heads/{abbrev}" if not detached else None,
        }
    return {
        "decision": "reset",
        "reason": "unique commits are known and will be pinned; cleanliness is known",
        "conditions": conditions,
        "head": head,
        "moving_ref": f"refs/heads/{abbrev}" if not detached else None,
    }


def _worktree_checking_out_branch(project_root: Path, branch: str) -> Path | None:
    """Return the repository worktree already holding this branch, if any."""
    output = _git(project_root, "worktree", "list", "--porcelain")
    current_path: Path | None = None
    target_ref = f"refs/heads/{branch}"
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :]).resolve(strict=False)
        elif line.startswith("branch ") and line[len("branch ") :] == target_ref:
            return current_path
    return None


def _dispatch_branch_in_use(
    branch_owner: Path | None,
    *,
    project_root: Path,
    branch: str,
) -> WorktreeSeatError:
    seat = branch_owner.name if branch_owner is not None else "unknown"
    if branch_owner is not None and _path_presence(branch_owner) == "absent":
        admin = _missing_checkout_admin_text(project_root, branch_owner)
        if admin == "unresolved":
            return WorktreeSeatError(
                f"dispatch_branch_in_use:{seat}; checkout {branch_owner} is "
                f"absent and its admin dir is unresolved; operator must inspect "
                f"{branch_owner}. Branch {branch} may still be in use"
            )
        command = drop_stale_command(project_root, admin)
        return WorktreeSeatError(
            f"dispatch_branch_in_use:{seat}; admin dir {admin} holds branch "
            f"{branch} as possibly in use; verify no process from that seat is "
            f"running (for example after a reboot), then {command}"
        )
    return WorktreeSeatError(
        f"dispatch_branch_in_use:{seat}; dispatch again under a new id"
    )


def _stuck_present_checkout_reason(worktree_path: Path, detail: str) -> str:
    """Operator text for a present tree that must not be reset or dropped."""
    return (
        f"{detail}; operator must inspect {worktree_path}; "
        "the checkout is present and its occupant is unknown, "
        "so no removal command is available"
    )


def _clear_fresh_seat_payload(worktree_path: Path) -> None:
    """Remove a new seat's partial payload while retaining Git's pointer."""
    if not (worktree_path / ".git").is_file():
        raise OSError(f"worktree {worktree_path} has no gitdir pointer")
    for entry in os.scandir(worktree_path):
        if entry.name == ".git":
            continue
        path = Path(entry.path)
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            os.unlink(path)
        else:
            shutil.rmtree(path)


def _report_cow_population(
    worktree_path: Path,
    *,
    enabled: bool,
    pattern: str,
    operation: str,
    cow_result: dict | None,
    reason: str | None,
) -> None:
    if not enabled:
        return
    import goalflight_seat_cow as cow

    if cow_result is None:
        detail = reason or "cow-not-used"
        cow_skipped = detail in {"base-changed", "tracked-changes"}
        retained = (
            operation == "reuse"
            and detail == "shared-repository-mode-unsupported"
            and (cow.read_seat_cow_report(worktree_path) or {}).get("population")
            in {"cow", "cow-retained"}
        )
        if retained:
            previous = cow.read_seat_cow_report(worktree_path) or {}
            previous.update(
                population="cow-retained",
                reason=detail,
                operation=operation,
            )
            try:
                cow.write_seat_cow_report(worktree_path, previous)
            except OSError as exc:
                print(
                    f"goalflight seat cow: {worktree_path}: evidence write failed: {exc}",
                    file=sys.stderr,
                )
            print(
                f"goalflight seat cow: {worktree_path}: population=cow-retained reason={detail}",
                file=sys.stderr,
            )
            return
        print(
            f"goalflight seat cow: {worktree_path}: population=plain"
            f"{' cow_skipped=true' if cow_skipped else ''} reason={detail}",
            file=sys.stderr,
        )
        try:
            cow.write_plain_report(
                worktree_path,
                reason=detail,
                pattern=pattern,
                operation=operation,
                cow_skipped=cow_skipped,
            )
        except OSError as exc:
            print(
                f"goalflight seat cow: {worktree_path}: evidence write failed: {exc}",
                file=sys.stderr,
            )
        return
    print(
        f"goalflight seat cow: {worktree_path}: population=cow "
        f"clone={cow_result.get('clone')} generation={cow_result.get('generation')}",
        file=sys.stderr,
    )
    try:
        cow.write_seat_cow_report(worktree_path, cow_result)
    except OSError as exc:
        print(
            f"goalflight seat cow: {worktree_path}: evidence write failed: {exc}",
            file=sys.stderr,
        )


def _cow_repair_refused(worktree_path: Path, detail: str) -> WorktreeSeatUnavailable:
    reason = _stuck_present_checkout_reason(worktree_path, detail)
    return WorktreeSeatUnavailable(reason, seat_name=worktree_path.name, reason=reason)


def _create_seat_worktree(
    project_root: Path,
    worktree_path: Path,
    *,
    branch: str,
    base_commit: str,
    keep_id: str,
    after_add: Callable[[], None] | None = None,
    cow_plan: tuple[bool, str | None, str | None] | None = None,
) -> bool:
    branch_owner = _worktree_checking_out_branch(project_root, branch)
    if branch_owner is not None and branch_owner.resolve(strict=False) != worktree_path.resolve(
        strict=False
    ):
        raise _dispatch_branch_in_use(
            branch_owner, project_root=project_root, branch=branch
        )
    # A deleted checkout can remain registered. Do not prune here: prune
    # deletes other slots' admin dirs, and those dirs are the evidence that
    # a holder may still have the registered inode open. GC removes admin
    # dirs under its lock and registration checks.
    presence = _path_presence(worktree_path)
    if presence == "unknown":
        raise WorktreeSeatError(
            f"cannot inspect worktree path {worktree_path}; refusing add"
        )
    if presence != "absent":
        raise WorktreeSeatError(
            f"seat_add_refused:{worktree_path.name}; path appeared before creation"
        )
    _exclude_managed_worktrees(project_root)
    ref = f"refs/heads/{branch}"
    exists = _git_proc(project_root, "show-ref", "--verify", "--quiet", ref)
    if exists is None:
        raise WorktreeSeatError(
            f"cannot determine whether worktree branch {branch} already exists; refusing add"
        )
    if exists.returncode not in (0, 1):
        detail = (exists.stderr or exists.stdout or "").strip() or f"exit {exists.returncode}"
        raise WorktreeSeatError(
            f"cannot determine whether worktree branch {branch} exists ({detail})"
        )

    def add_seat_worktree(*args: str) -> None:
        try:
            _git(project_root, "worktree", "add", *args)
        except WorktreeSeatError as exc:
            # Any add refusal skips this slot. Do not match git's text: the
            # locked-worktree wording and the already-registered wording are
            # the same outcome. A branch held by a different checkout is still
            # the named in-use refusal, because that check reads the owner.
            try:
                owner = _worktree_checking_out_branch(project_root, branch)
            except WorktreeSeatError as inspect_error:
                raise WorktreeSeatUnavailable(
                    f"worktree {worktree_path.name} skipped ({exc}); "
                    "git refused worktree add; "
                    f"owner inspection failed: {inspect_error}",
                    seat_name=worktree_path.name,
                    reason=str(exc),
                ) from exc
            if (
                owner is not None
                and owner.resolve(strict=False)
                != worktree_path.resolve(strict=False)
            ):
                # An add refusal skips this slot, including when another
                # checkout holds the branch. Raising the plain in-use error
                # aborts the whole allocation.
                in_use = _dispatch_branch_in_use(
                    owner, project_root=project_root, branch=branch
                )
                raise WorktreeSeatUnavailable(
                    str(in_use),
                    seat_name=worktree_path.name,
                    reason=str(in_use),
                ) from exc
            raise WorktreeSeatUnavailable(
                f"worktree {worktree_path.name} skipped ({exc}); "
                "git refused worktree add",
                seat_name=worktree_path.name,
                reason=str(exc),
            ) from exc

    def register_created_seat() -> Exception | None:
        # The callback releases the mutation lock, including when registration
        # fails. Checkout still has to run: the admin dir already names this
        # branch, and a later reclaim copies the index.
        if after_add is None:
            return None
        try:
            after_add()
        except Exception as exc:
            return exc
        return None

    sparse_pattern = _configured_seat_sparse_pattern(project_root)
    sparse_reason: str | None = None
    sparse_core_worktree = False
    if sparse_pattern is not None and not any(
        line.strip() == "/*" for line in sparse_pattern.splitlines()
    ):
        print(
            f"goalflight seat sparse: {project_root}: pattern must include '/*'; "
            "creating a full seat",
            file=sys.stderr,
        )
        sparse_pattern = None
    if sparse_pattern is not None:
        try:
            common_config = _git_common_dir(project_root) / "config"
            extension = _git_proc(
                project_root,
                "config",
                "--file",
                str(common_config),
                "--get",
                "extensions.worktreeConfig",
            )
            if extension is None or extension.returncode not in (0, 1):
                detail = "git config extensions.worktreeConfig could not run"
                if extension is not None:
                    detail = (extension.stderr or extension.stdout or "").strip() or detail
                sparse_reason = detail
            elif extension.returncode != 0 or extension.stdout.strip().lower() != "true":
                sparse_reason = "common Git config does not enable extensions.worktreeConfig"
            else:
                for setting in ("core.worktree", "core.bare"):
                    config_args = ("--bool", "--get") if setting == "core.bare" else ("--get",)
                    configured = _git_proc(
                        project_root,
                        "config",
                        "--file",
                        str(common_config),
                        *config_args,
                        setting,
                    )
                    if configured is None or configured.returncode not in (0, 1):
                        detail = f"git config {setting} could not run"
                        if configured is not None:
                            detail = (
                                configured.stderr or configured.stdout or ""
                            ).strip() or detail
                        sparse_reason = detail
                        break
                    if configured.returncode != 0:
                        continue
                    if setting == "core.worktree":
                        sparse_reason = f"common Git config sets {setting}"
                        sparse_core_worktree = True
                        break
                    if configured.stdout.strip().lower() == "true":
                        sparse_reason = f"common Git config sets {setting}=true"
                        break
        except (OSError, WorktreeSeatError) as exc:
            sparse_reason = f"cannot inspect common Git config: {exc}"
        if sparse_reason is not None:
            print(
                f"goalflight seat sparse: {project_root}: {sparse_reason}; "
                "creating a full seat",
                file=sys.stderr,
            )
            sparse_pattern = None

    cow_on, fallback_reason, cow_pattern = cow_plan or (False, None, None)
    cow_pattern = cow_pattern or sparse_pattern or "/*\n"
    if sparse_pattern is None and sparse_reason is not None and cow_on:
        fallback_reason = fallback_reason or f"sparse-profile-unavailable: {sparse_reason}"
        cow_pattern = "/*\n"

    if sparse_pattern is None:
        if exists.returncode == 0:
            _pin_existing_target_branch(
                project_root,
                branch=branch,
                base_commit=base_commit,
                keep_id=keep_id,
            )
            add_seat_worktree(
                "--no-checkout",
                "-B",
                branch,
                str(worktree_path),
                base_commit,
            )
        else:
            add_seat_worktree(
                "--no-checkout",
                "-b",
                branch,
                str(worktree_path),
                base_commit,
            )
        # Registration releases the mutation lock. Checkout then populates
        # files, including when registration failed, and must not keep the lock.
        registration_error = register_created_seat()
        primed: dict | None = None
        checkout_done = False
        fallback_needs_reset = False
        if cow_on and fallback_reason is None:
            import goalflight_seat_cow as cow

            try:
                primed = cow.prime_sparse_worktree(
                    project_root,
                    worktree_path,
                    base_commit=base_commit,
                    pattern=cow_pattern,
                    sparse=False,
                    operation="new",
                )
            except cow.CowFallback as exc:
                fallback_reason = exc.reason
                fallback_needs_reset = True
                try:
                    _clear_fresh_seat_payload(worktree_path)
                except OSError as repair_exc:
                    raise _cow_repair_refused(
                        worktree_path, f"copy-on-write repair failed: {repair_exc}"
                    ) from exc
        try:
            if sparse_core_worktree:
                _git(
                    worktree_path,
                    "config",
                    "--worktree",
                    "core.worktree",
                    str(worktree_path),
                )
            checkout_done = True
            checkout_target = (
                ["-B", branch, base_commit]
                if fallback_needs_reset
                else [branch]
            )
            _git(
                worktree_path,
                "-c",
                "submodule.recurse=false",
                "checkout",
                "--no-overwrite-ignore",
                "-f",
                *checkout_target,
            )
            if primed is not None:
                cow.normalize_checkout_modes(worktree_path)
        except Exception as exc:
            if primed is not None and checkout_done:
                raise _cow_repair_refused(
                    worktree_path, f"copy-on-write checkout failed: {exc}"
                ) from exc
            if registration_error is not None:
                raise registration_error
            raise
        _report_cow_population(
            worktree_path,
            enabled=cow_on,
            pattern=cow_pattern,
            operation="new",
            cow_result=primed,
            reason=fallback_reason,
        )
        if registration_error is not None:
            raise registration_error
        return False

    add_args = ["worktree", "add", "--no-checkout"]
    if exists.returncode == 0:
        _pin_existing_target_branch(
            project_root,
            branch=branch,
            base_commit=base_commit,
            keep_id=keep_id,
        )
        add_args.extend(["-B", branch, str(worktree_path), base_commit])
    else:
        add_args.extend(["-b", branch, str(worktree_path), base_commit])
    add_seat_worktree(*add_args[2:])
    registration_error = register_created_seat()
    primed: dict | None = None
    checkout_done = False
    force_base_checkout = False
    sparse_fell_back_to_full = False

    def configure_sparse_pattern() -> None:
        if primed is not None:
            return
        _git(worktree_path, "config", "--worktree", "core.sparseCheckout", "true")
        _git(
            worktree_path,
            "sparse-checkout",
            "set",
            "--no-cone",
            "--stdin",
            input_text=sparse_pattern,
        )

    sparse_verification_started = False

    def run_sparse_checkout() -> None:
        nonlocal sparse_verification_started
        nonlocal checkout_done
        configure_sparse_pattern()
        if not checkout_done:
            checkout_done = True
            checkout_target = (
                ["-B", branch, base_commit] if force_base_checkout else [branch]
            )
            _git(
                worktree_path,
                "-c",
                "submodule.recurse=false",
                "checkout",
                "--no-overwrite-ignore",
                "-f",
                *checkout_target,
            )
        sparse_verification_started = True
        if primed is not None:
            cow.normalize_checkout_modes(worktree_path)
        if _git(worktree_path, "rev-parse", "HEAD") != base_commit:
            raise WorktreeSeatError(f"sparse seat HEAD is not pinned to {base_commit}")
        status = _git(worktree_path, "status", "--porcelain")
        if status:
            raise WorktreeSeatError(
                f"sparse seat is dirty after checkout: {status.splitlines()[0]}"
            )
        tagged_entries = [
            entry
            for entry in _git_nul(worktree_path, "ls-files", "-t", "-z").split("\0")
            if entry
        ]
        if any(len(entry) < 3 or entry[1] != " " for entry in tagged_entries):
            raise WorktreeSeatError("sparse seat has malformed tracked path tags")
        skipped = [entry[2:] for entry in tagged_entries if entry.startswith("S ")]
        populated = [entry[2:] for entry in tagged_entries if not entry.startswith("S ")]
        if len(populated) != len(tagged_entries) - len(skipped):
            raise WorktreeSeatError("sparse seat tracked path count did not verify")
        if not skipped:
            raise WorktreeSeatError("sparse pattern excluded no tracked path")
        if not populated:
            raise WorktreeSeatError("sparse pattern excluded every tracked path")
        for path in skipped[:1]:
            try:
                (worktree_path / path).lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorktreeSeatError(f"cannot inspect sparse path {path!r}: {exc}") from exc
            raise WorktreeSeatError(f"sparse path {path!r} is still present on disk")
        for path in populated:
            try:
                (worktree_path / path).lstat()
                break
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise WorktreeSeatError(
                    f"included sparse path {path!r} is missing: {exc}"
                ) from exc
        else:
            raise WorktreeSeatError("sparse pattern populated no tracked path on disk")

    if cow_on and fallback_reason is None:
        import goalflight_seat_cow as cow

        try:
            primed = cow.prime_sparse_worktree(
                project_root,
                worktree_path,
                base_commit=base_commit,
                pattern=cow_pattern,
                operation="new",
            )
        except cow.CowFallback as exc:
            fallback_reason = exc.reason
            force_base_checkout = True
            try:
                _clear_fresh_seat_payload(worktree_path)
            except OSError as repair_exc:
                raise _cow_repair_refused(
                    worktree_path, f"copy-on-write repair failed: {repair_exc}"
                ) from exc

    try:
        run_sparse_checkout()
    except (OSError, UnicodeError, WorktreeSeatError) as exc:
        if isinstance(exc, WorktreeSeatUnavailable):
            raise
        if cow_on and primed is not None and checkout_done and not sparse_verification_started:
            raise _cow_repair_refused(
                worktree_path, f"copy-on-write checkout failed: {exc}"
            ) from exc
        rollback_error: str | None = None
        disabled = _git_proc(worktree_path, "sparse-checkout", "disable")
        if disabled is None or disabled.returncode != 0:
            unset = _git_proc(
                worktree_path,
                "config",
                "--worktree",
                "--unset",
                "core.sparseCheckout",
            )
            if unset is None or unset.returncode not in (0, 5):
                detail = "could not disable sparse checkout"
                if unset is not None:
                    detail = (unset.stderr or unset.stdout or "").strip() or detail
                rollback_error = detail
        try:
            _git(
                worktree_path,
                "-c",
                "submodule.recurse=false",
                "checkout",
                "--no-overwrite-ignore",
                "-f",
                "-B",
                branch,
                base_commit,
            )
            if _git(worktree_path, "rev-parse", "HEAD") != base_commit:
                raise WorktreeSeatError(
                    f"full-seat rollback HEAD is not pinned to {base_commit}"
                )
            if _git(worktree_path, "status", "--porcelain"):
                raise WorktreeSeatError("full-seat rollback left a dirty worktree")
            if any(
                entry.startswith("S ")
                for entry in _git_nul(worktree_path, "ls-files", "-t", "-z").split("\0")
            ):
                raise WorktreeSeatError("full-seat rollback left sparse paths enabled")
        except (OSError, UnicodeError, WorktreeSeatError) as rollback_exc:
            rollback_error = str(rollback_exc)
        else:
            rollback_error = None
        if rollback_error is not None:
            print(
                f"goalflight seat sparse: setup failed for {worktree_path}: {exc}; "
                f"full-seat rollback failed: {rollback_error}",
                file=sys.stderr,
            )
            raise WorktreeSeatError(
                f"sparse setup failed and full-seat rollback failed: {rollback_error}"
            ) from exc
        print(
            f"goalflight seat sparse: setup failed for {worktree_path}: {exc}; "
            "restored full seat",
            file=sys.stderr,
        )
        if cow_on:
            fallback_reason = fallback_reason or f"sparse-checkout-failed: {exc}"
            sparse_fell_back_to_full = True
        if registration_error is not None:
            raise registration_error
        if not cow_on:
            return False
        primed = None

    _report_cow_population(
        worktree_path,
        enabled=cow_on,
        pattern=cow_pattern,
        operation="new",
        cow_result=primed,
        reason=fallback_reason,
    )
    if registration_error is not None:
        raise registration_error
    return not sparse_fell_back_to_full


def _seat_head_from_metadata(worktree_path: Path) -> str | None:
    """Read a worktree HEAD without starting Git or inspecting its files."""
    if not worktree_path.is_dir():
        return None
    try:
        git_marker = worktree_path / ".git"
        if git_marker.is_file():
            marker = git_marker.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                return None
            git_dir = Path(marker.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree_path / git_dir).resolve()
        elif git_marker.is_dir():
            git_dir = git_marker.resolve()
        else:
            return None
        head_text = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head_text.startswith("ref: "):
            ref = head_text[5:].strip()
            common_dir = git_dir
            commondir = git_dir / "commondir"
            if commondir.is_file():
                common_dir = (git_dir / commondir.read_text(encoding="utf-8").strip()).resolve()
            ref_path = common_dir / ref
            if ref_path.is_file():
                head_text = ref_path.read_text(encoding="utf-8").strip()
            else:
                packed = common_dir / "packed-refs"
                head_text = next(
                    (
                        line.split(" ", 1)[1].strip()
                        for line in packed.read_text(encoding="utf-8").splitlines()
                        if line and not line.startswith(("#", "^")) and " " in line
                        and line.split(" ", 1)[1] == ref
                    ),
                    "",
                )
        return head_text.strip() or None
    except (OSError, UnicodeError, ValueError):
        return None


def _seat_head_is_ancestor(
    project_root: Path, head: str | None, base_commit: str
) -> bool | None:
    """Return whether ``head`` is an ancestor, or None when unverifiable."""
    if not head:
        return None
    proc = _git_proc(
        project_root, "merge-base", "--is-ancestor", str(head), str(base_commit)
    )
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _seat_base_distance(worktree_path: Path, base_commit: str) -> int | None:
    """Return zero for an exact metadata HEAD, otherwise one when known."""
    head = _seat_head_from_metadata(worktree_path)
    if head is None:
        return None
    return 0 if head == str(base_commit).strip() else 1


def _tree_paths(
    cwd: Path, treeish: str, *, timeout: float | None = None
) -> set[str]:
    return set(
        _nul_paths(
            _git_nul(
                cwd,
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                treeish,
                timeout=timeout,
            )
        )
    )


def _tree_blob_ids(
    cwd: Path, treeish: str, *, timeout: float | None = None
) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for entry in _git_nul(
        cwd, "ls-tree", "-r", "-z", treeish, timeout=timeout
    ).split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise WorktreeSeatResetRefused(
                f"malformed tree entry for {treeish}; refusing reset"
            )
        mode, entry_type, blob_id = fields
        if entry_type == "commit":
            if mode != "160000":
                raise WorktreeSeatResetRefused(
                    f"malformed gitlink entry for {treeish}; refusing reset"
                )
            continue
        if entry_type != "blob" or mode == "160000":
            raise WorktreeSeatResetRefused(
                f"malformed tree entry for {treeish}; refusing reset"
            )
        blobs[path] = blob_id
    return blobs


def _prove_lfs_objects(
    worktree_path: Path,
    paths: list[str],
    *,
    timeout: float | None = None,
) -> None:
    tree_blobs = _tree_blob_ids(worktree_path, "HEAD", timeout=timeout)
    blob_ids: list[str] = []
    for path in paths:
        blob_id = tree_blobs.get(path)
        if blob_id is None:
            raise WorktreeSeatResetRefused(
                f"LFS path {path!r} is not present in HEAD; refusing reset"
            )
        blob_ids.append(blob_id)
    requested = tuple(dict.fromkeys(blob_ids))
    batch = _git_proc(
        worktree_path,
        "cat-file",
        "--batch",
        input_text="\n".join(requested) + "\n",
        timeout=timeout,
    )
    if batch is None or batch.returncode != 0:
        raise WorktreeSeatResetRefused(
            "cannot read LFS pointer blobs; refusing reset"
        )
    contents: dict[str, str] = {}
    offset = 0
    for blob_id in requested:
        header_end = batch.stdout.find("\n", offset)
        if header_end < 0:
            raise WorktreeSeatResetRefused(
                "malformed LFS pointer batch; refusing reset"
            )
        header = batch.stdout[offset:header_end].split()
        if len(header) != 3 or header[0] != blob_id or header[1] != "blob":
            raise WorktreeSeatResetRefused(
                "missing or invalid LFS pointer blob; refusing reset"
            )
        try:
            size = int(header[2])
        except ValueError as exc:
            raise WorktreeSeatResetRefused(
                "invalid LFS pointer blob size; refusing reset"
            ) from exc
        content_start = header_end + 1
        content_end = content_start + size
        content = batch.stdout[content_start:content_end]
        if len(content) != size or batch.stdout[content_end : content_end + 1] != "\n":
            raise WorktreeSeatResetRefused(
                "truncated LFS pointer blob; refusing reset"
            )
        contents[blob_id] = content
        offset = content_end + 1

    common_lfs = (
        _git_common_dir(worktree_path, timeout=timeout) / "lfs" / "objects"
    )
    for path, blob_id in zip(paths, blob_ids):
        pointer = contents.get(blob_id, "")
        oid_match = re.search(r"(?m)^oid sha256:([0-9a-f]{64})$", pointer)
        size_match = re.search(r"(?m)^size ([0-9]+)$", pointer)
        if oid_match is None or size_match is None:
            raise WorktreeSeatResetRefused(
                f"LFS pointer for {path!r} is unparseable; refusing reset"
            )
        digest = oid_match.group(1)
        expected_size = int(size_match.group(1))
        object_path = common_lfs / digest[:2] / digest[2:4] / digest
        try:
            if not object_path.is_file():
                raise OSError("object is missing")
            if object_path.stat().st_size != expected_size:
                raise OSError("object size does not match pointer")
            actual = hashlib.sha256()
            actual_size = 0
            with object_path.open("rb") as object_file:
                for chunk in iter(lambda: object_file.read(1024 * 1024), b""):
                    actual.update(chunk)
                    actual_size += len(chunk)
            if actual_size != expected_size or actual.hexdigest() != digest:
                raise OSError("object hash does not match pointer")
            worktree_actual = hashlib.sha256()
            worktree_size = 0
            with (worktree_path / path).open("rb") as worktree_file:
                for chunk in iter(lambda: worktree_file.read(1024 * 1024), b""):
                    worktree_actual.update(chunk)
                    worktree_size += len(chunk)
            if (
                worktree_size != expected_size
                or worktree_actual.hexdigest() != digest
            ):
                raise OSError("worktree bytes do not match pointer")
        except OSError as exc:
            raise WorktreeSeatResetRefused(
                f"LFS object for {path!r} is missing or unreadable; refusing reset"
            ) from exc


def _refuse_ignored_tree_collisions(
    worktree_path: Path,
    *,
    head: str,
    target: str,
    timeout: float | None = None,
) -> None:
    ignored = _nul_paths(
        _git_nul(
            worktree_path,
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            timeout=timeout,
        )
    )
    if not ignored:
        return
    tree_paths = _tree_paths(
        worktree_path, head, timeout=timeout
    ) | _tree_paths(worktree_path, target, timeout=timeout)
    ignorecase = _git_proc(
        worktree_path, "config", "--bool", "core.ignorecase", timeout=timeout
    )
    if ignorecase is None or ignorecase.returncode not in (0, 1):
        detail = "git config core.ignorecase could not run"
        if ignorecase is not None:
            detail = (ignorecase.stderr or ignorecase.stdout or "").strip() or detail
        raise WorktreeSeatResetRefused(
            f"cannot determine filesystem case-sensitivity ({detail}); refusing reset"
        )
    case_insensitive = bool(
        ignorecase.returncode == 0
        and ignorecase.stdout.strip().lower() == "true"
    )
    if case_insensitive:
        tree_paths = {tree_path.casefold() for tree_path in tree_paths}
    for raw_path in ignored:
        path = raw_path.rstrip("/")
        if not path:
            continue
        compare_path = path.casefold() if case_insensitive else path
        components = compare_path.split("/")
        if compare_path in tree_paths or any(
            tree_path.startswith(compare_path + "/") for tree_path in tree_paths
        ) or any(
            "/".join(components[:end]) in tree_paths
            for end in range(1, len(components))
        ):
            raise WorktreeSeatResetRefused(
                f"ignored path {raw_path!r} collides with HEAD or target tree; "
                "refusing reset"
            )


def _prepare_seat_checkout(
    worktree_path: Path,
    *,
    branch: str,
    base_commit: str,
    keep_id: str,
    project_root: Path | None = None,
    clean_ignored: bool = False,
    cow_plan: tuple[bool, str | None, str | None] | None = None,
) -> None:
    current_branch = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    current_head = _git(worktree_path, "rev-parse", "HEAD")
    tracked_status = _status_records(worktree_path, untracked="no")
    needs_checkout = (
        current_branch != branch or current_head != base_commit or tracked_status
    )
    # Never ``git clean -fdx``. Preserve the reserved notes namespace even
    # when a temp repo has not gitignored ``.goal-flight/``. Check branch
    # occupancy before cleaning: checkout can still refuse a branch held by
    # another worktree.
    if needs_checkout:
        _ensure_branch_available(worktree_path, branch)
        _pin_existing_target_branch(
            worktree_path,
            branch=branch,
            base_commit=base_commit,
            keep_id=keep_id,
        )
    if clean_ignored:
        _git(worktree_path, "clean", "-fdX", "-e", ".goal-flight", "--", ".")
        remaining = _nul_paths(
            _git_nul(
                worktree_path,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            )
        )
        remaining = [
            item
            for item in remaining
            if item != ".goal-flight" and not item.startswith(".goal-flight/")
        ]
        if remaining:
            raise WorktreeSeatResetRefused(
                "ignored content remained after archive and cleanup: "
                + ", ".join(remaining[:5])
            )
    _git(worktree_path, "clean", "-fd", "-e", ".goal-flight")
    if needs_checkout:
        cow_on, cow_reason, cow_pattern = cow_plan or (False, None, None)
        cow_pattern = cow_pattern or "/*\n"
        cow_result: dict | None = None
        if cow_on and cow_reason is None:
            # Reuse priming replaces the index with the template tree. Keep the
            # real index whenever checkout must reconcile a different HEAD or
            # tracked changes, so checkout can remove paths absent from base.
            if current_head != base_commit:
                cow_reason = "base-changed"
            elif tracked_status:
                cow_reason = "tracked-changes"
        if cow_on and cow_reason is None:
            if project_root is None:
                cow_reason = "project-root-unavailable"
            else:
                import goalflight_seat_cow as cow

                configured_pattern = _configured_seat_sparse_pattern(project_root)
                sparse = (
                    configured_pattern is not None
                    and _sparse_pattern_has_root(configured_pattern)
                )
                try:
                    cow_result = cow.prime_sparse_worktree(
                        project_root,
                        worktree_path,
                        base_commit=base_commit,
                        pattern=cow_pattern,
                        sparse=sparse,
                        overlay=True,
                        operation="reuse",
                    )
                except cow.CowFallback as exc:
                    cow_reason = exc.reason
        _git(
            worktree_path,
            "-c",
            "submodule.recurse=false",
            "checkout",
            "--no-overwrite-ignore",
            "-f",
            "-B",
            branch,
            base_commit,
        )
        import goalflight_seat_cow as cow

        cow.normalize_checkout_modes(
            worktree_path,
            preserve_root_mode=(cow_reason == "shared-repository-mode-unsupported"),
        )
        _report_cow_population(
            worktree_path,
            enabled=cow_on,
            pattern=cow_pattern,
            operation="reuse",
            cow_result=cow_result,
            reason=cow_reason,
        )


def _ensure_branch_available(worktree_path: Path, branch: str) -> None:
    branch_owner = _worktree_checking_out_branch(worktree_path, branch)
    if (
        branch_owner is not None
        and branch_owner.resolve(strict=False)
        != worktree_path.resolve(strict=False)
    ):
        raise _dispatch_branch_in_use(
            branch_owner, project_root=worktree_path, branch=branch
        )


def _assert_seat_on_named_branch(worktree_path: Path, *, seat_name: str, branch: str) -> str:
    actual = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    if actual == "HEAD":
        raise WorktreeSeatError(
            f"worktree {seat_name} is detached after prepare; "
            "refusing to hand a detached HEAD to a worker"
        )
    if actual != branch:
        raise WorktreeSeatError(
            f"worktree {seat_name} checked out {actual!r}, expected {branch!r}"
        )
    return actual


def _update_ref_and_verify(
    cwd: Path,
    ref: str,
    commit: str,
    *,
    old: str = "",
) -> None:
    """Create one keep/quarantine ref and verify it before any reset."""
    _git(cwd, "update-ref", "--create-reflog", ref, commit, old)
    verified = _git_proc(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if verified is None or verified.returncode != 0 or verified.stdout.strip() != commit:
        raise WorktreeSeatResetRefused(
            f"ref {ref} did not verify after creation; refusing reset"
        )


def _precheck_quarantine_worktree(
    worktree_path: Path,
    *,
    seat_name: str,
    timeout: float | None = None,
) -> tuple[list[str], list[tuple[str, tuple[str, ...]]], list[str], bool]:
    hidden_entries = _nul_paths(
        _git_nul(worktree_path, "ls-files", "-v", "-z", timeout=timeout)
    )
    sparse_config = _git_proc(
        worktree_path,
        "config",
        "--worktree",
        "--bool",
        "--get",
        "core.sparseCheckout",
        timeout=timeout,
    )
    sparse_config_error = sparse_config is None or sparse_config.returncode not in (0, 1)
    sparse_checkout_enabled = bool(
        not sparse_config_error
        and sparse_config is not None
        and sparse_config.returncode == 0
        and sparse_config.stdout.strip().lower() == "true"
    )
    for entry in hidden_entries:
        if len(entry) < 3 or entry[1] != " ":
            raise WorktreeSeatResetRefused(
                f"malformed hidden index entry in dirty worktree {seat_name}; "
                "refusing reset"
            )
        tag, path = entry[0], entry[2:]
        if tag.islower():
            raise WorktreeSeatResetRefused(
                f"dirty worktree {seat_name} has hidden index entry {path!r}; "
                "refusing reset"
            )
        if tag != "S":
            continue
        if not sparse_checkout_enabled:
            raise WorktreeSeatResetRefused(
                f"dirty worktree {seat_name} has hidden index entry {path!r}; "
                "refusing reset"
            )
        try:
            (worktree_path / path).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WorktreeSeatResetRefused(
                f"cannot inspect sparse path {path!r} in dirty worktree "
                f"{seat_name}; refusing reset"
            ) from exc
        raise WorktreeSeatResetRefused(
            f"dirty worktree {seat_name} has hidden index entry {path!r}; "
            "refusing reset"
        )

    records = _status_records(worktree_path, timeout=timeout)
    submodule_paths = set()
    for entry in _nul_paths(
        _git_nul(worktree_path, "ls-files", "--stage", "-z", timeout=timeout)
    ):
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise WorktreeSeatResetRefused(
                f"malformed index entry in dirty worktree {seat_name}; refusing reset"
            )
        if fields[0] == "160000":
            submodule_paths.add(path)
    for record in records:
        _status, paths = record
        if any(
            path == submodule or path.startswith(submodule + "/")
            for path in paths
            for submodule in submodule_paths
        ):
            raise WorktreeSeatResetRefused(
                f"dirty worktree {seat_name} has dirty submodule; refusing reset"
            )

    reserved_untracked = list(
        dict.fromkeys(
            path
            for status, paths in records
            if status == "??"
            for path in paths
            if is_reserved_seat_notes_path(path)
        )
    )
    product = [record for record in records if _status_is_product(record)]
    staged_and_worktree = [
        record
        for record in product
        if record[0][0] not in {" ", "?"}
        and record[0][1] not in {" ", "?"}
    ]
    if staged_and_worktree:
        raise WorktreeSeatResetRefused(
            f"dirty worktree {seat_name} has separate staged and working versions "
            "that cannot be represented by one quarantine commit; refusing reset"
        )

    dirty_paths = list(
        dict.fromkeys(path for record in product for path in _status_tree_paths(record))
    )
    tracked_paths = _nul_paths(
        _git_nul(worktree_path, "ls-files", "-z", timeout=timeout)
    )
    attribute_paths = tuple(dict.fromkeys((*tracked_paths, *dirty_paths)))
    if attribute_paths:
        # A clean lfs-filtered file is safe: status confirms that its worktree
        # bytes are the smudge of the pinned pointer, and the LFS object lives
        # in the common .git/lfs/objects shared by all worktrees. Other clean
        # filters may be lossy, so they retain the seat.
        attributes = _git_nul(
            worktree_path,
            "check-attr",
            "-z",
            "--stdin",
            "filter",
            "working-tree-encoding",
            "eol",
            "text",
            input_text="\0".join(attribute_paths) + "\0",
            timeout=timeout,
        )
        fields = attributes.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) % 3:
            raise WorktreeSeatResetRefused(
                f"malformed Git attributes in dirty worktree {seat_name}; refusing reset"
            )
        dirty_set = set(dirty_paths)
        lfs_paths: list[str] = []
        for index in range(0, len(fields), 3):
            path, attribute, value = fields[index : index + 3]
            active_filter = attribute == "filter" and value not in {
                "",
                "unspecified",
                "unset",
            }
            if active_filter and (value != "lfs" or path in dirty_set):
                raise WorktreeSeatResetRefused(
                    f"dirty worktree {seat_name} path {path!r} uses active "
                    f"filter {value!r}; refusing reset"
                )
            if active_filter and value == "lfs":
                lfs_paths.append(path)
            if path not in dirty_set:
                continue
            reencodes = False
            if attribute == "working-tree-encoding":
                reencodes = value not in {"", "unspecified", "unset"}
            elif (
                attribute == "eol" and value in {"lf", "crlf"}
            ) or (attribute == "text" and value in {"set", "auto"}):
                try:
                    data = (worktree_path / path).read_bytes()
                except OSError:
                    reencodes = True
                else:
                    if attribute == "eol":
                        reencodes = (
                            b"\r\n" in data
                            if value == "lf"
                            else b"\n" in data.replace(b"\r\n", b"")
                        )
                    else:
                        reencodes = b"\r\n" in data and (
                            value == "set" or b"\x00" not in data
                        )
            if not reencodes:
                continue
            raise WorktreeSeatResetRefused(
                f"dirty worktree {seat_name} path {path!r} uses active "
                f"{attribute} {value!r}; refusing reset"
            )
        if lfs_paths:
            _prove_lfs_objects(worktree_path, lfs_paths, timeout=timeout)
    return reserved_untracked, product, dirty_paths, sparse_checkout_enabled


def _quarantine_dirty_worktree(
    worktree_path: Path,
    *,
    seat_name: str,
    abandoned_dispatch_id: str,
    precheck: tuple[
        list[str], list[tuple[str, tuple[str, ...]]], list[str], bool
    ]
    | None = None,
) -> str | None:
    if precheck is None:
        precheck = _precheck_quarantine_worktree(
            worktree_path,
            seat_name=seat_name,
        )
    reserved_untracked, product, dirty_paths, sparse_checkout_enabled = precheck
    if not product:
        return None

    # Seed the temporary index from the real index so staged content, including
    # force-added ignored files, is preserved. Sparse seats need --sparse so
    # edits and untracked files under excluded paths are included.
    with tempfile.TemporaryDirectory(prefix="goalflight-quarantine-") as temporary:
        temporary_index = Path(temporary) / "index"
        real_index = Path(_git(worktree_path, "rev-parse", "--git-path", "index"))
        if not real_index.is_absolute():
            real_index = (worktree_path / real_index).resolve()
        try:
            shutil.copyfile(real_index, temporary_index)
        except OSError as exc:
            raise WorktreeSeatResetRefused(
                f"cannot copy real index for quarantine: {exc}; refusing reset"
            ) from exc
        index_env = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}
        add_args = ["add"]
        if sparse_checkout_enabled:
            add_args.append("--sparse")
        add_args.extend(["-A", "--", "."])
        _git(worktree_path, *add_args, env=index_env)
        staged_status = _parse_porcelain_z(
            _git_nul(
                worktree_path,
                "status",
                "--porcelain=v1",
                "-z",
                "--no-renames",
                "--untracked-files=all",
                "--ignore-submodules=none",
                env=index_env,
            )
        )
        worktree_changes = [
            record
            for record in staged_status
            if _status_is_product(record)
            and (record[0] == "??" or record[0][1] not in {" ", "?"})
        ]
        if worktree_changes:
            changed_paths = list(
                dict.fromkeys(
                    path for _status, paths in worktree_changes for path in paths
                )
            )
            raise WorktreeSeatResetRefused(
                f"temporary quarantine index does not match worktree path(s) "
                f"{changed_paths!r}; refusing reset"
            )
        verification_paths = tuple(
            dict.fromkeys(path for _status, paths in product for path in paths)
        )
        for path in verification_paths:
            verified = _git_proc(
                worktree_path,
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--",
                path,
                env=index_env,
            )
            if verified is None or verified.returncode not in (0, 1):
                detail = "git diff could not verify worktree content"
                if verified is not None:
                    detail = (verified.stderr or verified.stdout or "").strip() or detail
                raise WorktreeSeatResetRefused(
                    f"cannot verify quarantined path {path!r} ({detail}); "
                    "refusing reset"
                )
            if verified.returncode != 0:
                raise WorktreeSeatResetRefused(
                    f"temporary quarantine index does not match worktree path "
                    f"{path!r}; refusing reset"
                )
        if reserved_untracked:
            _git(
                worktree_path,
                "reset",
                "-q",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                input_text="\0".join(reserved_untracked) + "\0",
                env=index_env,
            )
        tree = _git(worktree_path, "write-tree", env=index_env)
    parent = _git(worktree_path, "rev-parse", "HEAD")
    parent_tree = _git(worktree_path, "rev-parse", "HEAD^{tree}")
    if tree == parent_tree:
        raise WorktreeSeatError(
            f"dirty worktree {seat_name} cannot be represented by a branch commit; refusing reset"
        )

    stamp = str(time.time_ns())
    safe_dispatch_id = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", str(abandoned_dispatch_id))
        .strip(".-")
        or "unknown-dispatch"
    )
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
            f"quarantine commit for dirty worktree {seat_name} is empty; refusing reset"
        )
    branch = (
        f"{QUARANTINE_REF_PREFIX}/{seat_name}-{safe_dispatch_id}-"
        f"{stamp}-{commit[:12]}"
    )
    _update_ref_and_verify(worktree_path, f"refs/heads/{branch}", commit, old="")
    dirty_ref = (
        f"refs/{KEEP_REF_PREFIX}/{safe_dispatch_id}/dirty-"
        f"{stamp}-{commit[:12]}"
    )
    _update_ref_and_verify(worktree_path, dirty_ref, commit, old="")
    tree_paths = _tree_paths(worktree_path, dirty_ref)
    missing_paths = sorted(set(dirty_paths) - tree_paths)
    if missing_paths:
        raise WorktreeSeatResetRefused(
            f"dirty ref {dirty_ref} is missing status paths {missing_paths!r}; "
            "refusing reset"
        )
    return branch


def _restore_quarantined_worktree(
    worktree_path: Path,
    *,
    quarantine_ref: str,
    expected_head: str,
) -> None:
    """Restore a preserved tree without moving HEAD or changing its refs."""
    commit = _git(
        worktree_path, "rev-parse", "--verify", f"{quarantine_ref}^{{commit}}"
    )
    parent = _git(worktree_path, "rev-parse", "--verify", f"{commit}^")
    if parent != expected_head:
        raise WorktreeSeatResetRefused(
            f"quarantine ref {quarantine_ref} does not descend from saved head "
            f"{expected_head}; refusing resume restore"
        )
    head_before = _git(worktree_path, "rev-parse", "HEAD")
    if head_before != expected_head:
        raise WorktreeSeatResetRefused(
            f"worktree {worktree_path.name} is at {head_before}, expected saved head "
            f"{expected_head}; refusing resume restore"
        )
    target_tree = _git(worktree_path, "rev-parse", "--verify", f"{commit}^{{tree}}")

    # No-overlay checkout removes tracked paths absent from the snapshot.
    # Reset only the index afterward so edits remain unstaged and additions
    # remain untracked, matching the pre-quarantine working tree.
    _git(worktree_path, "checkout", "--no-overlay", commit, "--", ".")
    _git(worktree_path, "reset", "-q", "HEAD", "--", ".")
    if _git(worktree_path, "rev-parse", "HEAD") != head_before:
        raise WorktreeSeatResetRefused(
            f"restoring {quarantine_ref} moved HEAD in {worktree_path.name}; refusing resume"
        )

    with tempfile.TemporaryDirectory(prefix="goalflight-restore-") as temporary:
        temporary_index = Path(temporary) / "index"
        index_env = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}
        _git(worktree_path, "read-tree", "--empty", env=index_env)
        _git(
            worktree_path,
            "add",
            "-A",
            "-f",
            "--",
            ".",
            ":(exclude).goal-flight",
            ":(exclude).goal-flight/**",
            env=index_env,
        )
        restored_tree = _git(worktree_path, "write-tree", env=index_env)
        if restored_tree != target_tree:
            raise WorktreeSeatResetRefused(
                f"restored worktree {worktree_path.name} does not match quarantine "
                f"tree {target_tree} from {quarantine_ref}; got {restored_tree}"
            )


class _RepoCleanReport:
    """Repository-level stale-registration snapshot taken under the allocation lock."""

    def __init__(self, items: list[str], *, repo: Path) -> None:
        self.items = tuple(items)
        self.repo = repo

    @property
    def clean(self) -> bool:
        return not self.items

    @property
    def reason(self) -> str:
        return _not_clean_reason(self.repo, self.items)


def _normalized_path(path: Path | str) -> str:
    text = os.fspath(path)
    try:
        return os.path.normpath(os.path.realpath(text))
    except OSError:
        return os.path.normpath(os.path.abspath(text))


def _checkout_path_from_gitdir(gitdir_file: Path, recorded: str) -> Path | None:
    text = recorded.strip()
    if not text or "\n" in text or "\x00" in recorded:
        return None
    recorded_path = Path(text)
    if not recorded_path.is_absolute():
        recorded_path = gitdir_file.parent / recorded_path
    return recorded_path.parent


def _admin_dir_checkout(admin: Path) -> tuple[str, Path | None]:
    """Return ``(state, checkout)`` for one git admin directory.

    ``checkout_missing`` is used only when a readable gitdir names a checkout
    path and that path is absent. A missing, empty, unreadable, or unparsable
    gitdir leaves the checkout location unknown.
    """
    gitdir = Path(admin) / "gitdir"
    try:
        recorded = gitdir.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "gitdir_missing", None
    except (OSError, UnicodeError):
        return "gitdir_unreadable", None
    if "\x00" in recorded or "\n" in recorded.strip():
        return "gitdir_unparsable", None
    if not recorded.strip():
        return "gitdir_empty", None
    checkout = _checkout_path_from_gitdir(gitdir, recorded)
    if checkout is None:
        return "gitdir_unparsable", None
    presence = _path_presence(checkout)
    if presence == "present":
        return "present", checkout
    if presence == "absent":
        return "checkout_missing", checkout
    return "occupancy_unknown", checkout


def _present_seat_realpaths(project_root: Path) -> set[str] | None:
    """Realpaths of present managed seats. None when the seat root cannot be listed."""
    root = repository_worktree_root(project_root)
    presence = _path_presence(root)
    if presence == "absent":
        return set()
    if presence != "present":
        return None
    found: set[str] = set()

    def consider(path: Path) -> None:
        if path.name.startswith(".") or not is_captive_seat_name(path.name):
            return
        try:
            info = os.lstat(path)
        except OSError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return
        if _path_presence(path) != "present":
            return
        verdict, _reason = managed_worktree_path_verdict(path, project_root=project_root)
        if verdict == YES:
            found.add(_normalized_path(path))

    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            info = os.lstat(child)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        if is_captive_seat_name(child.name):
            consider(child)
            continue
        try:
            nested = list(child.iterdir())
        except OSError:
            continue
        for seat in nested:
            consider(seat)
    return found


def _known_worktree_realpaths(project_root: Path) -> set[str] | None:
    """Existing checkouts git lists. None when the list itself fails."""
    try:
        output = _git(project_root, "worktree", "list", "--porcelain")
    except (WorktreeSeatError, OSError):
        return None
    found: set[str] = set()
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :])
        if _path_presence(path) == "present":
            found.add(_normalized_path(path))
    return found


def _repo_clean_report(project_root: Path) -> _RepoCleanReport:
    """Classify every admin dir and occupancy registration.

    Clean means every admin dir has a readable gitdir whose checkout exists
    and is a present seat or another known worktree, and every occupancy
    registration names a lock inside such an admin dir with the same inode.
    Seat locks and the allocation lock are not occupancy registrations.
    A failure to inspect becomes one stale item; it does not raise.
    """
    items: list[str] = []
    try:
        common = _git_common_dir(project_root)
    except (WorktreeSeatError, OSError) as exc:
        return _RepoCleanReport(
            [f"occupancy_unknown {project_root}: {exc}"], repo=project_root
        )
    clean_admins: set[str] = set()
    worktrees = Path(common) / "worktrees"
    presence = _path_presence(worktrees)
    children: list[Path] = []
    if presence == "unknown":
        items.append(f"occupancy_unknown {worktrees}")
    elif presence == "present":
        try:
            children = list(worktrees.iterdir())
        except OSError as exc:
            items.append(f"occupancy_unknown {worktrees}: {exc}")
            children = []
    pending: list[tuple[Path, Path]] = []
    for child in children:
        try:
            info = os.lstat(child)
        except OSError as exc:
            items.append(f"occupancy_unknown {child}: {exc}")
            continue
        if child.name in {".", ".."}:
            continue
        if stat.S_ISLNK(info.st_mode):
            # A symlink is not a clean admin dir and is never droppable.
            # Following it would treat an unknown target as a checkout.
            items.append(f"admin_dir symlink {child}")
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        state, checkout = _admin_dir_checkout(child)
        if state == "gitdir_missing":
            items.append(f"admin_dir gitdir_missing {child}")
        elif state == "gitdir_empty":
            items.append(f"admin_dir gitdir_empty {child}")
        elif state == "gitdir_unparsable":
            items.append(f"admin_dir gitdir_unparsable {child}")
        elif state == "gitdir_unreadable":
            items.append(f"admin_dir gitdir_unreadable {child}")
        elif state == "checkout_missing":
            items.append(f"admin_dir checkout_missing {child}")
        elif state != "present" or checkout is None:
            items.append(f"occupancy_unknown {checkout or child}")
        else:
            pending.append((child, checkout))
    if pending:
        seats = _present_seat_realpaths(project_root)
        if seats is None:
            seats = set()
        unresolved = []
        for child, checkout in pending:
            if _normalized_path(checkout) in seats:
                clean_admins.add(_normalized_path(child))
            else:
                unresolved.append((child, checkout))
        if unresolved:
            known = _known_worktree_realpaths(project_root)
            for child, checkout in unresolved:
                if known is not None and _normalized_path(checkout) in known:
                    clean_admins.add(_normalized_path(child))
                elif known is None:
                    items.append(f"occupancy_unknown {checkout}")
                else:
                    items.append(f"admin_dir checkout_unrecognized {child}")
    try:
        payload = _read_lock_registry(common)
    except OSError as exc:
        items.append(f"occupancy_unknown {_lock_registry_path(common)}: {exc}")
        payload = None
    locks = payload.get("locks") if isinstance(payload, dict) else None
    if isinstance(locks, dict):
        for key, record in locks.items():
            if not isinstance(key, str) or Path(key).name != OCCUPANCY_LOCK_NAME:
                continue
            lock_path = Path(key)
            try:
                current = os.lstat(lock_path)
            except FileNotFoundError:
                items.append(f"registry file_missing {key}")
                continue
            except OSError as exc:
                items.append(f"occupancy_unknown {key}: {exc}")
                continue
            if not stat.S_ISREG(current.st_mode):
                items.append(f"registry identity_differs {key}")
                continue
            try:
                registered = _registered_stat_from_record(lock_path, record)
            except OSError as exc:
                items.append(f"occupancy_unknown {key}: {exc}")
                continue
            if registered.st_dev != current.st_dev or registered.st_ino != current.st_ino:
                items.append(f"registry identity_differs {key}")
                continue
            if _normalized_path(lock_path.parent) not in clean_admins:
                items.append(f"registry admin_not_clean {key}")
    items = sorted(set(items))
    return _RepoCleanReport(items, repo=project_root)


def _stale_item_path(item: str) -> str:
    parts = item.split(" ", 2)
    if len(parts) == 3 and parts[0] in {"admin_dir", "registry"}:
        return parts[2]
    if item.startswith("occupancy_unknown "):
        rest = item[len("occupancy_unknown ") :]
        return rest.split(": ", 1)[0]
    return item


def _droppable_stale_target(item: str) -> str | None:
    """Path ``--drop-stale`` can remove, or None when the operator must inspect.

    An admin dir is droppable only when its gitdir is readable, names a
    checkout path, and that path is absent. A registry entry is droppable
    only when its lock file is absent. Unknown checkout location is not stale.
    """
    if item.startswith(("admin_dir checkout_missing ", "registry file_missing ")):
        return _stale_item_path(item)
    return None


def drop_stale_command(repo: Path, item: str) -> str:
    """Operator command that drops one named stale item."""
    return (
        "python3 "
        + shlex.quote("scripts/goalflight_worktree_gc.py")
        + " "
        + shlex.quote(str(repo))
        + " --drop-stale "
        + shlex.quote(item)
    )


def _not_clean_reason(repo: Path, items: tuple[str, ...] | list[str]) -> str:
    if not items:
        return ""
    drop_targets: list[str] = []
    inspect_items: list[str] = []
    for item in items:
        target = _droppable_stale_target(item)
        if target:
            if target not in drop_targets:
                drop_targets.append(target)
        else:
            inspect_items.append(item)
    listed = "; ".join(items)
    parts = [f"repo_not_clean: {listed}"]
    if drop_targets:
        commands = "; ".join(
            drop_stale_command(repo, target) for target in drop_targets
        )
        parts.append(
            "verify no process from that seat is running "
            f"(for example after a reboot), then {commands}"
        )
    if inspect_items:
        named = "; ".join(inspect_items)
        parts.append(
            f"operator must inspect {named}; checkout location or identity "
            "is not proven, so no removal command is available"
        )
    return "; ".join(parts)


def _not_clean_skip_seat(items: tuple[str, ...] | list[str]) -> str:
    """Seat name to attach a repository-level not-clean skip to."""
    for item in items:
        path = Path(_stale_item_path(item))
        if path.name == OCCUPANCY_LOCK_NAME:
            path = path.parent
        if is_captive_seat_name(path.name):
            return path.name
    return "repo"


def _missing_checkout_admin_text(project_root: Path, worktree_path: Path) -> str:
    """Admin directories whose readable gitdir names this checkout."""
    try:
        common = _git_common_dir(project_root)
    except WorktreeSeatError:
        return "unresolved"
    worktrees = Path(common) / "worktrees"
    if _path_presence(worktrees) != "present":
        return "unresolved"
    try:
        children = list(worktrees.iterdir())
    except OSError:
        return "unresolved"
    wanted = _normalized_path(worktree_path)
    found: list[str] = []
    for child in children:
        try:
            info = os.lstat(child)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        _state, checkout = _admin_dir_checkout(child)
        if checkout is not None and _normalized_path(checkout) == wanted:
            found.append(str(child))
    if not found:
        return "unresolved"
    return ", ".join(sorted(found))


def _admin_dir_is_direct_child(common: Path, path: Path) -> bool:
    worktrees = Path(common) / "worktrees"
    if path.name in {".", ".."} or not path.name or path.name == OCCUPANCY_LOCK_NAME:
        return False
    try:
        parent = os.lstat(path.parent)
        root = os.lstat(worktrees)
    except OSError:
        return False
    return parent.st_dev == root.st_dev and parent.st_ino == root.st_ino


def _occupancy_keys_for_admin(payload: dict, admin: Path) -> list[str]:
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        return []
    admin_real = _normalized_path(admin)
    found: list[str] = []
    for key in locks:
        if not isinstance(key, str) or Path(key).name != OCCUPANCY_LOCK_NAME:
            continue
        parent = os.path.normpath(os.path.dirname(os.path.abspath(key)))
        try:
            parent_real = os.path.normpath(os.path.realpath(os.path.dirname(key)))
        except OSError:
            parent_real = parent
        if parent_real == admin_real or parent == admin_real:
            found.append(key)
    return found


def drop_one_stale_worktree_item(repo: Path, item: str) -> str:
    """Drop one proven-stale admin directory or one missing occupancy registration.

    Holds the mutation lock, then the registry mutex, across the recheck and
    edit. An admin directory is removed only when its gitdir is readable,
    names a checkout path, and that path is absent. A missing, empty,
    unreadable, or unparsable gitdir is unknown and is refused. A symlinked
    admin entry is refused. A registry entry is removed only when its lock
    file is absent.
    """
    repo = Path(repo)
    try:
        common = _git_common_dir(repo)
    except WorktreeSeatError as exc:
        raise OSError(errno.EINVAL, str(exc)) from exc
    raw = Path(str(item).strip())
    if not raw.is_absolute():
        raw = repo / raw
    registry_mode = raw.name == OCCUPANCY_LOCK_NAME
    admin_mode = _admin_dir_is_direct_child(common, raw)
    if registry_mode:
        admin_mode = False
    if not registry_mode and not admin_mode:
        raise OSError(
            errno.EINVAL,
            f"stale item is neither one admin dir nor an occupancy lock: {raw}",
        )
    registry_path = _lock_registry_path(common)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    mutex_path = registry_path.with_name(_LOCK_REGISTRY_MUTEX_NAME)

    def write_payload(payload: dict) -> None:
        temporary = registry_path.with_name(
            f"{registry_path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(registry_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    with _hold_worktree_mutation_lock(repo):
        with mutex_path.open("a+", encoding="utf-8") as mutex:
            _flock_exclusive_until(
                mutex.fileno(), deadline=None, description="lock registry mutex"
            )
            if admin_mode:
                try:
                    info = os.lstat(raw)
                except FileNotFoundError as exc:
                    raise OSError(errno.ENOENT, f"admin dir is absent: {raw}") from exc
                except OSError as exc:
                    raise OSError(
                        exc.errno or errno.EPERM,
                        f"admin dir could not be inspected: {raw}",
                    ) from exc
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise OSError(
                        errno.EINVAL, f"admin dir is not a real directory: {raw}"
                    )
                payload = _read_lock_registry(common)
                keys = _occupancy_keys_for_admin(payload, raw)
                state, checkout = _admin_dir_checkout(raw)
                proven_absent = (
                    state == "checkout_missing"
                    and checkout is not None
                    and _path_presence(checkout) == "absent"
                )
                if state == "present" or (
                    checkout is not None and _path_presence(checkout) == "present"
                ):
                    raise OSError(
                        errno.EBUSY, f"admin dir checkout still exists: {raw}"
                    )
                if not proven_absent:
                    raise OSError(
                        errno.EPERM,
                        f"admin dir checkout could not be proven missing: {raw}",
                    )
                shutil.rmtree(raw)
                if keys:
                    locks = payload.get("locks")
                    if isinstance(locks, dict):
                        for key in keys:
                            locks.pop(key, None)
                        write_payload(payload)
                return f"dropped admin dir {raw}"
            try:
                os.lstat(raw)
            except FileNotFoundError:
                current = None
            except OSError as exc:
                raise OSError(
                    exc.errno or errno.EPERM,
                    f"occupancy lock could not be inspected: {raw}",
                ) from exc
            else:
                current = True
            if current is not None:
                raise OSError(errno.EBUSY, f"occupancy lock file still exists: {raw}")
            payload = _read_lock_registry(common)
            locks = payload.get("locks")
            if not isinstance(locks, dict):
                locks = {}
                payload["locks"] = locks
            keys = _occupancy_registry_keys(payload, raw)
            exact = _lock_registry_key(raw)
            if exact in locks and exact not in keys:
                keys.append(exact)
            if not keys:
                raise OSError(errno.ENOENT, f"occupancy lock is not registered: {raw}")
            for key in keys:
                locks.pop(key, None)
            write_payload(payload)
            return f"dropped registry entry {exact}"


def _registry_has_occupancy_lock(payload: dict, admin_dir: Path) -> bool:
    """True when a registry key names the occupancy lock inside ``admin_dir``."""
    lock_path = Path(admin_dir) / OCCUPANCY_LOCK_NAME
    if _occupancy_registry_keys(payload, lock_path):
        return True
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        return False
    suffix = f"{os.sep}worktrees{os.sep}{Path(admin_dir).name}{os.sep}{OCCUPANCY_LOCK_NAME}"
    return any(isinstance(key, str) and key.endswith(suffix) for key in locks)


def _present_occupancy_skip_reason(worktree_path: Path) -> str:
    """Classify a present seat whose occupancy lock could not be opened.

    ``stale_registration`` is a registered path whose file is missing or whose
    inode is not the registered one. The allocator does not replace it.
    ``""`` means no registration was found; the caller treats that as unknown.
    """
    try:
        lock_path = worktree_path_lock_path(worktree_path)
        common = _git_common_dir(worktree_path)
        payload = _read_lock_registry(common)
    except (OSError, WorktreeSeatError, WorktreePathLockUnknown):
        return "occupancy_unknown"
    if not _registry_has_occupancy_lock(payload, lock_path.parent):
        return ""
    try:
        current = os.lstat(lock_path)
    except FileNotFoundError:
        return "stale_registration"
    except OSError:
        return "occupancy_unknown"
    if not stat.S_ISREG(current.st_mode):
        return "stale_registration"
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        return "occupancy_unknown"
    keys = set(_occupancy_registry_keys(payload, lock_path))
    suffix = (
        f"{os.sep}worktrees{os.sep}{lock_path.parent.name}{os.sep}{OCCUPANCY_LOCK_NAME}"
    )
    compared = False
    for key, record in locks.items():
        if key not in keys and not (isinstance(key, str) and key.endswith(suffix)):
            continue
        compared = True
        try:
            registered = _registered_stat_from_record(Path(key), record)
        except OSError:
            return "occupancy_unknown"
        if registered.st_dev != current.st_dev or registered.st_ino != current.st_ino:
            return "stale_registration"
    if not compared:
        return "occupancy_unknown"
    return "occupancy_unknown"


def _release_registered_occupancy_for_removal(
    lock_path: Path,
    *,
    registry_root: Path,
    deadline: float | None = None,
) -> int | None:
    """Drop the occupancy registration before the worktree is removed.

    Returns a fd holding the still-present lock file. The caller closes it
    after removal. A missing lock file whose registration remains is not
    dropped: that slot stays stale for the allocator. A missing lock file
    with no registration is a no-op. A held flock or an identity mismatch
    fails the removal.
    """
    if lock_path.name != OCCUPANCY_LOCK_NAME:
        raise OSError(
            errno.EPERM,
            f"refusing to drop a non-occupancy lock registration: {lock_path}",
        )
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(lock_path, flags)
    except FileNotFoundError:
        fd = None
    except OSError as exc:
        raise OSError(
            exc.errno, f"occupancy lock could not be inspected: {lock_path}"
        ) from exc
    try:
        payload = _read_lock_registry(registry_root)
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OSError(
                    errno.EAGAIN, f"occupancy lock is held: {lock_path}"
                ) from exc
            current = os.fstat(fd)
            for key in _occupancy_registry_keys(payload, lock_path):
                registered = _registered_stat_from_record(
                    lock_path, payload["locks"][key]
                )
                if (
                    registered.st_dev != current.st_dev
                    or registered.st_ino != current.st_ino
                ):
                    raise OSError(
                        errno.EAGAIN,
                        f"occupancy lock inode does not match its registration: {lock_path}",
                    )
        elif _registry_has_occupancy_lock(payload, lock_path.parent):
            raise OSError(
                errno.EBUSY,
                f"occupancy lock file is missing and its registration is stale: {lock_path}",
            )
        _drop_occupancy_registration(
            lock_path,
            registry_root=registry_root,
            deadline=deadline,
            require_absent=fd is None,
        )
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    return fd


def _install_created_occupancy_lock(worktree_path: Path, dispatch_id: str) -> None:
    """Create the occupancy lock in the admin dir git actually created.

    The admin dir is read back from the new worktree's ``.git`` file. A
    registration already naming that dir is left untouched.
    """
    try:
        lock_path = worktree_path_lock_path(worktree_path)
        common = _git_common_dir(worktree_path)
        payload = _read_lock_registry(common)
    except (OSError, WorktreeSeatError, WorktreePathLockUnknown) as exc:
        raise WorktreeSeatUnavailable(
            f"worktree {worktree_path.name} occupancy lock could not be placed: {exc}",
            seat_name=worktree_path.name,
            reason="occupancy_unknown",
        ) from exc
    if _registry_has_occupancy_lock(payload, lock_path.parent):
        raise WorktreeSeatUnavailable(
            f"worktree {worktree_path.name} occupancy registration is stale; refusing recreate",
            seat_name=worktree_path.name,
            reason="stale_registration",
        )
    try:
        lock = try_acquire_worktree_path_lock(worktree_path, dispatch_id)
    except WorktreePathLockBusy as exc:
        raise WorktreeSeatUnavailable(
            str(exc),
            seat_name=worktree_path.name,
            current_holder=getattr(exc, "occupant_id", None),
            flock_held=True,
        ) from exc
    except WorktreePathLockUnknown as exc:
        reason = _present_occupancy_skip_reason(worktree_path) or "occupancy_unknown"
        raise WorktreeSeatUnavailable(
            str(exc),
            seat_name=worktree_path.name,
            reason=reason,
        ) from exc
    lock.release()


def _prepare_claimed_seat(**kwargs) -> WorktreeSeatLease:
    """Exclude writers using the path lock before any checkout or reset."""
    expected = kwargs.pop("expected_prior_dispatch_id", None)
    preclaimed_occupancy_lock = kwargs.pop("_preclaimed_occupancy_lock", None)
    kwargs.pop("clean_report", None)
    release_allocation_lock = kwargs.pop("release_allocation_lock", None)
    kwargs.setdefault(
        "process_cwd_census",
        lambda: _process_cwd_snapshot(timeout_s=PROCESS_CWD_SNAPSHOT_TIMEOUT_S),
    )
    path = kwargs["worktree_path"]
    presence = _path_presence(path)
    if presence == "unknown":
        raise WorktreeSeatUnavailable(
            f"worktree {path} could not be inspected; refusing reset or recreate",
            seat_name=path.name,
            reason="identity_unknown",
        )
    dispatch_id = str(kwargs["dispatch_id"])
    preclaimed_matches_path = (
        isinstance(preclaimed_occupancy_lock, WorktreePathLock)
        and preclaimed_occupancy_lock.dispatch_id == dispatch_id
        and preclaimed_occupancy_lock.path == path
    )
    if preclaimed_matches_path and presence != "present":
        raise WorktreeSeatUnavailable(
            f"preclaimed worktree occupancy lock for {path} cannot be used because "
            "the worktree is not present",
            seat_name=path.name,
            reason="identity_unknown",
        )
    if presence == "present":
        try:
            if not preclaimed_matches_path:
                occupancy = try_acquire_worktree_path_lock(path, kwargs["dispatch_id"])
            else:
                try:
                    preclaimed_occupancy_lock.fileno()
                except WorktreeSeatError as exc:
                    raise WorktreePathLockUnknown(
                        f"preclaimed worktree occupancy lock for {path} was released"
                    ) from exc
                occupancy = preclaimed_occupancy_lock
        except WorktreePathLockBusy as exc:
            occupant = getattr(exc, "occupant_id", None)
            if expected is not None and occupant is not None and occupant != expected:
                raise WorktreeSeatReclaimed(
                    f"resume refused: worktree {path.name} was reclaimed by "
                    f"{occupant}; expected recorded holder {expected}; "
                    "refusing to reset or recreate it",
                    seat_name=path.name,
                    current_holder=occupant,
                    expected_holder=expected,
                    flock_held=True,
                ) from exc
            raise WorktreeSeatUnavailable(
                str(exc),
                seat_name=path.name,
                current_holder=occupant,
                flock_held=True,
            ) from exc
        except WorktreePathLockUnknown as exc:
            reason = _present_occupancy_skip_reason(path) or "occupancy_unknown"
            raise WorktreeSeatUnavailable(
                str(exc),
                seat_name=path.name,
                reason=reason,
            ) from exc
        occupancy_context = (
            contextlib.nullcontext(occupancy)
            if preclaimed_matches_path
            else occupancy
        )
        with occupancy_context:
            return _prepare_claimed_seat_locked(
                **kwargs,
                expected_prior_dispatch_id=expected,
                release_allocation_lock=release_allocation_lock,
            )
    return _prepare_claimed_seat_locked(
        **kwargs,
        expected_prior_dispatch_id=expected,
        release_allocation_lock=release_allocation_lock,
    )


def _prepare_claimed_seat_locked(
    *,
    project_root: Path,
    worktree_path: Path,
    seat_name: str,
    lock_file: TextIO,
    dispatch_id: str,
    prior_dispatch_id: str | None,
    branch: str,
    base_commit: str,
    reset: bool,
    expected_prior_dispatch_id: str | None = None,
    controller_label: str | None = None,
    before_reset: Callable[[Path, str | None], bool | None] | None = None,
    before_occupant: Callable[[Path], None] | None = None,
    after_occupant: Callable[[Path], None] | None = None,
    release_allocation_lock: Callable[[], None] | None = None,
    release_mutation_lock: Callable[[], None] | None = None,
    journal_state_cache: dict[str, str] | None = None,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
    cow_plan: tuple[bool, str | None, str | None] | None = None,
) -> WorktreeSeatLease:
    lock_file.seek(0)
    original_lock_contents = lock_file.read()
    original_lock_holder = _known_lock_dispatch_id(lock_file)
    original_lock_metadata = _lock_metadata(lock_file)
    lock_file.seek(0, os.SEEK_END)
    if release_allocation_lock is None:

        def release_allocation_lock() -> None:
            return None
    presence = _path_presence(worktree_path)
    if presence == "unknown":
        raise WorktreeSeatResetRefused(
            f"cannot inspect worktree {worktree_path}; refusing reset"
        )
    existing = presence == "present"
    sparse_applied = False
    safety: dict | None = None
    original_branch: str | None = None
    original_head: str | None = None
    clean_ignored = False
    if existing:
        # Present seats keep the short admission hold. Reset and checkout
        # run after this release and do not take the mutation lock.
        release_allocation_lock()
        _verify_existing_seat(project_root, worktree_path)
        if reset:
            try:
                original_branch = _git(
                    worktree_path, "rev-parse", "--abbrev-ref", "HEAD"
                ).strip()
                original_head = _git(worktree_path, "rev-parse", "HEAD").strip()
            except (WorktreeSeatError, OSError) as exc:
                raise WorktreeSeatError(
                    f"cannot inspect branch and HEAD for worktree {seat_name} "
                    f"before reset: {exc}"
                ) from exc
        if reset:
            _ensure_branch_available(worktree_path, branch)
            prior_dispatch_id = _validate_holder(
                worktree_path,
                prior_dispatch_id,
                project_root=project_root,
                lock_metadata=original_lock_metadata,
                journal_state_cache=journal_state_cache,
                process_cwd_census=process_cwd_census,
            )
        if before_reset is not None:
            clean_ignored = before_reset(worktree_path, prior_dispatch_id) is True
        if reset:
            safety = evaluate_seat_reset_safety(
                worktree_path,
                base_commit=base_commit,
                new_branch=branch,
            )
            if safety["decision"] != "reset":
                raise WorktreeSeatResetRefused(safety["reason"])
    elif not reset:
        raise WorktreeCwdRefused(
            f"refusing to create missing --cwd {worktree_path}; "
            "resume and occupy only attach an existing tree"
        )
    # Refusals before checkout leave the previous holder visible; later
    # preparation failures record this dispatch before releasing the seat lock.
    # The caller holds the mutation lock across this re-check and the add.
    # after_add registers occupancy and drops that lock before checkout.
    # allocation.lock is already released: a zero wait budget must still fail
    # fast when that admission lock is busy, so it cannot stay held for the
    # git subprocess. GC and --drop-stale take the mutation lock before
    # they edit admin dirs or registrations.
    else:
        if original_lock_holder is not None:
            prior_dispatch_id = _validate_holder(
                worktree_path,
                prior_dispatch_id or original_lock_holder,
                project_root=project_root,
                lock_metadata=original_lock_metadata,
                journal_state_cache=journal_state_cache,
                process_cwd_census=process_cwd_census,
            )
        fresh = _repo_clean_report(project_root)
        if not fresh.clean:
            reason = fresh.reason or "occupancy_unknown"
            raise WorktreeSeatUnavailable(
                f"worktree {seat_name} skipped ({reason}); refusing recreate",
                seat_name=seat_name,
                reason=reason,
            )

        def after_add() -> None:
            # Same step for a new slot and for a slot GC removed inside the
            # high-water mark: the occupancy lock exists before checkout.
            # Release even when registration fails so checkout does not hold
            # the mutation lock.
            try:
                _install_created_occupancy_lock(worktree_path, dispatch_id)
            finally:
                if release_mutation_lock is not None:
                    release_mutation_lock()

        try:
            sparse_applied = _create_seat_worktree(
                project_root,
                worktree_path,
                branch=branch,
                base_commit=base_commit,
                keep_id=dispatch_id,
                after_add=after_add,
                cow_plan=cow_plan,
            )
            _verify_existing_seat(project_root, worktree_path)
        finally:
            release_allocation_lock()
        # Sparse seats have no prior bytes to protect; their excluded index
        # entries are skip-worktree. Full seats use regular checkout preparation.
        if sparse_applied:
            reset = False
    if not reset:
        actual = _git(
            worktree_path, "rev-parse", "--abbrev-ref", "HEAD"
        ).strip()
        if expected_prior_dispatch_id is not None:
            expected_record, _expected_live = _holder_record(
                expected_prior_dispatch_id,
                project_root=project_root,
                lock_metadata=original_lock_metadata,
                journal_state_cache=journal_state_cache,
            )
            expected_branch = str(
                expected_record.get("worktree_branch") or ""
            ).strip()
            if not is_worktree_branch(expected_branch):
                raise WorktreeSeatUnavailable(
                    f"resume refused: worktree {seat_name} has no valid recorded "
                    f"branch for {expected_prior_dispatch_id}"
                )
            if actual != expected_branch:
                raise WorktreeSeatUnavailable(
                    f"resume refused: worktree {seat_name} is on branch {actual}; "
                    f"expected recorded branch {expected_branch} for "
                    f"{expected_prior_dispatch_id}"
                )
            if original_lock_holder is None or prior_dispatch_id is None:
                raise WorktreeSeatUnavailable(
                    f"resume refused: worktree {seat_name} has unknown lock holder",
                    seat_name=seat_name,
                    reason="identity_unknown",
                )
            _validate_holder(
                worktree_path,
                original_lock_holder,
                project_root=project_root,
                lock_metadata=original_lock_metadata,
                journal_state_cache=journal_state_cache,
                process_cwd_census=process_cwd_census,
            )
            if original_lock_holder != prior_dispatch_id:
                _validate_holder(
                    worktree_path,
                    prior_dispatch_id,
                    project_root=project_root,
                    lock_metadata=original_lock_metadata,
                    journal_state_cache=journal_state_cache,
                    process_cwd_census=process_cwd_census,
                )
        elif existing:
            _validate_holder(
                worktree_path,
                prior_dispatch_id or original_lock_holder or "unknown-dispatch",
                project_root=project_root,
                lock_metadata=original_lock_metadata,
                journal_state_cache=journal_state_cache,
                process_cwd_census=process_cwd_census,
            )
        _write_occupant(
            lock_file,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            controller_label=controller_label,
        )
        return WorktreeSeatLease(
            path=worktree_path,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            lock_file=lock_file,
            quarantine_branch=None,
            branch=actual,
            reclaimed_dispatch_id=None,
            controller_label=controller_label,
            created=not existing,
            previous_lock_contents=(
                None if sparse_applied else original_lock_contents
            ),
        )
    precheck = None
    head = original_head
    if existing and reset:
        try:
            assert head is not None
            _refuse_ignored_tree_collisions(
                worktree_path,
                head=head,
                target=base_commit,
                timeout=SEAT_RESET_GIT_TIMEOUT_S,
            )
            precheck = _precheck_quarantine_worktree(
                worktree_path,
                seat_name=seat_name,
                timeout=SEAT_RESET_GIT_TIMEOUT_S,
            )
        except WorktreeSeatResetRefused:
            raise
        except (WorktreeSeatError, OSError) as exc:
            raise WorktreeSeatResetRefused(
                f"cannot inspect dirty worktree {seat_name}: {exc}; refusing reset"
            ) from exc

    keep_ref = None
    if safety is not None and safety["conditions"]["commits_preserved"]["verdict"] == NO:
        pinned = pin_unique_commits(
            worktree_path,
            base_commit=base_commit,
            moving_ref=safety.get("moving_ref"),
            worktree_id=f"{seat_name}-{prior_dispatch_id or 'unknown-dispatch'}",
            timeout=SEAT_RESET_GIT_TIMEOUT_S,
        )
        if pinned["verdict"] != YES:
            raise WorktreeSeatResetRefused(
                f"refusing to reset worktree {seat_name}: {pinned['reason']}"
            )
        keep_ref = str(pinned.get("keep_ref") or "") or None
    if existing and reset:
        assert head is not None
        stamp = str(time.time_ns())
        keep_ref = (
            f"refs/{KEEP_REF_PREFIX}/{prior_dispatch_id}/head-"
            f"{stamp}-{head[:12]}"
        )
        _update_ref_and_verify(worktree_path, keep_ref, head, old="")
    try:
        quarantine_branch = _quarantine_dirty_worktree(
            worktree_path,
            seat_name=seat_name,
            abandoned_dispatch_id=prior_dispatch_id,
            precheck=precheck,
        )
    except WorktreeSeatResetRefused:
        raise
    except (WorktreeSeatError, OSError) as exc:
        raise WorktreeSeatResetRefused(
            f"cannot quarantine dirty worktree {seat_name}: {exc}"
        ) from exc
    try:
        _prepare_seat_checkout(
            worktree_path,
            project_root=project_root,
            branch=branch,
            base_commit=base_commit,
            keep_id=dispatch_id,
            clean_ignored=clean_ignored,
            cow_plan=cow_plan,
        )
        leftover = [record for record in _status_records(worktree_path) if _status_is_product(record)]
        if leftover:
            leftover_paths = sorted(
                {path for _status, paths in leftover for path in paths}
            )
            raise WorktreeSeatError(
                f"worktree {seat_name} is not clean after acquire-time reset; "
                f"leftover paths: {leftover_paths!r}"
            )
        actual_branch = _assert_seat_on_named_branch(
            worktree_path, seat_name=seat_name, branch=branch
        )
    except Exception as exc:
        if existing:
            # Checkout can refuse before changing branch or HEAD; only a
            # changed or unreadable state must stop this allocation.
            try:
                current_state = (
                    _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                    _git(worktree_path, "rev-parse", "HEAD").strip(),
                )
            except (OSError, WorktreeSeatError):
                current_state = None
            if current_state == (original_branch, original_head):
                raise WorktreeSeatResetRefused(
                    f"cannot reset worktree {seat_name}: {exc}"
                ) from exc
        try:
            _write_occupant(
                lock_file,
                seat_name=seat_name,
                dispatch_id=dispatch_id,
                controller_label=controller_label,
            )
        except Exception as holder_error:
            raise WorktreeSeatError(
                f"seat_preparation_failed:{seat_name}; original preparation refusal: "
                f"{exc}; holder record update also failed: {holder_error}"
            ) from exc
        raise WorktreeSeatError(
            f"seat_preparation_failed:{seat_name}; original preparation refusal: {exc}"
        ) from exc
    try:
        if before_occupant is not None:
            before_occupant(worktree_path)
        _write_occupant(
            lock_file,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            controller_label=controller_label,
        )
        if after_occupant is not None:
            after_occupant(worktree_path)
    except Exception as exc:
        rollback_errors: list[str] = []
        checkout_rollback_failed = False
        try:
            if original_head is not None:
                if original_branch and original_branch != "HEAD":
                    branch_owner = _worktree_checking_out_branch(
                        worktree_path, original_branch
                    )
                else:
                    branch_owner = None
                if (
                    original_branch
                    and original_branch != "HEAD"
                    and (
                        branch_owner is None
                        or branch_owner.resolve(strict=False)
                        == worktree_path.resolve(strict=False)
                    )
                ):
                    _git(
                        worktree_path,
                        "checkout",
                        "--force",
                        "-B",
                        original_branch,
                        original_head,
                    )
                else:
                    _git(
                        worktree_path,
                        "checkout",
                        "--force",
                        "--detach",
                        original_head,
                    )
                if _git(worktree_path, "rev-parse", "HEAD").strip() != original_head:
                    raise WorktreeSeatError(
                        f"rollback HEAD did not return to saved occupant head {original_head}"
                    )
        except Exception as rollback_error:
            checkout_rollback_failed = True
            rollback_errors.append(f"checkout rollback failed: {rollback_error}")
        if checkout_rollback_failed or not existing:
            try:
                lock_file.seek(0)
                lock_file.truncate()
                json.dump(
                    {
                        "seat": seat_name,
                        "dispatch_id": dispatch_id,
                        "reconciliation_required": True,
                    },
                    lock_file,
                    sort_keys=True,
                )
                lock_file.write("\n")
                lock_file.flush()
                os.fsync(lock_file.fileno())
            except Exception as rollback_error:
                rollback_errors.append(
                    f"reconciliation marker failed: {rollback_error}"
                )
        else:
            try:
                lock_file.seek(0)
                lock_file.write(original_lock_contents)
                lock_file.truncate()
                lock_file.flush()
                os.fsync(lock_file.fileno())
            except Exception as rollback_error:
                rollback_errors.append(f"occupant rollback failed: {rollback_error}")
        message = (
            f"seat_preparation_failed:{seat_name}; original preparation refusal: {exc}"
        )
        if rollback_errors:
            message += f"; {'; '.join(rollback_errors)}"
        if isinstance(exc, WorktreeSeatResetRefused) and not rollback_errors:
            raise
        raise WorktreeSeatError(message) from exc
    return WorktreeSeatLease(
        path=worktree_path,
        seat_name=seat_name,
        dispatch_id=dispatch_id,
        lock_file=lock_file,
        quarantine_branch=quarantine_branch,
        branch=actual_branch,
        keep_ref=keep_ref,
        reclaimed_dispatch_id=prior_dispatch_id,
        controller_label=controller_label,
        created=not existing,
        previous_lock_contents=None,
    )


def _legacy_ring_candidates(project_root: Path) -> list[tuple[Path, Path]]:
    """Return existing label-ring trees and their legacy lock files."""
    root = repository_worktree_root(project_root)
    found: list[tuple[Path, Path]] = []
    if not root.is_dir():
        return found
    for label_root in sorted(root.iterdir(), key=lambda item: item.name):
        if not label_root.is_dir() or label_root.name.startswith("."):
            continue
        for path in sorted(label_root.iterdir(), key=lambda item: item.name):
            if not is_captive_seat_name(path.name):
                continue
            if path.is_symlink():
                raise WorktreeSeatError(
                    f"managed worktree path must not be a symlink: {path}"
                )
            if not path.is_dir():
                continue
            lock_path = _seat_lock_root(project_root, controller_label=label_root.name) / (
                f"{path.name}.lock"
            )
            found.append((path, lock_path))
    return found


def _candidate_lock_path(
    project_root: Path,
    worktree_path: Path,
    *,
    managed_root: Path,
) -> Path:
    """Resolve global new-pool locks and label-local migration locks."""
    try:
        if worktree_path.parent.resolve() == managed_root.resolve():
            return _seat_lock_root(project_root) / f"{worktree_path.name}.lock"
    except OSError:
        pass
    rel = worktree_path.resolve().relative_to(repository_worktree_root(project_root).resolve())
    if len(rel.parts) == 2 and is_captive_seat_name(rel.parts[1]):
        return _seat_lock_root(project_root, controller_label=rel.parts[0]) / (
            f"{rel.parts[1]}.lock"
        )
    return _seat_lock_root(project_root) / f"{worktree_path.name}.lock"


def worktree_lock_path_for_path(project_root: Path, worktree_path: Path) -> Path:
    """Return the lock path for a new-pool or migrated label-ring path."""
    return _candidate_lock_path(
        project_root.resolve(),
        worktree_path.resolve(strict=False),
        managed_root=repository_worktree_root(project_root),
    )


def _probe_worktree_seat_flock(
    project_root: Path,
    worktree_path: Path,
    *,
    managed_root: Path | None = None,
) -> tuple[bool | None, str | None]:
    """Probe a seat lock without creating it; unreadable identity means unknown."""
    lock_file = None
    lock_fd = None
    try:
        root = project_root.resolve()
        path = worktree_path.expanduser().resolve(strict=False)
        lock_path = _candidate_lock_path(
            root,
            path,
            managed_root=managed_root or repository_worktree_root(root),
        )
        lock_fd = _open_registered_lock(
            lock_path,
            _lock_open_flags() & ~os.O_CREAT,
            registry_root=_git_common_dir(root),
            allow_create=False,
            allow_unregistered=True,
        )
        lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
        lock_fd = None
    except (OSError, ValueError):
        if lock_fd is not None:
            os.close(lock_fd)
        return None, None

    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        except OSError:
            return None, None
        else:
            held = False
        try:
            holder = _known_lock_dispatch_id(lock_file)
        except (OSError, ValueError, UnicodeError):
            holder = None
        return held, holder
    finally:
        lock_file.close()


def _busy_worktree_message(
    project_root: Path,
    limit: int,
    occupants: list[tuple[str, dict]],
    *,
    journal_state_cache: dict[str, str] | None = None,
) -> str:
    ordered = sorted(
        occupants,
        key=lambda item: str(item[1].get("acquired_at") or "9999"),
    )
    oldest = ", ".join(
        _holder_description(
            name,
            payload,
            project_root=project_root,
            journal_state_cache=journal_state_cache,
        )
        for name, payload in ordered
    ) or "none recorded"
    return (
        f"{len(occupants)}/{limit} worktrees busy in {project_root.name}; "
        f"oldest holders: {oldest}"
    )


@contextmanager
def _hold_worktree_allocation_lock(
    project_root: Path, *, deadline: float | None = None
):
    """Hold the short admission lock.

    A zero wait budget fails immediately when this lock is busy. It is not
    held across ``git worktree add``.
    """
    with _hold_named_worktree_lock(
        project_root,
        "allocation.lock",
        description="worktree allocation lock",
        deadline=deadline,
    ):
        yield


@contextmanager
def _hold_worktree_mutation_lock(
    project_root: Path, *, deadline: float | None = None
):
    """Hold the lock that covers admin-dir and registration mutations.

    Absent-slot creation re-checks CLEAN and holds this lock across
    ``git worktree add`` and the new occupancy registration. The yielded
    callable releases it before checkout, so a file checkout or a later
    template build does not block GC or ``--drop-stale``. The admission
    lock stays free so a zero wait budget does not fail while another
    seat is being added.
    """
    with _hold_named_worktree_lock(
        project_root,
        "worktree-mutation.lock",
        description="worktree mutation lock",
        deadline=deadline,
    ) as release:
        yield release


@contextmanager
def _hold_named_worktree_lock(
    project_root: Path,
    lock_name: str,
    *,
    description: str,
    deadline: float | None = None,
):
    """Open, flock, and register one pool lock.

    The yielded callable drops the flock early. Closing the file in
    ``finally`` releases it if the caller did not.
    """
    project_root = Path(project_root)
    lock_root = _seat_lock_root(project_root)
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / lock_name
    registry_root = _git_common_dir(project_root)
    try:
        lock_fd = _open_registered_lock(
            lock_path,
            _lock_open_flags(),
            registry_root=registry_root,
            allow_create=True,
            allow_unregistered=True,
        )
    except OSError as exc:
        raise WorktreeSeatError(
            f"cannot open {description} {lock_path}: {exc}"
        ) from exc
    lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
    try:
        _acquire_allocation_lock(
            lock_file,
            lock_path,
            deadline=deadline,
            description=description,
        )
        try:
            _adopt_exclusive_lock(
                lock_path,
                lock_file.fileno(),
                registry_root=registry_root,
                deadline=deadline,
            )
        except OSError as exc:
            raise WorktreeSeatError(
                f"cannot register {description} {lock_path}: {exc}"
            ) from exc
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            released = True

        yield release
    finally:
        lock_file.close()


def _remove_admin_dirs_naming_checkout(repo: Path, checkout: Path) -> list[Path]:
    """Remove real admin dirs whose readable gitdir names ``checkout``.

    Symlinks and admins whose checkout location is unknown are left in place.
    The caller holds the mutation lock. Returns the directories removed.
    """
    wanted = _normalized_path(checkout)
    removed: list[Path] = []
    try:
        common = _git_common_dir(repo)
    except WorktreeSeatError as exc:
        raise OSError(errno.EINVAL, str(exc)) from exc
    worktrees = Path(common) / "worktrees"
    if _path_presence(worktrees) != "present":
        return removed
    try:
        children = list(worktrees.iterdir())
    except OSError as exc:
        raise OSError(
            exc.errno or errno.EIO, f"cannot list admin dirs {worktrees}: {exc}"
        ) from exc
    for child in children:
        try:
            info = os.lstat(child)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        state, found = _admin_dir_checkout(child)
        if (
            state == "checkout_missing"
            and found is not None
            and _normalized_path(found) == wanted
            and _path_presence(found) == "absent"
        ):
            shutil.rmtree(child)
            removed.append(child)
    return removed


def _acquire_allocation_lock(
    allocation_file: TextIO,
    allocation_lock_path: Path,
    *,
    deadline: float | None,
    description: str = "worktree allocation lock",
) -> None:
    """Acquire one pool lock without overrunning a wait budget."""
    if deadline is None:
        fcntl.flock(allocation_file.fileno(), fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(allocation_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if deadline <= 0 or time.monotonic() >= deadline:
                raise WorktreeSeatUnavailable(
                    f"seat wait expired while waiting for {description} "
                    f"{allocation_lock_path}"
                ) from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    # Do not begin seat inspection or launch preparation after a positive
    # capacity deadline. Once this check passes, worktree setup may finish
    # after the deadline; the caller already owns the admitted seat.
    if deadline > 0 and time.monotonic() >= deadline:
        raise WorktreeSeatUnavailable(
            f"seat wait expired before worktree admission after acquiring "
            f"{allocation_lock_path}"
        )


def acquire_worktree_seat(
    project_root: Path,
    dispatch_id: str,
    *,
    _preclaimed_occupancy_lock: WorktreePathLock | None = None,
    base: str | None = None,
    branch: str | None = None,
    managed_root: Path | None = None,
    controller_label: str | None = None,
    reset: bool = True,
    occupy_path: Path | None = None,
    expected_prior_dispatch_id: str | None = None,
    allowed_prior_dispatch_ids: frozenset[str] | set[str] | None = None,
    capacity_deadline: float | None = None,
    before_reset: Callable[[Path, str | None], bool | None] | None = None,
    before_occupant: Callable[[Path], None] | None = None,
    after_occupant: Callable[[Path], None] | None = None,
) -> WorktreeSeatLease:
    """Acquire one repository-wide managed ``s-N`` worktree.

    Existing label-ring paths remain valid during migration and count against
    the same repository cap. New paths and locks live directly under the
    repository's common Git directory, so controller labels remain metadata
    instead of allocating independent rings.
    """
    project_root = project_root.resolve()
    _verify_project_root(project_root)
    seat_limit = configured_worktree_seats()
    label = default_controller_ring_label(controller_label, project_root=project_root)
    resolved_base = base if base is not None else default_seat_base(project_root)
    base_commit = _git(project_root, "rev-parse", "--verify", f"{resolved_base}^{{commit}}")
    branch = str(branch or seat_branch_name(dispatch_id)).strip()
    branch_suffix = branch.split("/", 1)[1] if "/" in branch else ""
    if (
        not is_worktree_branch(branch)
        or not branch_suffix
        or not _SAFE_RING_LABEL.fullmatch(branch_suffix)
    ):
        raise WorktreeSeatError(
            f"invalid managed worktree branch {branch!r}; expected worktree/<id> or seat/<id>"
        )
    cow_plan = (
        _cow_prepare_outside_locks(project_root, base_commit)
        if reset
        else (False, None, None)
    )

    worktrees_root = project_root / "worktrees"
    if worktrees_root.is_symlink():
        raise WorktreeSeatError(
            f"managed worktree root must not be a symlink: {worktrees_root}"
        )
    if managed_root is not None:
        managed_root = managed_root.expanduser()
        if managed_root.is_symlink():
            raise WorktreeSeatError(
                f"managed worktree root must not be a symlink: {managed_root}"
            )
        managed_root = managed_root.resolve(strict=False)
    else:
        managed_root = repository_worktree_root(project_root)
    if managed_root.is_symlink():
        raise WorktreeSeatError(f"managed worktree root must not be a symlink: {managed_root}")
    managed_root_state = _path_presence(managed_root)
    if managed_root_state == "unknown":
        raise WorktreeSeatError(
            f"managed worktree root could not be inspected: {managed_root}"
        )
    if managed_root_state == "present" and not managed_root.is_dir():
        raise WorktreeSeatError(f"managed worktree root is not a directory: {managed_root}")

    lock_root = _seat_lock_root(project_root)
    if lock_root.is_symlink():
        raise WorktreeSeatError(f"worktree lock root must not be a symlink: {lock_root}")
    lock_root_state = _path_presence(lock_root)
    if lock_root_state == "unknown":
        raise WorktreeSeatError(f"worktree lock root could not be inspected: {lock_root}")
    if lock_root_state == "present" and not lock_root.is_dir():
        raise WorktreeSeatError(f"worktree lock root is not a directory: {lock_root}")
    lock_root.mkdir(parents=True, exist_ok=True)
    registry_root = _git_common_dir(project_root)

    flags = _lock_open_flags()
    allocation_lock_path = lock_root / "allocation.lock"
    try:
        allocation_fd = _open_registered_lock(
            allocation_lock_path,
            flags,
            registry_root=registry_root,
            allow_create=True,
            allow_unregistered=True,
        )
    except OSError as exc:
        raise WorktreeSeatError(
            f"cannot open worktree allocation lock {allocation_lock_path}: {exc}"
        ) from exc
    allocation_file = os.fdopen(allocation_fd, "r+", encoding="utf-8")
    try:
        # The admission lock only serializes a nonblocking candidate pick.
        _acquire_allocation_lock(
            allocation_file,
            allocation_lock_path,
            deadline=capacity_deadline,
        )
        allocation_locked = True

        def release_allocation_lock() -> None:
            nonlocal allocation_locked
            if allocation_locked:
                fcntl.flock(allocation_file.fileno(), fcntl.LOCK_UN)
                allocation_locked = False

        while True:
            try:
                _adopt_exclusive_lock(
                    allocation_lock_path,
                    allocation_file.fileno(),
                    registry_root=registry_root,
                    deadline=time.monotonic(),
                )
            except OSError as exc:
                release_allocation_lock()
                if exc.errno != errno.ETIMEDOUT:
                    raise WorktreeSeatError(
                        f"cannot register worktree allocation lock "
                        f"{allocation_lock_path}: {exc}"
                    ) from exc
                if (
                    capacity_deadline is not None
                    and time.monotonic() >= capacity_deadline
                ):
                    raise WorktreeSeatUnavailable(
                        "seat wait expired while registering worktree allocation lock"
                    ) from exc
                time.sleep(0.01)
                _acquire_allocation_lock(
                    allocation_file,
                    allocation_lock_path,
                    deadline=capacity_deadline,
                )
                allocation_locked = True
            else:
                break
        # Candidate seat flocks serialize claims. Do not hold the global lock
        # while checking repository state or inspecting candidate holders.
        release_allocation_lock()
        clean_report = _repo_clean_report(project_root)
        journal_state_cache: dict[str, str] = {}
        process_cwd_census = lambda: _process_cwd_snapshot(
            timeout_s=PROCESS_CWD_SNAPSHOT_TIMEOUT_S
        )

        def reacquire_allocation_lock() -> None:
            nonlocal allocation_locked
            if not allocation_locked:
                _acquire_allocation_lock(
                    allocation_file,
                    allocation_lock_path,
                    deadline=capacity_deadline,
                )
                allocation_locked = True

        capacity_global_candidates = [
            (
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            for slot in range(1, seat_limit + 1)
        ]
        legacy_candidates = _legacy_ring_candidates(project_root)
        probe_flags = flags & ~os.O_CREAT

        if branch == seat_branch_name(dispatch_id):
            branch_owner = _worktree_checking_out_branch(project_root, branch)
            if branch_owner is not None and _path_presence(branch_owner) != "absent":
                if is_managed_worktree_path(
                    branch_owner, project_root=project_root
                ):
                    held, _holder = _probe_worktree_seat_flock(
                        project_root,
                        branch_owner,
                        managed_root=managed_root,
                    )
                    if held is True:
                        raise WorktreeSeatUnavailable(
                            f"worktree {branch_owner.name} already holds "
                            f"{branch}; refusing to launch a second holder",
                            seat_name=branch_owner.name,
                            flock_held=True,
                        )
                    if held is None:
                        raise WorktreeSeatUnavailable(
                            f"dispatch_branch_owner_unknown:{branch_owner.name}; "
                            "refusing to touch the existing checkout",
                            seat_name=branch_owner.name,
                            reason="identity_unknown",
                        )
                raise _dispatch_branch_in_use(
                    branch_owner, project_root=project_root, branch=branch
                )

        def checkout_dispatch_id(candidate_path: Path) -> str | None:
            try:
                current_branch = _git(
                    candidate_path, "rev-parse", "--abbrev-ref", "HEAD"
                ).strip()
            except (OSError, WorktreeSeatError):
                return None
            prefix = WORKTREE_BRANCH_PREFIX + "/"
            if current_branch.startswith(prefix) and current_branch[len(prefix) :]:
                return current_branch[len(prefix) :]
            return None

        def holder_priority(candidate_path: Path, candidate_lock: Path) -> tuple[int, float]:
            """Rank free seats by whether reusing them strands resumable work."""
            lock_state = _path_presence(candidate_lock)
            path_state = _path_presence(candidate_path)
            if lock_state == "absent" and path_state == "absent":
                return (0, 0.0)

            holder_id = None
            holder_metadata = None
            if lock_state == "present":
                try:
                    fd = _open_registered_lock(
                        candidate_lock,
                        probe_flags,
                        registry_root=registry_root,
                        allow_unregistered=True,
                    )
                    with os.fdopen(fd, "r+", encoding="utf-8") as lock_file:
                        holder_metadata = _lock_metadata(lock_file)
                        holder_id = _known_lock_dispatch_id(lock_file)
                except OSError:
                    return (3, float("inf"))
            if holder_id is None and path_state == "present":
                holder_id = checkout_dispatch_id(candidate_path)
            if holder_id is None:
                return (0, 0.0) if path_state == "absent" else (3, float("inf"))

            record, _live = _holder_record(
                holder_id,
                project_root=project_root,
                lock_metadata=holder_metadata,
                journal_state_cache=journal_state_cache,
            )
            if record.get("_holder_source") == "journal":
                # Journal fallback records have no worker identity. A terminal
                # journal state cannot turn that UNKNOWN into a dead holder.
                return (3, float("inf"))
            state = str(record.get("state") or "")
            terminal = goalflight_ledger.terminal_state_for(
                state, record.get("reason") or record.get("error")
            )
            if terminal == "complete" or state == "complete":
                terminal_at = goalflight_ledger.parse_utc(
                    record.get("ended_at") or record.get("updated_at")
                )
                if terminal_at is not None:
                    terminal_time = terminal_at.timestamp()
                    if (
                        time.time() - terminal_time
                        <= _worktree_complete_retention_grace_s()
                    ):
                        return (2, terminal_time)
                return (1, 0.0)
            if state == "cancelled" or terminal in {
                "error",
                "failed",
                "superseded",
                "withdrawn",
            }:
                return (1, 0.0)
            if terminal in {"", "unknown", "watcher_stopped"}:
                return (3, float("inf"))

            terminal_at = goalflight_ledger.parse_utc(
                record.get("ended_at") or record.get("updated_at")
            )
            try:
                terminal_time = terminal_at.timestamp() if terminal_at else float("inf")
            except (OverflowError, OSError, ValueError):
                terminal_time = float("inf")
            return (2, terminal_time)

        occupied_target = (
            Path(occupy_path).expanduser().resolve(strict=False)
            if occupy_path is not None
            else None
        )

        def scan_capacity_occupancy() -> tuple[
            list[tuple[str, dict]], set[Path], set[Path], list[tuple[str, str]]
        ]:
            occupants: list[tuple[str, dict]] = []
            occupied_paths: set[Path] = set()
            candidate_paths: set[Path] = set()
            skipped: list[tuple[str, str]] = []

            def note_occupant(candidate_path: Path, metadata: dict) -> None:
                resolved_candidate = candidate_path.resolve(strict=False)
                if resolved_candidate not in occupied_paths:
                    occupied_paths.add(resolved_candidate)
                    occupants.append((str(candidate_path), metadata))

            for candidate_path, candidate_lock in [
                *capacity_global_candidates,
                *legacy_candidates,
            ]:
                if (
                    occupied_target is not None
                    and candidate_path.resolve(strict=False) == occupied_target
                ):
                    continue
                lock_state = _path_presence(candidate_lock)
                candidate_state = _path_presence(candidate_path)
                if lock_state == "unknown" or candidate_state == "unknown":
                    note_occupant(candidate_path, {})
                    reason = _unknown_presence_reason(
                        candidate_path,
                        candidate_lock,
                        lock_state=lock_state,
                        path_state=candidate_state,
                    )
                    row = (candidate_path.name, reason)
                    if row not in skipped:
                        skipped.append(row)
                    continue
                if lock_state == "absent":
                    if candidate_state == "present":
                        note_occupant(candidate_path, {})
                        reason = _stuck_present_checkout_reason(
                            candidate_path, "no occupant record"
                        )
                        row = (candidate_path.name, reason)
                        if row not in skipped:
                            skipped.append(row)
                    continue
                # A known seat lock can proceed to candidate handling;
                # try_candidate rechecks the flock and the shared validator
                # decides whether an existing checkout can actually be reused.
                candidate_paths.add(candidate_path.resolve(strict=False))
                try:
                    probe_fd = _open_registered_lock(
                        candidate_lock,
                        probe_flags,
                        registry_root=registry_root,
                        allow_unregistered=True,
                    )
                except OSError:
                    # If the compatibility open cannot establish the lock
                    # identity, retain the candidate as an unknown occupant.
                    note_occupant(
                        candidate_path,
                        _unregistered_lock_metadata(candidate_lock),
                    )
                    continue
                probe_file = os.fdopen(probe_fd, "r+", encoding="utf-8")
                seat_lock_held = False
                try:
                    fcntl.flock(probe_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    note_occupant(candidate_path, _lock_metadata(probe_file))
                else:
                    seat_lock_held = True
                    note_occupant(candidate_path, _lock_metadata(probe_file))
                finally:
                    if seat_lock_held:
                        fcntl.flock(probe_file.fileno(), fcntl.LOCK_UN)
                    probe_file.close()

            return occupants, occupied_paths, candidate_paths, skipped

        (
            capacity_occupants,
            capacity_occupied_paths,
            capacity_candidate_paths,
            capacity_skipped,
        ) = scan_capacity_occupancy()

        try:
            listed_owner = _worktree_checking_out_branch(project_root, branch)
        except WorktreeSeatError:
            listed_owner = None
        if listed_owner is not None and _path_presence(listed_owner) == "absent":
            reason = clean_report.reason
            if "admin_dir" not in reason:
                admin = _missing_checkout_admin_text(project_root, listed_owner)
                admin_path = admin if admin != "unresolved" else str(listed_owner)
                extra = f"admin_dir checkout_missing {admin_path}"
                reason = f"repo_not_clean: {extra}; {reason}" if reason else extra
            skips = list(capacity_skipped)
            if not any(name == listed_owner.name for name, _skip_reason in skips):
                skips.append((listed_owner.name, reason))
            in_use = _dispatch_branch_in_use(
                listed_owner, project_root=project_root, branch=branch
            )
            in_use.skipped_slots = tuple(skips)
            raise in_use

        if (
            len(capacity_occupants) >= seat_limit
            and occupy_path is None
            and not capacity_candidate_paths
        ):
            release_allocation_lock()
            detail = _busy_worktree_message(
                project_root,
                seat_limit,
                capacity_occupants,
                journal_state_cache=journal_state_cache,
            )
            clause = ""
            if capacity_skipped:
                body = ", ".join(
                    f"{name}={reason}" for name, reason in capacity_skipped
                )
                clause = f"; skipped seats: {body}"
            raise WorktreeSeatUnavailable(
                f"{detail}; refusing to create a new unmanaged worktree{clause}",
                skipped_slots=tuple(capacity_skipped),
            )
        if occupy_path is None:
            managed_root.mkdir(parents=True, exist_ok=True)

        def pin_capacity_skips(exc: WorktreeSeatError) -> WorktreeSeatError:
            if not getattr(exc, "skipped_slots", ()):
                exc.skipped_slots = tuple(capacity_skipped)
            return exc

        if occupy_path is not None:
            worktree_path = Path(occupy_path).expanduser().resolve(strict=False)
            worktree_state = _path_presence(worktree_path)
            if worktree_state == "unknown":
                raise pin_capacity_skips(WorktreeCwdRefused(
                    f"refusing to inspect --cwd {worktree_path}"
                ))
            if worktree_state == "absent":
                raise pin_capacity_skips(WorktreeCwdRefused(
                    f"refusing to create missing --cwd {worktree_path}"
                ))
            seat_name = worktree_path.name
            if not is_captive_seat_name(seat_name) and _slot_from_seat_name(
                seat_name, WORKTREE_SEAT_PREFIX
            ) is None:
                raise pin_capacity_skips(WorktreeCwdRefused(
                    f"--cwd {worktree_path} is not a managed repository worktree in "
                    f"{managed_root}; pass --in-place for the project root"
                ))
            if not is_managed_worktree_path(worktree_path, project_root=project_root):
                raise pin_capacity_skips(WorktreeCwdRefused(
                    f"--cwd {worktree_path} is not in the repository worktree pool"
                ))
            lock_path = _candidate_lock_path(
                project_root, worktree_path, managed_root=managed_root
            )
            try:
                lock_fd = _open_registered_lock(
                    lock_path,
                    flags,
                    registry_root=registry_root,
                    allow_create=True,
                    allow_unregistered=True,
                )
            except OSError as exc:
                raise pin_capacity_skips(WorktreeSeatError(
                    f"cannot open worktree lock {lock_path}: {exc}"
                )) from exc
            lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_metadata = _lock_metadata(lock_file)
                release_allocation_lock()
                occupant = _occupant_description(
                    lock_file,
                    seat_name,
                    project_root=project_root,
                    journal_state_cache=journal_state_cache,
                )
                current_holder = _attribute_prelaunch_seat_holder(
                    worktree_path,
                    _known_lock_dispatch_id(lock_file),
                    project_root=project_root,
                    lock_metadata=lock_metadata,
                    journal_state_cache=journal_state_cache,
                )
                lock_file.close()
                if (
                    expected_prior_dispatch_id is not None
                    and current_holder is not None
                    and current_holder != expected_prior_dispatch_id
                ):
                    raise pin_capacity_skips(WorktreeSeatReclaimed(
                        f"resume refused: worktree {seat_name} was reclaimed by "
                        f"{current_holder}; expected recorded holder "
                        f"{expected_prior_dispatch_id}; refusing to reset or recreate it",
                        seat_name=seat_name,
                        current_holder=current_holder,
                        expected_holder=expected_prior_dispatch_id,
                        flock_held=True,
                    ))
                raise pin_capacity_skips(WorktreeSeatUnavailable(
                    f"worktree {seat_name} is held: {occupant}; "
                    "refusing to git worktree add a new unmanaged path",
                    seat_name=seat_name,
                    current_holder=current_holder,
                    flock_held=True,
                ))
            try:
                try:
                    _adopt_exclusive_lock(
                        lock_path,
                        lock_file.fileno(),
                        registry_root=registry_root,
                        deadline=time.monotonic(),
                    )
                except OSError as exc:
                    raise WorktreeSeatError(
                        f"cannot register worktree lock {lock_path}: {exc}"
                    ) from exc
                lock_metadata = _lock_metadata(lock_file)
                prior_dispatch_id = _attribute_prelaunch_seat_holder(
                    worktree_path,
                    _known_lock_dispatch_id(lock_file),
                    project_root=project_root,
                    lock_metadata=lock_metadata,
                    journal_state_cache=journal_state_cache,
                )
                if (
                    expected_prior_dispatch_id is not None
                    and prior_dispatch_id is None
                ):
                    raise WorktreeSeatUnavailable(
                        f"resume refused: worktree {seat_name} has unknown ownership; "
                        "its lock metadata is empty or unreadable, so it is not reclaimable",
                        seat_name=seat_name,
                        reason="identity_unknown",
                    )
                if (
                    expected_prior_dispatch_id is not None
                    and prior_dispatch_id != expected_prior_dispatch_id
                ):
                    allowed = allowed_prior_dispatch_ids or set()
                    if prior_dispatch_id not in allowed:
                        release_allocation_lock()
                        terminal_reclaimed = (
                            prior_dispatch_id is not None
                            and _holder_is_terminal(
                                prior_dispatch_id,
                                project_root=project_root,
                                lock_metadata=lock_metadata,
                                journal_state_cache=journal_state_cache,
                            )
                        )
                        raise WorktreeSeatReclaimed(
                            f"resume refused: worktree {seat_name} was reclaimed by "
                            f"{prior_dispatch_id}; expected recorded holder "
                            f"{expected_prior_dispatch_id}; refusing to reset or recreate it",
                            current_holder=prior_dispatch_id,
                            expected_holder=expected_prior_dispatch_id,
                            terminal_reclaimed=terminal_reclaimed,
                        )
                lease = _prepare_claimed_seat(
                    project_root=project_root,
                    worktree_path=worktree_path,
                    seat_name=seat_name,
                    lock_file=lock_file,
                    dispatch_id=dispatch_id,
                    prior_dispatch_id=prior_dispatch_id,
                    branch=branch,
                    base_commit=base_commit,
                    reset=reset,
                    expected_prior_dispatch_id=expected_prior_dispatch_id,
                    controller_label=label,
                    before_reset=before_reset,
                    before_occupant=before_occupant,
                    after_occupant=after_occupant,
                    release_allocation_lock=release_allocation_lock,
                    journal_state_cache=journal_state_cache,
                    process_cwd_census=process_cwd_census,
                    cow_plan=cow_plan,
                    _preclaimed_occupancy_lock=_preclaimed_occupancy_lock,
                )
                pinned_slots = list(lease.skipped_slots or capacity_skipped)
                if not clean_report.clean and not any(
                    "repo_not_clean" in reason for _name, reason in pinned_slots
                ):
                    pinned_slots.append(
                        (
                            _not_clean_skip_seat(clean_report.items),
                            clean_report.reason,
                        )
                    )
                lease.skipped_slots = tuple(pinned_slots)
                return lease
            except BaseException as exc:
                lock_file.close()
                if isinstance(exc, WorktreeSeatError) and not getattr(exc, "skipped_slots", ()):
                    exc.skipped_slots = tuple(capacity_skipped)
                raise

        hwm = min(_read_ring_hwm(lock_root), seat_limit)
        global_candidates = [
            (
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            for slot in range(1, hwm + 1)
        ]
        legacy_candidates = _legacy_ring_candidates(project_root)
        occupants = list(capacity_occupants)
        occupied_paths = set(capacity_occupied_paths)
        refused: list[str] = []
        skipped: list[tuple[str, str]] = []

        def take(lease: WorktreeSeatLease | None) -> WorktreeSeatLease | None:
            if lease is not None:
                if (
                    not clean_report.clean
                    and not created_under_clean_recheck
                    and not any(
                        "repo_not_clean" in reason for _name, reason in skipped
                    )
                ):
                    note_skip(
                        _not_clean_skip_seat(clean_report.items),
                        clean_report.reason,
                    )
                lease.skipped_slots = tuple(skipped)
            return lease

        def skip_clause() -> str:
            if not skipped:
                return ""
            body = ", ".join(f"{name}={reason}" for name, reason in skipped)
            return f"; skipped seats: {body}"

        def note_skip(name: str, reason: str) -> None:
            row = (name, reason)
            if row not in skipped:
                skipped.append(row)

        def other_occupant_count(candidate_path: Path) -> int:
            count = len(occupants)
            if candidate_path.resolve(strict=False) in occupied_paths:
                count -= 1
            return count

        def refresh_capacity_occupancy() -> None:
            nonlocal capacity_occupants, capacity_occupied_paths
            nonlocal capacity_candidate_paths, capacity_skipped
            nonlocal occupants, occupied_paths
            (
                capacity_occupants,
                capacity_occupied_paths,
                capacity_candidate_paths,
                capacity_skipped,
            ) = scan_capacity_occupancy()
            occupants = list(capacity_occupants)
            occupied_paths = set(capacity_occupied_paths)
            for name, reason in capacity_skipped:
                note_skip(name, reason)

        def remember_skips(exc: BaseException) -> None:
            if isinstance(exc, WorktreeSeatError) and not getattr(exc, "skipped_slots", ()):
                exc.skipped_slots = tuple(skipped)

        created_under_clean_recheck = False

        def try_candidate(
            worktree_path: Path,
            lock_path: Path,
            *,
            require_ancestor: bool = False,
            mutation_held: bool = False,
            release_mutation: Callable[[], None] | None = None,
        ):
            lock_state = _path_presence(lock_path)
            path_state = _path_presence(worktree_path)
            if lock_state == "unknown" or path_state == "unknown":
                note_skip(
                    worktree_path.name,
                    _unknown_presence_reason(
                        worktree_path,
                        lock_path,
                        lock_state=lock_state,
                        path_state=path_state,
                    ),
                )
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), {}))
                return None
            if path_state != "present" and not mutation_held:
                # Wait for the tree-mutation lock before taking this seat's
                # lock. Re-enter under the mutation lock only after a fresh
                # cleanliness check.
                release_allocation_lock()
                lease = None
                appeared = False
                with _hold_worktree_mutation_lock(project_root) as release_mutation:
                    if _path_presence(worktree_path) == "present":
                        appeared = True
                    else:
                        fresh = _repo_clean_report(project_root)
                        if not fresh.clean:
                            reason = fresh.reason or "occupancy_unknown"
                            note_skip(
                                worktree_path.name,
                                f"worktree {worktree_path.name} skipped ({reason}); "
                                "refusing recreate",
                            )
                            resolved_path = worktree_path.resolve(strict=False)
                            if resolved_path not in occupied_paths:
                                occupied_paths.add(resolved_path)
                                occupants.append((str(worktree_path), {}))
                        else:
                            lease = try_candidate(
                                worktree_path,
                                lock_path,
                                require_ancestor=require_ancestor,
                                mutation_held=True,
                                release_mutation=release_mutation,
                            )
                if appeared:
                    return try_candidate(
                        worktree_path,
                        lock_path,
                        require_ancestor=require_ancestor,
                    )
                return lease
            if path_state != "present" and mutation_held:
                refresh_capacity_occupancy()
                if other_occupant_count(worktree_path) >= seat_limit:
                    note_skip(
                        worktree_path.name,
                        "capacity reached under mutation lock",
                    )
                    return None
            lock_existed = lock_state == "present"
            path_existed = path_state == "present"

            def checkout_is_for_this_dispatch() -> bool:
                if _path_presence(worktree_path) != "present":
                    return False
                try:
                    return (
                        _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
                        == branch
                    )
                except (OSError, WorktreeSeatError):
                    return False

            def failed_preparation(original: BaseException) -> WorktreeSeatError:
                try:
                    _write_occupant(
                        lock_file,
                        seat_name=worktree_path.name,
                        dispatch_id=dispatch_id,
                        controller_label=label,
                    )
                except Exception as holder_error:
                    return WorktreeSeatError(
                        f"seat_preparation_failed:{worktree_path.name}; "
                        f"original preparation refusal: {original}; holder record "
                        f"update also failed: {holder_error}"
                    )
                return WorktreeSeatError(
                    f"seat_preparation_failed:{worktree_path.name}; "
                    f"original preparation refusal: {original}"
                )
            try:
                registered_before_open = (
                    _registered_lock_identity(
                        lock_path, registry_root=registry_root
                    )
                    is not None
                )
            except OSError:
                registered_before_open = False
            try:
                lock_fd = _open_registered_lock(
                    lock_path,
                    flags,
                    registry_root=registry_root,
                    allow_create=True,
                    allow_unregistered=True,
                )
            except OSError as exc:
                raise WorktreeSeatError(
                    f"cannot open worktree lock {lock_path}: {exc}"
                ) from exc
            lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
            try:
                # Serialize only the nonblocking seat pick. The allocation
                # lock wait happens before any candidate flock is held.
                if not mutation_held:
                    reacquire_allocation_lock()
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                release_allocation_lock()
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), _lock_metadata(lock_file)))
                lock_file.close()
                return None
            except BaseException:
                release_allocation_lock()
                lock_file.close()
                raise
            else:
                release_allocation_lock()

            seat_lock_held = True

            def release_candidate_lock() -> None:
                nonlocal seat_lock_held
                if not seat_lock_held:
                    return
                seat_lock_held = False
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

            try:
                lock_dispatch_id: str | None = None
                if require_ancestor and _seat_head_is_ancestor(
                    project_root,
                    _seat_head_from_metadata(worktree_path),
                    base_commit,
                ) is not True:
                    release_candidate_lock()
                    return None
                try:
                    _adopt_exclusive_lock(
                        lock_path,
                        lock_file.fileno(),
                        registry_root=registry_root,
                        deadline=time.monotonic(),
                    )
                except OSError as exc:
                    raise WorktreeSeatUnavailable(
                        f"worktree lock identity could not be adopted: {exc}"
                    ) from exc
                seat_name = worktree_path.name
                lock_metadata = _lock_metadata(lock_file)
                prior_dispatch_id = _attribute_prelaunch_seat_holder(
                    worktree_path,
                    _known_lock_dispatch_id(lock_file),
                    project_root=project_root,
                    lock_metadata=lock_metadata,
                    journal_state_cache=journal_state_cache,
                )
                lock_dispatch_id = prior_dispatch_id
                if not path_existed and lock_existed:
                    if prior_dispatch_id is None:
                        if not registered_before_open:
                            resolved_path = worktree_path.resolve(strict=False)
                            if resolved_path not in occupied_paths:
                                occupied_paths.add(resolved_path)
                                occupants.append(
                                    (str(worktree_path), _lock_metadata(lock_file))
                                )
                            release_candidate_lock()
                            return None
                    elif not registered_before_open and not reset:
                        _validate_holder(
                            worktree_path,
                            prior_dispatch_id,
                            project_root=project_root,
                            lock_metadata=lock_metadata,
                            journal_state_cache=journal_state_cache,
                            probe_journal=False,
                            process_cwd_census=process_cwd_census,
                        )
                    # A prior holder is validated during preparation, after
                    # releasing the allocation lock and acquiring the path lock.
                # A present checkout without a lock record is reclaimable only
                # when its branch names the dispatch that must pass ordinary
                # holder validation. Unknown branch ownership stays untouched.
                current_path_state = _path_presence(worktree_path)
                if current_path_state == "unknown":
                    raise WorktreeSeatUnavailable(
                        f"worktree {worktree_path} could not be inspected; holder is unknown",
                        seat_name=worktree_path.name,
                        reason="identity_unknown",
                )
                if current_path_state != "present" and not mutation_held:
                    release_candidate_lock()
                    return try_candidate(
                        worktree_path,
                        lock_path,
                        require_ancestor=require_ancestor,
                    )
                if prior_dispatch_id is None and current_path_state == "present":
                    branch_holder = checkout_dispatch_id(worktree_path)
                    if branch_holder is not None:
                        prior_dispatch_id = branch_holder
                    else:
                        note_skip(
                            worktree_path.name,
                            _stuck_present_checkout_reason(
                                worktree_path, "no occupant record"
                            ),
                        )
                        release_candidate_lock()
                        return None

                if (
                    current_path_state != "present"
                    and other_occupant_count(worktree_path) >= seat_limit
                ):
                    reason = "capacity reached; refusing to create an absent seat"
                    if not clean_report.clean:
                        reason = f"{clean_report.reason}; {reason}"
                    note_skip(worktree_path.name, reason)
                    release_candidate_lock()
                    return None

                def claim_seat(
                    release: Callable[[], None] | None,
                ) -> WorktreeSeatLease:
                    return _prepare_claimed_seat(
                        project_root=project_root,
                        worktree_path=worktree_path,
                        seat_name=seat_name,
                        lock_file=lock_file,
                        dispatch_id=dispatch_id,
                        prior_dispatch_id=prior_dispatch_id,
                        branch=branch,
                        base_commit=base_commit,
                        reset=reset,
                        controller_label=label,
                        before_reset=before_reset,
                        before_occupant=before_occupant,
                        after_occupant=after_occupant,
                        release_allocation_lock=release_allocation_lock,
                        release_mutation_lock=release,
                        journal_state_cache=journal_state_cache,
                        process_cwd_census=process_cwd_census,
                        cow_plan=cow_plan,
                        _preclaimed_occupancy_lock=_preclaimed_occupancy_lock,
                    )

                # Absent-seat creation already took the mutation lock before
                # acquiring this candidate lock.
                return claim_seat(release_mutation)
            except WorktreeSeatResetRefused as exc:
                if not path_existed and checkout_is_for_this_dispatch():
                    error = failed_preparation(exc)
                    remember_skips(error)
                    release_candidate_lock()
                    raise error from exc
                refused.append(f"{seat_name}: {exc}")
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), _lock_metadata(lock_file)))
                release_candidate_lock()
                # A failure during git worktree add is still inside the caller's
                # mutation lock. Let that lock unwind before another candidate
                # is examined.
                return None
            except WorktreeSeatUnavailable as exc:
                reason = exc.reason or ("unavailable" if not exc.flock_held else "")
                if (
                    reason
                    and path_existed
                    and lock_dispatch_id is None
                    and "operator must inspect" not in reason
                ):
                    reason = _stuck_present_checkout_reason(worktree_path, reason)
                if reason:
                    note_skip(worktree_path.name, reason)
                if not path_existed and checkout_is_for_this_dispatch():
                    exc.skipped_slots = tuple(skipped)
                    release_candidate_lock()
                    raise
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), _lock_metadata(lock_file)))
                release_candidate_lock()
                return None
            except BaseException as exc:
                error = None
                if (
                    isinstance(exc, Exception)
                    and not str(exc).startswith("seat_preparation_failed:")
                    and checkout_is_for_this_dispatch()
                ):
                    error = failed_preparation(exc)
                release_candidate_lock()
                if error is not None:
                    remember_skips(error)
                    raise error from exc
                remember_skips(exc)
                raise

        def create_next_slot() -> WorktreeSeatLease | None:
            nonlocal hwm, created_under_clean_recheck
            # Re-read the high-water mark under the mutation lock. Another
            # allocator may have created a seat after this scan released the
            # admission lock. The clean re-check and the add share this lock
            # with GC and --drop-stale.
            release_allocation_lock()
            with _hold_worktree_mutation_lock(project_root) as release_mutation:
                hwm = min(_read_ring_hwm(lock_root), seat_limit)
                if hwm >= seat_limit:
                    return None
                fresh = _repo_clean_report(project_root)
                if not fresh.clean:
                    if not any(
                        "repo_not_clean" in reason for _name, reason in skipped
                    ):
                        note_skip(
                            f"{CAPTIVE_SEAT_PREFIX}{hwm + 1}",
                            fresh.reason,
                        )
                    return None
                next_path = managed_root / f"{CAPTIVE_SEAT_PREFIX}{hwm + 1}"
                refresh_capacity_occupancy()
                if other_occupant_count(next_path) >= seat_limit:
                    note_skip(
                        next_path.name,
                        "capacity reached under mutation lock",
                    )
                    return None
                hwm += 1
                _write_ring_hwm(lock_root, hwm)
                lease = try_candidate(
                    managed_root / f"{CAPTIVE_SEAT_PREFIX}{hwm}",
                    lock_root / f"{CAPTIVE_SEAT_PREFIX}{hwm}.lock",
                    mutation_held=True,
                    release_mutation=release_mutation,
                )
                if lease is not None:
                    created_under_clean_recheck = True
                return lease

        # Never recycle a terminal seat while an unused configured seat can
        # still be admitted. The mutation lock and fresh clean check inside
        # create_next_slot keep this same path safe under concurrent allocators.
        while hwm < seat_limit and len(occupants) + 1 <= seat_limit:
            lease = create_next_slot()
            if lease is not None:
                return take(lease)
            if any("repo_not_clean" in reason for _name, reason in skipped):
                break

        slots = list(range(1, hwm + 1))
        heads = {
            slot: _seat_head_from_metadata(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}"
            )
            for slot in slots
        }
        slot_candidates = {
            slot: (
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            for slot in slots
        }
        release_allocation_lock()
        try:
            slot_priorities = {
                slot: holder_priority(*candidate)
                for slot, candidate in slot_candidates.items()
            }
            legacy_priorities = {
                candidate: holder_priority(*candidate)
                for candidate in legacy_candidates
            }
        finally:
            release_allocation_lock()
        priority_groups = sorted(
            set(slot_priorities.values()) | set(legacy_priorities.values())
        )

        ancestor_checks = 0
        for priority in priority_groups:
            priority_slots = [
                slot for slot in slots if slot_priorities[slot] == priority
            ]
            exact_slots = [slot for slot in priority_slots if heads[slot] == base_commit]
            other_slots = [slot for slot in priority_slots if slot not in exact_slots]

            for slot in exact_slots:
                lease = try_candidate(*slot_candidates[slot])
                if lease is not None:
                    return take(lease)

            for slot in other_slots:
                if ancestor_checks >= 8:
                    break
                ancestor_checks += 1
                lease = try_candidate(*slot_candidates[slot], require_ancestor=True)
                if lease is not None:
                    return take(lease)

            for slot in other_slots:
                lease = try_candidate(*slot_candidates[slot])
                if lease is not None:
                    return take(lease)

            for candidate in legacy_candidates:
                if legacy_priorities[candidate] != priority:
                    continue
                lease = try_candidate(*candidate)
                if lease is not None:
                    return take(lease)

        busy = len(occupants)
        if busy >= seat_limit and not refused:
            release_allocation_lock()
            detail = _busy_worktree_message(
                project_root,
                seat_limit,
                occupants,
                journal_state_cache=journal_state_cache,
            )
            raise WorktreeSeatUnavailable(
                f"{detail}; refusing to create a new unmanaged worktree{skip_clause()}",
                skipped_slots=tuple(skipped),
            )

        while hwm < seat_limit and len(occupants) + 1 <= seat_limit:
            lease = create_next_slot()
            if lease is not None:
                return take(lease)
            if any("repo_not_clean" in reason for _name, reason in skipped):
                break

        lost = "; ".join(refused)
        if lost:
            raise WorktreeSeatResetRefused(
                f"all available worktrees would lose work on reset: {lost}{skip_clause()}",
                skipped_slots=tuple(skipped),
            )
        release_allocation_lock()
        detail = _busy_worktree_message(
            project_root,
            seat_limit,
            occupants,
            journal_state_cache=journal_state_cache,
        )
        raise WorktreeSeatUnavailable(
            f"{detail}; refusing to create a new unmanaged worktree{skip_clause()}",
            skipped_slots=tuple(skipped),
        )
    finally:
        allocation_file.close()


def _lock_open_flags() -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _path_presence(path: Path) -> str:
    """Distinguish a missing path from one that cannot be inspected."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            return "absent"
        return "unknown"
    return "present"


def _unknown_presence_reason(
    worktree_path: Path,
    lock_path: Path,
    *,
    lock_state: str,
    path_state: str,
) -> str:
    """Name the path whose inspection failed, so one seat does not hide the pool."""
    parts: list[str] = []
    if path_state == "unknown":
        parts.append(str(worktree_path))
    if lock_state == "unknown":
        parts.append(str(lock_path))
    return "occupancy_unknown:" + ",".join(parts)


def _lock_path_identity(lock_path: Path) -> os.stat_result | None:
    """Read an unregistered in-place occupancy lock's current leaf identity."""
    try:
        identity = os.lstat(lock_path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(identity.st_mode):
        raise OSError(
            errno.ELOOP,
            f"lock path is not a regular file: {lock_path}",
        )
    return identity


def _lock_fd_matches_identity(
    fd: int, expected_stat: os.stat_result | None
) -> bool:
    """Require a regular fd to be the exact expected lock inode."""
    if expected_stat is None:
        return False
    opened_stat = os.fstat(fd)
    return (
        stat.S_ISREG(opened_stat.st_mode)
        and opened_stat.st_dev == expected_stat.st_dev
        and opened_stat.st_ino == expected_stat.st_ino
    )


def _open_registered_lock(
    lock_path: Path,
    flags: int,
    *,
    registry_root: Path,
    mode: int = 0o600,
    allow_create: bool = False,
    allow_unregistered: bool = False,
    registration_deadline: float | None = None,
) -> int:
    """Open a registered inode, with explicit legacy-lock compatibility."""
    expected_stat = _registered_lock_identity(
        lock_path, registry_root=registry_root
    )
    if expected_stat is not None:
        return _open_lock_path_safely(
            lock_path,
            flags & ~os.O_CREAT,
            mode,
            expected_stat=expected_stat,
        )
    if allow_unregistered:
        # Tolerate locks created before the registry existed. Callers that have
        # taken an exclusive lock may adopt the opened inode; probes and shared
        # readers leave this compatibility path unregistered.
        legacy_stat = _lock_path_identity(lock_path)
        if legacy_stat is not None:
            return _open_lock_path_safely(
                lock_path,
                flags & ~os.O_CREAT,
                mode,
                expected_stat=legacy_stat,
            )
    if not allow_create:
        raise OSError(
            errno.EPERM,
            f"lock identity is not registered: {lock_path}",
        )
    try:
        os.lstat(lock_path)
    except FileNotFoundError:
        pass
    except OSError:
        raise
    else:
        raise OSError(
            errno.EPERM,
            f"lock identity is not registered: {lock_path}",
        )
    try:
        fd = _open_lock_path_safely(
            lock_path,
            flags | os.O_CREAT,
            mode,
            create_only=True,
        )
    except FileExistsError:
        # Another allocator may have completed registration between the lstat
        # and create. Re-read the durable record; never adopt the existing path.
        expected_stat = _registered_lock_identity(
            lock_path, registry_root=registry_root
        )
        if expected_stat is None:
            raise OSError(
                errno.EPERM,
                f"lock identity is not registered: {lock_path}",
            )
        return _open_lock_path_safely(
            lock_path,
            flags & ~os.O_CREAT,
            mode,
            expected_stat=expected_stat,
        )
    try:
        _record_lock_identity(
            lock_path,
            fd,
            registry_root=registry_root,
            deadline=registration_deadline,
        )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_lock_path_safely(
    lock_path: Path,
    flags: int,
    mode: int = 0o600,
    *,
    expected_stat: os.stat_result | None = None,
    create_only: bool = False,
) -> int:
    """Open a lock only after safely walking every parent directory.

    ``O_NOFOLLOW`` protects the final component, not a parent replaced between
    registration and open. Walking from the filesystem root with directory
    descriptors makes each parent an opened, non-symlink directory and keeps
    the final open relative to that verified chain.
    """
    path = Path(lock_path)
    if not path.is_absolute():
        raise OSError(errno.EINVAL, f"lock path must be absolute: {path}")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "safe lock directory open is unavailable")
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise OSError(errno.ENOTSUP, "openat-style lock open is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    parts = path.parts
    if len(parts) < 2:
        raise OSError(errno.EINVAL, f"lock path has no parent: {path}")
    parent_fd = os.open(path.anchor, directory_flags)
    try:
        for component in parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = parts[-1]
        if expected_stat is None and create_only:
            fd = os.open(
                leaf,
                flags | os.O_CREAT | os.O_EXCL,
                mode,
                dir_fd=parent_fd,
            )
            try:
                created_stat = os.fstat(fd)
            except OSError:
                os.close(fd)
                raise
            if stat.S_ISREG(created_stat.st_mode):
                return fd
            os.close(fd)
            raise OSError(
                errno.ELOOP,
                f"created lock is not a regular file: {path}",
            )
        if expected_stat is None:
            try:
                expected_stat = os.stat(
                    leaf,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not flags & os.O_CREAT:
                    raise
                try:
                    fd = os.open(
                        leaf,
                        flags | os.O_EXCL,
                        mode,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    expected_stat = os.stat(
                        leaf,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                else:
                    try:
                        created_stat = os.fstat(fd)
                    except OSError:
                        os.close(fd)
                        raise
                    if stat.S_ISREG(created_stat.st_mode):
                        return fd
                    os.close(fd)
                    raise OSError(
                        errno.ELOOP,
                        f"created lock is not a regular file: {path}",
                    )
        fd = os.open(leaf, flags, mode, dir_fd=parent_fd)
        if not _lock_fd_matches_identity(fd, expected_stat):
            os.close(fd)
            raise OSError(
                errno.EAGAIN,
                f"registered lock identity changed while opening: {path}",
            )
        return fd
    finally:
        os.close(parent_fd)


def _verify_read_only_root(root: Path) -> None:
    if root.is_symlink():
        raise WorktreeSeatError(f"read-only worktree root must not be a symlink: {root}")
    try:
        if root.resolve(strict=False) != root:
            raise WorktreeSeatError(
                f"read-only worktree root resolves through a symlink: {root}"
            )
    except OSError as exc:
        raise WorktreeSeatError(
            f"read-only worktree root could not be resolved: {root}: {exc}"
        ) from exc


@contextmanager
def _read_only_allocation_lock(
    project_root: Path,
    *,
    timeout_s: float | None = None,
    deadline: float | None = None,
):
    started = time.monotonic()
    shared_deadline = deadline
    if deadline is None:
        budget = READ_ONLY_ALLOCATION_WAIT_S if timeout_s is None else max(0.0, timeout_s)
        deadline = time.monotonic() + budget
    else:
        budget = max(0.0, deadline - started)
    try:
        registry_root = _git_common_dir(
            project_root,
            # The git probe is a quick metadata read; it must not inherit the long lock wait.
            timeout=min(READ_ONLY_GIT_TIMEOUT_S, max(0.0, deadline - time.monotonic())),
            deadline=shared_deadline,
        )
    except WorktreeSeatError as exc:
        if "timed out" in str(exc).lower() or time.monotonic() >= deadline:
            raise WorktreeReadOnlyLockTimeout(
                "read-only allocation deadline expired while resolving Git common dir"
            ) from exc
        raise
    lock_root = registry_root / "goalflight-worktree-seat-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / "readonly-allocation.lock"
    try:
        fd = _open_registered_lock(
            lock_path,
            _lock_open_flags(),
            registry_root=registry_root,
            allow_create=True,
            allow_unregistered=True,
            registration_deadline=deadline,
        )
    except OSError as exc:
        if exc.errno == errno.ETIMEDOUT:
            raise WorktreeReadOnlyLockTimeout(
                f"read-only allocation lock wait expired after "
                f"{budget:g}s: {lock_path}; retry after it is released"
            ) from exc
        raise
    try:
        lock_file = os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    try:
        try:
            _flock_exclusive_until(
                lock_file.fileno(),
                deadline=deadline,
                description=f"read-only allocation lock {lock_path}",
            )
            _adopt_exclusive_lock(
                lock_path,
                lock_file.fileno(),
                registry_root=registry_root,
                deadline=deadline,
            )
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise WorktreeReadOnlyLockTimeout(
                    f"read-only allocation lock wait expired after "
                    f"{budget:g}s: "
                    f"{lock_path}; retry after it is released"
                ) from exc
            raise
        yield lock_file
    finally:
        lock_file.close()


def reap_read_only_worktrees(
    project_root: Path,
    *,
    requested_path: Path | None = None,
    deadline: float | None = None,
    process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
    fresh_process_cwd_census: Callable[[], list[tuple[int, str]] | None] | None = None,
) -> None:
    """Reap old detached checkouts under the ledger/allocator transaction."""
    project_root = project_root.resolve()
    shared_deadline = deadline is not None
    root = read_only_worktree_root(project_root)
    try:
        _verify_read_only_root(root)
        root_state = _path_presence(root)
        if root_state == "unknown":
            raise WorktreeSeatError(
                f"read-only worktree root could not be inspected: {root}"
            )
        if root_state == "absent":
            return
        root.mkdir(parents=True, exist_ok=True)
        _verify_read_only_root(root)
        import goalflight_worktree_gc

        # Lock order is allocation -> ledger. Resume holds the allocation
        # reservation while its child row is recorded; GC must never hold the
        # ledger lock while waiting for that reservation.
        with _read_only_allocation_lock(project_root, deadline=deadline):
            reap_deadline = deadline or (time.monotonic() + READ_ONLY_REAP_TIMEOUT_S)
            ledger_lock = goalflight_ledger.StateLock.try_acquire(reap_deadline)
            if ledger_lock is None:
                return
            try:
                ledger_index = goalflight_worktree_gc.ledger_index_for_dir(
                    goalflight_ledger.runs_dir(create=False), deadline=reap_deadline
                )
                ledger_stamp = _ledger_directory_stamp(
                    goalflight_ledger.runs_dir(create=False)
                )
            finally:
                ledger_lock.release()
            _reap_read_only_worktrees(
                project_root,
                root=root,
                requested_path=requested_path,
                deadline=reap_deadline,
                shared_deadline=shared_deadline,
                ledger_index=ledger_index,
                ledger_stamp=ledger_stamp,
                process_cwd_census=process_cwd_census,
                fresh_process_cwd_census=fresh_process_cwd_census,
            )
    except WorktreeReadOnlyLockTimeout:
        raise
    except (OSError, subprocess.SubprocessError, WorktreeSeatError):
        return


def shared_read_only_worktree(
    project_root: Path,
    *,
    base: str | None = None,
    reap: bool = True,
    _deadline: float | None = None,
) -> tuple[Path, str]:
    """Return a checkout shared by read-only dispatches at one commit."""
    deadline = _deadline or (time.monotonic() + READ_ONLY_REAP_TIMEOUT_S)
    project_root = project_root.resolve()
    _verify_project_root(project_root, timeout=_deadline_timeout(deadline))
    resolved_base = (
        base
        if base is not None
        else default_seat_base(project_root, timeout=_deadline_timeout(deadline))
    )
    if time.monotonic() >= deadline:
        raise WorktreeReadOnlyLockTimeout(
            "read-only allocation deadline expired before resolving base"
        )
    base_commit = _git(
        project_root,
        "rev-parse",
        "--verify",
        f"{resolved_base}^{{commit}}",
        timeout=_deadline_timeout(deadline),
    )
    root = read_only_worktree_root(project_root)
    _verify_read_only_root(root)
    ledger_index = None
    with contextlib.ExitStack() as locks:
        locks.enter_context(_read_only_allocation_lock(project_root))
        _exclude_managed_worktrees(
            project_root, timeout=_deadline_timeout(deadline),
        )
        root.mkdir(parents=True, exist_ok=True)
        _verify_read_only_root(root)
        # The queue wait above has its own budget; the allocation work gets a
        # fresh deadline once the lock is held so a long queue cannot starve it.
        deadline = time.monotonic() + READ_ONLY_REAP_TIMEOUT_S
        if reap:
            import goalflight_worktree_gc

            # Keep allocation outermost so a resume can hold it while its child
            # ledger row becomes durable without deadlocking the reaper.
            ledger_lock = goalflight_ledger.StateLock.try_acquire(deadline)
            if ledger_lock is None:
                raise WorktreeReadOnlyLockTimeout(
                    "read-only ledger lock wait expired; refusing reaping"
                )
            try:
                ledger_index = goalflight_worktree_gc.ledger_index_for_dir(
                    goalflight_ledger.runs_dir(create=False), deadline=deadline
                )
                ledger_stamp = _ledger_directory_stamp(
                    goalflight_ledger.runs_dir(create=False)
                )
            finally:
                ledger_lock.release()
        path = root / base_commit[:16]
        if reap and ledger_index is not None:
            _reap_read_only_worktrees(
                project_root,
                root=root,
                requested_path=path,
                deadline=deadline,
                ledger_index=ledger_index,
                ledger_stamp=ledger_stamp,
            )
        path_state = _path_presence(path)
        if path_state == "unknown":
            raise WorktreeSeatError(
                f"shared read-only worktree could not be inspected: {path}"
            )
        if path_state == "absent":
            if time.monotonic() >= deadline:
                raise WorktreeReadOnlyLockTimeout(
                    "read-only allocation deadline expired before worktree add"
                )
            _git(
                project_root,
                "worktree",
                "add",
                "--detach",
                str(path),
                base_commit,
                timeout=_deadline_timeout(deadline),
            )
        if time.monotonic() >= deadline:
            raise WorktreeReadOnlyLockTimeout(
                "read-only allocation deadline expired before checkout validation"
            )
        _verify_existing_seat(
            project_root,
            path,
            timeout=_deadline_timeout(deadline),
            deadline=deadline,
        )
        if time.monotonic() >= deadline:
            raise WorktreeReadOnlyLockTimeout(
                "read-only allocation deadline expired before HEAD validation"
            )
        actual = _git(
            path,
            "rev-parse",
            "HEAD^{commit}",
            timeout=_deadline_timeout(deadline),
        )
        if actual != base_commit:
            raise WorktreeSeatError(
                f"shared read-only worktree {path} is at {actual}, expected {base_commit}"
            )
        if time.monotonic() >= deadline:
            raise WorktreeReadOnlyLockTimeout(
                "read-only allocation deadline expired before cleanliness validation"
            )
        clean = check_seat_cleanliness(
            path, timeout=_deadline_timeout(deadline)
        )
        if clean["verdict"] != YES:
            raise WorktreeSeatError(
                f"shared read-only worktree {path} is not clean: {clean['reason']}"
            )
        try:
            os.utime(path, None)
        except OSError:
            pass
        return path, base_commit


def release_worktree_for_dispatch(
    project_root: Path,
    worktree_path: str | Path | None,
    dispatch_id: str,
    *,
    before_release: Callable[[Path], None] | None = None,
) -> tuple[bool, str]:
    """Clear stale occupant metadata after a terminal withdrawal.

    A live worker still holding the kernel descriptor is never interrupted.
    This only clears metadata from a lock that is already free and whose
    complete process cwd census shows no process in the seat.
    """
    if worktree_path is None or not str(worktree_path).strip():
        return False, "refusing release: no worktree seat path was recorded"
    raw_path = Path(str(worktree_path)).expanduser()
    if not raw_path.is_absolute():
        return False, f"refusing release: worktree seat path is not absolute: {worktree_path}"
    path = Path(os.path.realpath(str(raw_path)))
    root = Path(os.path.realpath(str(project_root)))
    if path == root:
        return False, f"refusing release: worktree seat path resolves to project root: {path}"
    if not path.is_dir():
        return False, f"refusing release: worktree seat path is unresolved: {path}"
    if not is_managed_worktree_path(path, project_root=project_root):
        return False, "path is not a managed repository worktree"
    verdict, reason, registered_lock_path, expected_stat = (
        _registered_pool_seat_lock_info(path, project_root=project_root)
    )
    if verdict != YES or registered_lock_path is None or expected_stat is None:
        return False, f"worktree lock unavailable: {reason}"
    try:
        handle = os.fdopen(
            _open_lock_path_safely(
                registered_lock_path,
                _lock_open_flags(),
                expected_stat=expected_stat,
            ),
            "r+",
            encoding="utf-8",
        )
    except OSError as exc:
        return False, f"worktree lock unavailable: {exc}"
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, "worker still holds the kernel worktree lease"
        payload = _lock_metadata(handle)
        if str(payload.get("dispatch_id") or "") not in {"", str(dispatch_id)}:
            return False, "worktree lock belongs to another dispatch"
        pid = payload.get("pid")
        token = payload.get("start_token")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(token, str)
            or not token
        ):
            return False, "refusing release: holder identity unknown (identity_unknown)"
        lock_live = _lock_identity_liveness(payload)
        if lock_live is True:
            return False, "refusing release: holder identity is live (identity_live)"
        if lock_live is None:
            return False, "refusing release: holder identity unknown (identity_unknown)"

        census_taken = False
        cwd_snapshot: list[tuple[int, str]] | None = None

        def process_cwd_census() -> list[tuple[int, str]] | None:
            nonlocal census_taken, cwd_snapshot
            if not census_taken:
                census_taken = True
                try:
                    cwd_snapshot = _process_cwd_snapshot(
                        timeout_s=PROCESS_CWD_SNAPSHOT_TIMEOUT_S
                    )
                except Exception:
                    cwd_snapshot = None
            return cwd_snapshot

        try:
            _validate_holder(
                path,
                str(dispatch_id),
                project_root=project_root,
                lock_metadata=payload,
                process_cwd_census=process_cwd_census,
                require_census_for_prelaunch=True,
            )
        except WorktreeSeatUnavailable as exc:
            return False, f"refusing release: {exc}"
        # Terminal prelaunch proof can skip the allocator census, but release
        # also protects the cleanup callback, so it always checks the cwd.
        if not census_taken:
            process_cwd_census()
        if _process_cwd_under_seat(cwd_snapshot, path) is not False:
            return False, "refusing release: process cwd census is occupied or unknown (worker_cwd_unknown)"
        if str(payload.get("dispatch_id") or "") == str(dispatch_id):
            child = goalflight_ledger.read_record(dispatch_id)
            if goalflight_ledger.prelaunch_without_worker_evidence(child):
                parent_id = str((child or {}).get("parent_dispatch_id") or "").strip()
                parent = goalflight_ledger.read_record(parent_id) if parent_id else None
                parent_path = (
                    (parent or {}).get("worker_cwd")
                    or (parent or {}).get("worktree_path")
                )
                parent_branch = str((parent or {}).get("worktree_branch") or "").strip()
                try:
                    actual_branch = _git(
                        path, "rev-parse", "--abbrev-ref", "HEAD"
                    ).strip()
                except WorktreeSeatError:
                    actual_branch = ""
                if (
                    parent_path
                    and Path(str(parent_path)).resolve(strict=False)
                    == path.resolve(strict=False)
                    and is_worktree_branch(parent_branch)
                    and actual_branch == parent_branch
                ):
                    _write_occupant(
                        handle,
                        seat_name=path.name,
                        dispatch_id=parent_id,
                        controller_label=(parent or {}).get("controller_label"),
                    )
                    return True, f"preserved parent worktree occupant {parent_id}"
                return False, (
                    "refusing release: never-launched child parent does not match "
                    "the checked-out worktree"
                )
        if before_release is not None:
            before_release(path)
        handle.seek(0)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        return True, "released stale worktree occupant"
    finally:
        handle.close()


def worktree_path_lock_path(target: Path) -> Path:
    """Return the per-worktree occupancy lock path.

    Git checkouts keep the lock inside the worktree's git dir so it is unique
    per tree and not an untracked file in the project. Non-git directories
    (test trees) fall back to a hidden file in the tree itself.
    """
    try:
        target = Path(os.path.realpath(str(target)))
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"worktree path {target} could not be resolved ({type(exc).__name__}: {exc})"
        ) from exc
    git_meta = target / ".git"
    try:
        if git_meta.is_file():
            text = git_meta.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.lower().startswith("gitdir:"):
                    git_dir = Path(line.split(":", 1)[1].strip())
                    if not git_dir.is_absolute():
                        git_dir = target / git_dir
                    return git_dir.resolve(strict=False) / OCCUPANCY_LOCK_NAME
        if git_meta.is_dir():
            return git_meta / OCCUPANCY_LOCK_NAME
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"worktree occupancy lock path for {target} could not be evaluated "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return target / f".{OCCUPANCY_LOCK_NAME}"


def _try_acquire_worktree_path_lock(
    target: Path,
    dispatch_id: str,
    *,
    shared: bool,
    deadline: float | None = None,
) -> WorktreePathLock:
    """Acquire a non-blocking shared or exclusive kernel lock on ``target``.

    Failure to acquire is occupancy: ``WorktreePathLockBusy``. Failure to
    evaluate the lock at all (unreadable path, fd exhaustion) is
    ``WorktreePathLockUnknown``. Exclusive callers must inherit the returned
    lock in the worker; read-only callers retain their shared handle in the
    controller lease.
    """
    try:
        resolved = Path(os.path.realpath(str(target)))
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"worktree path {target} could not be resolved ({type(exc).__name__}: {exc})"
        ) from exc
    if not resolved.is_dir():
        raise WorktreePathLockUnknown(
            f"worktree path {resolved} is not a readable directory"
        )
    lock_path = worktree_path_lock_path(resolved)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"cannot create occupancy lock directory {lock_path.parent} ({exc})"
        ) from exc
    try:
        registry_root = None
        git_metadata = resolved / ".git"
        git_backed = git_metadata.is_file() or git_metadata.is_dir()
        try:
            registry_root = _git_common_dir(
                resolved,
                timeout=(
                    None
                    if deadline is None
                    else _deadline_timeout(deadline)
                ),
            )
        except WorktreeSeatError as exc:
            if git_backed:
                raise WorktreePathLockUnknown(
                    f"cannot resolve lock registration for Git worktree {resolved}: {exc}"
                ) from exc
        # Git-backed trees use the durable lock identity discipline. Existing
        # pre-registry locks are tolerated on open and adopted after an
        # exclusive writer lock; non-Git trees have no independent registration
        # root and retain the old fail-closed safe walk.
        if registry_root is None:
            lock_fd = _open_lock_path_safely(
                lock_path,
                _lock_open_flags(),
                expected_stat=_lock_path_identity(lock_path),
            )
        else:
            lock_fd = _open_registered_lock(
                lock_path,
                _lock_open_flags(),
                registry_root=registry_root,
                allow_create=True,
                allow_unregistered=True,
                registration_deadline=deadline,
            )
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"cannot open worktree occupancy lock {lock_path}: {exc}"
        ) from exc
    lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
    try:
        lock_mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(lock_file.fileno(), lock_mode | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        payload = _lock_metadata(lock_file)
        occupant_id = str(payload.get("dispatch_id") or "unknown-dispatch")
        pid = payload.get("pid")
        lock_file.close()
        held = f" (kernel lock held pid={pid})" if isinstance(pid, int) else " (kernel lock held)"
        raise WorktreePathLockBusy(
            f"worktree {resolved} is already owned by non-terminal dispatch "
            f"{occupant_id}{held}; a second writer would share one filesystem "
            "tree with no merge discipline",
            occupant_id=occupant_id,
        ) from exc
    except OSError as exc:
        lock_file.close()
        raise WorktreePathLockUnknown(
            f"worktree occupancy lock of {resolved} could not be evaluated "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    try:
        if registry_root is not None and not shared:
            try:
                _adopt_exclusive_lock(
                    lock_path,
                    lock_file.fileno(),
                    registry_root=registry_root,
                    deadline=deadline,
                )
            except OSError as exc:
                raise WorktreePathLockUnknown(
                    f"worktree occupancy lock identity of {resolved} could not be adopted "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
        if not shared:
            os.set_inheritable(lock_file.fileno(), True)
            _write_occupant(
                lock_file, seat_name=resolved.name, dispatch_id=dispatch_id
            )
        return WorktreePathLock(
            path=resolved,
            lock_file=lock_file,
            dispatch_id=dispatch_id,
        )
    except BaseException:
        lock_file.close()
        raise


def try_acquire_worktree_path_lock(target: Path, dispatch_id: str) -> WorktreePathLock:
    """Acquire an exclusive, non-blocking kernel lock on ``target``."""
    return _try_acquire_worktree_path_lock(target, dispatch_id, shared=False)
