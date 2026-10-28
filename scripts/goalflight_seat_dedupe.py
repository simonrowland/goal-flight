#!/usr/bin/env python3
"""Share idle-seat files; writers bypassing seat locks and the idle gate are
outside this no-tamper guarantee. Processes of other uids cannot write the
operator's seat files without root and are out of scope."""
from __future__ import annotations

import concurrent.futures
import ctypes
import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any, Callable
import uuid

import goalflight_dispatch_states
import goalflight_subprocess

IdentityProbe = Callable[[dict[str, Any], float], tuple[bool, str]]
IdentitySnapshotProbe = Callable[
    [list[dict[str, Any]], float], Callable[[dict[str, Any]], tuple[bool, str]]
]
InactiveProbe = Callable[[dict[str, Any]], tuple[bool, str]]
IdentityInactive = Callable[[dict[str, Any], InactiveProbe], tuple[bool, str]]
_APFS_LIB: Any = None
_ATTR_CMNEXT_PRIVATESIZE = 0x00000008
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020
_ACL_TYPE_EXTENDED = 0x00000100
_RECENT_SECONDS = 10 * 60
_PASS_BUDGET_SECONDS = 15 * 60
# Resume waits up to 120 s for a seat, so the sweep gives one back within 60 s.
_SEAT_BUDGET_SECONDS = 60
_PROVENANCE_XATTR = b"com.apple.provenance"
_SEAT_SKIP_REASONS = (
    "live", "unknown_liveness", "lock_busy", "lock_missing", "lsof_busy",
    "lsof_error", "not_idle_other", "seat_budget",
)
_FILE_SKIP_REASONS = (
    "no_source", "recent", "unknown_private_size", "already_shared", "nlink",
    "flags_acl", "xattr_differ", "bytes_differ", "stat_changed", "deadline",
    "clone_error", "other",
)


def _new_skip_counts() -> dict[str, dict[str, int]]:
    return {
        "seats": {reason: 0 for reason in _SEAT_SKIP_REASONS},
        "files": {reason: 0 for reason in _FILE_SKIP_REASONS},
    }


def _count_skip(counts: dict[str, int] | None, reason: str) -> None:
    if counts is not None:
        counts[reason] += 1


class _AttrList(ctypes.Structure):
    _fields_ = (
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    )


def _apfs_library() -> Any:
    global _APFS_LIB
    if sys.platform != "darwin":
        raise OSError("APFS clonefile is only available on macOS")
    if _APFS_LIB is None:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
        library.clonefile.restype = ctypes.c_int
        library.getattrlist.argtypes = (
            ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_ulong,
        )
        library.getattrlist.restype = ctypes.c_int
        library.listxattr.argtypes = (
            ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
        )
        library.listxattr.restype = ctypes.c_ssize_t
        library.getxattr.argtypes = (
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        )
        library.getxattr.restype = ctypes.c_ssize_t
        library.acl_get_file.argtypes = (ctypes.c_char_p, ctypes.c_int)
        library.acl_get_file.restype = ctypes.c_void_p
        library.acl_free.argtypes = (ctypes.c_void_p,)
        library.acl_free.restype = ctypes.c_int
        _APFS_LIB = library
    return _APFS_LIB


def _clone_apfs_file(source: Path, destination: Path) -> None:
    if _apfs_library().clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _private_bytes(path: Path) -> int:
    attributes = _AttrList(5, 0, 0, 0, 0, 0, _ATTR_CMNEXT_PRIVATESIZE)
    buffer = ctypes.create_string_buffer(32)
    result = _apfs_library().getattrlist(
        os.fsencode(path), ctypes.byref(attributes), buffer, len(buffer),
        _FSOPT_ATTR_CMN_EXTENDED,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(path))
    if ctypes.c_uint32.from_buffer(buffer).value < 12:
        raise OSError(f"short ATTR_CMNEXT_PRIVATESIZE result for {path}")
    value = ctypes.c_int64.from_buffer(buffer, 4).value
    if value < 0:
        raise OSError(f"unknown ATTR_CMNEXT_PRIVATESIZE result for {path}")
    return value


