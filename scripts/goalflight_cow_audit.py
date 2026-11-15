#!/usr/bin/env python3
"""Find git checkouts (and folders) that hold their own disk, and say whether each is safe to delete.

Why: `du` counts a cloned block once per clone, so it cannot tell a cheap `cp -c` seat from a
full `git worktree add` checkout, and it overstates what deleting a cloned tree frees.  APFS can
answer directly: getattrlist(ATTR_CMNEXT_PRIVATESIZE) returns the bytes of a file that are not
trapped inside a clone or a snapshot.  Summed over a checkout, that is what deleting it frees NOW.

  private ~= allocated  -> NON-COW: the checkout owns its blocks; deleting it frees them
  private ~= 0          -> shared: its blocks are also held by a clone, a clone source, or a
                           local APFS snapshot, so deleting it frees little today

"shared" is not proof of "made with cp -c": the source of a clone and anything older than the
newest local snapshot also read ~0.  The figure answers "what would deleting this free", which is
the question for cleanup.

Hard links: a file with st_nlink > 1 (e.g. objects of a local `git clone`) is counted once per
inode and contributes 0 private bytes, because deleting one link frees nothing while another
exists.  Its bytes are reported separately as `hardlinked_gb`.

Safety verdict (checkout rows).  Every git call is read-only and runs no repo code: an explicit
--git-dir, --no-optional-locks, --no-replace-objects, no fsmonitor, no hooks, no signature
checks, filter drivers replaced by `cat`, status never enters submodules, and inherited GIT_*
variables stripped (except config-scope ones).  `link` is
repo, linked (a `git worktree add` checkout), separate (submodule, --separate-git-dir or a
symlinked .git: checked like a repo) or orphan.
  SAFE   = nothing in this checkout exists only here, by these checks: no dirty or untracked
           files, no live open files, old enough, and its commits are safe elsewhere.
  REVIEW = one or more `reasons`:
    dirty               uncommitted or untracked files (`git status`, counted, never printed)
    ignored-data        gitignored entries other than rebuildable caches/environments
                        (an ignored docs-private/ or build/ may be the only copy of results)
    ignored-data-large  that ignored data is above --salvage-max-gb (too big to tar on the way out)
    untracked-large     untracked files above --salvage-max-gb: tarring them would move, not free,
                        the bytes, and big run results are data (column untracked_gb; a lower
                        bound once past the cap)
    salvage-large       untracked + ignored data + changed tracked files (working-tree size plus
                        index blob size, which the --binary patches carry) above --salvage-max-gb
    unsafe-path         an ignored entry name holds a newline, so it can't go on a plan line
    hidden-index-flags  assume-unchanged entries, or skip-worktree entries whose file is on disk,
                        which hide changes from status (a sparse seat's absent files don't count)
    unpushed, no-remote repo/separate: commits on ANY ref (`--all`: branches, tags, notes, stash,
                        pins like refs/goalflight/keep/*, worktree HEADs) not on the DEFAULT-BRANCH
                        tracking ref (HEAD, else main, else master) of a NETWORK remote; other
                        tracking refs can vanish upstream, so they don't count.  A remote counts only if all its fetch and push URLs
                        (after insteadOf) are http(s)/ssh/git:// or scp-style host:path and hold
                        no --skip-name substring; local paths and file:// never count.  LIMITS:
                        tracking refs are trusted as they are (no network call), and an ssh URL
                        that points at another machine's seat still counts as network.
                        (a ref pinned inside a disposable clone is not safekeeping, so it blocks)
    stash               repo/separate: a stash entry exists
    stash-reflog        more than one stash entry: older ones live only in the reflog
    detached-commits    linked worktree: commits reachable only from its detached HEAD
                        (its branch commits and stash live in the main repo, so they don't block)
    worktree-refs       linked worktree: per-worktree refs (refs/worktree/*, refs/bisect/*)
    locked              linked worktree that is locked
    open, open-unknown  some process has a file open under it (one global lsof), or lsof missing;
                        without root, lsof sees only this user's processes
    orphan              worktree whose gitdir (or its main repo) is gone: commits may be gone too
    primary             a repo directly under a --primary-root (default ~/Repos): a source checkout
    hosts-worktrees     repo whose .git administers a linked worktree that is locked or whose
                        .git file still points back at it (copied admin entries don't count)
    orphan-module       a submodule repo in .git/modules/** that has no checkout row this run
    submodule           a submodule checkout: never planned (the minter doesn't re-mint them);
                        on its own it does not block the parent
    filter-driver       a filter driver is configured (e.g. git-lfs: unpushed LFS objects can't
                        be checked cheaply)
    alternates-source   another audited checkout borrows objects from this one (alternates,
                        followed transitively; borrowers outside the scanned roots are not seen)
    remote-ci-seat      under a `remote-ci-managed` dir: goal-flight's lease logic owns removal
    protected           at, under or containing a --protect path (always ~/.goal-flight/skill, ~/.claude,
                        ~/.codex and ~/.gstack: installed agent tooling, not seats)
    recent              newest write is younger than --stale-days
    has-skipped-names   holds --skip-name entries, so `git status` was not run on it; or its
                        gitdir/main repo/worktree paths hold one, so no git call was made at all
    nested-review       a checkout nested inside it is REVIEW (deleting this deletes that)
    git-error           a git call failed or timed out

`--dirs ROOT` sizes plain folders: every directory --dirs-depth below ROOT is a `dir` row that
counts everything inside it, checkouts included (what deleting that folder frees); no verdict.

Efficient trawl:
  * discovery walks each root to --max-depth with os.scandir, prunes heavy or excluded names,
    and stops at a checkout (measuring it finds the checkouts nested inside, at any depth);
  * inside a checkout every byte is counted (only --skip-name substrings are pruned there);
  * every file of >= --big-mib is measured exactly (big files dominate the bytes);
  * small files are sampled 1 in --sample-every (the first one always), and their private fraction
    is scaled to the small-file total; --exact measures every file;
  * nested checkouts are their own rows and never counted in their parent;
  * rows are measured on --jobs threads (the ctypes and git calls release the GIL).

macOS/APFS only for the private size; elsewhere (or if the call fails) the allocated size is used
and the row is marked `api=fallback`.

The tool never deletes anything.  --delete-plan prints a shell script to review and run by hand.
The table view also prints a CLEANUP TARGETS section on stderr (stdout stays a clean TSV): the
full-bytes (NON-COW) checkouts that are SAFE or SALVAGE, biggest first, then what is on HOLD.

Tiers (column `tier`; `reasons` still lists every blocker):
  SAFE     nothing here exists only in this folder: delete it.
  SALVAGE  every blocker can be saved cheaply first: dirty -> patch + untracked tar; unpushed,
           no-remote, stash, worktree-refs, detached-commits -> git bundle; ignored-data below
           --salvage-max-gb -> tar.  --delete-plan writes the save commands, chained with `&&`
           before the delete, into --salvage-dir.
  HOLD     anything else (open, recent, protected, remote-ci-seat, locked, hosts-worktrees,
           has-skipped-names, alternates-source, submodule, filter-driver, git-error, orphan,
           nested-review, ignored-data-large, hidden-index-flags, orphan-module, stash-reflog,
           unsafe-path): never planned.

Recipes:
  # old checkouts under a repo's docs-private that are safe to delete, one path per line
  goalflight_cow_audit.py --root ~/Repos/<repo>/docs-private --older-than-days 7 --verdict SAFE --paths-only
  # reviewable salvage-then-delete script for SAFE and SALVAGE full-bytes seats (executes nothing)
  goalflight_cow_audit.py --root <seat-parent> --older-than-days 7 --delete-plan > plan.sh
  # which folders under ~ hold at least 1 GB of their own
  goalflight_cow_audit.py --dirs ~ --dirs-depth 2 --min-gb 1
  # checkouts outside the expected places (docs-private, /tmp, stray clones): see the MISPLACED block
  goalflight_cow_audit.py --wide --min-gb 0.05

Usage:
  goalflight_cow_audit.py [--root DIR ...] [--dirs DIR ...] [--skip-name SUBSTR ...] [--min-gb 0.5]
                          [--verdict SAFE|REVIEW] [--older-than-days N] [--json|--paths-only|--delete-plan]
Default roots (only when neither --root nor --dirs is given): ~/Repos and ~/.goal-flight.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import fnmatch
import json
import os
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

ATTR_BIT_MAP_COUNT = 5
ATTR_CMNEXT_PRIVATESIZE = 0x00000008
FSOPT_NOFOLLOW = 0x00000001
FSOPT_ATTR_CMN_EXTENDED = 0x00000020
# Pruned during DISCOVERY only (never searched for checkouts); bytes inside a checkout always count.
# docs-private is deliberately NOT here: old seats under a repo's docs-private are a main target.
DISCOVERY_PRUNE = {"node_modules", ".venv", "venv", "__pycache__", "Library", ".Trash", ".cache"}
GIT_TIMEOUT = 30
LSOF_TIMEOUT = 120
# Inherited GIT_* variables (GIT_DIR, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, ...) would redirect the
# audit's git calls; only these config-scope ones are kept.
_GIT_ENV_KEEP = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
REASON_ORDER = ("dirty", "ignored-data", "ignored-data-large", "untracked-large", "salvage-large", "unsafe-path", "hidden-index-flags", "unpushed", "no-remote", "stash", "stash-reflog",
                "detached-commits", "worktree-refs", "locked", "open", "open-unknown", "orphan",
                "hosts-worktrees", "primary", "orphan-module", "submodule", "filter-driver", "alternates-source", "remote-ci-seat", "protected", "recent", "git-error",
                "has-skipped-names", "nested-review")
EXTRA_COLS = ("verdict", "reasons", "dirty", "unpushed", "remotes", "stash", "open", "link", "branch",
              "last_commit_utc", "detached", "ignored", "tier", "placement", "untracked_gb")
# Where checkouts are supposed to live.  Anything else (docs-private, /tmp, research dirs, copies
# inside another checkout, stray clones in ~) is `misplaced`; placement never changes the tier.
DEFAULT_EXPECTED = (
    "~/Repos/*",  # primaries: immediate children only
    "~/Repos/*/worktrees/*", "~/Repos/*/.worktrees/*", "~/Repos/*/.claude/worktrees/*",
    "~/Repos/ci-jobs/*", "~/Repos/ci-jobs/*/tree",
    "~/Repos/ci-fixtures/*/*/*",  # shared CI fixture store
    "~/.goal-flight/worktrees/*",
    "~/.goal-flight/remote-ci-managed/*/seats/*/checkout",
    "~/.goal-flight/remote-ci-managed/repos/*/seats/*/checkout",
    "~/.goal-flight/remote-ci-managed/*/mirror*",
    "~/.goal-flight/skill",
)


def wide_roots() -> list[str]:
    """--wide adds these: home, /private/tmp and this user's TMPDIR (under /private/var/folders)."""
    return ["~", "/private/tmp", os.path.realpath(tempfile.gettempdir())]


