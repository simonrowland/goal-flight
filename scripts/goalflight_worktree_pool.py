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
import stat
import subprocess
from typing import TextIO


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
# at 100% disk. Seats are REUSED, so N seats sustains N CONCURRENT workers per
# project indefinitely rather than N total dispatches. Several controllers
# share one project root, so four seats starved the whole project between them.
#
# Derivation. The binding constraints are RAM and the machine concurrency cap,
# not disk: a seat is one git worktree, ~40MB of checkout here, so 24 seats is
# under 1GB per project. The machine cap is 120 concurrent workers across ~5
# active projects, i.e. ~24 per project if every project ran flat out at once --
# the point where seats stop binding before the real capacity gate does. Sanity
# check: the busiest project observed ~12 concurrent workers, so 24 leaves 2x
# headroom and still cannot, alone, reach the 120 machine cap.
#
# Raise via GOALFLIGHT_WORKTREE_SEATS when one repo needs more concurrent seats.
# NEVER lower this default to "shape" concurrency -- that is what made 4 behave
# as a worker cap.
DEFAULT_WORKTREE_SEATS = 24
WORKTREE_SEAT_PREFIX = "wt-"
CAPTIVE_SEAT_PREFIX = "s-"
SEAT_BRANCH_PREFIX = "seat"
QUARANTINE_REF_PREFIX = "goalflight/quarantine"
_SAFE_RING_LABEL = re.compile(r"[A-Za-z0-9._-]+")

# Three-state verdicts, same shape as goalflight_worktree_gc.py. UNKNOWN always
# retains (refuses reset). Do not collapse "could not tell" into a green light.
YES = "yes"
NO = "no"
UNKNOWN = "unknown"


class WorktreeSeatError(RuntimeError):
    """Base error for managed worktree seat acquisition."""


class WorktreeSeatUnavailable(WorktreeSeatError):
    """Raised when every configured seat is held."""


class WorktreeSeatResetRefused(WorktreeSeatError):
    """Raised when resetting a free seat would lose unique or undetermined work."""


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
    """A worktree seat whose ownership is exactly one kernel lock."""

    def __init__(
        self,
        *,
        path: Path,
        seat_name: str,
        dispatch_id: str,
        lock_file: TextIO,
        quarantine_branch: str | None,
        branch: str,
    ) -> None:
        self.path = path
        self.seat_name = seat_name
        self.dispatch_id = dispatch_id
        self.quarantine_branch = quarantine_branch
        self.branch = branch
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
    """Return validated inherited seat-lock and occupancy-lock descriptors."""
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
    both the pooled-seat fd and the occupancy fd. A parent that acquired a
    *new* seat puts the fd in the child env dict without exporting it on
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
    """Return ``s-N`` or legacy ``wt-N`` when the basename matches a seat pattern.

    A matching name is necessary but not sufficient for a maintained seat.
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
    """Ask the pool whether ``path`` is a registered seat.

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
            f"{resolved} is not under the managed seat root {managed_root}",
        )
    except OSError as exc:
        return "unknown", f"managed seat path could not be compared ({exc})"

    parts = rel.parts
    lock_subdir: str | None = None
    if len(parts) == 1:
        seat_name = pool_seat_name(parts[0])
        prefix = WORKTREE_SEAT_PREFIX
        if seat_name is None or _slot_from_seat_name(seat_name, prefix) is None:
            return "no", f"{resolved.name} is not a pool seat name"
    elif len(parts) == 2:
        seat_name = pool_seat_name(parts[1])
        prefix = CAPTIVE_SEAT_PREFIX
        if seat_name is None or _slot_from_seat_name(seat_name, prefix) is None:
            return "no", f"{resolved.name} is not a pool seat name"
        lock_subdir = parts[0]
    else:
        return (
            "no",
            f"{resolved} is not a managed seat path under {managed_root}",
        )

    try:
        seat_limit = configured_worktree_seats()
    except WorktreeSeatError as exc:
        return "unknown", f"seat configuration unreadable ({exc})"

    slot = _slot_from_seat_name(seat_name, prefix)
    if slot is None or slot > seat_limit:
        return (
            "no",
            f"{seat_name} is outside the configured seat range 1..{seat_limit}",
        )

    try:
        lock_root = _git_common_dir(root) / "goalflight-worktree-seat-locks"
    except WorktreeSeatError as exc:
        return "unknown", f"seat lock directory unreadable ({exc})"
    if lock_subdir:
        lock_root = lock_root / lock_subdir

    lock_path = lock_root / f"{seat_name}.lock"
    try:
        if lock_root.is_symlink():
            return "unknown", f"seat lock root is a symlink ({lock_root})"
        st = os.lstat(lock_path)
    except FileNotFoundError:
        return (
            "no",
            f"no seat lock for {seat_name}; path is not a registered pool seat",
        )
    except OSError as exc:
        return "unknown", f"seat lock unreadable for {seat_name} ({exc})"

    if stat.S_ISLNK(st.st_mode):
        return "unknown", f"seat lock is a symlink ({lock_path})"
    if not stat.S_ISREG(st.st_mode):
        return "unknown", f"seat lock is not a regular file ({lock_path})"
    return "yes", f"registered pool seat {seat_name}"


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


