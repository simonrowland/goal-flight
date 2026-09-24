#!/usr/bin/env python3
"""Fixed, kernel-leased pool of reusable local Git worktrees."""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import TextIO

import goalflight_compat
import goalflight_ledger


WORKTREES_PER_REPO_ENV = "GOALFLIGHT_WORKTREES_PER_REPO"
# Deprecated compatibility alias. Keep the symbol and on-disk lock names for
# mixed-version controllers during ``goalflight update`` windows.
WORKTREE_SEATS_ENV = "GOALFLIGHT_WORKTREE_SEATS"
WORKTREE_LOCK_FD_ENV = "GOALFLIGHT_WORKTREE_LOCK_FD"
OCCUPANCY_LOCK_FD_ENV = "GOALFLIGHT_OCCUPANCY_LOCK_FD"
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
_SAFE_RING_LABEL = re.compile(r"[A-Za-z0-9._-]+")

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


class WorktreeSeatResetRefused(WorktreeSeatError):
    """Raised when resetting a free worktree would lose unique or undetermined work."""


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
    """Exclusive kernel lock on an arbitrary worktree path.

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
        controller_label: str | None = None,
    ) -> None:
        self.path = path
        self.seat_name = seat_name
        self.dispatch_id = dispatch_id
        self.quarantine_branch = quarantine_branch
        self.branch = branch
        self.keep_ref = keep_ref
        self.controller_label = controller_label
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

    def __enter__(self) -> "WorktreeSeatLease":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def configured_worktree_seats() -> int:
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


def inherited_worktree_lock_fds() -> tuple[int, ...]:
    """Return validated inherited worktree-lock and occupancy-lock descriptors."""
    fds: list[int] = []
    errors: list[str] = []
    for env_name in (WORKTREE_LOCK_FD_ENV, OCCUPANCY_LOCK_FD_ENV):
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
    both the pooled-worktree fd and the occupancy fd. A parent that acquired a
    *new* worktree puts the fd in the child env dict without exporting it on
    itself; that fd still has to be in ``pass_fds`` or the helper exec closes
    it and the seat frees while the worker runs. Occupancy is usually
    exported on the parent; the env-dict lookup still covers a child env
    that names an occupancy fd the parent has not exported.

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
    for env_name in (WORKTREE_LOCK_FD_ENV, OCCUPANCY_LOCK_FD_ENV):
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


def default_seat_base(project_root: Path) -> str:
    """Project default ref: ``origin/main`` when it exists, else ``HEAD``."""
    proc = _git_proc(project_root, "rev-parse", "--verify", "origin/main^{commit}")
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


def is_managed_worktree_path(path: str | Path, *, project_root: Path) -> bool:
    """Recognize new repo-wide paths and legacy label-ring paths."""
    try:
        resolved = Path(path).resolve()
        root = repository_worktree_root(project_root).resolve()
        rel = resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    parts = rel.parts
    if len(parts) == 1:
        return is_captive_seat_name(parts[0])
    return len(parts) == 2 and is_captive_seat_name(parts[1])


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


def registered_pool_seat_verdict(
    path: str | Path,
    *,
    project_root: Path,
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
    try:
        root = project_root.resolve()
    except OSError as exc:
        return "unknown", f"project root unresolvable ({exc})"
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        return "unknown", f"worktree path unresolvable ({exc})"

    managed_root = root / "worktrees"
    try:
        managed_root = managed_root.resolve()
    except OSError as exc:
        return "unknown", f"managed worktree root unresolvable ({exc})"

    try:
        rel = resolved.relative_to(managed_root)
    except ValueError:
        return (
            "no",
            f"{resolved} is not under the managed worktree root {managed_root}",
        )
    except OSError as exc:
        return "unknown", f"managed worktree path could not be compared ({exc})"

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
            return "no", f"{resolved.name} is not a pool worktree name"
    elif len(parts) == 2:
        seat_name = pool_seat_name(parts[1])
        prefix = CAPTIVE_SEAT_PREFIX
        if seat_name is None or _slot_from_seat_name(seat_name, prefix) is None:
            return "no", f"{resolved.name} is not a pool worktree name"
        lock_subdir = parts[0]
    else:
        return (
            "no",
            f"{resolved} is not a managed worktree path under {managed_root}",
        )

    try:
        seat_limit = configured_worktree_seats()
    except WorktreeSeatError as exc:
        return "unknown", f"worktree configuration unreadable ({exc})"

    slot = _slot_from_seat_name(seat_name, prefix)
    if slot is None:
        return (
            "no",
            f"{seat_name} is not a valid managed worktree id",
        )

    try:
        lock_root = _git_common_dir(root) / "goalflight-worktree-seat-locks"
    except WorktreeSeatError as exc:
        return "unknown", f"worktree lock directory unreadable ({exc})"
    if lock_subdir:
        lock_root = lock_root / lock_subdir

    lock_path = lock_root / f"{seat_name}.lock"
    try:
        if lock_root.is_symlink():
            return "unknown", f"worktree lock root is a symlink ({lock_root})"
        st = os.lstat(lock_path)
    except FileNotFoundError:
        return (
            "no",
            f"no worktree lock for {seat_name}; path is not a registered pool worktree",
        )
    except OSError as exc:
        return "unknown", f"worktree lock unreadable for {seat_name} ({exc})"

    if stat.S_ISLNK(st.st_mode):
        return "unknown", f"worktree lock is a symlink ({lock_path})"
    if not stat.S_ISREG(st.st_mode):
        return "unknown", f"worktree lock is not a regular file ({lock_path})"
    return "yes", f"registered pool worktree {seat_name}"


def _git(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = _git_proc(cwd, *args, input_text=input_text, env=env)
    if result is None:
        raise WorktreeSeatError(f"git {' '.join(args)} could not run in {cwd}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise WorktreeSeatError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result.stdout.strip()


def _git_identity(cwd: Path) -> tuple[str, str, str] | None:
    """Return realpath git-dir, common-dir, and worktree top-level."""
    try:
        result = subprocess.run(
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


def guard_worktree_mutation(cwd: Path, *args: str) -> str | None:
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

    source = _git_identity(cwd)
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
        target_identity = _git_identity(Path(target_real)) if Path(target_real).is_dir() else None
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
) -> subprocess.CompletedProcess[str] | None:
    guard_error = guard_worktree_mutation(cwd, *args)
    if guard_error is not None:
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", guard_error
        )
    try:
        return subprocess.run(
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


def _git_common_dir(cwd: Path) -> Path:
    raw = Path(_git(cwd, "rev-parse", "--git-common-dir"))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def _git_dir(cwd: Path) -> Path:
    raw = Path(_git(cwd, "rev-parse", "--git-dir"))
    return (raw if raw.is_absolute() else cwd / raw).resolve()


def is_captive_seat_name(name: str) -> bool:
    """True when ``name`` is a captive ``s-N`` seat."""
    return _slot_from_seat_name(name, CAPTIVE_SEAT_PREFIX) is not None


def _seat_lock_root(project_root: Path, *, controller_label: str | None = None) -> Path:
    root = _git_common_dir(project_root.resolve()) / "goalflight-worktree-seat-locks"
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
        if direct.exists() or not legacy.exists():
            return _seat_lock_root(project_root) / f"{seat_name}.lock"
        return _seat_lock_root(project_root, controller_label=controller_label) / f"{seat_name}.lock"
    return _seat_lock_root(project_root) / f"{seat_name}.lock"


def _ring_state_path(lock_root: Path) -> Path:
    return lock_root / "ring.json"


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


def _porcelain_relpaths(line: str) -> list[str]:
    text = line.rstrip("\n")
    if len(text) < 4:
        return []
    rest = text[3:]
    if text[0] not in {" ", "?"} and text[1] == " ":
        rest = text[2:]
    if " -> " in rest:
        return [part.replace("\\", "/").strip() for part in rest.split(" -> ", 1)]
    return [rest.replace("\\", "/").strip()]


def _porcelain_is_product(line: str) -> bool:
    paths = _porcelain_relpaths(line)
    if not paths:
        return bool(line.strip())
    if line[:2] != "??":
        return True
    return any(not is_reserved_seat_notes_path(path) for path in paths)


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
    lock_file.truncate()
    json.dump(payload, lock_file, sort_keys=True)
    lock_file.write("\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _clear_occupant(lock_file: TextIO) -> None:
    """Remove a partial holder record after a failed transactional bind."""
    try:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.flush()
        os.fsync(lock_file.fileno())
    except OSError:
        # The kernel lock still closes in the caller's finally block. A failed
        # cleanup must not hide the original bind error.
        pass


def _lock_metadata(lock_file: TextIO) -> dict:
    try:
        lock_file.seek(0)
        payload = json.load(lock_file)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _occupant_description(lock_file: TextIO, seat_name: str) -> str:
    payload = _lock_metadata(lock_file)
    return _holder_description(seat_name, payload)


def _holder_record(dispatch_id: str) -> tuple[dict, bool | None]:
    """Read worker evidence, never the allocator PID from lock metadata."""
    record = goalflight_ledger.read_record(dispatch_id)
    if not record or goalflight_ledger.record_is_unreadable(record):
        return {}, None
    record = dict(record)
    status_path = record.get("status_path")
    if status_path:
        try:
            status = json.loads(Path(status_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return record, None
        if not isinstance(status, dict) or status.get("dispatch_id") != dispatch_id:
            return record, None
        for key in ("state", "worker_pid", "wrapper_pid"):
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
        return record, None
    pid = record.get("worker_pid")
    token = identity.get("start_token")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not token:
        return record, None
    if identity.get("pid", pid) != pid:
        return record, None
    return record, goalflight_compat.process_identity_matches(pid, token)


def _holder_description(name: str, metadata: dict) -> str:
    dispatch_id = str(metadata.get("dispatch_id") or "unknown-dispatch")
    if Path(name).is_absolute() and Path(name).is_dir():
        branch = _git_proc(Path(name), "rev-parse", "--abbrev-ref", "HEAD")
        if branch is not None and branch.returncode == 0:
            for prefix in (WORKTREE_BRANCH_PREFIX, SEAT_BRANCH_PREFIX):
                if branch.stdout.strip().startswith(prefix + "/"):
                    branch_id = branch.stdout.strip()[len(prefix) + 1:]
                    if branch_id != dispatch_id:
                        return (
                            _holder_description(Path(name).name, {"dispatch_id": branch_id})
                            + " / lock holder: "
                            + _holder_description(Path(name).name, metadata)
                        )
                    break
    record, live = _holder_record(dispatch_id)
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


def _validate_holder(worktree_path: Path, prior_dispatch_id: str) -> str:
    """Existing branch names identify holders; directory labels never do."""
    metadata_dispatch_id = prior_dispatch_id
    branch = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    for prefix in (WORKTREE_BRANCH_PREFIX, SEAT_BRANCH_PREFIX):
        if branch.startswith(prefix + "/"):
            prior_dispatch_id = branch[len(prefix) + 1:]
            break
    holders = {prior_dispatch_id, metadata_dispatch_id} - {"unknown-dispatch"}
    if not holders:
        raise WorktreeSeatUnavailable(f"worktree {worktree_path} has unknown ownership")
    for holder in holders:
        # Resumes keep the original branch but update lock ownership. Both
        # identities must be settled before a later dispatch may reset it.
        record, live = _holder_record(holder)
        state = str(record.get("state") or "")
        terminal = goalflight_ledger.terminal_state_for(state, record.get("reason"))
        if state != "cancelled" and terminal in {"", "unknown", "watcher_stopped"}:
            raise WorktreeSeatUnavailable(
                _holder_description(worktree_path.name, {"dispatch_id": holder})
            )
        # A terminal dispatch with an explicit pre-worker launch failure has
        # no process identity to probe. That is proven non-launch, unlike a
        # started worker whose identity is unreadable (UNKNOWN, retain).
        if live is True or (live is None and record.get("prelaunch_failure") is not True):
            raise WorktreeSeatUnavailable(
                _holder_description(worktree_path.name, {"dispatch_id": holder})
            )
    return prior_dispatch_id


def _refnames(cwd: Path) -> tuple[list[str] | None, str]:
    proc = _git_proc(cwd, "for-each-ref", "--format=%(refname)")
    if proc is None:
        return None, "git for-each-ref could not run"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return None, detail
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()], ""


def check_reset_preserves_commits(
    cwd: Path,
    *,
    start: str,
    base_commit: str,
    moving_ref: str | None,
) -> dict[str, str]:
    """YES if moving ``start`` to ``base_commit`` would not lose unique commits.

    ``moving_ref`` is the full refname that ``checkout -B`` / ``worktree add -B``
    will force-move. That ref is excluded from the keep-set. Detached HEAD
    passes ``moving_ref=None``: nothing currently names those commits.
    UNKNOWN retains — losing a commit is irreversible.
    """
    refs, err = _refnames(cwd)
    if refs is None:
        return _condition(
            UNKNOWN,
            f"cannot list refs ({err}); unique commits are unknown",
        )
    exclude = [base_commit]
    for ref in refs:
        if moving_ref and ref == moving_ref:
            continue
        exclude.append(ref)
    proc = _git_proc(cwd, "rev-list", "--oneline", start, "--not", *exclude)
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


def check_seat_cleanliness(worktree_path: Path) -> dict[str, str]:
    """YES clean / NO dirty / UNKNOWN. Same three-state as worktree GC check_clean."""
    proc = _git_proc(
        worktree_path, "status", "--porcelain=v1", "--untracked-files=all"
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
    dirty = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and _porcelain_is_product(line)
    ]
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
) -> dict[str, str | None]:
    """Durably pin commits that a reset would otherwise make unreachable."""
    refs, err = _refnames(worktree_path)
    if refs is None:
        return {"verdict": UNKNOWN, "reason": f"cannot list refs ({err})", "keep_ref": None}
    head_proc = _git_proc(worktree_path, "rev-parse", "HEAD^{commit}")
    if head_proc is None or head_proc.returncode != 0:
        return {"verdict": UNKNOWN, "reason": "cannot resolve worktree HEAD", "keep_ref": None}
    head = head_proc.stdout.strip()
    exclude = [base_commit]
    for ref in refs:
        if moving_ref and ref == moving_ref:
            continue
        exclude.append(ref)
    unique = _git_proc(worktree_path, "rev-list", head, "--not", *exclude)
    if unique is None or unique.returncode != 0:
        return {
            "verdict": UNKNOWN,
            "reason": "cannot enumerate unique commits",
            "keep_ref": None,
        }
    if not [line for line in unique.stdout.splitlines() if line.strip()]:
        return {"verdict": YES, "reason": "no unique commits need pinning", "keep_ref": None}

    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(worktree_id)).strip(".-") or "worktree"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    keep_ref = f"refs/{KEEP_REF_PREFIX}/{stamp}-{safe_id}-{head[:12]}"
    updated = _git_proc(worktree_path, "update-ref", keep_ref, head, "")
    if updated is None or updated.returncode != 0:
        return {
            "verdict": UNKNOWN,
            "reason": "cannot durably create keep ref",
            "keep_ref": None,
        }
    verified = _git_proc(worktree_path, "rev-parse", "--verify", f"{keep_ref}^{{commit}}")
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
    abbrev_proc = _git_proc(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
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

    head_proc = _git_proc(worktree_path, "rev-parse", "HEAD")
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
            worktree_path, start=head, base_commit=base_commit, moving_ref=None
        )
    elif abbrev == new_branch:
        commits = check_reset_preserves_commits(
            worktree_path,
            start=head,
            base_commit=base_commit,
            moving_ref=f"refs/heads/{new_branch}",
        )
    else:
        commits = _condition(
            YES,
            f"branch {abbrev!r} remains after checkout of {new_branch!r}",
        )

    clean = check_seat_cleanliness(worktree_path)
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


def _create_seat_worktree(
    project_root: Path,
    worktree_path: Path,
    *,
    branch: str,
    base_commit: str,
) -> None:
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
    if exists.returncode == 0:
        commits = check_reset_preserves_commits(
            project_root,
            start=ref,
            base_commit=base_commit,
            moving_ref=ref,
        )
        if commits["verdict"] != YES:
            raise WorktreeSeatResetRefused(
                f"refusing to reset {branch}: {commits['reason']}"
            )
        _git(
            project_root,
            "worktree",
            "add",
            "-B",
            branch,
            str(worktree_path),
            base_commit,
        )
        return
    _git(
        project_root,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree_path),
        base_commit,
    )


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


def _prepare_seat_checkout(
    worktree_path: Path, *, branch: str, base_commit: str
) -> None:
    current_branch = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    current_head = _git(worktree_path, "rev-parse", "HEAD")
    tracked_status = _git(
        worktree_path, "status", "--porcelain", "--untracked-files=no"
    )
    if current_branch != branch or current_head != base_commit or tracked_status:
        _git(worktree_path, "checkout", "-f", "-B", branch, base_commit)
    # Never ``git clean -fdx``. Preserve the reserved notes namespace even
    # when a temp repo has not gitignored ``.goal-flight/``.
    _git(worktree_path, "clean", "-fd", "-e", ".goal-flight")


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
    _git(cwd, "update-ref", ref, commit, old)
    verified = _git_proc(cwd, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if verified is None or verified.returncode != 0 or verified.stdout.strip() != commit:
        raise WorktreeSeatResetRefused(
            f"ref {ref} did not verify after creation; refusing reset"
        )


def _quarantine_dirty_worktree(
    worktree_path: Path,
    *,
    seat_name: str,
    abandoned_dispatch_id: str,
) -> str | None:
    dirty = _git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all")
    product = [
        line for line in dirty.splitlines() if line.strip() and _porcelain_is_product(line)
    ]
    staged_and_worktree = [
        line
        for line in product
        if len(line) >= 2
        and line[0] not in {" ", "?"}
        and line[1] not in {" ", "?"}
    ]
    if staged_and_worktree:
        raise WorktreeSeatResetRefused(
            f"dirty worktree {seat_name} has separate staged and working versions "
            "that cannot be represented by one quarantine commit; refusing reset"
        )

    dirty_paths = list(
        dict.fromkeys(path for line in product for path in _porcelain_relpaths(line))
    )
    if dirty_paths:
        attributes = _git(
            worktree_path,
            "check-attr",
            "--stdin",
            "filter",
            input_text="\n".join(dirty_paths) + "\n",
        )
        for line in attributes.splitlines():
            fields = line.rsplit(": ", 2)
            if len(fields) != 3 or fields[1] != "filter":
                continue
            filter_name = fields[2].strip()
            if filter_name in {"", "unspecified", "unset"}:
                continue
            raise WorktreeSeatResetRefused(
                f"dirty worktree {seat_name} path {fields[0]} uses active filter "
                f"{filter_name!r}; refusing reset"
            )
    if not product:
        return None

    # Seed the temporary index from the real index so staged content, including
    # force-added ignored files, is preserved. `git add -A` adds other dirty
    # paths; ignored untracked notes are intentionally left out of the tree.
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
        _git(worktree_path, "add", "-A", "--", ".", env=index_env)
        tree = _git(worktree_path, "write-tree", env=index_env)
    parent = _git(worktree_path, "rev-parse", "HEAD")
    parent_tree = _git(worktree_path, "rev-parse", "HEAD^{tree}")
    if tree == parent_tree:
        raise WorktreeSeatError(
            f"dirty worktree {seat_name} cannot be represented by a branch commit; refusing reset"
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
            f"quarantine commit for dirty worktree {seat_name} is empty; refusing reset"
        )
    _update_ref_and_verify(
        worktree_path, f"refs/heads/{branch}", commit, old=""
    )
    dirty_ref = f"refs/{KEEP_REF_PREFIX}/{abandoned_dispatch_id}/dirty-{stamp}"
    _update_ref_and_verify(worktree_path, dirty_ref, commit, old="")
    tree_paths = set(
        _git(worktree_path, "ls-tree", "-r", "--name-only", dirty_ref).splitlines()
    )
    missing_paths = sorted(set(dirty_paths) - tree_paths)
    if missing_paths:
        raise WorktreeSeatResetRefused(
            f"dirty ref {dirty_ref} is missing status paths {missing_paths!r}; "
            "refusing reset"
        )
    return branch


def _prepare_claimed_seat(**kwargs) -> WorktreeSeatLease:
    """Exclude writers using the path lock before any checkout or reset."""
    path = kwargs["worktree_path"]
    if path.exists() and kwargs["reset"]:
        try:
            occupancy = try_acquire_worktree_path_lock(path, kwargs["dispatch_id"])
        except (WorktreePathLockBusy, WorktreePathLockUnknown) as exc:
            raise WorktreeSeatUnavailable(str(exc)) from exc
        with occupancy:
            return _prepare_claimed_seat_locked(**kwargs)
    return _prepare_claimed_seat_locked(**kwargs)


def _prepare_claimed_seat_locked(
    *,
    project_root: Path,
    worktree_path: Path,
    seat_name: str,
    lock_file: TextIO,
    dispatch_id: str,
    prior_dispatch_id: str,
    branch: str,
    base_commit: str,
    reset: bool,
    controller_label: str | None = None,
) -> WorktreeSeatLease:
    existing = worktree_path.exists() or worktree_path.is_symlink()
    safety: dict | None = None
    if existing:
        _verify_existing_seat(project_root, worktree_path)
        if reset:
            prior_dispatch_id = _validate_holder(worktree_path, prior_dispatch_id)
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
    if not existing:
        _create_seat_worktree(
            project_root,
            worktree_path,
            branch=branch,
            base_commit=base_commit,
        )
        _verify_existing_seat(project_root, worktree_path)
    if not reset:
        actual = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
        try:
            _write_occupant(
                lock_file,
                seat_name=seat_name,
                dispatch_id=dispatch_id,
                controller_label=controller_label,
            )
        except BaseException:
            _clear_occupant(lock_file)
            raise
        return WorktreeSeatLease(
            path=worktree_path,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            lock_file=lock_file,
            quarantine_branch=None,
            branch=actual,
            controller_label=controller_label,
        )
    keep_ref = None
    if safety is not None and safety["conditions"]["commits_preserved"]["verdict"] == NO:
        pinned = pin_unique_commits(
            worktree_path,
            base_commit=base_commit,
            moving_ref=safety.get("moving_ref"),
            worktree_id=seat_name,
        )
        if pinned["verdict"] != YES:
            raise WorktreeSeatResetRefused(
                f"refusing to reset worktree {seat_name}: {pinned['reason']}"
            )
        keep_ref = str(pinned.get("keep_ref") or "") or None
    if existing and reset:
        keep_ref = f"refs/{KEEP_REF_PREFIX}/{prior_dispatch_id}/head"
        head = _git(worktree_path, "rev-parse", "HEAD")
        previous = _git_proc(worktree_path, "rev-parse", "--verify", "--quiet", keep_ref)
        if previous is None or previous.returncode not in (0, 1):
            raise WorktreeSeatResetRefused(f"cannot inspect saved head {keep_ref}")
        if previous.returncode == 0 and previous.stdout.strip() != head:
            raise WorktreeSeatResetRefused(f"refusing to overwrite saved head {keep_ref}")
        if previous.returncode != 0:
            _update_ref_and_verify(worktree_path, keep_ref, head, old="")
        else:
            verified = _git_proc(
                worktree_path, "rev-parse", "--verify", f"{keep_ref}^{{commit}}"
            )
            if verified is None or verified.returncode != 0 or verified.stdout.strip() != head:
                raise WorktreeSeatResetRefused(
                    f"saved head {keep_ref} did not verify; refusing reset"
                )
    try:
        quarantine_branch = _quarantine_dirty_worktree(
            worktree_path,
            seat_name=seat_name,
            abandoned_dispatch_id=prior_dispatch_id,
        )
    except WorktreeSeatResetRefused:
        raise
    except (WorktreeSeatError, OSError) as exc:
        raise WorktreeSeatResetRefused(
            f"cannot quarantine dirty worktree {seat_name}: {exc}"
        ) from exc
    _prepare_seat_checkout(worktree_path, branch=branch, base_commit=base_commit)
    remaining = _git(
        worktree_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    leftover = [
        line
        for line in remaining.splitlines()
        if line.strip() and _porcelain_is_product(line)
    ]
    if leftover:
        raise WorktreeSeatError(
            f"worktree {seat_name} is not clean after acquire-time reset"
        )
    actual_branch = _assert_seat_on_named_branch(
        worktree_path, seat_name=seat_name, branch=branch
    )
    try:
        _write_occupant(
            lock_file,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            controller_label=controller_label,
        )
    except BaseException:
        _clear_occupant(lock_file)
        raise
    return WorktreeSeatLease(
        path=worktree_path,
        seat_name=seat_name,
        dispatch_id=dispatch_id,
        lock_file=lock_file,
        quarantine_branch=quarantine_branch,
        branch=actual_branch,
        keep_ref=keep_ref,
        controller_label=controller_label,
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
            if lock_path.is_file():
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


def _busy_worktree_message(
    project_root: Path,
    limit: int,
    occupants: list[tuple[str, dict]],
) -> str:
    ordered = sorted(
        occupants,
        key=lambda item: str(item[1].get("acquired_at") or "9999"),
    )
    oldest = ", ".join(
        _holder_description(name, payload)
        for name, payload in ordered
    ) or "none recorded"
    return (
        f"{len(occupants)}/{limit} worktrees busy in {project_root.name}; "
        f"oldest holders: {oldest}"
    )


def _acquire_allocation_lock(
    allocation_file: TextIO,
    allocation_lock_path: Path,
    *,
    deadline: float | None,
) -> None:
    """Acquire the pool transaction lock without overrunning a wait budget."""
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
                    f"seat wait expired while waiting for worktree allocation lock "
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
    base: str | None = None,
    managed_root: Path | None = None,
    controller_label: str | None = None,
    reset: bool = True,
    occupy_path: Path | None = None,
    expected_prior_dispatch_id: str | None = None,
    capacity_deadline: float | None = None,
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
    branch = seat_branch_name(dispatch_id)

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
    if managed_root.exists() and not managed_root.is_dir():
        raise WorktreeSeatError(f"managed worktree root is not a directory: {managed_root}")

    lock_root = _seat_lock_root(project_root)
    if lock_root.is_symlink():
        raise WorktreeSeatError(f"worktree lock root must not be a symlink: {lock_root}")
    if lock_root.exists() and not lock_root.is_dir():
        raise WorktreeSeatError(f"worktree lock root is not a directory: {lock_root}")
    lock_root.mkdir(parents=True, exist_ok=True)

    flags = _lock_open_flags()
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
        _acquire_allocation_lock(
            allocation_file,
            allocation_lock_path,
            deadline=capacity_deadline,
        )

        # Count every held global or legacy-ring lock before any checkout/reset
        # or directory creation. Legacy rings are migration input, not extra
        # capacity, so a full set of old holders must refuse immediately.
        global_candidates = [
            (
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            for slot in range(1, seat_limit + 1)
        ]
        legacy_candidates = _legacy_ring_candidates(project_root)
        capacity_occupants: list[tuple[str, dict]] = []
        capacity_occupied_paths: set[Path] = set()
        probe_flags = flags & ~os.O_CREAT

        for candidate_path, candidate_lock in [*global_candidates, *legacy_candidates]:
            if not candidate_lock.is_file():
                continue
            try:
                probe_fd = os.open(candidate_lock, probe_flags, 0o600)
            except OSError:
                continue
            probe_file = os.fdopen(probe_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(probe_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                resolved_candidate = candidate_path.resolve(strict=False)
                if resolved_candidate not in capacity_occupied_paths:
                    capacity_occupied_paths.add(resolved_candidate)
                    capacity_occupants.append(
                        (str(candidate_path), _lock_metadata(probe_file))
                    )
            finally:
                probe_file.close()

        if len(capacity_occupants) >= seat_limit:
            detail = _busy_worktree_message(
                project_root, seat_limit, capacity_occupants
            )
            raise WorktreeSeatUnavailable(
                f"{detail}; refusing to create a new unmanaged worktree"
            )
        if occupy_path is None:
            managed_root.mkdir(parents=True, exist_ok=True)

        if occupy_path is not None:
            worktree_path = Path(occupy_path).expanduser().resolve(strict=False)
            if not worktree_path.exists():
                raise WorktreeCwdRefused(
                    f"refusing to create missing --cwd {worktree_path}"
                )
            seat_name = worktree_path.name
            if not is_captive_seat_name(seat_name) and _slot_from_seat_name(
                seat_name, WORKTREE_SEAT_PREFIX
            ) is None:
                raise WorktreeCwdRefused(
                    f"--cwd {worktree_path} is not a managed repository worktree in "
                    f"{managed_root}; pass --in-place for the project root"
                )
            if not is_managed_worktree_path(worktree_path, project_root=project_root):
                raise WorktreeCwdRefused(
                    f"--cwd {worktree_path} is not in the repository worktree pool"
                )
            lock_path = _candidate_lock_path(
                project_root, worktree_path, managed_root=managed_root
            )
            try:
                lock_fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise WorktreeSeatError(
                    f"cannot open worktree lock {lock_path}: {exc}"
                ) from exc
            lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                occupant = _occupant_description(lock_file, seat_name)
                lock_file.close()
                raise WorktreeSeatUnavailable(
                    f"worktree {seat_name} is held: {occupant}; "
                    "refusing to git worktree add a new unmanaged path"
                )
            try:
                prior_dispatch_id = str(
                    _lock_metadata(lock_file).get("dispatch_id") or "unknown-dispatch"
                )
                if (
                    expected_prior_dispatch_id is not None
                    and prior_dispatch_id != expected_prior_dispatch_id
                ):
                    raise WorktreeSeatUnavailable(
                        f"resume refused: worktree {seat_name} was reclaimed by "
                        f"{prior_dispatch_id}; expected recorded holder "
                        f"{expected_prior_dispatch_id}; refusing to reset or recreate it"
                    )
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
                )
            except BaseException:
                lock_file.close()
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

        def try_candidate(
            worktree_path: Path,
            lock_path: Path,
            *,
            require_ancestor: bool = False,
        ):
            try:
                lock_fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise WorktreeSeatError(
                    f"cannot open worktree lock {lock_path}: {exc}"
                ) from exc
            lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), _lock_metadata(lock_file)))
                lock_file.close()
                return None
            try:
                if require_ancestor and _seat_head_is_ancestor(
                    project_root,
                    _seat_head_from_metadata(worktree_path),
                    base_commit,
                ) is not True:
                    lock_file.close()
                    return None
                seat_name = worktree_path.name
                prior_dispatch_id = str(
                    _lock_metadata(lock_file).get("dispatch_id") or "unknown-dispatch"
                )
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
                )
            except WorktreeSeatResetRefused as exc:
                refused.append(f"{seat_name}: {exc}")
                lock_file.close()
                return None
            except WorktreeSeatUnavailable:
                resolved_path = worktree_path.resolve(strict=False)
                if resolved_path not in occupied_paths:
                    occupied_paths.add(resolved_path)
                    occupants.append((str(worktree_path), _lock_metadata(lock_file)))
                lock_file.close()
                return None
            except BaseException:
                lock_file.close()
                raise

        slots = list(range(1, hwm + 1))
        heads = {
            slot: _seat_head_from_metadata(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}"
            )
            for slot in slots
        }
        exact_slots = [slot for slot in slots if heads[slot] == base_commit]
        other_slots = [slot for slot in slots if slot not in exact_slots]

        for slot in exact_slots:
            lease = try_candidate(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            if lease is not None:
                return lease

        ancestor_checks = 0
        for slot in other_slots:
            if ancestor_checks >= 8:
                break
            ancestor_checks += 1
            lease = try_candidate(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
                require_ancestor=True,
            )
            if lease is not None:
                return lease

        for slot in other_slots:
            lease = try_candidate(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{slot}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{slot}.lock",
            )
            if lease is not None:
                return lease

        for worktree_path, lock_path in legacy_candidates:
            lease = try_candidate(worktree_path, lock_path)
            if lease is not None:
                return lease

        busy = len(occupants)
        if busy >= seat_limit:
            detail = _busy_worktree_message(project_root, seat_limit, occupants)
            raise WorktreeSeatUnavailable(
                f"{detail}; refusing to create a new unmanaged worktree"
            )

        def create_next_slot() -> WorktreeSeatLease | None:
            nonlocal hwm
            if hwm >= seat_limit:
                return None
            hwm += 1
            _write_ring_hwm(lock_root, hwm)
            return try_candidate(
                managed_root / f"{CAPTIVE_SEAT_PREFIX}{hwm}",
                lock_root / f"{CAPTIVE_SEAT_PREFIX}{hwm}.lock",
            )

        while hwm < seat_limit and len(occupants) + 1 <= seat_limit:
            lease = create_next_slot()
            if lease is not None:
                return lease

        lost = "; ".join(refused)
        if lost:
            raise WorktreeSeatResetRefused(
                f"all available worktrees would lose work on reset: {lost}"
            )
        detail = _busy_worktree_message(project_root, seat_limit, occupants)
        raise WorktreeSeatUnavailable(
            f"{detail}; refusing to create a new unmanaged worktree"
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


def shared_read_only_worktree(project_root: Path, *, base: str | None = None) -> tuple[Path, str]:
    """Return a checkout shared by read-only dispatches at one commit."""
    project_root = project_root.resolve()
    _verify_project_root(project_root)
    resolved_base = base if base is not None else default_seat_base(project_root)
    base_commit = _git(project_root, "rev-parse", "--verify", f"{resolved_base}^{{commit}}")
    root = repository_worktree_root(project_root) / READ_ONLY_WORKTREE_DIR
    root.mkdir(parents=True, exist_ok=True)
    lock_root = _seat_lock_root(project_root)
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / "readonly-allocation.lock"
    fd = os.open(lock_path, _lock_open_flags(), 0o600)
    lock_file = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        path = root / base_commit[:16]
        if not path.exists():
            _git(project_root, "worktree", "add", "--detach", str(path), base_commit)
        _verify_existing_seat(project_root, path)
        actual = _git(path, "rev-parse", "HEAD^{commit}")
        if actual != base_commit:
            raise WorktreeSeatError(
                f"shared read-only worktree {path} is at {actual}, expected {base_commit}"
            )
        return path, base_commit
    finally:
        lock_file.close()


def release_worktree_for_dispatch(
    project_root: Path, worktree_path: str | Path | None, dispatch_id: str
) -> tuple[bool, str]:
    """Clear stale occupant metadata after a terminal withdrawal.

    A live worker still holding the kernel descriptor is never interrupted.
    This only releases a lock that is already free and whose recorded identity
    is not live, so withdrawal cannot strand a dead holder while preserving
    the no-kill contract.
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
    lock_path = worktree_lock_path_for_path(project_root, path)
    try:
        handle = os.fdopen(os.open(lock_path, _lock_open_flags(), 0o600), "r+", encoding="utf-8")
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
        if isinstance(pid, int) and isinstance(token, str) and token:
            live = goalflight_compat.process_identity_matches(pid, token)
            if live is not False:
                return False, "recorded worktree holder identity is live or unknown"
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


def try_acquire_worktree_path_lock(target: Path, dispatch_id: str) -> WorktreePathLock:
    """Acquire an exclusive, non-blocking kernel lock on ``target``.

    Failure to acquire is occupancy: ``WorktreePathLockBusy``. Failure to
    evaluate the lock at all (unreadable path, fd exhaustion) is
    ``WorktreePathLockUnknown``. The returned lock must be inherited by the
    worker; closing it in the launcher without passing the fd vacates the tree
    while the worker still writes.
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
        lock_fd = os.open(str(lock_path), _lock_open_flags(), 0o600)
    except OSError as exc:
        raise WorktreePathLockUnknown(
            f"cannot open worktree occupancy lock {lock_path}: {exc}"
        ) from exc
    lock_file = os.fdopen(lock_fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