def tmp_roots() -> list[str]:
    return [os.path.realpath("/private/tmp"), os.path.realpath(tempfile.gettempdir())]


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch per path component, so `*` never crosses a `/` (`~/Repos/*` is children only)."""
    pp, gp = path.split(os.sep), pattern.split(os.sep)
    return len(pp) == len(gp) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(pp, gp))
# Tiers.  SAFE: nothing to keep.  SALVAGE: every blocker is something the delete plan can save
# cheaply first (a patch + untracked tar for dirty files, a git bundle for local-only commits, a
# tar for small ignored data).  HOLD: everything else (live, owned by someone else, or not
# capturable by those three), so it is never planned.
#   Not salvageable although they sound like it: hidden-index-flags (git diff skips
#   assume-unchanged/skip-worktree files, so a patch would silently miss them), orphan-module
#   (a bundle of this repo does not contain the submodule repos), stash-reflog (stash entries
#   older than the newest live only in the reflog, which a bundle cannot carry).
SALVAGEABLE = {"dirty", "unpushed", "no-remote", "stash", "worktree-refs", "detached-commits", "ignored-data"}
BUNDLE_REASONS = {"unpushed", "no-remote", "stash", "worktree-refs", "detached-commits"}


# For GB-per-reason summaries a HOLD row counts once, under its most important HOLD reason (a
# salvageable reason such as no-remote never explains why a row is on HOLD).
HOLD_ORDER = ("open", "recent", "has-skipped-names", "protected", "primary", "remote-ci-seat", "locked",
              "hosts-worktrees", "open-unknown", "orphan", "alternates-source", "submodule", "filter-driver",
              "orphan-module", "hidden-index-flags", "stash-reflog", "ignored-data-large", "untracked-large",
              "salvage-large", "unsafe-path",
              "nested-review", "git-error")


def tier_of(reasons: set[str]) -> str:
    if not reasons:
        return "SAFE"
    return "SALVAGE" if reasons <= SALVAGEABLE else "HOLD"
# Installed agent tooling, never seats: the deployed goal-flight skill (a linked worktree) and the skill
# packs and CLI homes that agent hosts load from (gstack lives in ~/.gstack and ~/.claude/skills).
DEFAULT_PROTECT = ("~/.goal-flight/skill", "~/.claude", "~/.codex", "~/.gstack")
# Ignored entries that can be rebuilt.  Only DIRECTORY components are matched (an ignored entry
# ending in "/" or a component before the last); a final file component is allowed only by the
# explicit file names/globs below.  `env`, `build`, `dist` and `target` are deliberately absent:
# simulation output lives in such directories.  Anything else ignored raises `ignored-data`.
REGENERABLE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
                    ".ruff_cache", ".tox", ".nox", ".cache", ".eggs", "htmlcov", ".ipynb_checkpoints"}
REGENERABLE_DIR_GLOBS = ("*.egg-info",)
REGENERABLE_FILES = {".DS_Store", ".coverage"}
REGENERABLE_FILE_GLOBS = ("*.pyc", "*.pyo")


def _regenerable(entry: str, extra: frozenset = frozenset()) -> bool:
    """entry: one `!! ` path from git status porcelain v1 (a directory ends in '/'); extra: more
    directory names the owner opted in with --regenerable."""
    if entry.startswith('"') and entry.endswith('"'):
        entry = entry[1:-1]  # C-quoted name; the components we match contain no escapes
    parts = [c for c in entry.split("/") if c]
    if not parts:
        return False
    dirs, last = (parts, None) if entry.endswith("/") else (parts[:-1], parts[-1])
    if any(c in REGENERABLE_DIRS or c in extra or any(fnmatch.fnmatchcase(c, g) for g in REGENERABLE_DIR_GLOBS)
           for c in dirs):
        return True
    return last is not None and (last in REGENERABLE_FILES
                                 or any(fnmatch.fnmatchcase(last, g) for g in REGENERABLE_FILE_GLOBS))


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


class PrivateSize:
    """getattrlist(ATTR_CMNEXT_PRIVATESIZE); returns None when unavailable or not returned.

    Not thread-safe (one result buffer per instance): use one instance per thread (_thread_priv)."""

    def __init__(self) -> None:
        self._fn = None
        if sys.platform == "darwin":
            try:
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                fn = libc.getattrlist
                fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]
                fn.restype = ctypes.c_int
                self._fn = fn
            except (OSError, AttributeError):
                self._fn = None
        self._buf = ctypes.create_string_buffer(64)
        self._al = _AttrList(ATTR_BIT_MAP_COUNT, 0, 0, 0, 0, 0, ATTR_CMNEXT_PRIVATESIZE)

    def __call__(self, path: str) -> int | None:
        if self._fn is None:
            return None
        ctypes.memset(self._buf, 0, 64)
        rc = self._fn(os.fsencode(path), ctypes.byref(self._al), self._buf, 64,
                      FSOPT_NOFOLLOW | FSOPT_ATTR_CMN_EXTENDED)
        if rc != 0:
            return None
        length, priv = struct.unpack_from("<Iq", self._buf.raw, 0)
        # Each attribute is 4-byte aligned, off_t included: 4-byte length + 8-byte value = 12.
        return priv if length >= 12 else None


_tls = threading.local()


def _thread_priv() -> PrivateSize:
    p = getattr(_tls, "priv", None)
    if p is None:
        p = _tls.priv = PrivateSize()
    return p


def _skipped(name: str, skip: tuple[str, ...]) -> bool:
    return any(s in name for s in skip)


def _is_checkout(path: str) -> str | None:
    """'worktree' if path/.git is a file (linked worktree or submodule), 'repo' if a directory or a
    symlink (a gitdir kept elsewhere; git follows it, so it is a checkout marker too)."""
    try:
        st = os.lstat(os.path.join(path, ".git"))
    except OSError:
        return None
    if stat.S_ISREG(st.st_mode):
        return "worktree"
    if stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        return "repo"
    return None


def _norm_roots(roots: list[str], skip: tuple[str, ...]) -> list[str]:
    """Real paths of the roots (one spelling per path, so rows dedupe and no planned path runs
    through a symlink).  A root whose path holds a skip substring is dropped before any stat."""
    out = []
    for r in roots:
        a = os.path.abspath(os.path.expanduser(r))
        if not _skipped(a, skip):
            out.append(os.path.realpath(a))
    return out


def _entry_dev(e: os.DirEntry) -> int:
    return e.stat(follow_symlinks=False).st_dev


def find_checkouts(roots: list[str], skip: tuple[str, ...], max_depth: int,
                   prune: frozenset = frozenset()) -> list[tuple[str, str]]:
    """Checkouts under the roots.  Discovery never leaves a root's filesystem (st_dev), so
    /Volumes and network mounts are walked only when given as a root themselves."""
    found: dict[str, str] = {}
    stack = []
    for r in _norm_roots(roots, skip):
        try:
            stack.append((r, 0, os.stat(r).st_dev))
        except OSError:
            continue
    while stack:
        path, depth, rdev = stack.pop()
        kind = _is_checkout(path)
        if kind:
            found[path] = kind
            continue  # measure() walks the checkout and reports the checkouts nested inside it
        if depth >= max_depth:
            continue
        try:
            with os.scandir(path) as it:
                for e in it:
                    # Names first: a skip-named entry is never stat'ed.
                    if e.name == ".git" or e.name in DISCOVERY_PRUNE or e.name in prune or _skipped(e.name, skip):
                        continue
                    # Per-entry guard: an entry that vanishes between listing and stat (routine in
                    # /tmp and TMPDIR) must not abort the scan of its remaining siblings.
                    try:
                        same_fs = e.is_dir(follow_symlinks=False) and _entry_dev(e) == rdev
                    except OSError:
                        continue
                    if same_fs:
                        stack.append((e.path, depth + 1, rdev))
        except OSError:
            continue
    return sorted(found.items())


def find_dirs(roots: list[str], depth: int, skip: tuple[str, ...]) -> list[str]:
    """Every directory exactly `depth` levels below each root (no symlinks, no skip-names)."""
    out: set[str] = set()
    for r in _norm_roots(roots, skip):
        level = [r]
        for _ in range(depth):
            nxt = []
            for d in level:
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if not _skipped(e.name, skip) and e.is_dir(follow_symlinks=False):
                                nxt.append(e.path)
                except OSError:
                    continue
            level = nxt
        out.update(level)
    return sorted(out)


def measure(root: str, skip: tuple[str, ...], priv: PrivateSize, big: int, every: int,
            split_nested: bool = True) -> dict:
    """Bytes under root.  With split_nested, nested checkouts are excluded and returned in `nested`
    for their own rows; without it (folder rows) everything inside is counted."""
    logical = allocated = files = 0
    big_priv = 0
    small_alloc = small_samp_alloc = small_samp_priv = 0
    hard_alloc = 0
    newest = 0.0
    fallback = False
    n_small = n_sampled = 0
    n_skipped = 0
    seen_inodes: set[tuple[int, int]] = set()
    nested: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            if _skipped(e.name, skip):
                n_skipped += 1  # never stat it; only record that it is here
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if split_nested and not (d == root and e.name == ".git") and _is_checkout(e.path):
                        nested.append(e.path)
                        continue
                    stack.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen_inodes:
                continue
            seen_inodes.add(key)
            files += 1
            alloc = st.st_blocks * 512
            logical += st.st_size
            allocated += alloc
            newest = max(newest, st.st_mtime)
            if st.st_nlink > 1:  # another link keeps these blocks alive: deleting this one frees nothing
                hard_alloc += alloc
                continue
            if alloc <= 0:
                continue
            if alloc >= big:
                p = priv(e.path)
                if p is None:
                    fallback, p = True, alloc
                big_priv += p
            else:
                small_alloc += alloc
                n_small += 1
                if every <= 1 or (n_small - 1) % every == 0:  # always sample the first small file
                    p = priv(e.path)
                    if p is None:
                        fallback, p = True, alloc
                    n_sampled += 1
                    small_samp_alloc += alloc
                    small_samp_priv += p
    small_priv = (small_samp_priv / small_samp_alloc * small_alloc) if small_samp_alloc else float(small_alloc)
    return {"logical": logical, "allocated": allocated, "private": int(big_priv + small_priv),
            "hardlinked": hard_alloc, "files": files, "newest": newest,
            "estimated": n_sampled < n_small, "fallback": fallback,
            "skipped": n_skipped, "nested": nested}


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts)) if ts else "-"


def _git(path: str, gitdir: str, *args: str, pre: tuple[str, ...] = (), ok: tuple[int, ...] = (0,),
         inp: str | None = None, z: bool = False) -> list[str] | None:
    """Output lines of a read-only git call on one checkout, or None on any failure.

    --git-dir is the gitdir resolved (and skip-checked) by _layout, so git never re-discovers one.
    --no-optional-locks: status must not refresh (rewrite) the index.  --no-replace-objects,
    core.hooksPath, core.fsmonitor and log.showSignature (which would run gpg.program) stop repo
    config from changing answers or running code."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in _GIT_ENV_KEEP}
    env["LC_ALL"] = "C"
    cmd = ["git", "--no-optional-locks", "--no-replace-objects", "-c", "core.fsmonitor=false",
           "-c", "core.hooksPath=/dev/null", "-c", "log.showSignature=false",
           "-C", path, "--git-dir", gitdir, *pre, *args]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT,
                           input=inp if inp is not None else "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode not in ok:
        return None
    return [t for t in r.stdout.split("\0") if t] if z else r.stdout.splitlines()


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


