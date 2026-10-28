#!/usr/bin/env python3
"""Copy-on-write payload for one new sparse seat.

A template generation is a plain directory of the tracked sparse files at one
commit. It is built with a private index and is never a registered worktree,
so it has no admin directory and nothing here prunes or unregisters.

The profile build lock lives outside the profile directory and is never
unlinked, so retiring a profile cannot replace the inode a builder already
opened. The publisher creates a generation's reader lock. Readers open that
file without creating it; a missing lock means the generation is retired and
its payload can be removed. A busy lock means a reader still holds it.
A template is used only when Git, in the new seat, cannot change checkout
bytes. Any other repository checks the seat out the ordinary way.

Flag-off seat creation must not call this module. ``prepare_for_new_seat``
runs only after the caller has opted in, and it does not take the allocation
lock or ``worktree-mutation.lock``.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time


# The measured full-tree write was 215 MB and 81 MB from a template 40
# commits behind. At this distance checkout still rewrites less than it copies.
REFRESH_WHEN_BEHIND = 40
TEMPLATE_DIRNAME = "goalflight-seat-templates"
REPORT_NAME = "goalflight-cow.json"
REPORT_VERSION = 1

_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMNEXT_CLONEID = 0x00000100
_FSOPT_ATTR_CMN_EXTENDED = 0x00000020
_FSOPT_NOFOLLOW = 0x00000001


class CowFallback(Exception):
    """Population cannot use a template. The caller checks out the same seat."""

    def __init__(
        self,
        reason: str,
        *,
        profile_digest: str | None = None,
        filesystem: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.profile_digest = profile_digest
        self.filesystem = filesystem


class _Generation:
    def __init__(
        self,
        *,
        payload: Path,
        commit: str,
        gen_id: str,
        digest: str,
        behind: int,
        fd: int,
        checkout_config: tuple[bool | None, bool | None] | None,
    ) -> None:
        self.payload = payload
        self.commit = commit
        self.gen_id = gen_id
        self.digest = digest
        self.behind = behind
        self.checkout_config = checkout_config
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = -1
        if fd < 0:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _short(text: object, limit: int = 400) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def profile_digest(pattern: str) -> str:
    """Digest the sparse pattern text. The pattern filename is not the key."""
    return hashlib.sha256(pattern.encode("utf-8")).hexdigest()


def _pool():
    import goalflight_worktree_pool as pool

    return pool


def _git_out(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _git_ok(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = _git_out(cwd, *args, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise OSError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _statfs_fstype(path: Path) -> str | None:
    if sys.platform != "darwin":
        return None
    fstype_len = 16
    mount_len = 1024

    class _Fsid(ctypes.Structure):
        _fields_ = [("val", ctypes.c_int32 * 2)]

    class _StatFS(ctypes.Structure):
        _fields_ = [
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", _Fsid),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * fstype_len),
            ("f_mntonname", ctypes.c_char * mount_len),
            ("f_mntfromname", ctypes.c_char * mount_len),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    statfs = libc.statfs
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFS)]
    statfs.restype = ctypes.c_int
    buf = _StatFS()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if statfs(os.fsencode(probe), ctypes.byref(buf)) != 0:
        return None
    return buf.f_fstypename.split(b"\0", 1)[0].decode("ascii", "replace")


def clonefile_support(path: Path) -> tuple[bool, str | None, str | None]:
    """Whether ``cp -c`` can be asked to clone onto this volume.

    Non-Darwin and non-APFS volumes never take the copy-on-write path.
    ``cp -c`` can still copy instead of clone; that is reported after the copy.
    """
    if sys.platform != "darwin":
        return False, "platform-not-darwin", None
    fstype = _statfs_fstype(path)
    if fstype is None:
        return False, "filesystem-not-apfs: unknown", None
    if fstype != "apfs":
        return False, f"filesystem-not-apfs: {fstype}", fstype
    return True, None, fstype


def _clone_id(path: Path) -> int | None:
    """APFS clone id via ``getattrlist(ATTR_CMNEXT_CLONEID)``, or None."""
    if sys.platform != "darwin":
        return None

    class _AttrList(ctypes.Structure):
        _fields_ = [
            ("bitmapcount", ctypes.c_ushort),
            ("reserved", ctypes.c_uint16),
            ("commonattr", ctypes.c_uint32),
            ("volattr", ctypes.c_uint32),
            ("dirattr", ctypes.c_uint32),
            ("fileattr", ctypes.c_uint32),
            ("forkattr", ctypes.c_uint32),
        ]

    attr = _AttrList()
    attr.bitmapcount = _ATTR_BIT_MAP_COUNT
    attr.forkattr = _ATTR_CMNEXT_CLONEID
    raw = ctypes.create_string_buffer(64)
    libc = ctypes.CDLL(None, use_errno=True)
    getattrlist = libc.getattrlist
    getattrlist.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_AttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_ulong,
    ]
    getattrlist.restype = ctypes.c_int
    options = _FSOPT_ATTR_CMN_EXTENDED | _FSOPT_NOFOLLOW
    if getattrlist(os.fsencode(path), ctypes.byref(attr), raw, len(raw), options) != 0:
        return None
    length = struct.unpack_from("<I", raw, 0)[0]
    if length < 12:
        return None
    value = struct.unpack_from("<Q", raw, 4)[0]
    if value == 0:
        return None
    return value


def templates_root(project_root: Path) -> Path:
    return _pool()._git_common_dir(project_root) / TEMPLATE_DIRNAME


def profile_dir(project_root: Path, digest: str) -> Path:
    return templates_root(project_root) / "profiles" / digest


def profile_lock_path(project_root: Path, digest: str) -> Path:
    return templates_root(project_root) / "locks" / f"{digest}.build"


def generation_lock_path(project_root: Path, digest: str, gen_id: str) -> Path:
    return templates_root(project_root) / "locks" / digest / gen_id


def generation_payload(project_root: Path, digest: str, gen_id: str) -> Path:
    return profile_dir(project_root, digest) / "gens" / gen_id / "payload"


def _open_lock(path: Path, *, create: bool) -> int:
    """Open a lock file without truncating or replacing its inode.

    Readers pass ``create=False``. A missing reader lock is a retired
    generation, and opening it must not recreate the file.
    """
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o644)


def _lock(path: Path, operation: int, *, create: bool) -> int:
    fd = _open_lock(path, create=create)
    try:
        fcntl.flock(fd, operation)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _try_exclusive(path: Path, *, create: bool) -> tuple[str, int | None]:
    """Non-blocking exclusive lock.

    ``missing`` is only for an absent file opened without creating it, so no
    reader can hold that lock. ``busy`` means the lock is held or not safe to
    treat as free. ``free`` means the caller holds it.
    """
    try:
        fd = _lock(path, fcntl.LOCK_EX | fcntl.LOCK_NB, create=create)
    except FileNotFoundError:
        return "missing", None
    except BlockingIOError:
        return "busy", None
    except OSError:
        return "busy", None
    return "free", fd


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def linked_git_dir(worktree_path: Path) -> Path | None:
    marker = worktree_path / ".git"
    try:
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text.lower().startswith("gitdir:"):
        return None
    git_dir = Path(text.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = worktree_path / git_dir
    return git_dir


def write_seat_cow_report(worktree_path: Path, payload: dict) -> None:
    git_dir = linked_git_dir(worktree_path)
    if git_dir is None:
        raise OSError(f"worktree {worktree_path} has no gitdir pointer")
    _write_json_atomic(git_dir / REPORT_NAME, payload)


def read_seat_cow_report(worktree_path: Path) -> dict | None:
    git_dir = linked_git_dir(worktree_path)
    if git_dir is None:
        return None
    try:
        return json.loads((git_dir / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def write_plain_report(
    worktree_path: Path,
    *,
    reason: str,
    pattern: str,
    operation: str = "new",
    cow_skipped: bool = False,
) -> None:
    _supported, _reason, filesystem = clonefile_support(worktree_path)
    report = {
        "version": REPORT_VERSION,
        "population": "plain",
        "clone": None,
        "reason": reason,
        "generation": None,
        "template_commit": None,
        "profile_digest": profile_digest(pattern),
        "behind": None,
        "filesystem": filesystem,
        "operation": operation,
    }
    if cow_skipped:
        report["cow_skipped"] = True
    write_seat_cow_report(worktree_path, report)


def _commits_behind(project_root: Path, template_commit: str, base_commit: str) -> int | None:
    if template_commit == base_commit:
        return 0
    ancestor = _git_out(
        project_root,
        "merge-base",
        "--is-ancestor",
        template_commit,
        base_commit,
    )
    if ancestor.returncode != 0:
        return None
    counted = _git_ok(
        project_root, "rev-list", "--count", f"{template_commit}..{base_commit}"
    )
    return int(counted)


def _rm_real_dir(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not os.path.isdir(path) or os.path.islink(path):
        raise OSError(f"refusing to remove {path}")
    del info
    shutil.rmtree(path)


def _tree_rows(root: Path) -> list[tuple[str, str, str | None, int | None]]:
    rows: list[tuple[str, str, str | None, int | None]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relative = base.relative_to(root)
        dirnames.sort()
        filenames.sort()
        for name in list(dirnames):
            path = base / name
            rel = (relative / name).as_posix()
            if path.is_symlink():
                rows.append((rel, "symlink", os.readlink(path), None))
                dirnames.remove(name)
            else:
                rows.append((rel, "dir", None, None))
        for name in filenames:
            if relative == Path(".") and name == ".git":
                continue
            path = base / name
            rel = name if relative == Path(".") else (relative / name).as_posix()
            if path.is_symlink():
                rows.append((rel, "symlink", os.readlink(path), None))
            elif path.is_file():
                rows.append((rel, "file", None, path.stat().st_size))
            else:
                rows.append((rel, "other", None, None))
    rows.sort()
    return rows


def _classify_clone(
    src: Path,
    dst: Path,
    rows: list[tuple[str, str, str | None, int | None]] | None = None,
) -> str:
    matches = 0
    unproven = 0
    for rel, kind, _link, size in rows if rows is not None else _tree_rows(src):
        if kind != "file":
            continue
        src_path = src / rel
        dst_path = dst / rel
        src_id = _clone_id(src_path)
        dst_file = dst_path.is_file() and not dst_path.is_symlink()
        dst_id = _clone_id(dst_path) if dst_file else None
        if src_id is not None and src_id == dst_id:
            matches += 1
            continue
        if size == 0 and dst_file and dst_path.stat().st_size == 0:
            continue
        if src_id is None or dst_id is None:
            unproven += 1
        else:
            return "full-copy"
    if matches and not unproven:
        return "clonefile"
    return "full-copy"


def _check_overlay_layout(src: Path, dst: Path) -> None:
    """Refuse cp's ambiguous file/directory and symlink replacement cases."""
    for rel, kind, _link, _size in _tree_rows(src):
        source = src / rel
        target = dst / rel
        for parent in source.parents:
            if parent == src:
                break
            target_parent = dst / parent.relative_to(src)
            try:
                mode = target_parent.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise OSError(f"overlay parent is not a directory: {target_parent}")
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            continue
        if kind == "dir":
            compatible = stat.S_ISDIR(mode)
        elif kind == "symlink":
            compatible = stat.S_ISLNK(mode)
        elif kind == "file":
            compatible = stat.S_ISREG(mode)
        else:
            compatible = False
        if not compatible:
            raise OSError(f"overlay path type differs: {target}")