def _has_acl(path: Path) -> bool:
    library = _apfs_library()
    acl = library.acl_get_file(os.fsencode(path), _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise OSError(error or errno.EIO, os.strerror(error or errno.EIO), str(path))
    library.acl_free(acl)
    return True


def _extended_attributes(path: Path) -> dict[bytes, bytes] | None:
    library = _apfs_library()
    encoded_path = os.fsencode(path)
    options = 0x0001  # XATTR_NOFOLLOW
    names_size = library.listxattr(encoded_path, None, 0, options)
    if names_size < 0:
        return None
    names = ctypes.create_string_buffer(names_size) if names_size else None
    if names and library.listxattr(encoded_path, names, names_size, options) != names_size:
        return None
    attributes: dict[bytes, bytes] = {}
    for name in names.raw[:names_size].split(b"\0") if names else ():
        if not name:
            continue
        size = library.getxattr(encoded_path, name, None, 0, 0, options)
        if size < 0:
            return None
        value = ctypes.create_string_buffer(size) if size else None
        if value and library.getxattr(encoded_path, name, value, size, 0, options) != size:
            return None
        attributes[name] = value.raw[:size] if value else b""
    return attributes


def _xattrs_match(left: dict[bytes, bytes] | None, right: dict[bytes, bytes] | None) -> bool:
    if left is None or right is None:
        return False
    # The kernel stamps a new file with its creator's provenance tag, so a
    # staged clone cannot carry the destination's tag; it records the writer,
    # not file content.
    return (
        {name: value for name, value in left.items() if name != _PROVENANCE_XATTR}
        == {name: value for name, value in right.items() if name != _PROVENANCE_XATTR}
    )


def _git_index_files(
    checkout: Path,
    *,
    deadline: float,
) -> dict[bytes, tuple[bytes, bytes]]:
    def git(*args: str) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 0)
        return goalflight_subprocess.run_with_deadline(
            [
                "git", "-C", str(checkout), "-c", "core.fsmonitor=false", *args,
            ],
            check=True, capture_output=True, timeout=min(30, remaining),
            deadline=deadline,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        ).stdout

    entries: dict[bytes, list[tuple[bytes, bytes, bytes]]] = {}
    for entry in git("ls-files", "-s", "-z").split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, blob, stage = metadata.split()
            entries.setdefault(path, []).append((mode, blob, stage))
    files = {
        path: (mode, blob)
        for path, stages in entries.items()
        if len(stages) == 1
        for mode, blob, stage in stages
        if stage == b"0"
    }
    return files


def _safe_indexed_path(checkout: Path, raw_path: bytes) -> Path | None:
    relative = Path(os.fsdecode(raw_path))
    if relative.is_absolute() or not relative.parts or any(
        part in {".", ".."} for part in relative.parts
    ):
        return None
    try:
        root = checkout.resolve(strict=True)
        current = root
        for index, part in enumerate(relative.parts):
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return None
        current.resolve(strict=False).relative_to(root)
        return current
    except (OSError, RuntimeError, ValueError):
        return None