class _Blind(Exception):
    """A path git would follow holds a --skip-name substring: stop before touching it."""


class _Broken(Exception):
    """Unreadable or malformed git metadata."""


def _join(base: str, rel: str, skip: tuple[str, ...]) -> str:
    """base joined with a path read from git metadata; refuses skip-named results and NULs."""
    if "\0" in rel:
        raise _Broken
    p = os.path.normpath(os.path.join(base, rel))
    if _skipped(p, skip):
        raise _Blind
    return p


def _layout(path: str, skip: tuple[str, ...]) -> dict:
    """Where a checkout's git data lives, resolved without following any skip-named path.

    link: repo (.git is a directory), linked (gitdir has a `commondir`: a `git worktree add`
    checkout whose refs and objects live in the main repo), separate (gitdir elsewhere with its own
    refs: a submodule, --separate-git-dir or a symlinked .git; checked like a repo), orphan (the
    gitdir or its commondir is gone).  Raises _Blind or _Broken."""
    dotgit = os.path.join(path, ".git")
    try:
        st = os.lstat(dotgit)
        if stat.S_ISDIR(st.st_mode):
            gitdir, link = dotgit, "repo"
        else:
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(dotgit)
            else:
                target = _gitfile_target(dotgit, skip)
                if target is None:
                    raise _Broken
            gitdir, link = _join(path, target, skip), "separate"
    except OSError:
        raise _Broken from None
    lay = {"gitdir": gitdir, "common": gitdir, "link": link, "main_repo": None}
    if not os.path.isdir(gitdir):
        lay["link"] = "orphan"
        return lay
    cfile = os.path.join(gitdir, "commondir")
    if os.path.lexists(cfile):
        rel = (_read_text(cfile) or "").strip()
        if not rel:
            raise _Broken
        common = _join(gitdir, rel, skip)
        if not os.path.isdir(common):
            lay["link"] = "orphan"
            return lay
        common = os.path.realpath(common)
        main = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        if _skipped(common, skip) or _skipped(main, skip):
            raise _Blind
        lay.update(common=common, link="linked", main_repo=main)
    return lay


def _gitfile_target(dotgit: str, skip: tuple[str, ...]) -> str | None:
    """Target of a `gitdir: X` file (X relative to the file's directory), or None."""
    first = (_read_text(dotgit) or "").split("\n", 1)[0].strip()
    if not first.startswith("gitdir:") or not first[7:].strip():
        return None
    return _join(os.path.dirname(dotgit), first[7:].strip(), skip)


def _hosted_worktrees(common: str, skip: tuple[str, ...]) -> int:
    """Linked worktrees this repo's gitdir administers that are live: the entry is locked (a lock
    means "keep it even if its path is missing"), or the worktree's .git file still points back at
    this admin dir.  The back-pointer test matters for a `cp -c` clone: it copies the admin
    entries, but the worktrees they name belong to the original repo."""
    n = 0
    try:
        with os.scandir(os.path.join(common, "worktrees")) as it:
            admins = [e for e in it]
    except OSError:
        return 0
    for e in admins:
        if _skipped(e.name, skip):
            raise _Blind
        if not e.is_dir(follow_symlinks=False):
            continue
        if os.path.lexists(os.path.join(e.path, "locked")):
            n += 1
            continue
        target = (_read_text(os.path.join(e.path, "gitdir")) or "").strip()  # the worktree's .git file
        if not target:
            continue
        back = _gitfile_target(_join(e.path, target, skip), skip)
        if back and os.path.realpath(back) == os.path.realpath(e.path):
            n += 1
    return n