def _git_proc(
    cwd: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
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
    """Return the named branch a seat for ``dispatch_id`` must be checked out on."""
    raw = str(dispatch_id).strip()
    if not raw:
        raise WorktreeSeatError("dispatch id is empty; cannot name a seat branch")
    return f"{SEAT_BRANCH_PREFIX}/{raw}"


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
    root = _git_dir(project_root.resolve()) / "goalflight-worktree-seat-locks"
    if controller_label is None:
        return root
    return root / sanitize_controller_ring_label(controller_label)


def worktree_seat_lock_path(
    project_root: Path,
    seat_name: str,
    *,
    controller_label: str | None = None,
) -> Path:
    """Return the per-repository lock path for an already-named seat.

    Captive ``s-N`` seats store locks under ``{lock_root}/{label}/s-N.lock``.
    Legacy ``wt-N`` seats keep ``{lock_root}/wt-N.lock`` so in-flight
    workers are not evicted during the overlap window.
    """
    if controller_label is not None and is_captive_seat_name(seat_name):
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
    if " -> " in rest:
        return [part.replace("\\", "/").strip() for part in rest.split(" -> ", 1)]
    return [rest.replace("\\", "/").strip()]


def _porcelain_is_product(line: str) -> bool:
    paths = _porcelain_relpaths(line)
    if not paths:
        return bool(line.strip())
    return any(not is_reserved_seat_notes_path(path) for path in paths)


def classify_dispatch_cwd(
    cwd: Path,
    *,
    project_root: Path,
    controller_label: str | None,
) -> str:
    """Classify ``--cwd`` as ``in-place``, ``ring-seat``, or ``refuse``.

    In-place is the project root of *this* dispatch:

    - ``cwd`` is that path's git toplevel and equals ``project_root``, or
    - ``cwd`` is not inside any git checkout and equals ``project_root``
      (a non-git project identity, the same fallback
      ``resolve_project_root`` uses).

    A nested path under another git checkout — ``.cache/worktrees/foo``,
    an ad-hoc linked worktree of the same repo, ``/tmp`` clones — is not
    in-place. Those are the sprawl paths: refuse unless they are a seat
    in this controller's ring.
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
    if is_controller_ring_seat(
        resolved, project_root=root, controller_label=controller_label
    ):
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


def evaluate_seat_reset_safety(
    worktree_path: Path,
    *,
    base_commit: str,
    new_branch: str,
) -> dict:
    """Decide whether an existing seat may be reset onto ``new_branch`` at base.

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
    if blockers:
        return {
            "decision": "retain",
            "reason": "; ".join(blockers),
            "conditions": conditions,
        }
    return {
        "decision": "reset",
        "reason": "unique commits stay reachable; cleanliness is known",
        "conditions": conditions,
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
            f"cannot determine whether seat branch {branch} already exists; refusing add"
        )
    if exists.returncode not in (0, 1):
        detail = (exists.stderr or exists.stdout or "").strip() or f"exit {exists.returncode}"
        raise WorktreeSeatError(
            f"cannot determine whether seat branch {branch} exists ({detail})"
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


def _prepare_seat_checkout(
    worktree_path: Path, *, branch: str, base_commit: str
) -> None:
    _git(worktree_path, "checkout", "-f", "-B", branch, base_commit)
    # Never ``git clean -fdx``. Preserve the reserved notes namespace even
    # when a temp repo has not gitignored ``.goal-flight/``.
    _git(worktree_path, "clean", "-fd", "-e", ".goal-flight")


def _assert_seat_on_named_branch(worktree_path: Path, *, seat_name: str, branch: str) -> str:
    actual = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD")
    if actual == "HEAD":
        raise WorktreeSeatError(
            f"worktree seat {seat_name} is detached after prepare; "
            "refusing to hand a detached HEAD to a worker"
        )
    if actual != branch:
        raise WorktreeSeatError(
            f"worktree seat {seat_name} checked out {actual!r}, expected {branch!r}"
        )
    return actual


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
    if not product:
        return None

    _git(
        worktree_path,
        "add",
        "-A",
        "--",
        ".",
        ":(exclude).goal-flight",
        ":(exclude).goal-flight/**",
    )
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


def _prepare_claimed_seat(
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
) -> WorktreeSeatLease:
    existing = worktree_path.exists() or worktree_path.is_symlink()
    if existing:
        _verify_existing_seat(project_root, worktree_path)
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
    _write_occupant(lock_file, seat_name=seat_name, dispatch_id=dispatch_id)
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
        return WorktreeSeatLease(
            path=worktree_path,
            seat_name=seat_name,
            dispatch_id=dispatch_id,
            lock_file=lock_file,
            quarantine_branch=None,
            branch=actual,
        )
    quarantine_branch = _quarantine_dirty_worktree(
        worktree_path,
        seat_name=seat_name,
        abandoned_dispatch_id=prior_dispatch_id,
    )
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
            f"worktree seat {seat_name} is not clean after acquire-time reset"
        )
    actual_branch = _assert_seat_on_named_branch(
        worktree_path, seat_name=seat_name, branch=branch
    )
    return WorktreeSeatLease(
        path=worktree_path,
        seat_name=seat_name,
        dispatch_id=dispatch_id,
        lock_file=lock_file,
        quarantine_branch=quarantine_branch,
        branch=actual_branch,
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
) -> WorktreeSeatLease:
    """Acquire one captive ``s-N`` seat. Never mint past the fuse or HWM.

    Isolation is not a mode. New acquires grow this controller's ring to the
    live nonterminal high-water mark and reuse those paths forever. Exhaustion
    names occupants and refuses ``git worktree add``.
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
        managed_root = controller_ring_root(project_root, label)
    if managed_root.is_symlink():
        raise WorktreeSeatError(f"managed worktree root must not be a symlink: {managed_root}")
    if managed_root.exists() and not managed_root.is_dir():
        raise WorktreeSeatError(f"managed worktree root is not a directory: {managed_root}")
    if occupy_path is None:
        managed_root.mkdir(parents=True, exist_ok=True)

    lock_root = _seat_lock_root(project_root, controller_label=label)
    if lock_root.is_symlink():
        raise WorktreeSeatError(f"worktree seat lock root must not be a symlink: {lock_root}")
    if lock_root.exists() and not lock_root.is_dir():
        raise WorktreeSeatError(f"worktree seat lock root is not a directory: {lock_root}")
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
        fcntl.flock(allocation_file.fileno(), fcntl.LOCK_EX)

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
                    f"--cwd {worktree_path} is not a captive seat in "
                    f"{managed_root}; pass --in-place for the project root"
                )
            if worktree_path.parent.resolve() != managed_root.resolve():
                raise WorktreeCwdRefused(
                    f"--cwd {worktree_path} is not in this controller ring "
                    f"{managed_root}"
                )
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
                occupant = _occupant_description(lock_file, seat_name)
                lock_file.close()
                raise WorktreeSeatUnavailable(
                    f"worktree seat {seat_name} is held: {occupant}; "
                    "refusing to git worktree add a new unmanaged path"
                )
            try:
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
                )
            except BaseException:
                lock_file.close()
                raise

        occupants: list[str] = []
        for slot in range(1, seat_limit + 1):
            seat_name = f"{CAPTIVE_SEAT_PREFIX}{slot}"
            lock_path = lock_root / f"{seat_name}.lock"
            if not lock_path.exists():
                continue
            try:
                probe_fd = os.open(lock_path, flags, 0o600)
            except OSError:
                continue
            probe_file = os.fdopen(probe_fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(probe_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                occupants.append(_occupant_description(probe_file, seat_name))
            probe_file.close()

        live = len(occupants) + 1
        if live > seat_limit:
            held = ", ".join(occupants)
            raise WorktreeSeatUnavailable(
                f"all {seat_limit} worktree seats are held: {held}; "
                "refusing to git worktree add a new unmanaged path"
            )
        hwm = max(_read_ring_hwm(lock_root), live)
        if hwm > seat_limit:
            hwm = seat_limit
        _write_ring_hwm(lock_root, hwm)

        refused: list[str] = []
        occupants = []
        for slot in range(1, hwm + 1):
            seat_name = f"{CAPTIVE_SEAT_PREFIX}{slot}"
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
                )
            except WorktreeSeatResetRefused as exc:
                refused.append(f"{seat_name}: {exc}")
                lock_file.close()
                continue
            except BaseException:
                lock_file.close()
                raise

        while hwm < seat_limit and refused and len(occupants) + 1 <= seat_limit:
            hwm += 1
            _write_ring_hwm(lock_root, hwm)
            seat_name = f"{CAPTIVE_SEAT_PREFIX}{hwm}"
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
                break
            try:
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
                )
            except WorktreeSeatResetRefused as exc:
                refused.append(f"{seat_name}: {exc}")
                lock_file.close()
                continue
            except BaseException:
                lock_file.close()
                raise

        held = ", ".join(occupants)
        lost = "; ".join(refused)
        if refused and not occupants:
            raise WorktreeSeatResetRefused(
                f"all {hwm} worktree seats would lose work on reset: {lost}"
            )
        if refused:
            raise WorktreeSeatResetRefused(
                f"all {hwm} worktree seats are unavailable: "
                f"held: {held or 'none'}; refusing reset: {lost}"
            )
        raise WorktreeSeatUnavailable(
            f"all {seat_limit} worktree seats are held: {held}; "
            "refusing to git worktree add a new unmanaged path"
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