def _clone_matches_source(
    src_rows: list[tuple[str, str, str | None, int | None]],
    dst: Path,
    *,
    allow_extra: bool,
) -> bool:
    dst_by_path = {row[0]: row[1:] for row in _tree_rows(dst)}
    for rel, kind, link, size in src_rows:
        if dst_by_path.get(rel) != (kind, link, size):
            return False
    return allow_extra or _tree_rows(dst) == src_rows


def _template_fits_base(project_root: Path, payload: Path, base_commit: str) -> bool:
    result = _git_bytes(project_root, "ls-tree", "-rz", "--full-tree", base_commit)
    if result.returncode != 0:
        return False
    target: dict[str, str] = {}
    for row in result.stdout.split(b"\0"):
        if not row:
            continue
        try:
            header, raw_path = row.split(b"\t", 1)
            mode, object_type, _oid = header.decode("ascii").split()
            path = os.fsdecode(raw_path)
        except (ValueError, UnicodeError):
            return False
        kind = "symlink" if mode == "120000" else "gitlink" if object_type == "commit" else "file"
        target[path] = kind
    for rel, kind, _link, _size in _tree_rows(payload):
        if kind == "dir":
            if rel in target and target[rel] != "gitlink":
                return False
            continue
        if kind not in {"file", "symlink"} or target.get(rel) != kind:
            return False
        components = rel.split("/")
        if any("/".join(components[:end]) in target for end in range(1, len(components))):
            return False
    return True