def _alternates(objects_dirs: set[str], skip: tuple[str, ...]) -> set[str]:
    """Real paths of every object dir these object dirs borrow from, followed transitively through
    each `objects/info/alternates` (A -> B.git -> C makes C a source even if B is not a row)."""
    out: set[str] = set()
    todo = list(objects_dirs)
    seen = set(todo)
    while todo:
        od = todo.pop()
        for ln in (_read_text(os.path.join(od, "info", "alternates")) or "").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                src = os.path.realpath(_join(od, ln, skip))
                out.add(src)
                if src not in seen:
                    seen.add(src)
                    todo.append(src)
    return out


def _module_gitdirs(common: str, skip: tuple[str, ...]) -> set[str]:
    """Real paths of the submodule gitdirs kept under <common>/modules/** (a dir holding HEAD).
    Descends into a found gitdir only through its own `modules` (nested submodules)."""
    out: set[str] = set()
    todo = [os.path.join(common, "modules")]
    while todo:
        d = todo.pop()
        try:
            with os.scandir(d) as it:
                subs = [e for e in it if not e.is_symlink()]
        except OSError:
            continue
        for e in subs:
            if _skipped(e.name, skip):
                raise _Blind
            if not e.is_dir(follow_symlinks=False):
                continue
            if os.path.isfile(os.path.join(e.path, "HEAD")):
                out.add(os.path.realpath(e.path))
                todo.append(os.path.join(e.path, "modules"))
            else:
                todo.append(e.path)  # submodule names may contain slashes
    return out


_NETWORK_SCHEMES = {"http", "https", "ssh", "git"}