def _seat_records_by_cwd(
    seats: list[Path], records: list[dict[str, Any]],
) -> dict[Path, list[dict[str, Any]]]:
    records_by_seat = {seat: [] for seat in seats}
    seats_by_path: dict[Path, list[Path]] = {}
    for seat in seats:
        seats_by_path.setdefault(seat, []).append(seat)

    for record in records:
        raw_cwd = record.get("worker_cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd:
            for seat_records in records_by_seat.values():
                seat_records.append(record)
            continue
        try:
            raw_path = Path(raw_cwd).expanduser()
            if not raw_path.is_absolute():
                for seat_records in records_by_seat.values():
                    seat_records.append(record)
                continue
            current = raw_path.resolve(strict=False)
        except (OSError, ValueError):
            for seat_records in records_by_seat.values():
                seat_records.append(record)
            continue
        while True:
            for seat in seats_by_path.get(current, ()):
                records_by_seat[seat].append(record)
            if current.parent == current:
                break
            current = current.parent
    return records_by_seat


def _seat_has_live_worker(
    seat: Path,
    records: list[dict[str, Any]],
    identity_probe: IdentityProbe,
    identity_inactive: IdentityInactive,
    deadline: float,
    identity_snapshot_probe: IdentitySnapshotProbe | None = None,
) -> tuple[bool, str | None]:
    snapshot_probe = None
    if identity_snapshot_probe is not None:
        relevant = []
        for record in records:
            raw_cwd = record.get("worker_cwd")
            if not isinstance(raw_cwd, str) or not raw_cwd:
                relevant.append(record)
                continue
            try:
                raw_path = Path(raw_cwd).expanduser()
                if raw_path.is_absolute():
                    raw_path.resolve(strict=False).relative_to(seat)
                    relevant.append(record)
            except (OSError, ValueError):
                continue
        snapshot_probe = identity_snapshot_probe(relevant, deadline)

    def inactive(record: dict[str, Any]) -> bool:
        if time.monotonic() >= deadline:
            return False
        return identity_inactive(
            record,
            (
                snapshot_probe
                if snapshot_probe is not None
                else lambda candidate: identity_probe(candidate, deadline)
            ),
        )[0]

    for record in records:
        raw_cwd = record.get("worker_cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd:
            if time.monotonic() >= deadline:
                return True, "unknown_liveness"

            raw_reason: str | None = None

            def probe(candidate: dict[str, Any]) -> tuple[bool, str]:
                nonlocal raw_reason
                live, raw_reason = (
                    snapshot_probe(candidate)
                    if snapshot_probe is not None
                    else identity_probe(candidate, deadline)
                )
                return live, raw_reason

            inactive_result, reason = identity_inactive(
                record, probe
            )
            if (
                goalflight_dispatch_states.is_terminal_state(record.get("state"))
                and raw_reason == "no_pid"
            ):
                # A terminal no-process record cannot run in a seat; the allocation lock and
                # seat-level lsof gate still catch workers the ledger did not record.
                continue
            if not inactive_result:
                return True, "live" if reason == "live_worker" else "unknown_liveness"
            continue
        try:
            raw_path = Path(raw_cwd).expanduser()
            if not raw_path.is_absolute():
                return True, "unknown_liveness"
            raw_path.resolve(strict=False).relative_to(seat)
        except ValueError:
            continue
        except OSError:
            return True, "unknown_liveness"
        if time.monotonic() >= deadline:
            return True, "unknown_liveness"
        inactive_result, reason = identity_inactive(
            record,
            (
                snapshot_probe
                if snapshot_probe is not None
                else lambda candidate: identity_probe(candidate, deadline)
            ),
        )
        if not inactive_result:
            return True, "live" if reason == "live_worker" else "unknown_liveness"
    return False, None


def _try_hold_seat_locks(
    project_root: Path, seat: Path, pool: Any, *, deadline: float,
) -> tuple[list[int] | None, str | None]:
    if time.monotonic() >= deadline:
        return None, "dedupe deadline expired before seat lock acquisition"
    descriptors: list[int] = []
    transferred = False
    try:
        verdict, reason, seat_lock, lock_stat = pool._registered_pool_seat_lock_info(
            seat,
            project_root=project_root,
            deadline=deadline,
            git_args_prefix=("-c", "core.fsmonitor=false"),
        )
        if verdict != "yes" or seat_lock is None or lock_stat is None:
            return None, f"seat allocation lock is {verdict}: {reason}"
        if time.monotonic() >= deadline:
            return None, "dedupe deadline expired while checking seat registration"
        common_dir = pool._git_common_dir(
            project_root,
            timeout=max(0.0, deadline - time.monotonic()),
            deadline=deadline,
            git_args_prefix=("-c", "core.fsmonitor=false"),
        )
        descriptors.append(pool._open_registered_lock(
            seat_lock, pool._lock_open_flags() & ~os.O_CREAT,
            registry_root=common_dir,
            registration_deadline=deadline,
        ))
        try:
            fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None, None
        if time.monotonic() >= deadline:
            return None, "dedupe deadline expired while acquiring seat locks"
        descriptors.append(pool._open_registered_lock(
            pool.worktree_path_lock_path(seat),
            pool._lock_open_flags() & ~os.O_CREAT,
            registry_root=common_dir, allow_unregistered=True,
            registration_deadline=deadline,
        ))
        try:
            fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None, None
        transferred = True
        return descriptors, None
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if descriptors and not transferred:
            for descriptor in descriptors:
                os.close(descriptor)


def _seat_has_open_handles(
    seat: Path, timeout: float, *, deadline: float | None = None
) -> bool:
    result = goalflight_subprocess.run_with_deadline(
        ["lsof", "-a", "-u", str(os.getuid()), "+D", str(seat), "-Fp0"],
        check=False, capture_output=True, timeout=timeout,
        deadline=deadline,
    )
    if (
        result.returncode == 1
        and not result.stdout
        and goalflight_subprocess.lsof_stderr_is_mount_stat_warnings_only(result.stderr or b"")
    ):
        return False
    # Any reported handle means busy. The -F records also carry fd fields and
    # newlines (b"p123\0\nfcwd\0\n"), so busy needs no parse.
    if result.stdout or result.returncode == 0:
        return True
    detail = result.stderr.decode("utf-8", "replace").strip()
    raise OSError(f"lsof exited {result.returncode}: {detail}")


def _file_info(
    path: Path,
    *,
    device: int,
    now_ns: int,
    needs_private_bytes: bool,
    skip_counts: dict[str, int] | None = None,
) -> tuple[os.stat_result, int] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device:
            _count_skip(skip_counts, "other")
            return None
        if info.st_nlink != 1:
            _count_skip(skip_counts, "nlink")
            return None
        if getattr(info, "st_flags", 0):
            _count_skip(skip_counts, "flags_acl")
            return None
        if now_ns - info.st_mtime_ns < _RECENT_SECONDS * 1_000_000_000:
            _count_skip(skip_counts, "recent")
            return None
        try:
            has_acl = _has_acl(path)
        except OSError:
            _count_skip(skip_counts, "flags_acl")
            return None
        if has_acl:
            _count_skip(skip_counts, "flags_acl")
            return None
        try:
            private = _private_bytes(path)
        except OSError:
            _count_skip(skip_counts, "unknown_private_size")
            return None
        if needs_private_bytes and private == 0:
            _count_skip(skip_counts, "already_shared")
            return None
        return info, private
    except OSError:
        _count_skip(skip_counts, "other")
        return None


def _same_bytes(first: Path, second: Path, deadline: float) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            if time.monotonic() >= deadline:
                return False
            a = left.read(1024 * 1024)
            if time.monotonic() >= deadline:
                return False
            b = right.read(1024 * 1024)
            if time.monotonic() >= deadline:
                return False
            if a != b:
                return False
            if not a:
                return True


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _clear_pass_scratch(path: Path, deadline: float) -> bool:
    try:
        for entry in path.iterdir():
            if time.monotonic() >= deadline:
                return False
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                return False
            entry.unlink()
        if time.monotonic() >= deadline:
            return False
        path.rmdir()
        return True
    except OSError:
        return False


def _clear_stale_scratch(scratch_root: Path, deadline: float) -> bool:
    try:
        for entry in scratch_root.iterdir():
            if time.monotonic() >= deadline:
                return False
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                owner_pid = int(entry.name.split("-", 1)[0])
            except ValueError:
                continue
            if owner_pid <= 0 or owner_pid == os.getpid() or _pid_is_alive(owner_pid):
                continue
            if not _clear_pass_scratch(entry, deadline):
                if time.monotonic() >= deadline:
                    return False
        return time.monotonic() < deadline
    except OSError:
        return False


def _clone_if_unchanged(
    source: Path,
    destination: Path,
    scratch: Path,
    *,
    source_root: Path,
    source_index_path: bytes,
    destination_root: Path,
    destination_index_path: bytes,
    device: int,
    now_ns: int,
    deadline: float,
    skip_counts: dict[str, int] | None = None,
) -> int:
    def skip(reason: str) -> int:
        _count_skip(skip_counts, reason)
        return 0

    if (
        _safe_indexed_path(source_root, source_index_path) != source
        or _safe_indexed_path(destination_root, destination_index_path) != destination
    ):
        return skip("other")
    destination_state = _file_info(
        destination, device=device, now_ns=now_ns, needs_private_bytes=True,
        skip_counts=skip_counts,
    )
    source_state = _file_info(
        source, device=device, now_ns=now_ns, needs_private_bytes=False,
        skip_counts=skip_counts,
    )
    if destination_state is None or source_state is None:
        return 0
    source_xattrs = _extended_attributes(source)
    destination_xattrs = _extended_attributes(destination)
    if not _xattrs_match(source_xattrs, destination_xattrs):
        return skip("xattr_differ")
    destination_info, _private = destination_state
    _source_info, _private = source_state
    temporary = scratch / f"{uuid.uuid4().hex}.clone"
    try:
        if (
            _safe_indexed_path(source_root, source_index_path) != source
            or _safe_indexed_path(destination_root, destination_index_path) != destination
        ):
            return skip("other")
        _clone_apfs_file(source, temporary)
        if stat.S_IMODE(temporary.lstat().st_mode) != stat.S_IMODE(destination_info.st_mode):
            os.chmod(temporary, stat.S_IMODE(destination_info.st_mode))
        staged = _file_info(
            temporary, device=device, now_ns=now_ns, needs_private_bytes=False,
            skip_counts=skip_counts,
        )
        if staged is None:
            return 0
        if staged[1] != 0:
            return skip("other")
        if time.monotonic() >= deadline:
            return skip("deadline")
        current_state = _file_info(
            destination, device=device, now_ns=now_ns, needs_private_bytes=True
        )
        if current_state is None:
            return skip("stat_changed")
        current = current_state[0]
        if (
            stat.S_IMODE(current.st_mode) != stat.S_IMODE(destination_info.st_mode)
            or current.st_size != temporary.stat().st_size
        ):
            return skip("stat_changed")
        fingerprint = (
            current.st_dev, current.st_ino, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns,
        )
        if (
            _safe_indexed_path(source_root, source_index_path) != source
            or _safe_indexed_path(destination_root, destination_index_path) != destination
        ):
            return skip("other")
        if not _same_bytes(temporary, destination, deadline):
            return skip("deadline" if time.monotonic() >= deadline else "bytes_differ")
        if time.monotonic() >= deadline:
            return skip("deadline")
        if _safe_indexed_path(destination_root, destination_index_path) != destination:
            return skip("other")
        latest = destination.lstat()
        if fingerprint != (
            latest.st_dev, latest.st_ino, latest.st_size,
            latest.st_mtime_ns, latest.st_ctime_ns,
        ):
            return skip("stat_changed")
        staged_xattrs = _extended_attributes(temporary)
        latest_xattrs = _extended_attributes(destination)
        if not _xattrs_match(staged_xattrs, latest_xattrs):
            return skip("xattr_differ")
        if time.monotonic() >= deadline:
            return skip("deadline")
        os.replace(temporary, destination)
        return current.st_size
    except (OSError, ValueError, subprocess.SubprocessError):
        return skip("clone_error")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _dedupe_pool(
    project_root: Path,
    *,
    records: list[dict[str, Any]],
    identity_probe: IdentityProbe,
    identity_inactive: IdentityInactive,
    deadline: float,
    identity_snapshot_probe: IdentitySnapshotProbe | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        "skip_counts": _new_skip_counts(),
    }
    if sys.platform != "darwin":
        return report
    if time.monotonic() >= deadline:
        return report
    import goalflight_worktree_pool

    reference = project_root.resolve()
    pass_scratch: Path | None = None
    try:
        seats = goalflight_worktree_pool._registered_pool_worktrees(
            reference,
            deadline=deadline,
            git_args_prefix=("-c", "core.fsmonitor=false"),
        )
        if not seats:
            return report
        pool_root = goalflight_worktree_pool.repository_worktree_root(reference)
        pool_device = pool_root.stat().st_dev
        scratch_root = pool_root / ".goalflight-dedupe-tmp"
        scratch_root.mkdir(parents=True, exist_ok=True)
        if scratch_root.is_symlink() or not scratch_root.is_dir() or scratch_root.stat().st_dev != pool_device:
            raise OSError("dedupe scratch directory is not on the pool volume")
        if not _clear_stale_scratch(scratch_root, deadline):
            return report
        pass_scratch = scratch_root / f"{os.getpid()}-{time.time_ns()}"
        pass_scratch.mkdir()
        if time.monotonic() >= deadline:
            return report
        seats = [seat.resolve(strict=False) for seat in seats if not seat.is_symlink()]
        if any(seat.stat().st_dev != pool_device for seat in seats):
            raise OSError("a pooled seat is on a different volume")
        records_by_seat = _seat_records_by_cwd(seats, records)
        reference_files = _git_index_files(reference, deadline=deadline)
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        if pass_scratch is not None:
            _clear_pass_scratch(pass_scratch, deadline)
        return report
    try:
        now_ns = time.time_ns()
        sources: dict[bytes, tuple[Path, bytes]] = {}
        for path, (mode, blob) in reference_files.items():
            if time.monotonic() >= deadline:
                report["skip_counts"]["files"]["deadline"] += 1
                return report
            if mode not in {b"100644", b"100755"}:
                continue
            source = _safe_indexed_path(reference, path)
            if source is not None and _file_info(
                source, device=pool_device, now_ns=now_ns, needs_private_bytes=False,
                skip_counts=report["skip_counts"]["files"],
            ) is not None:
                sources.setdefault(blob, (reference, path))

        def seat_budget_spent(seat_deadline: float) -> bool:
            return seat_deadline < deadline and time.monotonic() >= seat_deadline

        def release_locks(locks: list[int] | None) -> None:
            if locks is not None:
                for descriptor in reversed(locks):
                    os.close(descriptor)

        def inspect_seat(seat: Path) -> dict[str, Any]:
            seat_started = time.monotonic()
            if seat_started >= deadline:
                return {"started": False, "inspection_finished": seat_started}
            seat_deadline = min(deadline, seat_started + _SEAT_BUDGET_SECONDS)
            locks: list[int] | None = None

            def result(**values: Any) -> dict[str, Any]:
                finished = time.monotonic()
                return {
                    "started": True,
                    "seat_deadline": seat_deadline,
                    "inspection_elapsed": max(0.0, finished - seat_started),
                    "inspection_finished": finished,
                    **values,
                }

            try:
                locks, error = _try_hold_seat_locks(
                    reference, seat, goalflight_worktree_pool,
                    deadline=seat_deadline,
                )
                if locks is None:
                    return result(lock_busy=error is None, error=error)
                if time.monotonic() >= seat_deadline:
                    return result(error="dedupe deadline expired while checking seat liveness")
                live, reason = _seat_has_live_worker(
                    seat, records_by_seat[seat], identity_probe, identity_inactive,
                    seat_deadline, identity_snapshot_probe,
                )
                if live:
                    return result(error=reason or "unknown_liveness")
                files = _git_index_files(seat, deadline=seat_deadline)
                return result(files=files)
            except Exception as exc:
                return result(exception=exc)
            finally:
                release_locks(locks)

        def process_seat(seat: Path, result: dict[str, Any]) -> bool:
            if not result["started"]:
                report["skip_counts"]["seats"]["not_idle_other"] += 1
                return False
            report["seats_scanned"] += 1
            seat_deadline = result["seat_deadline"]
            locks: list[int] | None = None
            budget_stopped = False
            try:
                if result.get("lock_busy"):
                    report["skip_counts"]["seats"]["lock_busy"] += 1
                    return True
                error = result.get("error")
                exc = result.get("exception")
                if error:
                    if seat_budget_spent(seat_deadline):
                        budget_stopped = True
                    elif error in {"live", "unknown_liveness"}:
                        report["skip_counts"]["seats"][error] += 1
                    else:
                        report["errors"].append(f"{seat}: {error}")
                        reason = (
                            "lock_missing" if "allocation lock is no" in error.lower()
                            or "not found" in error.lower() or "missing" in error.lower()
                            else "not_idle_other"
                        )
                        report["skip_counts"]["seats"][reason] += 1
                    return True
                if exc is not None:
                    if seat_budget_spent(seat_deadline):
                        budget_stopped = True
                    else:
                        report["skip_counts"]["seats"]["not_idle_other"] += 1
                        report["errors"].append(f"{seat}: {type(exc).__name__}: {exc}")
                    return True
                processing_started = time.monotonic()
                processing_budget = max(
                    0.0, _SEAT_BUDGET_SECONDS - result["inspection_elapsed"],
                )
                seat_deadline = min(deadline, processing_started + processing_budget)
                if seat_budget_spent(seat_deadline):
                    budget_stopped = True
                    return True
                if time.monotonic() >= seat_deadline:
                    report["skip_counts"]["seats"]["not_idle_other"] += 1
                    return True
                locks, lock_error = _try_hold_seat_locks(
                    reference, seat, goalflight_worktree_pool,
                    deadline=seat_deadline,
                )
                if locks is None:
                    if lock_error is None:
                        report["skip_counts"]["seats"]["lock_busy"] += 1
                    elif seat_budget_spent(seat_deadline):
                        budget_stopped = True
                    else:
                        report["errors"].append(f"{seat}: {lock_error}")
                        reason = (
                            "lock_missing" if "allocation lock is no" in lock_error.lower()
                            or "not found" in lock_error.lower()
                            or "missing" in lock_error.lower()
                            else "not_idle_other"
                        )
                        report["skip_counts"]["seats"][reason] += 1
                    return True
                if time.monotonic() >= seat_deadline:
                    budget_stopped = seat_budget_spent(seat_deadline)
                    return True
                live, reason = _seat_has_live_worker(
                    seat, records_by_seat[seat], identity_probe, identity_inactive,
                    seat_deadline, identity_snapshot_probe,
                )
                if live:
                    report["skip_counts"]["seats"][reason or "unknown_liveness"] += 1
                    return True
                remaining = seat_deadline - time.monotonic()
                if remaining <= 0:
                    budget_stopped = seat_budget_spent(seat_deadline)
                    return True
                try:
                    has_handles = _seat_has_open_handles(
                        seat, remaining, deadline=seat_deadline
                    )
                except Exception as lsof_error:
                    if seat_budget_spent(seat_deadline):
                        budget_stopped = True
                    else:
                        report["skip_counts"]["seats"]["lsof_error"] += 1
                        report["errors"].append(
                            f"{seat}: {type(lsof_error).__name__}: {lsof_error}"
                        )
                    return True
                if has_handles:
                    report["skip_counts"]["seats"]["lsof_busy"] += 1
                    return True
                files = result["files"]
                for path, (mode, blob) in files.items():
                    if time.monotonic() >= seat_deadline:
                        if seat_budget_spent(seat_deadline):
                            budget_stopped = True
                        else:
                            report["skip_counts"]["files"]["deadline"] += 1
                        break
                    if mode not in {b"100644", b"100755"}:
                        report["skip_counts"]["files"]["other"] += 1
                        continue
                    destination = _safe_indexed_path(seat, path)
                    if destination is None:
                        report["skip_counts"]["files"]["other"] += 1
                        continue
                    source_copy = sources.get(blob)
                    if source_copy is None:
                        report["skip_counts"]["files"]["no_source"] += 1
                        if _file_info(
                            destination, device=pool_device, now_ns=now_ns,
                            needs_private_bytes=False,
                            skip_counts=report["skip_counts"]["files"],
                        ) is not None:
                            sources.setdefault(blob, (seat, path))
                        continue
                    source_tree, source_path = source_copy
                    source = _safe_indexed_path(source_tree, source_path)
                    if source is None:
                        report["skip_counts"]["files"]["no_source"] += 1
                        continue
                    if source != destination:
                        shared = _clone_if_unchanged(
                            source, destination, pass_scratch,
                            source_root=source_tree,
                            source_index_path=source_path,
                            destination_root=seat,
                            destination_index_path=path,
                            device=pool_device, now_ns=now_ns,
                            deadline=seat_deadline,
                            skip_counts=report["skip_counts"]["files"],
                        )
                        if shared:
                            report["files_cloned"] += 1
                            report["bytes_shared"] += shared
                    else:
                        report["skip_counts"]["files"]["other"] += 1
            except Exception as exc:
                if seat_budget_spent(seat_deadline):
                    budget_stopped = True
                else:
                    report["skip_counts"]["seats"]["not_idle_other"] += 1
                    report["errors"].append(f"{seat}: {type(exc).__name__}: {exc}")
            finally:
                release_locks(locks)
                if budget_stopped:
                    report["skip_counts"]["seats"]["seat_budget"] += 1
            return True

        seats_to_process = iter(seats)
        seat_indexes = {seat: index for index, seat in enumerate(seats)}
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        pending: dict[concurrent.futures.Future, Path] = {}
        deferred: dict[int, tuple[Path, dict[str, Any]]] = {}
        processed_seats: set[int] = set()

        def submit_next() -> None:
            while len(pending) + len(deferred) < 4 and time.monotonic() < deadline:
                try:
                    seat = next(seats_to_process)
                except StopIteration:
                    return
                pending[executor.submit(inspect_seat, seat)] = seat

        def needs_prior_source(seat: Path, result: dict[str, Any]) -> bool:
            for path, (mode, blob) in result.get("files", {}).items():
                if (
                    mode in {b"100644", b"100755"}
                    and blob not in sources
                    and _safe_indexed_path(seat, path) is not None
                ):
                    return True
            return False

        def handle_result(seat: Path, result: dict[str, Any]) -> bool:
            index = seat_indexes[seat]
            if (
                result["started"]
                and result.get("files") is not None
                and not result.get("lock_busy")
                and not result.get("error")
                and result.get("exception") is None
                and index > 0
                and any(prior not in processed_seats for prior in range(index))
                and needs_prior_source(seat, result)
            ):
                deferred[index] = (seat, result)
                return True
            processed_seats.add(index)
            return process_seat(seat, result)

        def process_ready_deferred() -> bool:
            while True:
                if time.monotonic() >= deadline:
                    return True
                for index in sorted(deferred):
                    seat, result = deferred[index]
                    missing_prior = (
                        needs_prior_source(seat, result)
                        and any(prior not in processed_seats for prior in range(index))
                    )
                    if missing_prior:
                        continue
                    seat, result = deferred.pop(index)
                    processed_seats.add(index)
                    if not process_seat(seat, result):
                        return False
                    submit_next()
                    break
                else:
                    return True

        try:
            submit_next()
            while pending:
                if not process_ready_deferred():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, _waiting = concurrent.futures.wait(
                    pending, timeout=remaining,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    break
                completed = min(
                    done,
                    key=lambda future: (
                        future.result().get("inspection_finished", deadline),
                        os.fspath(pending[future]),
                    ),
                )
                seat = pending.pop(completed)
                if not handle_result(seat, completed.result()):
                    break
                if not process_ready_deferred():
                    break
                submit_next()
        finally:
            running = [future for future in pending if not future.done()]
            remaining = deadline - time.monotonic()
            if running and remaining > 0:
                concurrent.futures.wait(running, timeout=remaining)
            deadline_expired = time.monotonic() >= deadline
            if deadline_expired:
                report["skip_counts"]["seats"]["seat_budget"] += (
                    len(pending) + len(deferred)
                )
            still_running = any(not future.done() for future in running)
            executor.shutdown(wait=not still_running, cancel_futures=True)
    finally:
        if pass_scratch is not None:
            _clear_pass_scratch(pass_scratch, deadline)
    return report


def dedupe_pool(
    project_root: Path,
    *,
    records: list[dict[str, Any]],
    identity_probe: IdentityProbe,
    identity_inactive: IdentityInactive,
) -> dict[str, Any]:
    deadline = time.monotonic() + _PASS_BUDGET_SECONDS
    previous = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return _dedupe_pool(
            project_root, records=records, identity_probe=identity_probe,
            identity_inactive=identity_inactive, deadline=deadline,
        )
    finally:
        if previous is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = previous


def _dedupe_pools(
    project_roots: list[Path] | set[Path],
    *,
    records: list[dict[str, Any]],
    identity_probe: IdentityProbe,
    identity_inactive: IdentityInactive,
    project_timings: dict[Path, float] | None = None,
    identity_snapshot_probe: IdentitySnapshotProbe | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run one bounded dedupe stage across all project pools."""
    if deadline is None:
        deadline = time.monotonic() + _PASS_BUDGET_SECONDS
    report: dict[str, Any] = {
        "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0, "errors": [],
        "skip_counts": _new_skip_counts(),
    }
    previous = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        for project_root in sorted(project_roots, key=os.fspath):
            if time.monotonic() >= deadline:
                break
            project_started = time.monotonic()
            pool_report = _dedupe_pool(
                project_root,
                records=records,
                identity_probe=identity_probe,
                identity_inactive=identity_inactive,
                deadline=deadline,
                identity_snapshot_probe=identity_snapshot_probe,
            )
            if project_timings is not None:
                project_timings[project_root] = time.monotonic() - project_started
            for key in ("seats_scanned", "files_cloned", "bytes_shared"):
                report[key] += pool_report[key]
            pool_skips = pool_report.get("skip_counts", _new_skip_counts())
            for group in ("seats", "files"):
                for reason, count in pool_skips[group].items():
                    report["skip_counts"][group][reason] += count
            report["errors"].extend(
                f"{project_root}: {error}" for error in pool_report["errors"]
            )
    finally:
        if previous is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = previous
    return report
