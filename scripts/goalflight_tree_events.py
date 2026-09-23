"""Event-driven worktree write tracking for the watcher (macOS FSEvents).

The watcher's idle and wedge checks need two facts about a worker's worktree:
the newest file mtime, and how many files changed after a given mtime. A walk
answers both but stats every file in the checkout on every poll; a fleet of
quiet workers made that the watchers' main filesystem load (2026-09-22).

``TreeEventMonitor`` keeps a map of file -> mtime, seeded by one walk. After
that it re-stats only the paths FSEvents reports as changed, so each snapshot
matches what a fresh walk would return, at the cost of a few stats.

FSEvents names paths but not times, so mtimes always come from ``stat``: a
replayed or coalesced event can never make an old file look fresh.

The stream is scheduled on the calling thread's run loop and drained only
inside ``snapshot()``. That keeps the watcher single-threaded (it forks lsof
with a ``preexec_fn``, which is unsafe once other threads exist) and means no
locking. Events that arrive between snapshots queue in fseventsd; if it drops
any, the stream says so and the next snapshot re-seeds with a walk.

Anything that could leave the map incomplete (dropped events, a root change,
an event queue past ``MAX_PENDING_EVENTS``, a failed walk) forces a re-seed,
and a failed seed returns None so callers walk instead. Some gaps are never
signalled: a symlinked file whose target lives outside the tree, events still
in flight, a case-only rename on a case-insensitive volume. So this is an
optimization, not an authority: the watcher confirms any verdict that could
stop a worker with a real walk, and every ``RESEED_INTERVAL_S`` the map is
rebuilt from a walk anyway. Off macOS, or if CoreServices will not load,
``start()`` returns None.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # POSIX only; this module is only useful on macOS anyway.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# FSEventStreamCreateFlags
_CREATE_FLAG_NO_DEFER = 0x00000002
_CREATE_FLAG_WATCH_ROOT = 0x00000004
_CREATE_FLAG_FILE_EVENTS = 0x00000010
_EVENT_ID_SINCE_NOW = 0xFFFFFFFFFFFFFFFF
# FSEventStreamEventFlags that mean "the stream missed something here".
_FLAG_MUST_SCAN_SUBDIRS = 0x00000001
_FLAG_USER_DROPPED = 0x00000002
_FLAG_KERNEL_DROPPED = 0x00000004
_FLAG_EVENT_IDS_WRAPPED = 0x00000008
_FLAG_ROOT_CHANGED = 0x00000020
_FLAG_MOUNT = 0x00000040
_FLAG_UNMOUNT = 0x00000080
_RESEED_FLAGS = (
    _FLAG_MUST_SCAN_SUBDIRS
    | _FLAG_USER_DROPPED
    | _FLAG_KERNEL_DROPPED
    | _FLAG_EVENT_IDS_WRAPPED
    | _FLAG_ROOT_CHANGED
    | _FLAG_MOUNT
    | _FLAG_UNMOUNT
)
_FLAG_ITEM_CREATED = 0x00000100
_FLAG_ITEM_REMOVED = 0x00000200
_FLAG_ITEM_RENAMED = 0x00000800
_K_CFSTRING_ENCODING_UTF8 = 0x08000100
_K_CFRUNLOOP_RUN_HANDLED_SOURCE = 4
# Coalescing window for event delivery. Well under the watcher's 2 s poll.
_LATENCY_S = 0.05
# Bound on events queued between snapshots. A worker that rewrites a huge
# tree while active would otherwise grow this list without limit; past it we
# drop the list and re-seed with one walk, which is what the list would have
# cost to replay anyway.
MAX_PENDING_EVENTS = 100_000
# Bound on callback batches handled per drain, so a tree that never stops
# changing cannot hold the watcher in ``snapshot()``. Batches left over are
# delivered on the next drain; the map is at most one poll behind.
MAX_DRAIN_BATCHES = 256
# Rebuild the map from a walk at least this often, bounding any gap FSEvents
# does not signal. Still 300x fewer walks than one per 2 s poll.
RESEED_INTERVAL_S = 600.0
# lstat errors that mean "this path is gone" (a parent turned into a file,
# or a symlink loop), not "could not look".
_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


@dataclass(frozen=True)
class TreeSnapshot:
    """Every file under the root (skip dirs excluded) and its mtime.

    ``mtimes`` is the monitor's live map, valid until its next ``snapshot()``;
    callers read it at once and must not mutate it. ``stat_failed`` is True
    when some listed file could not be stat'ed (for example a broken symlink).
    The tree walks treat that as "could not look" unless a newer file was
    found anyway; callers apply the same rule.
    """

    mtimes: dict[str, float]
    stat_failed: bool

    def newest(self) -> float | None:
        return max(self.mtimes.values(), default=None)

    def count_newer_than(self, since_mtime: float) -> int:
        return sum(1 for mtime in self.mtimes.values() if mtime > since_mtime)


class _CoreServices:
    """ctypes bindings, loaded once per process. None when unavailable."""

    _loaded: "_CoreServices | None | bool" = False

    @classmethod
    def get(cls) -> "_CoreServices | None":
        if cls._loaded is False:
            try:
                cls._loaded = cls()
            except (OSError, AttributeError, ValueError):
                cls._loaded = None
        return cls._loaded  # type: ignore[return-value]

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("FSEvents is macOS-only")
        cs_path = ctypes.util.find_library("CoreServices")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cs_path or not cf_path:
            raise OSError("CoreServices/CoreFoundation not found")
        cs = ctypes.CDLL(cs_path)
        cf = ctypes.CDLL(cf_path)
        self.callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,  # stream
            ctypes.c_void_p,  # info
            ctypes.c_size_t,  # numEvents
            ctypes.POINTER(ctypes.c_char_p),  # eventPaths (char **)
            ctypes.POINTER(ctypes.c_uint32),  # eventFlags
            ctypes.POINTER(ctypes.c_uint64),  # eventIds
        )
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        cf.CFArrayCreate.restype = ctypes.c_void_p
        cf.CFArrayCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p,
        ]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
        cs.FSEventStreamCreate.restype = ctypes.c_void_p
        cs.FSEventStreamCreate.argtypes = [
            ctypes.c_void_p, self.callback_type, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32,
        ]
        cs.FSEventStreamScheduleWithRunLoop.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        cs.FSEventStreamStart.restype = ctypes.c_bool
        cs.FSEventStreamStart.argtypes = [ctypes.c_void_p]
        for name in ("FSEventStreamStop", "FSEventStreamInvalidate", "FSEventStreamRelease"):
            getattr(cs, name).argtypes = [ctypes.c_void_p]
        self.cs = cs
        self.cf = cf
        # A private mode: draining runs only our stream, never other sources
        # or timers scheduled on this thread's default mode. Kept for the
        # life of the process.
        self.run_loop_mode = cf.CFStringCreateWithCString(
            None, b"goalflight.tree-events", _K_CFSTRING_ENCODING_UTF8
        )
        if not self.run_loop_mode:
            raise OSError("could not create the run loop mode string")


def canonical_dir(path: Path) -> str:
    """The on-disk spelling of ``path``: symlinks resolved, case as stored.

    FSEvents reports canonical paths. A root registered as ``~/repos`` on a
    case-insensitive volume gets events under ``~/Repos``, so prefix matching
    against the uncanonicalized root would silently drop every event.
    """
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        getpath = getattr(fcntl, "F_GETPATH", None) if fcntl is not None else None
        if getpath is None:
            return os.path.realpath(str(path))
        raw = fcntl.fcntl(fd, getpath, bytes(1024))
        return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
    finally:
        os.close(fd)


class TreeEventMonitor:
    """File -> mtime map for one worktree, kept current by FSEvents."""

    def __init__(self, root: str, skip_names: frozenset[str], api: _CoreServices) -> None:
        self.root = root
        self._prefix = root.rstrip("/") + "/"
        self._skip_names = skip_names
        self._api = api
        self._mtimes: dict[str, float] = {}
        self._unstatable: set[str] = set()
        self._pending: list[tuple[str, int]] = []
        self._reseed = True
        self._seeded_at: float | None = None
        self._stream = None
        # Held for the stream's lifetime: ctypes frees the trampoline with it.
        self._callback = api.callback_type(self._on_events)

    @classmethod
    def start(
        cls, root: Path | str, *, skip_names: Iterable[str]
    ) -> "TreeEventMonitor | None":
        """Start watching ``root``; None when FSEvents cannot be used."""
        api = _CoreServices.get()
        if api is None:
            return None
        try:
            canonical = canonical_dir(Path(root))
        except OSError:
            return None
        monitor = cls(canonical, frozenset(skip_names), api)
        if not monitor._open_stream():
            return None
        return monitor

    def _open_stream(self) -> bool:
        cf, cs = self._api.cf, self._api.cs
        path_cf = cf.CFStringCreateWithCString(
            None, self.root.encode("utf-8", "surrogateescape"), _K_CFSTRING_ENCODING_UTF8
        )
        if not path_cf:
            return False
        values = (ctypes.c_void_p * 1)(path_cf)
        paths_cf = cf.CFArrayCreate(None, values, 1, None)
        try:
            if not paths_cf:
                return False
            stream = cs.FSEventStreamCreate(
                None,
                self._callback,
                None,
                paths_cf,
                _EVENT_ID_SINCE_NOW,
                _LATENCY_S,
                _CREATE_FLAG_FILE_EVENTS | _CREATE_FLAG_NO_DEFER | _CREATE_FLAG_WATCH_ROOT,
            )
        finally:
            # The stream copies the path array; our references can go.
            if paths_cf:
                cf.CFRelease(paths_cf)
            cf.CFRelease(path_cf)
        if not stream:
            return False
        cs.FSEventStreamScheduleWithRunLoop(
            stream, cf.CFRunLoopGetCurrent(), self._api.run_loop_mode
        )
        if not cs.FSEventStreamStart(stream):
            cs.FSEventStreamInvalidate(stream)
            cs.FSEventStreamRelease(stream)
            return False
        self._stream = stream
        return True

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream:
            cs = self._api.cs
            cs.FSEventStreamStop(stream)
            cs.FSEventStreamInvalidate(stream)
            cs.FSEventStreamRelease(stream)

    # Runs only inside _drain(), on the watcher's own thread.
    def _on_events(self, _stream, _info, count, paths, flags, _ids) -> None:
        for index in range(count):
            self.handle_event(
                paths[index].decode("utf-8", "surrogateescape"), int(flags[index])
            )

    def handle_event(self, path: str, flags: int) -> None:
        """Queue one event. Public so tests can inject stream flags."""
        root_moved = path.rstrip("/") == self.root and flags & (
            _FLAG_ITEM_RENAMED | _FLAG_ITEM_REMOVED
        )
        if flags & _RESEED_FLAGS or root_moved:
            self._reseed = True
            self._pending.clear()
            return
        if self._reseed:
            return
        if len(self._pending) >= MAX_PENDING_EVENTS:
            self._reseed = True
            self._pending.clear()
            return
        self._pending.append((path, flags))

    def _drain(self) -> None:
        cf = self._api.cf
        mode = self._api.run_loop_mode
        for _ in range(MAX_DRAIN_BATCHES):
            if cf.CFRunLoopRunInMode(mode, 0.0, True) != _K_CFRUNLOOP_RUN_HANDLED_SOURCE:
                break

    def snapshot(self) -> TreeSnapshot | None:
        """Current file -> mtime map, or None if the tree cannot be read."""
        if self._stream is None:
            return None
        self._drain()
        if (
            self._seeded_at is not None
            and time.monotonic() - self._seeded_at >= RESEED_INTERVAL_S
        ):
            self._reseed = True
        if not self._reseed:
            pending, self._pending = self._pending, []
            merged: dict[str, int] = {}
            for path, flags in pending:
                merged[path] = merged.get(path, 0) | flags
            for path, flags in merged.items():
                self._refresh(path, flags)
        if self._reseed:
            # Also reached when a refresh above failed partway: never serve a
            # half-updated map. Clear the flag first: events that land during
            # the walk are queued and re-stat'ed next time, so none are lost.
            self._reseed = False
            self._pending.clear()
            if not self._seed():
                self._reseed = True
                return None
        return TreeSnapshot(self._mtimes, bool(self._unstatable))

    def _skipped(self, path: str) -> bool:
        if not path.startswith(self._prefix):
            return True
        parts = path[len(self._prefix):].split("/")
        # A skip name only excludes directories: a file named "venv" counts.
        return any(part in self._skip_names for part in parts[:-1])

    def _seed(self) -> bool:
        mtimes: dict[str, float] = {}
        unstatable: set[str] = set()
        if not self._walk_into(self.root, mtimes, unstatable):
            return False
        self._mtimes = mtimes
        self._unstatable = unstatable
        self._seeded_at = time.monotonic()
        return True

    def _walk_into(self, top: str, mtimes: dict[str, float], unstatable: set[str]) -> bool:
        """Walk ``top`` like the tree walks: no symlinked dirs, skip names pruned."""

        def _raise(err: OSError) -> None:
            raise err

        try:
            for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=_raise):
                dirnames[:] = [name for name in dirnames if name not in self._skip_names]
                for name in filenames:
                    self._stat_into(os.path.join(dirpath, name), mtimes, unstatable)
        except OSError:
            return False
        return True

    @staticmethod
    def _stat_into(path: str, mtimes: dict[str, float], unstatable: set[str]) -> None:
        try:
            mtimes[path] = os.stat(path).st_mtime
            unstatable.discard(path)
        except OSError:
            mtimes.pop(path, None)
            unstatable.add(path)

    def _known_file(self, path: str) -> bool:
        return path in self._mtimes or path in self._unstatable

    def _forget_under(self, path: str) -> None:
        was_file = self._known_file(path)
        self._mtimes.pop(path, None)
        self._unstatable.discard(path)
        if not was_file:
            self._forget_children(path)

    def _forget_children(self, path: str) -> None:
        # O(map size): callers skip it for paths already known to be files,
        # which cannot have children.
        prefix = path.rstrip("/") + "/"
        for key in [key for key in self._mtimes if key.startswith(prefix)]:
            del self._mtimes[key]
        for key in [key for key in self._unstatable if key.startswith(prefix)]:
            self._unstatable.discard(key)

    def _refresh(self, path: str, flags: int) -> None:
        if path == self.root or self._skipped(path):
            return
        try:
            info = os.lstat(path)
        except OSError as exc:
            if exc.errno in _GONE_ERRNOS:
                self._forget_under(path)
            else:
                self._unstatable.add(path)
            return
        if stat.S_ISDIR(info.st_mode):
            if os.path.basename(path) in self._skip_names:
                return
            if not flags & (_FLAG_ITEM_CREATED | _FLAG_ITEM_RENAMED):
                # Metadata only: files inside report their own events.
                return
            # A created or renamed-in directory can bring a whole subtree
            # with no per-file events (a mv into the tree). Re-read it.
            self._forget_under(path)
            if not self._walk_into(path, self._mtimes, self._unstatable):
                self._reseed = True
            return
        # Not a directory (any more): nothing can live under it. A known file
        # never had children, so only an unknown path can need the scan.
        if not self._known_file(path):
            self._forget_children(path)
        if stat.S_ISLNK(info.st_mode) and os.path.isdir(path):
            # The walks list a symlinked directory as a dir and do not follow it.
            self._forget_under(path)
            return
        self._stat_into(path, self._mtimes, self._unstatable)