def _is_network_url(url: str) -> bool:
    """True for scheme://host URLs with a network scheme and for scp-style [user@]host:path.
    Local paths, file:// and `.` are not network: a copy on this machine is not safekeeping."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", url)
    if m:
        return m.group(1).lower() in _NETWORK_SCHEMES
    # git's scp form: a colon before the first slash, and not a path that starts locally.
    return bool(re.match(r"^(?:[^@/:\s]+@)?[^@/:\s]+:", url)) and not url.startswith(("/", ".", "~"))


def _tree_bytes(root: str, cap: int, skip: tuple[str, ...] = ()) -> int:
    """Allocated bytes (st_blocks*512) under root, no symlinks followed and no skip-named entry
    stat'ed (an untracked dir may hold a nested checkout with blinded names).  Stops once past
    cap, so the result is a lower bound when it exceeds cap."""
    total, stack = 0, [root]
    while stack and total <= cap:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(p) as it:
                    stack.extend(e.path for e in it if not _skipped(e.name, skip))
            except OSError:
                pass
        else:
            total += st.st_blocks * 512
    return total


def _sum_bytes(path: str, names: list[str], cap: int, skip: tuple[str, ...]) -> int:
    """Bytes of the status entries `names` under path, counted until past cap."""
    total = 0
    for n in names:
        total += _tree_bytes(os.path.join(path, n.rstrip("/")), cap - total, skip)
        if total > cap:
            break
    return total


DEFAULT_OPTS = {"salvage_max": 10 ** 9, "regenerable": frozenset()}


def git_facts(path: str, kind: str, tree_skipped: bool, skip: tuple[str, ...],
              opts: dict | None = None) -> tuple[dict, list[str]]:
    """Read-only git facts for one checkout and the reasons they raise (open/recent come later)."""
    opts = opts or DEFAULT_OPTS
    f: dict = {"dirty": None, "ignored": None, "unpushed": None, "remotes": None, "stash": None,
               "detached": None, "link": "-", "branch": "-", "last_commit_utc": "-",
               "main_repo": None, "objects": None, "borrows": set(), "gitdir": None, "modules": set(),
               "tracking": [], "ignored_entries": [], "untracked_gb": None, "bundle_base": [], "bundle_revs": [], "bundle_empty": False}
    reasons: list[str] = []
    try:
        lay = _layout(path, skip)
        f["link"], f["main_repo"] = lay["link"], lay["main_repo"]
        if lay["link"] == "orphan":
            return f, ["orphan"]  # no gitdir: its commits may have gone with the main repo
        gitdir, common = lay["gitdir"], lay["common"]
        linked = lay["link"] == "linked"
        f["gitdir"] = os.path.realpath(gitdir)
        f["borrows"] = _alternates({os.path.join(gitdir, "objects"), os.path.join(common, "objects")}, skip)
        if linked:
            if os.path.lexists(os.path.join(gitdir, "locked")):
                reasons.append("locked")
        else:
            f["objects"] = os.path.realpath(os.path.join(common, "objects"))
            if _hosted_worktrees(common, skip):
                reasons.append("hosts-worktrees")
            f["modules"] = _module_gitdirs(common, skip)  # matched against nested rows in audit()
            if lay["link"] == "separate" and f"{os.sep}.git{os.sep}modules{os.sep}" in f["gitdir"]:
                reasons.append("submodule")  # the seat minter does not re-mint submodules
    except _Blind:
        return f, reasons + ["has-skipped-names"]  # no git call may follow a blinded path
    except (_Broken, OSError, ValueError):
        return f, reasons + ["git-error"]
    err = False

    def run(*args: str, **kw) -> list[str] | None:
        nonlocal err
        out = _git(path, gitdir, *args, **kw)
        if out is None:
            err = True
        return out

    def revcount(*args: str) -> int | None:
        nonlocal err
        out = run("rev-list", "--count", *args)
        if out is not None and out and out[0].strip().isdigit():
            return int(out[0])
        err = True
        return None

    # Remotes: only a NETWORK remote is safekeeping, and only if every fetch and push URL (after
    # insteadOf rewrites, which `remote get-url` applies) is a network URL.  A local path or file://
    # remote may be the next folder to be deleted, so it never counts and is never stat'ed.  A URL
    # holding a --skip-name substring does not count either.  The tracking refs of counted remotes
    # are trusted as they are: no network call checks that the remote still has them.
    counted: list[str] = []
    for name in run("remote") or []:
        urls = (run("remote", "get-url", "--all", name) or []) + (run("remote", "get-url", "--push", "--all", name) or [])
        if urls and all(_is_network_url(u) and not _skipped(u, skip) for u in urls):
            counted.append(name)
    f["remotes"] = len(counted)
    tracking: list[str] = []
    for name in counted:
        tracking += run("for-each-ref", "--format=%(refname)", f"refs/remotes/{name}/") or []
    # --all: every ref (branches, tags, notes, refs/stash, pins such as refs/goalflight/keep/*) and
    # every worktree's HEAD, minus exactly the base refs below (fed as ^ref on stdin, so no glob
    # can widen the set).
    f["tracking"] = tracking
    # A salvage bundle leaves out only each counted remote's default-branch tracking ref: other
    # tracking refs (e.g. a feature branch squash-merged and deleted upstream) can vanish on the
    # server, and a bundle whose prerequisites are gone cannot be restored.
    # refs/remotes/<name>/HEAD counts only through what it points at, and only if that is an
    # existing tracking ref of the same remote: a hand-set symref to refs/heads/main would
    # otherwise make local-only commits look pushed.
    for name in counted:
        pre = f"refs/remotes/{name}/"
        target = ""
        if pre + "HEAD" in tracking:
            target = ((run("symbolic-ref", "-q", pre + "HEAD", ok=(0, 1)) or [""])[0]).strip()
        cands = ([target] if target.startswith(pre) and target != pre + "HEAD" else []) + [pre + "main", pre + "master"]
        base = next((c for c in cands if c in tracking), None)
        if base:
            f["bundle_base"].append(base)
    # Unpushed uses the same base: a commit held only by another tracking ref (e.g. a feature
    # branch the server deleted after a squash-merge) is not safe, so it makes the row SALVAGE and
    # goes into the bundle.  With no default-branch ref every commit counts.
    out = run("rev-list", "--count", "--all", "--stdin", inp="".join(f"^{t}\n" for t in f["bundle_base"]))
    if out and out[0].strip().isdigit():
        f["unpushed"] = int(out[0])
    else:
        err = True
    f["detached"] = revcount("HEAD", "--not", "--branches", "--tags", "--remotes")
    st = run("stash", "list")  # older stash entries live only in the reflog, which --all misses
    f["stash"] = None if st is None else len(st)
    # assume-unchanged (lowercase tag) hides changes from status and diff.  skip-worktree (`S`) is
    # what a sparse seat uses for files it never checked out; it hides something only when the
    # file is actually on disk.  With blinded names in the tree nothing is stat'ed: count it.
    lsv = run("ls-files", "-v", "-z", z=True)
    if lsv is not None and any(t[:1].islower() or (t[:1] == "S" and (tree_skipped or os.path.lexists(
            os.path.join(path, t[2:])))) for t in lsv):
        reasons.append("hidden-index-flags")
    wrefs: list[str] = []
    if linked:
        wrefs = run("for-each-ref", "--format=%(refname)", "refs/worktree", "refs/bisect") or []
        if wrefs:
            reasons.append("worktree-refs")  # per-worktree refs die with the worktree
    # What a salvage bundle would carry: a linked worktree's own HEAD and per-worktree refs (its
    # branches live in the main repo); a repo's every ref.  Empty -> the plan writes refs.txt.
    f["bundle_revs"] = ["HEAD", *wrefs] if linked else ["--all"]
    cnt = run("rev-list", "--count", *f["bundle_revs"], "--stdin", inp="".join(f"^{t}\n" for t in f["bundle_base"]))
    f["bundle_empty"] = bool(cnt) and cnt[0].strip() == "0"
    b = run("rev-parse", "--abbrev-ref", "HEAD")
    ct = run("log", "-1", "--format=%ct")
    if b and ct and ct[0].strip().isdigit():
        f["branch"] = "-" if _skipped(b[0], skip) else b[0]
        f["last_commit_utc"] = _utc(float(ct[0]))
    else:
        err = True
    # A filter driver (git-lfs above all) can keep content outside git objects (e.g. LFS objects
    # not yet uploaded), which none of the checks here can see cheaply: REVIEW.  The same list is
    # neutralised for the status call below so that config cannot run code.
    names = {ln[len("filter."):ln.rindex(".")] for ln in
             run("config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process|smudge)$", ok=(0, 1)) or []}
    if names:
        reasons.append("filter-driver")
    if tree_skipped:
        reasons.append("has-skipped-names")  # status would stat the blinded entries
    else:
        # --work-tree pins the tree to this folder even if a stray core.worktree points elsewhere.
        # --ignore-submodules=dirty: still reports a changed or staged submodule pointer, but never
        # runs a status inside the submodule with its own config; submodules have their own rows.
        if any("=" in n or not n for n in names):
            err = True
        else:
            pre: list[str] = []
            for n in sorted(names):
                pre += ["-c", f"filter.{n}.process=", "-c", f"filter.{n}.clean=cat",
                        "-c", f"filter.{n}.smudge=cat", "-c", f"filter.{n}.required=false"]
            # One status call (-z: raw names, no quoting): `!!` entries are ignored (--ignored=matching
            # lists an ignored directory once, without descending); every other entry is dirty or
            # untracked.  A rename/copy entry is followed by its original path, which is skipped.
            toks = run("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored=matching",
                       "--ignore-submodules=dirty", pre=(*pre, "--work-tree", path), z=True)
            if toks is not None:
                entries, i = [], 0
                while i < len(toks):
                    t, i = toks[i], i + 1
                    if len(t) > 3:
                        entries.append((t[:2], t[3:]))
                        if t[0] in "RC":
                            i += 1
                # A nested checkout shows up as `?? dir/` or `!! dir/`; it is judged by its own row
                # (and reaches this one through nested-review), so it is not counted here.
                entries = [(c, n) for c, n in entries if not (c in ("??", "!!") and n.endswith("/")
                                                             and _is_checkout(os.path.join(path, n[:-1])))]
                ign = [n for c, n in entries if c == "!!" and not _regenerable(n, opts["regenerable"])]
                f["dirty"] = sum(1 for c, _ in entries if c != "!!")
                f["ignored"] = len(ign)
                f["ignored_entries"] = ign
                # Salvaging copies untracked and ignored data into the salvage dir: past the cap that
                # moves the bytes (often on the same disk) instead of freeing them, and big run
                # results are data, so such rows are HOLD.  Sized only up to the cap (lower bound).
                cap = opts["salvage_max"]
                unt = [n for c, n in entries if c == "??"]
                u_bytes = _sum_bytes(path, unt, cap, skip)
                i_bytes = _sum_bytes(path, ign, cap, skip)
                f["untracked_gb"] = round(u_bytes / 1e9, 6)
                if u_bytes > cap:
                    reasons.append("untracked-large")
                if i_bytes > cap:
                    reasons.append("ignored-data-large")
                # Changed tracked files land in index.patch and worktree.patch (--binary): count each
                # one's working-tree size plus its index blob size into the same total.
                tracked = {n for c, n in entries if c not in ("??", "!!")}
                t_bytes = 0
                if tracked and u_bytes <= cap and i_bytes <= cap:
                    oids = {}
                    for t in run("ls-files", "-s", "-z", z=True) or []:
                        meta, _, name = t.partition("\t")
                        if name in tracked and len(meta.split()) == 3:
                            oids[name] = meta.split()[1]  # "<mode> <oid> <stage>"
                    if oids:
                        for ln in run("cat-file", "--batch-check=%(objecttype) %(objectsize)",
                                      inp="".join(o + "\n" for o in oids.values())) or []:
                            parts = ln.split()
                            if len(parts) == 2 and parts[0] == "blob" and parts[1].isdigit():
                                t_bytes += int(parts[1])
                    for n in tracked:
                        try:
                            st = os.lstat(os.path.join(path, n))
                        except OSError:
                            continue  # deleted in the working tree
                        if stat.S_ISREG(st.st_mode):
                            t_bytes += st.st_size
                if u_bytes <= cap and i_bytes <= cap and u_bytes + i_bytes + t_bytes > cap:
                    reasons.append("salvage-large")  # what the salvage would copy exceeds the cap
                if ign:
                    if any("\n" in n or "\r" in n for n in ign):
                        reasons.append("unsafe-path")  # cannot be written as a literal plan line
    if f["dirty"]:
        reasons.append("dirty")
    if f["ignored"]:
        reasons.append("ignored-data")
    if linked:
        # Branch commits and the stash live in the main repo's refs and objects: removing this
        # folder loses neither.  Only commits reachable from a detached HEAD alone would go.
        if f["detached"]:
            reasons.append("detached-commits")
    else:
        if f["remotes"] == 0:
            reasons.append("no-remote")  # with no network remote every commit is "unpushed"
        elif f["unpushed"]:
            reasons.append("unpushed")
        if f["stash"]:
            reasons.append("stash")
        if f["stash"] and f["stash"] > 1:
            reasons.append("stash-reflog")  # only the newest stash is a ref a bundle can carry
    if err:
        reasons.append("git-error")
    return f, reasons


def _open_paths() -> set[str] | None:
    """Paths some process has open (one global snapshot), or None when lsof is unavailable."""
    try:
        r = subprocess.run(["lsof", "-n", "-P", "-F", "n"], capture_output=True, text=True,
                           errors="replace", timeout=LSOF_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 when it could not inspect some processes; the rest of its output is still valid.
    if r.returncode != 0 and not r.stdout:
        return None
    return {ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n/")}


def _measure_row(path: str, kind: str, skip: tuple[str, ...], big: int, every: int,
                 opts: dict | None = None) -> tuple[dict, list[str]]:
    m = measure(path, skip, _thread_priv(), big, every, split_nested=(kind != "dir"))
    frac = m["private"] / m["allocated"] if m["allocated"] else 0.0
    row = {"path": path, "kind": kind, "logical_gb": m["logical"] / 1e9,
           "allocated_gb": m["allocated"] / 1e9, "private_gb": m["private"] / 1e9,
           "hardlinked_gb": m["hardlinked"] / 1e9, "private_frac": frac,
           "cow": "NON-COW" if frac >= 0.5 else "shared", "files": m["files"],
           "estimated": m["estimated"], "api": "fallback" if m["fallback"] else "apfs",
           "newest_utc": _utc(m["newest"]), "_newest": m["newest"], "_reasons": []}
    if kind == "dir":
        row.update(dirty=None, ignored=None, unpushed=None, remotes=None, stash=None, detached=None, link="-",
                   branch="-", last_commit_utc="-", _main_repo=None, _objects=None, _borrows=set(),
                   _gitdir=None, _modules=set(), _tracking=[], _ignored_entries=[], _bundle_base=[],
                   _bundle_revs=[], _bundle_empty=False, untracked_gb=None)
    else:
        facts, row["_reasons"] = git_facts(path, kind, m["skipped"] > 0, skip, opts)
        for k in ("main_repo", "objects", "borrows", "gitdir", "modules", "tracking", "ignored_entries",
                  "bundle_base", "bundle_revs", "bundle_empty"):
            row["_" + k] = facts.pop(k)
        row.update(facts)
    return row, m["nested"]


def _under(p: str, top: str) -> bool:
    return p == top or p.startswith(top.rstrip(os.sep) + os.sep)


def _count_open(rows: list[dict], opens: set[str]) -> dict[str, int]:
    """Open paths per checkout, matched by realpath prefix (lsof reports /private/tmp etc.).
    Walking each open path's ancestors costs O(depth) dict lookups instead of O(rows) prefix tests;
    a file in a nested checkout counts for its parents too, since deleting them removes it."""
    idx = {os.path.realpath(r["path"]): 0 for r in rows}
    for p in opens:
        q = p
        while True:
            if q in idx:
                idx[q] += 1
            parent = os.path.dirname(q)
            if parent == q:
                break
            q = parent
    return idx


def audit(roots: list[str], skip: tuple[str, ...], max_depth: int, big: int, every: int,
          dirs: list[str] | None = None, dirs_depth: int = 1, jobs: int = 1,
          stale_days: float = 7.0, now: float | None = None, protect: list[str] | None = None,
          opts: dict | None = None, primary_roots: list[str] | None = None,
          prune: frozenset = frozenset(), expected: list[str] | None = None) -> list[dict]:
    now = time.time() if now is None else now
    # Real path of every entry directly under a primary root, so a repo symlinked in counts too.
    primary: set[str] = set()
    for p in primary_roots or []:
        p = os.path.abspath(os.path.expanduser(p))
        if _skipped(p, skip):
            continue
        try:
            with os.scandir(p) as it:
                for e in it:
                    if _skipped(e.name, skip) or (e.is_symlink() and _skipped(os.readlink(e.path), skip)):
                        continue
                    primary.add(os.path.realpath(e.path))
        except OSError:
            continue
    protect = [os.path.abspath(os.path.expanduser(p)) for p in (protect or [])]
    protect += [os.path.realpath(p) for p in protect if not _skipped(p, skip)]
    checkouts = find_checkouts(roots, skip, max_depth, prune) if roots else []
    folders = find_dirs(dirs or [], dirs_depth, skip)
    seen = {p for p, _ in checkouts}
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        lsof_f = ex.submit(_open_paths) if checkouts else None
        pending = {ex.submit(_measure_row, p, k, skip, big, every, opts) for p, k in checkouts}
        pending |= {ex.submit(_measure_row, p, "dir", skip, big, every) for p in folders}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                row, nested = fut.result()
                rows.append(row)
                for n in nested:  # nested checkouts at any depth get their own row
                    k = _is_checkout(n)
                    if k and n not in seen:
                        seen.add(n)
                        pending.add(ex.submit(_measure_row, n, k, skip, big, every, opts))
        opens = lsof_f.result() if lsof_f else None
    co_rows = [r for r in rows if r["kind"] != "dir"]
    borrowed = set().union(*(r["_borrows"] for r in co_rows))
    row_gitdirs = {r["_gitdir"] for r in co_rows if r["_gitdir"]}
    for r in co_rows:
        if r["_modules"] - row_gitdirs:
            # A submodule repo kept in .git/modules with no checkout row (e.g. after deinit) can
            # hold commits nothing else has, and no row of its own checks them.
            r["_reasons"].append("orphan-module")
        names = {r["path"], os.path.realpath(r["path"])}
        if r["_objects"] and r["_objects"] in borrowed:
            r["_reasons"].append("alternates-source")  # another checkout reads objects from this one
        if any("remote-ci-managed" in n.split(os.sep) for n in names):
            r["_reasons"].append("remote-ci-seat")  # goal-flight's lease logic owns these seats
        # Protected: the row is at/under a protected path, or contains one (deleting it deletes that).
        if any(_under(n, p) or _under(p, n) for n in names for p in protect):
            r["_reasons"].append("protected")
        # A source checkout directly under a primary root (default ~/Repos) is what other work
        # depends on and no minter re-creates; its linked worktrees and nested seats are not primary.
        if r["kind"] == "repo" and r["link"] in ("repo", "separate") and any(n in primary for n in names):
            r["_reasons"].append("primary")
    open_by = _count_open(co_rows, opens) if opens is not None else {}
    for r in co_rows:
        if opens is None:
            r["open"] = None
            r["_reasons"].append("open-unknown")
        else:
            r["open"] = open_by[os.path.realpath(r["path"])]
            if r["open"]:
                r["_reasons"].append("open")
        if r["_newest"] > now - stale_days * 86400:
            r["_reasons"].append("recent")
    # `submodule` alone only keeps that row out of the plan; deleting its parent is still fine.
    own_review = {r["path"] for r in co_rows if set(r["_reasons"]) - {"submodule"}}
    for r in co_rows:
        # Deleting a checkout deletes every checkout nested in it, so their REVIEW is its REVIEW.
        pre = r["path"] + os.sep
        if any(p.startswith(pre) for p in own_review):
            r["_reasons"].append("nested-review")
    pats = [os.path.realpath(os.path.expanduser(g)) for g in (expected if expected is not None else DEFAULT_EXPECTED)]
    tmps = tmp_roots()
    paths = [r["path"] for r in co_rows]
    for r in co_rows:
        real = os.path.realpath(r["path"])
        if any(_glob_match(real, g) for g in pats):
            r["placement"], r["_location"] = "expected", "-"
            continue
        r["placement"] = "misplaced"
        if "docs-private" in real.split(os.sep):
            r["_location"] = "docs-private"
        elif any(_under(real, t) for t in tmps):
            r["_location"] = "tmp"
        elif any(p != r["path"] and _under(r["path"], p) for p in paths):
            r["_location"] = "nested-in-checkout"
        else:
            r["_location"] = "home-other"
    for r in rows:
        if r["kind"] == "dir":
            r.update(verdict="-", reasons="", open=None, tier="-", placement="-", _location="-")
        else:
            rs = sorted(set(r["_reasons"]), key=REASON_ORDER.index)
            r.update(verdict="REVIEW" if rs else "SAFE", reasons=",".join(rs), tier=tier_of(set(rs)))
    rows.sort(key=lambda r: (-r["private_gb"], r["path"], r["kind"]))
    return rows


def _plannable_path(p: str) -> bool:
    """Paths printed for a cleanup step must be one line and must not be symlinks."""
    return "\n" not in p and "\r" not in p


def _plan_rows(rows: list[dict]) -> list[dict]:
    """The rows a delete plan covers: SAFE or SALVAGE, full-bytes (NON-COW), one-line paths, not
    symlinks, and not inside another planned row (removing that one removes them)."""
    pick = [r for r in rows if r["tier"] in ("SAFE", "SALVAGE") and r["cow"] == "NON-COW"
            and _plannable_path(r["path"]) and not os.path.islink(r["path"])]
    tops = [r["path"] for r in pick]
    return [r for r in pick if not any(t != r["path"] and _under(r["path"], t) for t in tops)]


def _plain_branch(r: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._/+-]", "?", r["branch"])


def _saves(r: dict) -> str:
    """What the salvage block keeps for a row, e.g. `patch+untracked-tar+bundle`."""
    rs = set(r["reasons"].split(",")) if r["reasons"] else set()
    parts = (["patch", "untracked-tar"] if "dirty" in rs else []) + (["bundle"] if rs & BUNDLE_REASONS else []) \
        + (["ignored-tar"] if "ignored-data" in rs else [])
    return "+".join(parts) or "-"


_HEREDOC = "GF_COW_AUDIT_LIST_EOF"
_PATCH_FLAGS = ("--binary --no-color --no-relative --no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/")


def _salvage_cmds(r: dict, dest: str) -> list[str]:
    """Literal commands that save what only this checkout holds, into dest, which must not exist
    yet: `mkdir` of the leaf (not -p) fails on an earlier salvage, so nothing is overwritten.
    Restore: clone the remote, fetch refs.bundle, check out HEAD, `git apply --cached index.patch`,
    `git apply worktree.patch` (skip an empty patch), unpack the tars."""
    rs = set(r["reasons"].split(","))
    q, d = shlex.quote(r["path"]), shlex.quote(dest)
    g = (f"git --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=/dev/null -c log.showSignature=false"
         f" -C {q} --git-dir {shlex.quote(r['_gitdir'])} --work-tree {q}")
    cmds = [f"mkdir -p {shlex.quote(os.path.dirname(dest))}", f"mkdir {d}",
            f"{g} rev-parse HEAD > {d}/HEAD", f"{g} remote -v > {d}/remotes"]
    if rs & BUNDLE_REASONS:
        if r["_bundle_empty"]:
            # Nothing a bundle would add over the remote's default branch: record the commit ids.
            pats = " ".join(shlex.quote(x) for x in r["_bundle_revs"] if x.startswith("refs/"))
            cmds.append(f"{g} for-each-ref {shlex.quote('--format=%(objectname) %(refname)')} {pats}".rstrip()
                        + f" > {d}/refs.txt")
        else:
            revs = " ".join(shlex.quote(x) for x in r["_bundle_revs"])
            nots = " ".join(shlex.quote(t) for t in r["_bundle_base"])
            cmds.append(f"{g} bundle create {d}/refs.bundle {revs}" + (f" --not {nots}" if nots else ""))
            cmds.append(f"{g} bundle verify {d}/refs.bundle")
    if "dirty" in rs:
        cmds.append(f"{g} diff --cached {_PATCH_FLAGS} HEAD > {d}/index.patch")
        cmds.append(f"{g} diff {_PATCH_FLAGS} HEAD > {d}/worktree.patch")
        # List file first, so a failing ls-files stops the chain (a pipe would hide its status).
        cmds.append(f"{g} ls-files -z --others --exclude-standard > {d}/untracked.list")
        cmds.append(f"tar -czf {d}/untracked.tgz -C {q} --null -T {d}/untracked.list")
        cmds.append(f"find {q} -name .git -prune -o -type d -empty -print > {d}/empty-dirs.txt")
    if "ignored-data" in rs:
        # The entry list goes through a quoted here-document (no expansion; every line starts
        # with ./, so none equals the delimiter): no argument-length limit for many entries.
        listing = "\n".join("./" + n.rstrip("/") for n in r["_ignored_entries"])
        cmds.append(f"cat > {d}/ignored.list <<'{_HEREDOC}' &&\n{listing}\n{_HEREDOC}\n"
                    f"tar -czf {d}/ignored.tgz -C {q} -T {d}/ignored.list")
    return cmds


def _delete_cmd(r: dict) -> str:
    q = shlex.quote(r["path"])
    if r["link"] == "linked" and r["_main_repo"]:
        # No --force for a clean worktree: git's own refusal is a last check at run time.  A
        # SALVAGE row with dirty files needs one --force (never the double form, which would also
        # remove a locked worktree); its changes were saved by the preceding commands.
        force = "--force " if "dirty" in r["reasons"].split(",") else ""
        return f"git -C {shlex.quote(r['_main_repo'])} worktree remove {force}-- {q}"
    return f"rm -rf -- {q}"


def delete_plan(rows: list[dict], salvage_dir: str) -> str:
    """A reviewable shell script: SAFE rows are deleted; SALVAGE rows are first saved into
    salvage_dir, with every step chained by `&&` so a failed save stops that row's delete."""
    pick = _plan_rows(rows)
    salvage = [r for r in pick if r["tier"] == "SALVAGE"]
    out = ["#!/bin/sh",
           "unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY "
           "GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE GIT_CEILING_DIRECTORIES",
           f"# goalflight_cow_audit delete plan, {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
           f" on {re.sub(r'[^A-Za-z0-9._-]', '?', socket.gethostname())}",
           f"# {len(pick)} rows, {sum(r['private_gb'] for r in pick):.2f} private GB "
           f"({len(pick) - len(salvage)} SAFE, {len(salvage)} SALVAGE; NON-COW only)",
           f"# SALVAGE rows save a patch/bundle/tar first, under {shlex.quote(salvage_dir)}",
           "# Review it, drop any row you do not want, then run it.  Nothing has been deleted.",
           "# Clones borrowing objects from outside the scanned roots were not checked."]
    used: set[str] = set()
    for r in pick:
        # Comment lines never carry a path; the branch is reduced to plain ref characters.
        out.append(f"# {r['tier']} {r['private_gb']:.2f} GB private, newest write {r['newest_utc']}, "
                   f"branch {_plain_branch(r)}" + (f", saves {_saves(r)}" if r["tier"] == "SALVAGE" else ""))
        cmds = [_delete_cmd(r)]
        if r["tier"] == "SALVAGE":
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", r["path"].strip(os.sep))[-120:] or "checkout"
            base, n = name, 2
            while name in used:
                name, n = f"{base}-{n}", n + 1
            used.add(name)
            cmds = _salvage_cmds(r, os.path.join(salvage_dir, name)) + cmds
        out += [c + " &&" for c in cmds[:-1]] + [cmds[-1]]
    out.append("# Afterwards re-run the audit and compare `df -h` from before and after.")
    return "\n".join(out) + "\n"