def clone_tree_payload(src: Path, dst: Path, *, overlay: bool = False) -> str:
    """Clone a template payload into a new checkout or a cleaned seat."""
    if overlay:
        _check_overlay_layout(src, dst)
    copied = False
    for entry in os.scandir(src):
        if entry.name == ".git":
            continue
        result = subprocess.run(
            ["cp", "-c", "-R", "-p", "--", entry.path, str(dst)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
            raise OSError(f"cp -c {entry.name} failed: {detail}")
        copied = True
    if not copied:
        raise OSError("template payload is empty")
    src_rows = _tree_rows(src)
    if not _clone_matches_source(src_rows, dst, allow_extra=overlay):
        raise OSError("cloned payload does not match the template tree")
    return _classify_clone(src, dst, src_rows)


def normalize_checkout_modes(
    worktree_path: Path, *, preserve_root_mode: bool = False
) -> None:
    """Apply the current checkout's umask modes to tracked files and directories."""
    shared = _config_get(worktree_path, "core.sharedRepository")
    preserve_root_mode = preserve_root_mode or (
        shared is not None
        and shared.lower() not in {"false", "umask"}
    )
    umask = os.umask(0)
    os.umask(umask)
    listed = _git_bytes(worktree_path, "ls-files", "-s", "-z")
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", "replace").strip() or "git ls-files failed"
        raise OSError(detail)
    directories = set() if preserve_root_mode else {worktree_path}
    for row in listed.stdout.split(b"\0"):
        if not row:
            continue
        metadata, separator, raw_path = row.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) < 3:
            raise OSError("malformed git ls-files mode entry")
        mode = fields[0]
        path = worktree_path / os.fsdecode(raw_path)
        parent = path.parent
        while parent != worktree_path:
            directories.add(parent)
            parent = parent.parent
        if mode in {b"100644", b"100755"}:
            if path.is_file() and not path.is_symlink():
                base_mode = 0o777 if mode == b"100755" else 0o666
                os.chmod(path, base_mode & ~umask)
        elif mode == b"120000" and path.is_symlink():
            lchmod = getattr(os, "lchmod", None)
            if lchmod is not None:
                try:
                    lchmod(path, 0o777 & ~umask)
                except NotImplementedError:
                    pass
                except OSError as exc:
                    if exc.errno not in {
                        errno.EINVAL,
                        errno.ENOTSUP,
                        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                    }:
                        raise
        elif mode == b"160000":
            directories.add(path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        if directory.is_dir() and not directory.is_symlink():
            os.chmod(directory, 0o777 & ~umask)
    if not preserve_root_mode:
        os.chmod(worktree_path, 0o777 & ~umask)


def _materialize_sparse_payload(
    project_root: Path,
    payload: Path,
    private: Path,
    commit: str,
    pattern: str,
) -> None:
    """Write ``commit``'s sparse files into ``payload`` without registering a worktree.

    Hooks do not run: the private git directory is not a checkout, and every
    command points ``core.hooksPath`` at an empty directory that is deleted
    with it.
    """
    common = _pool()._git_common_dir(project_root)
    hooks = private / "no-hooks"
    info = private / "info"
    info.mkdir(parents=True, exist_ok=True)
    hooks.mkdir(exist_ok=True)
    payload.mkdir(parents=True, exist_ok=True)
    (private / "commondir").write_text(str(common) + "\n", encoding="utf-8")
    (private / "HEAD").write_text(commit + "\n", encoding="utf-8")
    (info / "sparse-checkout").write_text(pattern, encoding="utf-8")

    def git(*args: str) -> str:
        return _git_ok(
            payload,
            "--git-dir",
            str(private),
            "--work-tree",
            str(payload),
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "core.sparseCheckout=true",
            "-c",
            "core.sparseCheckoutCone=false",
            *args,
        )

    git("read-tree", commit)
    git("sparse-checkout", "reapply")
    git("checkout-index", "--all")
    # Seat preparation rejects any Git context that would convert these bytes.
    # Keep checkout-index's output and modes; rewriting blobs here doubled cold
    # template writes and discarded its umask-derived permissions.
    status = git(
        "status",
        "--porcelain",
        "--ignored",
        "--untracked-files=all",
    )
    if status:
        raise CowFallback(f"template-not-pristine: {_short(status.splitlines()[0])}")


# Asked of Git, in order, for every path in the template commit and the base.
# check-attr prints a state word and a literal value with the same text.
# filter=unset and filter=unspecified name drivers, and check-attr prints
# those names as the state words. filter is accepted only when unspecified
# and no filter.unspecified.* config exists; a real -filter takes a plain
# seat. text, crlf, and ident may
# be unspecified or unset: convert.c treats an unknown string value of those
# three as undefined, so a literal "unset" does not change checkout bytes.
# eol and working-tree-encoding change bytes when set, so only unspecified.
_CHECK_ATTRS = (
    "text",
    "eol",
    "crlf",
    "filter",
    "ident",
    "working-tree-encoding",
)
_ATTR_UNSET_OK = frozenset({"text", "crlf", "ident"})


def _config_get(cwd: Path, key: str) -> str | None:
    """Return a config value with its trailing newline removed, or None if unset.

    Other whitespace stays. ``core.attributesFile`` can end in a space, and
    stripping it would point at a different file.
    """
    result = _git_out(cwd, "config", "--get", key)
    if result.returncode == 0:
        text = result.stdout
        if text.endswith("\n"):
            text = text[:-1]
        return text
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise OSError(f"git config --get {key} failed: {detail}")


def _git_bytes(
    cwd: Path, *args: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


# Bytes of pathname text per ``check-attr`` call. Paths are passed as ARGUMENTS,
# so the bound that matters is the kernel's argument-block limit, not a count:
# macOS ARG_MAX is 1 MiB (``getconf ARG_MAX`` = 1048576) and the block also holds
# argv[0], the options, and the whole environment. 128 KiB leaves an order of
# magnitude of headroom for even a very large environment, and costs little:
# measured 2026-10-24, one call carrying 4000 of project-a's paths (~452 KiB of
# pathname text) returned in 0.81 s, so a 128 KiB batch is a fraction of that and
# a 17,156-path tree needs ~15 calls.
_CHECK_ATTR_BATCH_BYTES = 128 * 1024

# Seconds for one batch. Generous: the slowest measured call of any batch size on
# any repo here was 0.81 s, so this fires only on a genuine fault, never on work.
#
# Treat it as a backstop for an UNKNOWN future fault, not as the guard against
# the fault below. Review measured (2026-10-24) that `communicate(timeout=5)`
# did not raise against the original `--stdin` hang at all -- only an external
# SIGALRM broke that wait -- so a timeout here would not have caught the very
# bug this code was written for. What rules that bug out is that there is no
# stdin pipe any more, not this number.
_CHECK_ATTR_TIMEOUT_S = 120


def _check_attr_batches(paths: list[bytes]) -> Iterator[list[bytes]]:
    """Split paths into argument batches bounded by pathname bytes, never empty."""
    batch: list[bytes] = []
    used = 0
    for path in paths:
        # +1 for the NUL the kernel appends to each argument.
        cost = len(path) + 1
        if batch and used + cost > _CHECK_ATTR_BATCH_BYTES:
            yield batch
            batch = []
            used = 0
        batch.append(path)
        used += cost
    if batch:
        yield batch


def _check_attr_argv(
    cwd: Path, commit: str, batch: list[bytes]
) -> subprocess.CompletedProcess[bytes] | None:
    """One ``check-attr`` call over a batch, or None when it had to be killed.

    Paths are arguments and stdin is /dev/null, which is the whole point: the
    ``--stdin`` form of this call deadlocks. Measured 2026-10-24 on project-a (17,156
    paths, no .gitattributes anywhere, so no rule can match): one path through
    ``--stdin`` returned in 0.06 s, 500 paths never returned, and the hung ``git``
    had used 0.01 s of CPU after 28 minutes, i.e. it was blocked rather than
    working. Six such processes were found across the fleet, the oldest 5 h 23 m,
    each holding a dispatcher's seat locks. A timeout alone does NOT fix it:
    ``Popen.communicate`` did not honour its own timeout against that child, and a
    SIGKILL to the process group followed by a bounded reap did not return either.
    Passing the same paths as arguments returns in 0.81 s for 4000 of them.
    """
    argv = [
        "git",
        "check-attr",
        f"--source={commit}",
        "-z",
        *_CHECK_ATTRS,
        "--",
        *[os.fsdecode(path) for path in batch],
    ]
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=_CHECK_ATTR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Kill the group, not the pid: a stuck git may have children, and leaving
        # one behind is how this fault held seat locks for hours. The child has
        # its own session, so this cannot reach the dispatcher.
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=10)
        finally:
            # If even the post-SIGKILL reap times out, communicate() leaves the
            # pipes open and they would survive until Popen is collected. This
            # runs inside a dispatcher holding seat locks, so close them here
            # rather than relying on GC.
            for stream in (process.stdout, process.stderr, process.stdin):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        return None
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def _check_attr_block(cwd: Path, commit: str) -> str | None:
    """Plain-seat reason from ``git check-attr --source``, or None.

    Any failure, including an unsupported ``--source``, is a plain seat.
    Git applies macros, ``info/attributes``, and the global and system files.
    """
    listed = _git_bytes(cwd, "ls-tree", "-r", "-z", "--name-only", commit)
    if listed.returncode != 0:
        return "conversion:check-attr"
    paths = [path for path in listed.stdout.split(b"\0") if path]
    if not paths:
        return None
    for batch in _check_attr_batches(paths):
        checked = _check_attr_argv(cwd, commit, batch)
        if checked is None or checked.returncode != 0:
            return "conversion:check-attr"
        fields = checked.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) != len(batch) * len(_CHECK_ATTRS) * 3:
            return "conversion:check-attr"
        for index in range(0, len(fields), 3):
            try:
                name = fields[index + 1].decode("utf-8")
                info = fields[index + 2].decode("utf-8")
            except UnicodeError:
                return "conversion:check-attr"
            if name == "filter":
                blocked = info != "unspecified"
            elif name in _ATTR_UNSET_OK:
                blocked = info not in {"unspecified", "unset"}
            else:
                blocked = info != "unspecified"
            if blocked:
                return f"conversion:{name}"
    return None


def _filter_unspecified_configured(cwd: Path) -> bool:
    """True when a driver is literally named unspecified.

    ``check-attr`` prints that name as the state word ``unspecified``.
    """
    result = _git_out(cwd, "config", "--get-regexp", r"^filter\.unspecified\.")
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise OSError(f"git config --get-regexp filter.unspecified failed: {detail}")


def _config_bool(cwd: Path, key: str, *prefix: str) -> bool | None:
    """Canonical bool from ``git config --type=bool``, or None when unset."""
    result = _git_out(cwd, *prefix, "config", "--type=bool", "--get", key)
    if result.returncode == 0:
        text = result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout
        if text == "true":
            return True
        if text == "false":
            return False
        raise OSError(f"git config --type=bool --get {key} returned {text!r}")
    if result.returncode == 1:
        return None
    detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    raise OSError(f"git config --type=bool --get {key} failed: {detail}")


def _stored_bool(meta: dict, key: str) -> bool | None:
    value = meta[key]
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(key)


def _stored_checkout_config(meta: dict) -> tuple[bool | None, bool | None] | None:
    """Symlinks and precomposeUnicode recorded with the generation, if both are present."""
    try:
        return (
            _stored_bool(meta, "core.symlinks"),
            _stored_bool(meta, "core.precomposeUnicode"),
        )
    except (KeyError, TypeError):
        return None


def conversion_block_reason(
    cwd: Path,
    template_commit: str,
    base_commit: str,
    checkout_config: tuple[bool | None, bool | None] | None,
) -> str | None:
    """Why this Git context must not use a template, or None.

    ``cwd`` is the new seat, so every lookup is ``git -C <seat>`` in the
    dispatcher's environment, the same context a plain checkout uses.
    ``checkout_config`` is ``(core.symlinks, core.precomposeUnicode)`` as
    canonical bools from the template build, or None when a generation did
    not record them.
    """
    shared = _config_get(cwd, "core.sharedRepository")
    if shared is not None and shared.lower() not in {"false", "umask"}:
        return "shared-repository-mode-unsupported"
    if "GIT_ATTR_SOURCE" in os.environ:
        return "conversion:attr-source"
    if _config_get(cwd, "attr.tree") is not None:
        return "conversion:attr-tree"
    autocrlf = _config_get(cwd, "core.autocrlf")
    if autocrlf is not None and autocrlf != "false":
        return "conversion:autocrlf"
    if _config_get(cwd, "core.eol") is not None:
        return "conversion:eol"
    # includeIf gitdir/onbranch can see a different core.symlinks in the seat
    # than the private git dir that wrote the template. A false seat writes a
    # regular file where the template has a symlink, and checkout then leaves
    # the cloned symlink. Unset or true on both sides is the only safe case.
    if checkout_config is None:
        return "conversion:symlinks"
    recorded_symlinks, recorded_precompose = checkout_config
    symlinks = _config_bool(cwd, "core.symlinks")
    if recorded_symlinks not in (None, True) or symlinks not in (None, True):
        return "conversion:symlinks"
    if _config_bool(cwd, "core.precomposeUnicode") != recorded_precompose:
        return "conversion:precomposeunicode"
    if _filter_unspecified_configured(cwd):
        return "conversion:filter"
    seen: set[str] = set()
    for commit in (template_commit, base_commit):
        if commit in seen:
            continue
        seen.add(commit)
        blocked = _check_attr_block(cwd, commit)
        if blocked:
            return blocked
    return None


def _unlink_file(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def _drop_reader_lock_if_free(path: Path) -> None:
    """Unlink a reader lock when no reader holds it. Leave it when the lock is busy."""
    state, fd = _try_exclusive(path, create=False)
    if state != "free" or fd is None:
        return
    try:
        _unlink_file(path)
    finally:
        _unlock(fd)


def _read_meta(profile: Path, gen_id: str) -> dict | None:
    try:
        payload = json.loads((profile / "gens" / gen_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("commit"), str):
        return None
    return payload


def _remove_other_generations(project_root: Path, digest: str, *, keep: str) -> None:
    gens = profile_dir(project_root, digest) / "gens"
    try:
        children = list(gens.iterdir())
    except FileNotFoundError:
        children = []
    for child in children:
        if child.name == keep or child.is_symlink() or not child.is_dir():
            continue
        lock = generation_lock_path(project_root, digest, child.name)
        state, fd = _try_exclusive(lock, create=False)
        if state == "busy":
            continue
        try:
            _rm_real_dir(child)
            # ``missing`` has no reader: readers do not create the lock file.
            # ``free`` holds the only lock, so the inode can go with the payload.
            if state == "free":
                _unlink_file(lock)
        finally:
            if fd is not None:
                _unlock(fd)
    lock_dir = templates_root(project_root) / "locks" / digest
    try:
        leftovers = list(lock_dir.iterdir())
    except FileNotFoundError:
        return
    for leftover in leftovers:
        if leftover.name == keep or leftover.is_symlink() or not leftover.is_file():
            continue
        if (gens / leftover.name).exists():
            continue
        _drop_reader_lock_if_free(leftover)


def _retire_other_profiles(project_root: Path, *, keep: str) -> None:
    root = templates_root(project_root) / "profiles"
    try:
        children = list(root.iterdir())
    except FileNotFoundError:
        return
    for child in children:
        if child.name == keep or child.is_symlink() or not child.is_dir():
            continue
        if len(child.name) != 64:
            continue
        build_state, build = _try_exclusive(
            profile_lock_path(project_root, child.name), create=True
        )
        if build_state != "free" or build is None:
            continue
        held: list[tuple[int, str]] = []
        try:
            gens = child / "gens"
            busy = False
            if gens.is_dir():
                for gen in gens.iterdir():
                    if gen.is_symlink() or not gen.is_dir():
                        continue
                    state, fd = _try_exclusive(
                        generation_lock_path(project_root, child.name, gen.name),
                        create=False,
                    )
                    if state == "busy":
                        busy = True
                        break
                    if state == "free" and fd is not None:
                        held.append((fd, gen.name))
            if not busy:
                _rm_real_dir(child)
                for fd, gen_name in held:
                    _unlink_file(generation_lock_path(project_root, child.name, gen_name))
                lock_dir = templates_root(project_root) / "locks" / child.name
                try:
                    for leftover in list(lock_dir.iterdir()):
                        if leftover.is_file() and not leftover.is_symlink():
                            _drop_reader_lock_if_free(leftover)
                    lock_dir.rmdir()
                except (FileNotFoundError, OSError):
                    pass
        finally:
            for fd, _gen_name in held:
                _unlock(fd)
            _unlock(build)


def _publish_generation(
    project_root: Path,
    *,
    digest: str,
    commit: str,
    pattern: str,
) -> str:
    profile = profile_dir(project_root, digest)
    gen_id = f"gen-{commit[:12]}-{os.getpid()}-{time.monotonic_ns()}"
    staging = profile / "building" / gen_id
    payload = staging / "payload"
    private = staging / "private-git"
    final = profile / "gens" / gen_id
    try:
        _materialize_sparse_payload(project_root, payload, private, commit, pattern)
        # The private git dir is the context that wrote the payload. A seat
        # includeIf does not apply here, so these are the bytes' config.
        git_prefix = ("--git-dir", str(private), "--work-tree", str(payload))
        symlinks = _config_bool(payload, "core.symlinks", *git_prefix)
        precompose = _config_bool(payload, "core.precomposeUnicode", *git_prefix)
        _rm_real_dir(private)
        _write_json_atomic(
            staging / "meta.json",
            {
                "version": REPORT_VERSION,
                "commit": commit,
                "profile_digest": digest,
                "core.symlinks": symlinks,
                "core.precomposeUnicode": precompose,
            },
        )
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, final)
        # The reader lock exists before ``current`` moves. Readers do not create it.
        lock_fd = _open_lock(
            generation_lock_path(project_root, digest, gen_id), create=True
        )
        os.close(lock_fd)
        pointer = profile / "current"
        temporary = profile / f".current.{os.getpid()}.tmp"
        temporary.write_text(gen_id + "\n", encoding="utf-8")
        os.replace(temporary, pointer)
    except BaseException:
        _rm_real_dir(staging)
        if final.exists() and _read_text(profile / "current") != gen_id:
            _rm_real_dir(final)
            _drop_reader_lock_if_free(generation_lock_path(project_root, digest, gen_id))
        raise
    try:
        _remove_other_generations(project_root, digest, keep=gen_id)
    except OSError:
        pass
    return gen_id


def prepare_for_new_seat(
    project_root: Path,
    *,
    base_commit: str,
    pattern: str,
) -> str | None:
    """Publish a usable generation, or return a plain-fallback reason.

    Expected I/O failures stay in the reason. They do not escape to the caller,
    so a plain seat can still be created. No template directory is created when
    this volume cannot clone.
    """
    supported, fs_reason, _filesystem = clonefile_support(project_root)
    if not supported:
        return fs_reason or "filesystem-not-apfs"
    digest = profile_digest(pattern)
    try:
        fd = _lock(profile_lock_path(project_root, digest), fcntl.LOCK_EX, create=True)
    except OSError as exc:
        return f"template-build-failed: {_short(exc)}"
    try:
        profile = profile_dir(project_root, digest)
        try:
            _rm_real_dir(profile / "building")
        except OSError as exc:
            return f"template-build-failed: {_short(exc)}"
        current = _read_text(profile / "current")
        meta = _read_meta(profile, current) if current else None
        commit = str(meta.get("commit")) if meta else ""
        behind = _commits_behind(project_root, commit, base_commit) if commit else None
        lock_ready = bool(current) and generation_lock_path(
            project_root, digest, current
        ).is_file()
        reusable = (
            behind is not None
            and behind <= REFRESH_WHEN_BEHIND
            and lock_ready
        )
        if reusable:
            try:
                _remove_other_generations(project_root, digest, keep=current or "")
                _retire_other_profiles(project_root, keep=digest)
            except OSError:
                pass
            return None
        try:
            _publish_generation(
                project_root,
                digest=digest,
                commit=base_commit,
                pattern=pattern,
            )
        except CowFallback as exc:
            return exc.reason
        try:
            _retire_other_profiles(project_root, keep=digest)
        except OSError:
            pass
        return None
    except OSError as exc:
        return f"template-build-failed: {_short(exc)}"
    finally:
        _unlock(fd)


def open_usable_generation(
    project_root: Path,
    *,
    base_commit: str,
    pattern: str,
) -> _Generation | None:
    """Lock a published generation shared. The directory stays until release."""
    digest = profile_digest(pattern)
    profile = profile_dir(project_root, digest)
    gen_id = _read_text(profile / "current")
    if not gen_id:
        return None
    meta = _read_meta(profile, gen_id)
    if meta is None:
        return None
    commit = str(meta["commit"])
    behind = _commits_behind(project_root, commit, base_commit)
    if behind is None or behind > REFRESH_WHEN_BEHIND:
        return None
    payload = generation_payload(project_root, digest, gen_id)
    try:
        fd = _lock(
            generation_lock_path(project_root, digest, gen_id),
            fcntl.LOCK_SH,
            create=False,
        )
    except OSError:
        return None
    if _read_text(profile / "current") != gen_id or not payload.is_dir():
        _unlock(fd)
        return None
    return _Generation(
        payload=payload,
        commit=commit,
        gen_id=gen_id,
        digest=digest,
        behind=behind,
        fd=fd,
        checkout_config=_stored_checkout_config(meta),
    )


def _configure_sparse(worktree_path: Path, pattern: str) -> None:
    """Record the sparse pattern without checking files out."""
    pool = _pool()
    git_dir = pool._git_dir(worktree_path)
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "sparse-checkout").write_text(pattern, encoding="utf-8")
    pool._git(worktree_path, "config", "--worktree", "core.sparseCheckout", "true")
    pool._git(worktree_path, "config", "--worktree", "core.sparseCheckoutCone", "false")


def prime_sparse_worktree(
    project_root: Path,
    worktree_path: Path,
    *,
    base_commit: str,
    pattern: str,
    sparse: bool = True,
    overlay: bool = False,
    operation: str = "new",
) -> dict:
    """Clone a template and prepare the index without running checkout hooks.

    ``overlay`` is used only after normal seat reclamation has preserved prior
    work and cleared disposable files.
    """
    supported, fs_reason, filesystem = clonefile_support(worktree_path)
    digest = profile_digest(pattern)
    kwargs = {"profile_digest": digest, "filesystem": filesystem}
    if not supported:
        raise CowFallback(fs_reason or "filesystem-not-apfs", **kwargs)
    pool = _pool()
    generation = None
    try:
        try:
            generation = open_usable_generation(
                project_root, base_commit=base_commit, pattern=pattern
            )
            if generation is None:
                raise CowFallback("no-template", **kwargs)
            blocked = conversion_block_reason(
                worktree_path,
                generation.commit,
                base_commit,
                generation.checkout_config,
            )
            if blocked:
                raise CowFallback(blocked, **kwargs)
            if overlay:
                if not _template_fits_base(
                    project_root, generation.payload, base_commit
                ):
                    raise CowFallback("template-path-mismatch", **kwargs)
                current_head = pool._git(worktree_path, "rev-parse", "HEAD").strip()
                pool._refuse_ignored_tree_collisions(
                    worktree_path,
                    head=current_head,
                    target=generation.commit,
                )
                worktree_config = pool._git_proc(
                    worktree_path, "config", "--bool", "--get", "extensions.worktreeConfig"
                )
                if worktree_config is None or worktree_config.returncode not in (0, 1):
                    raise CowFallback("git-error: sparse-config-unreadable", **kwargs)
                sparse_config = None
                if (
                    worktree_config.returncode == 0
                    and worktree_config.stdout.strip().lower() == "true"
                ):
                    sparse_config = pool._git_proc(
                        worktree_path,
                        "config",
                        "--worktree",
                        "--bool",
                        "--get",
                        "core.sparseCheckout",
                    )
                    if sparse_config is None or sparse_config.returncode not in (0, 1):
                        raise CowFallback("git-error: sparse-config-unreadable", **kwargs)
                if (
                    not sparse
                    and sparse_config is not None
                    and sparse_config.returncode == 0
                    and sparse_config.stdout.strip() == "true"
                ):
                    raise CowFallback("sparse-profile-mismatch", **kwargs)
            if overlay:
                kind = clone_tree_payload(
                    generation.payload, worktree_path, overlay=True
                )
            else:
                kind = clone_tree_payload(generation.payload, worktree_path)
            if kind != "clonefile":
                raise CowFallback("clonefile-not-proven", **kwargs)
            if sparse:
                _configure_sparse(worktree_path, pattern)
            pool._git(worktree_path, "read-tree", generation.commit)
            if sparse:
                pool._git(worktree_path, "sparse-checkout", "reapply")
            refreshed = pool._git_proc(
                worktree_path, "update-index", "-q", "--refresh"
            )
            if refreshed is None or refreshed.returncode not in (0, 1):
                detail = "could not run"
                if refreshed is not None:
                    detail = (
                        refreshed.stderr or refreshed.stdout or ""
                    ).strip() or f"exit {refreshed.returncode}"
                raise CowFallback(f"git-error: refresh-failed: {_short(detail)}", **kwargs)
        except CowFallback:
            raise
        except OSError as exc:
            label = "git-error" if str(exc).startswith("git ") else "clone-copy-failed"
            raise CowFallback(f"{label}: {_short(exc)}", **kwargs) from exc
        except pool.WorktreeSeatError as exc:
            raise CowFallback(f"git-error: {_short(exc)}", **kwargs) from exc
        return {
            "version": REPORT_VERSION,
            "population": "cow",
            "clone": kind,
            "reason": None,
            "generation": generation.gen_id,
            "template_commit": generation.commit,
            "profile_digest": digest,
            "behind": generation.behind,
            "filesystem": filesystem,
            "operation": operation,
        }
    finally:
        if generation is not None:
            generation.release()
