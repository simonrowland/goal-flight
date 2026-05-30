"""Cross-platform compatibility shim for goal-flight scripts.

Why this exists: the read/plan layer eagerly ``import fcntl`` and calls
``os.getuid()`` / ``os.kill(pid, 0)`` at import time or in liveness probes. All
three break on native Windows (``os.name == "nt"``):

  * ``fcntl`` does not exist on Windows -> ``import`` of the whole status layer
    raises ``ModuleNotFoundError`` before anything runs.
  * ``os.getuid()`` does not exist on Windows -> ``AttributeError`` at import for
    module-level state-dir computations.
  * ``os.kill(pid, 0)`` is NOT a liveness probe on Windows: signal ``0`` collides
    with ``CTRL_C_EVENT``, so CPython routes it to
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` -> for a normal worker pid it
    RAISES ``OSError`` WinError 87, and for a ``CREATE_NEW_PROCESS_GROUP`` console
    leader it delivers a real Ctrl+C. Used as ``try: os.kill(pid,0)`` it reads
    every pid as dead (false-dead lease release). See
    ``docs-private/research/2026-05-29-windows-CONVERGED-HANDOFF.md`` P0-A.

This module is a drop-in subset of ``fcntl`` (``flock`` + ``LOCK_*``) PLUS the
Windows-safe helpers ``is_windows()`` / ``pid_alive()`` / ``default_state_dir()``.
POSIX behavior is preserved byte-for-byte (paths stay under ``/tmp``; ``flock``
delegates to the real ``fcntl``; ``pid_alive`` is the same ``os.kill(pid, 0)``).
The Windows branches use stdlib ``ctypes`` / ``msvcrt`` only -- zero new wheels.
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

__all__ = [
    "is_windows",
    "LOCK_EX",
    "LOCK_SH",
    "LOCK_NB",
    "LOCK_UN",
    "flock",
    "pid_alive",
    "default_state_dir",
    "temp_base",
]


def is_windows() -> bool:
    """True on native Windows (``os.name == "nt"``). NOT true under WSL."""
    return os.name == "nt"


# --------------------------------------------------------------------------- #
# flock: drop-in subset of fcntl.                                             #
# POSIX -> re-export the real fcntl. Windows -> msvcrt byte-range locking.    #
# --------------------------------------------------------------------------- #
if is_windows():  # pragma: no cover - exercised only on Windows
    import msvcrt

    # fcntl.LOCK_* numeric values are POSIX-specific; on Windows we only need a
    # self-consistent set. msvcrt has no shared lock, so LOCK_SH maps to the
    # exclusive lock (advisory note: goal-flight uses LOCK_EX|LOCK_NB in practice).
    LOCK_SH = 0x1
    LOCK_EX = 0x2
    LOCK_NB = 0x4
    LOCK_UN = 0x8

    # msvcrt.locking() locks ``nbytes`` from the CURRENT file position. We seek to
    # 0 and lock the whole 2GiB-1 range (the documented whole-file idiom); unlock
    # MUST match the same offset+length.
    _WHOLE_FILE = 0x7FFFFFFF

    def _fd(fd_or_file) -> int:
        return fd_or_file if isinstance(fd_or_file, int) else fd_or_file.fileno()

    def flock(fd_or_file, operation: int) -> None:
        fd = _fd(fd_or_file)
        os.lseek(fd, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _WHOLE_FILE)
            return
        non_blocking = bool(operation & LOCK_NB)
        # Errnos msvcrt raises when the byte-range is already locked (contention).
        _contended = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
        if non_blocking:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _WHOLE_FILE)
            except OSError as exc:
                # Contended non-blocking lock -> BlockingIOError so callers match
                # POSIX flock(LOCK_NB). Do NOT mask unrelated errors (EBADF/EINVAL).
                if exc.errno in _contended:
                    raise BlockingIOError(exc.errno, str(exc)) from exc
                raise
            return
        # Blocking lock: msvcrt.LK_LOCK only retries ~10x1s then raises, but POSIX
        # LOCK_EX blocks indefinitely -> loop until acquired so semantics match.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, _WHOLE_FILE)
                return
            except OSError as exc:
                if exc.errno in _contended:
                    os.lseek(fd, 0, os.SEEK_SET)
                    continue
                raise
else:
    # POSIX: re-export the genuine article so behavior is identical.
    from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock  # noqa: F401


# --------------------------------------------------------------------------- #
# State directory.                                                            #
# POSIX path is preserved EXACTLY (/tmp/goal-flight-<uid>) -- note: on macOS  #
# tempfile.gettempdir() is $TMPDIR (/var/folders/...), NOT /tmp, so we must   #
# NOT route POSIX through gettempdir() or we relocate existing state.         #
# Windows has no /tmp and no getuid() -> gettempdir() + USERNAME.             #
# --------------------------------------------------------------------------- #
def _uid_tag() -> str:
    if is_windows():  # pragma: no cover - Windows only
        return os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return str(os.getuid())


def temp_base() -> Path:
    """Base temp dir: ``/tmp`` on POSIX (unchanged), ``gettempdir()`` on Windows."""
    if is_windows():  # pragma: no cover - Windows only
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def default_state_dir() -> Path:
    """``<temp_base>/goal-flight-<uid-or-username>``.

    On POSIX this equals the historical ``/tmp/goal-flight-<os.getuid()>`` exactly.
    Callers that honor ``$GOALFLIGHT_STATE_DIR`` keep their own env check and use
    this as the fallback default.
    """
    return temp_base() / f"goal-flight-{_uid_tag()}"


# --------------------------------------------------------------------------- #
# Non-destructive liveness probe.                                            #
# POSIX: os.kill(pid, 0) (unchanged). Windows: OpenProcess +                  #
# GetExitCodeProcess -- NEVER os.kill(pid, 0). ctypes, zero wheels.          #
# --------------------------------------------------------------------------- #
def pid_alive(pid) -> bool:
    """True if ``pid`` is a live process. Non-destructive on every platform."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if not is_windows():
        # Any OSError (ProcessLookupError, PermissionError, ...) -> not alive.
        # This matches the historical per-site `except OSError: return False`
        # behavior exactly, so POSIX liveness stays byte-identical to the
        # pre-port code (goal-flight pids are same-user, so PermissionError does
        # not arise here in practice).
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    # Windows: query-only handle + exit code. A handle can still open a
    # just-exited process, so the exit-code check (STILL_ACTIVE == 259) is what
    # actually decides liveness. Access-denied means the process exists but is
    # protected -> treat as alive.  pragma: no cover - Windows only
    import ctypes  # pragma: no cover
    from ctypes import wintypes  # pragma: no cover

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # pragma: no cover
    STILL_ACTIVE = 259  # pragma: no cover
    ERROR_ACCESS_DENIED = 5  # pragma: no cover

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pragma: no cover
    kernel32.OpenProcess.restype = wintypes.HANDLE  # pragma: no cover
    kernel32.OpenProcess.argtypes = (  # pragma: no cover
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    handle = kernel32.OpenProcess(  # pragma: no cover
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:  # pragma: no cover
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:  # pragma: no cover
        # Declare signatures so 64-bit HANDLE/exit-code marshal correctly (ctypes
        # defaults to int=32-bit, which truncates handles on 64-bit Windows).
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return True  # could not query exit code; assume alive (conservative)
        return code.value == STILL_ACTIVE
    finally:  # pragma: no cover
        kernel32.CloseHandle(handle)