def _cell(v) -> str:
    return "-" if v is None or v == "" else str(v)


TSV_HEADER = ("private_gb\tallocated_gb\tprivate_frac\tcow\tkind\tfiles\tnewest_utc\tapi\thardlinked_gb\tpath\t"
              + "\t".join(EXTRA_COLS))


def _tsv_line(r: dict) -> str:
    return ("%.2f\t%.2f\t%.2f\t%s\t%s\t%d\t%s\t%s%s\t%.2f\t%s\t" % (
        r["private_gb"], r["allocated_gb"], r["private_frac"], r["cow"], r["kind"], r["files"],
        r["newest_utc"], r["api"], "~" if r["estimated"] else "", r["hardlinked_gb"], r["path"])
        + "\t".join(_cell(r[c]) for c in EXTRA_COLS))


def _lead_reason(r: dict) -> str:
    """The reason a row is attributed to: for HOLD its most important HOLD-class reason, else its first."""
    rs = r["reasons"].split(",") if r["reasons"] else []
    if r["tier"] == "HOLD":
        return next((x for x in HOLD_ORDER if x in rs), rs[0] if rs else "-")
    return rs[0] if rs else "-"


def _by_primary_reason(rows: list[dict]) -> str:
    """GB per reason, each row counted once under its lead reason, so the parts add up."""
    acc: Counter = Counter()
    for r in rows:
        acc[_lead_reason(r)] += r["private_gb"]
    return ", ".join(f"{k} {v:.2f}" for k, v in sorted(acc.items(), key=lambda kv: (-kv[1], kv[0])))


