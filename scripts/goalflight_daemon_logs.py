#!/usr/bin/env python3
"""Run macOS daemon-log retention, then exec the scheduled drain tick.

These launchd jobs reopen stdout/stderr on every non-overlapping invocation.
newsyslog keeps an uncompressed archive of the same inode: an in-flight writer
and tail retain it until the job exits; the next invocation opens the pathname.
See protocols/drainer.md for the writer inventory and retention limits.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import ctypes
import ctypes.util
import errno
from pathlib import Path

import goalflight_compat


_STATE_ATIME_MARKER = ".goalflight-atime-keepalive"
_STATE_ATIME_INTERVAL_NS = 24 * 60 * 60 * 1_000_000_000
_UTIME_OMIT = -2  # <sys/stat.h> on macOS
_AT_SYMLINK_NOFOLLOW = 0x0020  # <sys/fcntl.h> on macOS
_ATIME_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def _set_atime(path: str, now_ns: int, *, dir_fd: int = -2) -> None:
    """Set only a path's atime; never write a possibly concurrent mtime."""
    times = (_Timespec * 2)(
        _Timespec(now_ns // 1_000_000_000, now_ns % 1_000_000_000),
        _Timespec(0, _UTIME_OMIT),
    )
    if _ATIME_LIBC.utimensat(dir_fd, os.fsencode(path), times, _AT_SYMLINK_NOFOLLOW) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), path)


def _open_state_directory(root_fd: int, relative: str) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in filter(None, relative.split(os.sep)):
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _keepalive_state_atime() -> None:
    """Refresh stale state-file atimes without changing retention mtimes."""
    root = goalflight_compat.resolve_state_dir()
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"state atime keep-alive could not inspect {root}: {exc}", file=sys.stderr, flush=True)
        return
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        return

    now_ns = time.time_ns()
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        print(f"state atime keep-alive could not open {root}: {exc}", file=sys.stderr, flush=True)
        return
    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
            return

        try:
            marker_info = os.stat(_STATE_ATIME_MARKER, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            marker_info = None
        except OSError as exc:
            print(f"state atime keep-alive could not inspect marker {root / _STATE_ATIME_MARKER}: {exc}", file=sys.stderr, flush=True)
            return

        if marker_info is not None:
            if stat.S_ISLNK(marker_info.st_mode):
                print(f"state atime keep-alive skipped symlink marker: {root / _STATE_ATIME_MARKER}", file=sys.stderr, flush=True)
                return
            if (not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_dev != root_info.st_dev):
                print(f"state atime keep-alive skipped invalid marker: {root / _STATE_ATIME_MARKER}", file=sys.stderr, flush=True)
                return
            if (marker_info.st_size == 0
                    and now_ns - marker_info.st_mtime_ns < _STATE_ATIME_INTERVAL_NS):
                return

        # tmp_cleaner's measured threshold is >= 96 h (`find -atime +3`
        # matches >= 4 days). A daily sweep with one missed tick leaves a max
        # gap of about 48 h, below 96 h. Dispatch scratch is transient; leaving
        # it for tmp_cleaner to reap after 4 idle days is fine.
        failures = 0
        examples = []

        def record_failure(relative: str, exc: OSError) -> None:
            nonlocal failures
            failures += 1
            if len(examples) < 3:
                name = errno.errorcode.get(exc.errno, f"ERRNO_{exc.errno}")
                examples.append(f"{relative or '.'} ({name})")

        stack = [""]
        while stack:
            relative = stack.pop()
            try:
                directory_fd = _open_state_directory(root_fd, relative)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                record_failure(relative, exc)
                continue
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if not relative and entry.name == _STATE_ATIME_MARKER:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            record_failure(os.path.join(relative, entry.name), exc)
                            continue
                        if stat.S_ISLNK(info.st_mode) or info.st_dev != root_info.st_dev:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name.endswith(".scratch"):
                                continue
                            try:
                                child_fd = os.open(
                                    entry.name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory_fd,
                                )
                            except (FileNotFoundError, NotADirectoryError):
                                continue
                            except OSError as exc:
                                record_failure(os.path.join(relative, entry.name), exc)
                                continue
                            try:
                                child_info = os.fstat(child_fd)
                            except OSError as exc:
                                record_failure(os.path.join(relative, entry.name), exc)
                                continue
                            finally:
                                os.close(child_fd)
                            if ((child_info.st_dev, child_info.st_ino)
                                    == (info.st_dev, info.st_ino)):
                                stack.append(os.path.join(relative, entry.name))
                        elif stat.S_ISREG(info.st_mode):
                            try:
                                _set_atime(entry.name, now_ns, dir_fd=directory_fd)
                            except (FileNotFoundError, NotADirectoryError):
                                continue
                            except OSError as exc:
                                record_failure(os.path.join(relative, entry.name), exc)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                record_failure(relative, exc)
            finally:
                os.close(directory_fd)

        if failures:
            print(
                f"state atime keep-alive could not refresh or descend {failures} entries: "
                + ", ".join(examples),
                file=sys.stderr,
                flush=True,
            )

        try:
            marker_fd = os.open(
                _STATE_ATIME_MARKER,
                os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_TRUNC,
                0o600,
                dir_fd=root_fd,
            )
        except OSError as exc:
            print(f"state atime keep-alive could not update marker {root / _STATE_ATIME_MARKER}: {exc}", file=sys.stderr, flush=True)
            return
        try:
            os.utime(marker_fd, ns=(now_ns, now_ns))
        finally:
            os.close(marker_fd)
    finally:
        os.close(root_fd)


def main(argv: list[str]) -> None:
    drain_log, *command = argv
    try:
        _keepalive_state_atime()
    except Exception as exc:
        print(f"state atime keep-alive failed: {exc}", file=sys.stderr, flush=True)
    log_dir = Path.home() / ".goal-flight"
    reaper = Path(__file__).resolve().parent / "goalflight_reap_dispatch_homes.py"
    sweep_log_path = Path(drain_log).parent / "dispatch-retention.log"
    requested_logs = [Path(drain_log), sweep_log_path] + [log_dir / name for name in (
        "fleet-console-fleet-launchd.log",
        "fleet-console-attention-launchd.log",
        "codex-seatd.log",
        "grok-seatd.log",
        "codex-rotate.log",
    )]
    logs = []
    for path in requested_logs:
        try:
            if path.is_symlink():
                print(f"daemon log retention skipped symlink: {path}", file=sys.stderr, flush=True)
                continue
        except OSError as exc:
            print(f"daemon log retention skipped unreadable path {path}: {exc}", file=sys.stderr, flush=True)
            continue
        logs.append(str(path))
    try:
        with sweep_log_path.open("ab", buffering=0) as sweep_log:
            subprocess.Popen(
                [sys.executable, str(reaper), "--delete"],
                stdin=subprocess.DEVNULL,
                stdout=sweep_log,
                stderr=sweep_log,
                close_fds=True,
                start_new_session=True,
            )
    except OSError as exc:
        # The drain timer owns retention scheduling; failure does not
        # disable log rotation or queue draining.
        print(f"daemon retention sweep could not start: {exc}", file=sys.stderr, flush=True)
    config = Path(__file__).resolve().parent / "templates/daemon-logs.newsyslog.conf"
    try:
        subprocess.run(
            ["/usr/sbin/newsyslog", "-r", "-s", "-f", str(config), *logs],
            check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Housekeeping failure must be visible without disabling queue draining.
        print(f"daemon log retention failed: {exc}", file=sys.stderr, flush=True)
    os.execv(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