def cleanup_block(shown: list[dict]) -> str:
    """The stderr section a human reads first: the full-bytes checkouts that are cleanup targets."""
    targets = sorted(_plan_rows(shown), key=lambda r: (-r["private_gb"], r["path"]))
    safe = sum(r["private_gb"] for r in targets if r["tier"] == "SAFE")
    salv = sum(r["private_gb"] for r in targets if r["tier"] == "SALVAGE")
    lines = [f"# CLEANUP TARGETS: {len(targets)} non-COW checkouts, {safe + salv:.2f} GB freeable "
             f"(SAFE {safe:.2f} GB, SALVAGE {salv:.2f} GB)"]
    for r in targets:
        what = f"  saves {_saves(r)}" if r["tier"] == "SALVAGE" else ""
        lines.append(f"#   {r['tier']:<7} {r['private_gb']:8.2f} GB  newest {r['newest_utc']}{what}  "
                     + r["path"].replace("\n", "?").replace("\r", "?"))
    hold = [r for r in shown if r["cow"] == "NON-COW" and r["tier"] == "HOLD"]
    lines.append(f"# NOT TARGETS (HOLD): {len(hold)} checkouts, {sum(r['private_gb'] for r in hold):.2f} GB"
                 + (f" ({_by_primary_reason(hold)})" if hold else ""))
    hints = {"ignored-data-large": "big ignored data; opt in with --regenerable NAME (e.g. build) or a larger --salvage-max-gb",
             "untracked-large": "big untracked files (run results are data); commit or move them, or a larger --salvage-max-gb",
             "salvage-large": "untracked + ignored data together exceed --salvage-max-gb; opt in with a larger cap"}
    for reason, hint in hints.items():
        big = [r for r in hold if reason in r["reasons"].split(",")]
        if big:
            lines.append(f"#   {reason}: {len(big)} checkouts, {sum(r['private_gb'] for r in big):.2f} GB hold {hint}")
    if targets:
        lines.append("# next: rerun with --delete-plan > plan.sh (SALVAGE rows are saved to --salvage-dir "
                     "first), review it, run it, then re-check `df -h`")
    return "\n".join(lines) + "\n"


def misplaced_block(shown: list[dict], limit: int = 15) -> str:
    """stderr summary of checkouts outside the expected places, by location class."""
    mis = sorted((r for r in shown if r.get("placement") == "misplaced"), key=lambda r: (-r["private_gb"], r["path"]))
    if not mis:
        return ""
    lines = [f"# MISPLACED: {len(mis)} checkouts, {sum(r['private_gb'] for r in mis):.2f} GB private "
             f"({sum(r['private_gb'] for r in mis if r['cow'] == 'NON-COW'):.2f} GB in NON-COW)"]
    by: dict[str, list[dict]] = {}
    for r in mis:
        by.setdefault(r["_location"], []).append(r)
    lines.append("#   by location: " + ", ".join(
        f"{k} {len(v)} ({sum(r['private_gb'] for r in v):.2f} GB)"
        for k, v in sorted(by.items(), key=lambda kv: -sum(r["private_gb"] for r in kv[1]))))
    for r in mis[:limit]:
        lines.append(f"#   {r['private_gb']:8.2f} GB  {r['tier']:<7} newest {r['newest_utc']}  {r['_location']:<18} "
                     + r["path"].replace("\n", "?").replace("\r", "?"))
    if len(mis) > limit:
        lines.append(f"#   ... and {len(mis) - limit} more")
    return "\n".join(lines) + "\n"


def _tier_summary(co: list[dict]) -> str:
    nc = [r for r in co if r["cow"] == "NON-COW"]
    parts = []
    for t in ("SAFE", "SALVAGE", "HOLD"):
        rs = [r for r in nc if r["tier"] == t]
        gb = sum(r["private_gb"] for r in rs)
        parts.append(f"{t} {gb:.2f}" + (f" ({_by_primary_reason(rs)})" if rs and t != "SAFE" else ""))
    return f"# NON-COW {sum(r['private_gb'] for r in nc):.2f} GB: " + " / ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find git checkouts and folders that own their disk blocks "
                                             "(APFS private size), with a safe-to-delete verdict.")
    ap.add_argument("--root", action="append", help="root to trawl for checkouts (repeatable); "
                    "default ~/Repos and ~/.goal-flight when neither --root nor --dirs is given")
    ap.add_argument("--dirs", action="append", default=[],
                    help="size every folder --dirs-depth below this root as a `dir` row (repeatable)")
    ap.add_argument("--dirs-depth", type=int, default=1, help="depth of --dirs rows below the root (default 1)")
    ap.add_argument("--skip-name", action="append", default=[],
                    help="never stat or enter any name containing this non-empty substring (repeatable)")
    ap.add_argument("--max-depth", type=int, default=12,
                    help="discovery depth below each root outside checkouts (default 12)")
    ap.add_argument("--big-mib", type=float, default=1.0, help="files at least this size are measured exactly")
    ap.add_argument("--sample-every", type=int, default=16, help="measure 1 in N small files (default 16)")
    ap.add_argument("--exact", action="store_true", help="measure every file (no sampling)")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="rows measured in parallel")
    ap.add_argument("--stale-days", type=float, default=7.0,
                    help="a write newer than this many days makes a checkout REVIEW (recent); default 7")
    ap.add_argument("--min-gb", type=float, default=0.0, help="only print rows with at least this many private GB")
    ap.add_argument("--verdict", choices=("SAFE", "SALVAGE", "HOLD", "REVIEW"),
                    help="only print rows of this tier (REVIEW = SALVAGE or HOLD)")
    ap.add_argument("--salvage-max-gb", type=float, default=1.0,
                    help="ignored data above this size makes a checkout HOLD (ignored-data-large); default 1")
    ap.add_argument("--salvage-dir", help="where --delete-plan saves SALVAGE rows "
                    "(default ~/checkout-salvage/<UTC YYYYMMDDTHHMMSSZ>); must not be inside any audited row")
    ap.add_argument("--regenerable", action="append", default=[],
                    help="also treat ignored directories with this name as rebuildable (repeatable; e.g. build)")
    ap.add_argument("--primary-root", action="append",
                    help="a repo directly under this dir is a primary source checkout: HOLD `primary` "
                         "(repeatable; default ~/Repos)")
    ap.add_argument("--older-than-days", type=float, help="only print rows whose newest write is older than this")
    ap.add_argument("--protect", action="append", default=[],
                    help=f"a checkout at, under or containing this path is REVIEW `protected` (repeatable); "
                         f"{', '.join(DEFAULT_PROTECT)} are always protected")
    ap.add_argument("--wide", action="store_true",
                    help="also search ~, /private/tmp and $TMPDIR for misplaced checkouts (same filesystem only; "
                         "Library, .Trash, caches and --prune names are skipped)")
    ap.add_argument("--prune", action="append", default=[],
                    help="also skip directories with this name during discovery (repeatable)")
    ap.add_argument("--expected", action="append",
                    help="glob (per path component, ~ expanded) for where checkouts belong; repeatable, replaces "
                         "the defaults (~/Repos/*, ~/Repos/*/worktrees/*, ~/.goal-flight/worktrees/*, ...)")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="emit JSON lines instead of TSV")
    out.add_argument("--paths-only", action="store_true", help="print only the selected paths, one per line")
    out.add_argument("--delete-plan", action="store_true",
                     help="print a reviewable shell script removing selected SAFE NON-COW checkouts; runs nothing")
    args = ap.parse_args(argv)
    if any(s == "" for s in args.skip_name):
        ap.error("--skip-name must not be empty (an empty substring would match every name)")
    if args.dirs_depth < 0:
        ap.error("--dirs-depth must be >= 0")

    roots = args.root or ([] if args.dirs else ["~/Repos", "~/.goal-flight"])
    if args.wide:
        roots = [*roots, *wide_roots()]
    every = 1 if args.exact else max(1, args.sample_every)
    now = time.time()
    rows = audit(roots, tuple(args.skip_name), args.max_depth, int(args.big_mib * 1024 * 1024), every,
                 dirs=args.dirs, dirs_depth=args.dirs_depth, jobs=args.jobs, stale_days=args.stale_days, now=now,
                 protect=[*DEFAULT_PROTECT, *args.protect], primary_roots=args.primary_root or ["~/Repos"],
                 opts={"salvage_max": int(args.salvage_max_gb * 1e9), "regenerable": frozenset(args.regenerable)},
                 prune=frozenset(args.prune), expected=args.expected)
    shown = [r for r in rows if r["private_gb"] >= args.min_gb
             and (args.verdict is None or r["tier"] == args.verdict
                  or (args.verdict == "REVIEW" and r["tier"] in ("SALVAGE", "HOLD")))
             and (args.older_than_days is None or r["_newest"] < now - args.older_than_days * 86400)]
    # A newline or carriage return in a path would split it into two lines (two commands) downstream.
    unsafe = [r for r in shown if not _plannable_path(r["path"])]
    if args.delete_plan:
        sdir = os.path.abspath(os.path.expanduser(
            args.salvage_dir or f"~/checkout-salvage/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"))
        real = os.path.realpath(sdir)
        inside = [r["path"] for r in rows if _under(sdir, r["path"]) or _under(real, os.path.realpath(r["path"]))]
        if inside or not _plannable_path(sdir):
            print("--salvage-dir must not be inside an audited row (deleting it would delete the salvage)",
                  file=sys.stderr)
            return 2
        sys.stdout.write(delete_plan(shown, sdir))
    elif args.paths_only:
        for r in shown:
            if _plannable_path(r["path"]):
                print(r["path"])
    elif args.json:
        for r in shown:
            print(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}))
    else:
        print(TSV_HEADER)
        for r in shown:
            print(_tsv_line(r))
    co = [r for r in rows if r["kind"] != "dir"]
    safe = [r for r in co if r["verdict"] == "SAFE"]
    tot_p = sum(r["private_gb"] for r in co)
    tot_nc = sum(r["private_gb"] for r in co if r["cow"] == "NON-COW")
    n_t = Counter(r["tier"] for r in co)
    print(f"# {len(co)} checkouts (SAFE {n_t['SAFE']}, SALVAGE {n_t['SALVAGE']}, HOLD {n_t['HOLD']}); freeable (private) total "
          f"{tot_p:.1f} GB; in NON-COW checkouts {tot_nc:.1f} GB; in SAFE checkouts "
          f"{sum(r['private_gb'] for r in safe):.1f} GB", file=sys.stderr)
    if unsafe and (args.delete_plan or args.paths_only):
        print(f"# unsafe-path: {len(unsafe)} rows left out (newline or carriage return in the path)", file=sys.stderr)
    dr = [r for r in rows if r["kind"] == "dir"]
    if dr:
        print(f"# {len(dr)} dirs; private total {sum(r['private_gb'] for r in dr):.1f} GB "
              "(dirs include any checkouts inside them)", file=sys.stderr)
    print(_tier_summary(co), file=sys.stderr)
    sys.stderr.write(misplaced_block([r for r in shown if r["kind"] != "dir"]))
    if not (args.json or args.paths_only or args.delete_plan):
        sys.stderr.write(cleanup_block(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
