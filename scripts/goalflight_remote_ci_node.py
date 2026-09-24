"""Node-side admission authority, sent through the configured remote executor.

Standard library only. One managed root identifies one physical box's pool.
A token stays held until cleanup has verified the workload tree is dead and
written released under the admission lock. Closing the token fd is not
enough: a draining lease keeps the slot and the token index.
"""
from __future__ import annotations

import base64
import builtins
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

# One file per machine, not per project root. The first managed root is pinned
# here; a later request that names a different root is refused so it cannot
# open a second token pool on this box.
AUTHORITY_FILE = Path("/var/lib/goalflight/remote-ci/authority.json")
SUCCESS_KEEP = 20
SUCCESS_AGE_SECONDS = 7 * 24 * 3600
FAILURE_KEEP = 50
FAILURE_AGE_SECONDS = 14 * 24 * 3600
TREE_GRACE_SECONDS = 0.3
# Distinct from a dead workload. 75 is the retryable capacity refusal.
# 77 is not used: callers were treating 77 as remote death.
EXIT_CAPACITY = 75
EXIT_CANCELLED = 130
EXIT_DEADLINE = 124
PROC_PIDTBSDINFO = 3
# Not in the public SDK header. The kernel accepts flavor 20 and returns
# 40 bytes: two coalition ids, then three reserved uint64s. The first id is
# the resource coalition. proc_listcoalitions is not in libproc; members are
# found by scanning proc_listallpids.
PROC_PIDCOALITIONINFO = 20


class _CoalInfo(ctypes.Structure):
    _fields_ = [("ids", ctypes.c_uint64 * 2), ("reserved", ctypes.c_uint64 * 3)]


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _libproc():
    lib = getattr(_libproc, "handle", None)
    if lib is not None:
        return lib
    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.proc_listallpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    _libproc.handle = lib
    return lib


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def try_lock(path):
    handle = path.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


def locked(path):
    handle = try_lock(path)
    if handle is None:
        return True
    handle.close()
    return False


def limits(root):
    policy = read_json(root / 'policy.json')
    caps = root / 'caps.json'
    if caps.exists():
        for key, value in read_json(caps).items():
            if key not in {'p_cores', 'token_pool_size', 'self_cap'}:
                raise ValueError('unknown live cap: ' + key)
            if type(value) is not int or value <= 0:
                raise ValueError('live caps must be positive integers')
            # A live cap can lower the shared budget, never enlarge it.
            policy[key] = min(value, policy[key]) if key in policy else value
    return policy


def sample(root):
    policy = limits(root)
    load = os.getloadavg()[0]
    if not math.isfinite(load) or load < 0:
        raise ValueError('unknown node load')
    return {'hostname': socket.gethostname(), 'load1': load, **policy}


def record(run):
    value = read_json(run / 'lease.json')
    value.update(read_json(run / 'owner.json'))
    return value


def identity_matches(run, identity):
    current = record(run).get('remote_run', {})
    return all(current.get(key) == identity.get(key) and current.get(key)
               for key in ('pid', 'start_token', 'run_dir'))


def authority_file():
    override = getattr(builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY", None)
    if override:
        return Path(override)
    return AUTHORITY_FILE


def pin_managed_root(managed):
    """Return the one managed root for this box, or refuse a second one."""
    path = authority_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        wanted = os.path.normpath(str(managed))
        if raw.strip():
            pinned = os.path.normpath(json.loads(raw)["managed_root"])
            if pinned != wanted:
                raise ValueError("this box already has one admission authority at " + pinned)
            return Path(pinned)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"managed_root": wanted}))
        handle.flush()
        return Path(wanted)
    finally:
        handle.close()


def safe_repo(value):
    if not isinstance(value, str) or not value or len(value) > 64 or not value[0].isalnum():
        return "default"
    if not all(ch.isalnum() or ch in "_.-" for ch in value):
        return "default"
    return value


def directory_bytes(path):
    total = 0
    if not path.is_dir():
        return 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def append_result(managed, state, run):
    """One durable line. Callers must do this before deleting the body."""
    exit_code = None
    result_path = run / "result.json"
    if result_path.exists():
        try:
            exit_code = read_json(result_path).get("returncode")
        except (OSError, ValueError):
            exit_code = None
    line = {
        "run_id": state.get("lease_id"),
        "repo": state.get("repo") or "default",
        "sha": state.get("sha") or "",
        "slot": state.get("slot") or "",
        "host": (state.get("remote_run") or {}).get("host") or socket.gethostname(),
        "start": state.get("started_at"),
        "finish": time.time(),
        "status": state.get("release_reason") or state.get("state"),
        "exit_code": exit_code,
        "body_path": str(run),
        "bytes": directory_bytes(run),
    }
    path = managed / "results" / "index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(line, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    # Every append fsyncs the directory. A retry after a crash between the
    # file fsync and the directory fsync must not skip it just because the
    # file is already visible.
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def result_ids(managed):
    path = managed / "results" / "index.jsonl"
    found = set()
    if not path.exists():
        return found
    try:
        text = path.read_text()
    except OSError:
        return None
    for raw in text.splitlines():
        try:
            found.add(json.loads(raw).get("run_id"))
        except ValueError:
            continue
    found.discard(None)
    return found


def audit(root, event):
    path = root / "audit.log"
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _limit(request, key, default, numeric):
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, numeric) or value < 0:
        return default
    return min(default, value)


def cwd_snapshot():
    """One lsof of every process cwd. None means the snapshot cannot be trusted."""
    try:
        proc = subprocess.run(["lsof", "-nP", "-d", "cwd", "-F", "pn"],
                              capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode not in (0, 1):
        return None
    rows = []
    pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            rows.append((pid, line[1:]))
    return rows


def _canon_path(path):
    """Resolve symlinks. None if the path cannot be named.

    ``/var`` is a symlink to ``/private/var`` on macOS. ``lsof`` reports the
    resolved cwd. Comparing only ``normpath`` strings then says a live
    process is not in the slot, and that ``[]`` releases it.
    """
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def cwd_intruders(paths):
    """Pids whose cwd is under ``paths``. None means the snapshot failed.

    Slot, GC, and pre-launch checks only. This is not proof a workload tree
    is dead; that proof is the launchd coalition.
    """
    snap = cwd_snapshot()
    if snap is None:
        return None
    roots = []
    for path in paths:
        if not path:
            continue
        resolved = _canon_path(path)
        if resolved is None:
            return None
        roots.append(resolved)
    found = []
    for pid, cwd in snap:
        if not cwd:
            continue
        cwd_n = _canon_path(cwd)
        if cwd_n is None:
            return None
        if not any(cwd_n == root or cwd_n.startswith(root + os.sep) for root in roots):
            continue
        try:
            os.getpgid(pid)
        except OSError:
            continue
        found.append(pid)
    return found


def _start_time(pid):
    try:
        lib = _libproc()
        info = _ProcBsdInfo()
        size = lib.proc_pidinfo(int(pid), PROC_PIDTBSDINFO, 0,
                                ctypes.byref(info), ctypes.sizeof(info))
    except OSError:
        return None
    if size != ctypes.sizeof(info) or int(info.pbi_pid) != int(pid):
        return None
    return (int(info.pbi_start_tvsec), int(info.pbi_start_tvusec))


def _pid_exists(pid):
    """False for exited processes and zombies. None if liveness is unreadable.

    ``kill(pid, 0)`` succeeds on a zombie, and a zombie is not a live member.
    ``getpgid`` fails with ESRCH for both, which is the check cleanup needs.
    """
    try:
        os.getpgid(int(pid))
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _same_process(pid, started):
    """True, False, or None when the pid cannot be tied to ``started``."""
    exists = _pid_exists(pid)
    if exists is False:
        return False
    if exists is None:
        return None
    now = _start_time(pid)
    if started is None or now is None:
        return None
    return now == started


def _as_start(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _coalition_id(pid):
    """Resource coalition id, or None when the query fails.

    Zero is a successful read of "no resource coalition", not a failure.
    Callers must not treat None as non-membership.
    """
    try:
        lib = _libproc()
        info = _CoalInfo()
        size = lib.proc_pidinfo(int(pid), PROC_PIDCOALITIONINFO, 0,
                                ctypes.byref(info), ctypes.sizeof(info))
    except OSError:
        return None
    if size != ctypes.sizeof(info):
        return None
    return int(info.ids[0])


def _all_pids():
    """Every pid. None means the list itself failed.

    A short buffer returns a truncated count with no overflow flag, same as
    proc_listchildpids. Grow until the result fits.
    """
    try:
        lib = _libproc()
    except OSError:
        return None
    cap = 256
    while cap <= 65536:
        buf = (ctypes.c_int * cap)()
        try:
            got = lib.proc_listallpids(ctypes.byref(buf), ctypes.sizeof(buf))
        except OSError:
            return None
        if got < 0:
            return None
        if got < cap:
            return [int(buf[i]) for i in range(got) if int(buf[i]) > 1]
        cap *= 2
    return None


def _classify_pid(pid, cid):
    """One pid against one coalition.

    Returns ('member', start), 'other', 'vanished', or None. None is UNKNOWN:
    the query failed or the pid was already gone. 'vanished' means this
    coalition was read and then the process did not stay still. A successful
    read of a different coalition is 'other'.
    """
    found = _coalition_id(pid)
    if found is None:
        return None
    if found != cid:
        return "other"
    start = _start_time(pid)
    again = _coalition_id(pid)
    start_again = _start_time(pid)
    if again != found or start is None or start_again != start:
        # In this coalition, then gone or inconsistent before the read
        # finished. Not the same as a process that was already dead.
        return "vanished"
    return ("member", start)


def _collect_members(cid, pids, found):
    """Classify ``pids`` into ``found``. None if this pass is not proof."""
    for pid in pids:
        kind = _classify_pid(pid, cid)
        if kind == "vanished":
            # A member disappeared mid-classify. A child may already exist
            # and have been omitted from this pid list. Start a new pass.
            return None
        if kind is None:
            if _pid_exists(pid) is False:
                found.pop(pid, None)
                continue
            return None
        if kind == "other":
            found.pop(pid, None)
            continue
        found[pid] = kind[1]
    return found


def _scan_coalition_once(cid):
    """One complete pass. None if the pass is not proof.

    A pid that is already gone, including a zombie, is not a live member.
    A pid that appears during the pass is classified too: it may be the child
    of the one that disappeared. A live pid whose coalition and start time
    cannot be read together is UNKNOWN. Unrelated new pids are not.
    """
    pids = _all_pids()
    if pids is None:
        return None
    found = {}
    seen = set(pids)
    if _collect_members(cid, pids, found) is None:
        return None
    # Fold in births until a snapshot adds nobody. A parent that exits after
    # fork has already created its child, so the child is in a later snapshot.
    for _ in range(8):
        again = _all_pids()
        if again is None:
            return None
        born = [pid for pid in again if pid not in seen]
        if not born:
            break
        if _collect_members(cid, born, found) is None:
            return None
        seen.update(again)
    else:
        return None
    living = []
    for pid, start in found.items():
        verdict = _incarnation_in_coalition(pid, start, cid)
        # False is not proof this pid left no child. It can exit after the
        # last snapshot and the child is absent from this pass. Unknown is
        # the same: this pass cannot prove the coalition is empty. Only a
        # later pass that finds nobody does.
        if verdict is not True:
            return None
        living.append((pid, start))
    return living


def _coalition_members(cid):
    """(pid, start) for every process in this resource coalition.

    None means UNKNOWN. An empty list is one finished pass that saw nobody
    from its first snapshot through the confirming check. A member that
    vanishes during that check is not an empty tree: the pass restarts.
    A live pid that cannot be classified, or a birth that never settles,
    is not proof either.
    """
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        return None
    for _ in range(20):
        found = _scan_coalition_once(cid)
        if found is not None:
            return found
    return None


def _incarnation_in_coalition(pid, started, cid):
    """True if this start time is in ``cid``, False if it is gone, else None.

    Membership and start time are re-read together. A pid reused into another
    coalition is not this incarnation and is not signalled.
    """
    same = _same_process(pid, started)
    if same is not True:
        return False if same is False else None
    found = _coalition_id(pid)
    if found is None:
        return None
    after = _same_process(pid, started)
    if after is not True:
        return False if after is False else None
    if found != cid:
        return None
    return True


def _signal_incarnation(pid, started, cid):
    """TERM, then KILL only while pid, start time, and coalition still agree.

    Returns True when that incarnation is gone. A reused pid is not signalled.
    """
    pid = int(pid)
    if (pid <= 1 or pid == os.getpid() or started is None
            or isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0):
        return False

    def gone():
        verdict = _incarnation_in_coalition(pid, started, cid)
        if verdict is True:
            return False
        return True if verdict is False else None

    ready = gone()
    if ready is None:
        return False
    if ready:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + TREE_GRACE_SECONDS
    while time.monotonic() < deadline:
        ready = gone()
        if ready is None:
            return False
        if ready:
            return True
        time.sleep(0.02)
    ready = gone()
    if ready is None:
        return False
    if ready:
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    time.sleep(0.05)
    ready = gone()
    return ready is True


def _member_map(state):
    found = {}
    for member in state.get("members") or []:
        if not isinstance(member, dict):
            continue
        pid = member.get("pid")
        start = _as_start(member.get("start"))
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or start is None:
            continue
        found[pid] = start
    try:
        leader = int(state.get("workload_pid"))
    except (TypeError, ValueError):
        leader = None
    leader_start = _as_start(state.get("workload_start"))
    if leader and leader > 1 and leader_start is not None:
        found.setdefault(leader, leader_start)
    return found


def _store_members(run, state, members):
    state["members"] = [{"pid": pid, "start": [start[0], start[1]]}
                        for pid, start in sorted(members.items())]
    if run is None:
        return
    try:
        lease = read_json(run / "lease.json")
    except (OSError, ValueError):
        return
    lease["members"] = state["members"]
    if state.get("coalition_id"):
        lease["coalition_id"] = state["coalition_id"]
    if state.get("workload_start"):
        lease["workload_start"] = state["workload_start"]
    if state.get("holder_coalition_id"):
        lease["holder_coalition_id"] = state["holder_coalition_id"]
    write_json(run / "lease.json", lease)


_LEASE_KEYS = (
    "launch_label", "coalition_id", "holder_coalition_id", "workload_pid",
    "workload_start", "exit_code", "exit_known", "members", "job_remove_pending",
    "pending_result", "launch_submitted", "launch_seen",
)


def _remember_identity(run, state):
    """Merge launch ownership onto the lease. Never drop a label already there."""
    if run is None:
        return
    try:
        lease = read_json(run / "lease.json")
    except (OSError, ValueError):
        return
    for key in _LEASE_KEYS:
        if key in state:
            lease[key] = state[key]
    write_json(run / "lease.json", lease)


def _private_coalition(state):
    cid = state.get("coalition_id")
    holder = state.get("holder_coalition_id")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        return None
    if cid == holder:
        return None
    return cid


def _gate_open(run):
    return run is not None and (run / "workload-go").is_file()


def _workload_released(run, state):
    """True when the real command was allowed to exec."""
    return _gate_open(run) or bool(state.get("members"))


def clear_tree(managed, state, run):
    """Kill every member of this run's coalition. False keeps the lease.

    Membership is the launchd resource coalition recorded at launch. A child
    reparented to launchd, or a grandchild spawned while its parent handles
    SIGTERM, stays in that coalition. The session coalition is never a
    target. An incomplete enumeration is UNKNOWN, not an empty tree. The
    launchd job is removed only after exit status is captured, and only a
    verified removal lets the lease go.
    """
    del managed
    label = state.get("launch_label") or ""
    if label and _private_coalition(state) is None:
        adopted = _adopt_launch(run, state)
        if adopted == "unknown":
            return False
        if adopted == "empty":
            return True
    _capture_exit_status(run, state)
    cid = _private_coalition(state)
    if cid is None:
        if label or state.get("workload_pid") or state.get("members") or _gate_open(run):
            state["job_remove_pending"] = bool(label)
            _remember_identity(run, state)
            return False
        # No launch identity is not proof the slot is empty. A pre-start
        # holder can be draining with a live cwd and nothing else to name.
        paths = [p for p in (run, state.get("slot")) if p]
        intruders = cwd_intruders(paths)
        if intruders is None or intruders:
            return False
        return True
    return _kill_members(run, state, cid, label)


def _adopt_launch(run, state):
    """Name a job whose coalition was not recorded yet.

    'ready' means state now has a private coalition. 'empty' means the
    workload never exec'd and the job is verified gone. 'unknown' keeps
    the lease.
    """
    label = state.get("launch_label") or ""
    view = _job_view(label)
    if view == "unknown":
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    if view == "absent":
        # Submit returned and the job has not been listed yet. Absence is
        # launchd being slow, not proof nothing was started.
        if _workload_released(run, state) or (
                state.get("launch_submitted") and not state.get("launch_seen")):
            state["job_remove_pending"] = True
            _remember_identity(run, state)
            return "unknown"
        state["job_remove_pending"] = False
        _remember_identity(run, state)
        return "empty"
    kind, pid, code = view
    if kind == "exited":
        if isinstance(code, int) and not isinstance(code, bool):
            state["exit_code"] = code
            state["exit_known"] = True
        if _workload_released(run, state):
            _remember_identity(run, state)
            return "unknown"
        if not _remove_job(label):
            state["job_remove_pending"] = True
            _remember_identity(run, state)
            return "unknown"
        state["job_remove_pending"] = False
        _remember_identity(run, state)
        return "empty"
    identity = _stable_identity(pid)
    if identity is None:
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    start, cid = identity
    holder = state.get("holder_coalition_id")
    if isinstance(holder, bool) or not isinstance(holder, int) or holder <= 0:
        holder = _coalition_id(os.getpid())
    state["workload_pid"] = str(pid)
    state["workload_start"] = [start[0], start[1]]
    if holder is None or cid == holder or cid <= 0:
        _remember_identity(run, state)
        if _workload_released(run, state):
            return "unknown"
        if not _remove_job(label):
            state["job_remove_pending"] = True
            _remember_identity(run, state)
            return "unknown"
        if _same_process(pid, start) is not False:
            state["job_remove_pending"] = True
            _remember_identity(run, state)
            return "unknown"
        state["job_remove_pending"] = False
        _remember_identity(run, state)
        return "empty"
    state["coalition_id"] = cid
    state["holder_coalition_id"] = holder
    _remember_identity(run, state)
    return "ready"


def _kill_members(run, state, cid, label):
    persisted = _member_map(state)
    for _ in range(5):
        scanned = _coalition_members(cid)
        if scanned is None:
            state["job_remove_pending"] = bool(label)
            _store_members(run, state, persisted)
            _remember_identity(run, state)
            return False
        current = dict(persisted)
        for pid, start in scanned:
            if pid <= 1 or pid == os.getpid():
                continue
            current[pid] = start
        living = {}
        for pid, start in current.items():
            verdict = _incarnation_in_coalition(pid, start, cid)
            if verdict is False:
                continue
            if verdict is None:
                state["job_remove_pending"] = bool(label)
                _store_members(run, state, current)
                _remember_identity(run, state)
                return False
            living[pid] = start
        _store_members(run, state, living)
        if not living:
            if not _remove_job(label):
                state["job_remove_pending"] = True
                _remember_identity(run, state)
                return False
            again = _coalition_members(cid)
            if again is None:
                state["job_remove_pending"] = bool(label)
                _remember_identity(run, state)
                return False
            leftovers = [(pid, start) for pid, start in again
                         if pid > 1 and pid != os.getpid()]
            if leftovers:
                persisted = dict(leftovers)
                continue
            state["job_remove_pending"] = False
            _remember_identity(run, state)
            return True
        for pid, start in living.items():
            _signal_incarnation(pid, start, cid)
        persisted = living
    state["job_remove_pending"] = bool(label)
    _store_members(run, state, persisted)
    _remember_identity(run, state)
    return False


def _job_label(lease_id):
    return "com.goalflight.remote-ci." + str(lease_id)


def _launchctl_jobs():
    """label -> (pid or None, exit code or None). None if the list failed."""
    try:
        listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    jobs = {}
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            continue
        label = parts[-1]
        pid_text, status = parts[0], parts[1]
        if label == "Label" and pid_text == "PID":
            continue
        pid = int(pid_text) if pid_text.isdigit() else None
        code = int(status) if status.lstrip("-").isdigit() else None
        jobs[label] = (pid, code)
    return jobs


def _job_view(label):
    """'absent', 'unknown', or ('running'|'exited', pid or None, code or None)."""
    if not label:
        return "absent"
    jobs = _launchctl_jobs()
    if jobs is None:
        return "unknown"
    if label not in jobs:
        return "absent"
    pid, code = jobs[label]
    if pid:
        return ("running", pid, code)
    return ("exited", None, code)


def _remove_job(label):
    """True only when a successful listing proves the label is gone."""
    if not label:
        return True
    if _job_view(label) == "absent":
        return True
    if _job_view(label) == "unknown":
        return False
    try:
        subprocess.run(["launchctl", "remove", label], capture_output=True, text=True)
    except OSError:
        return False
    return _job_view(label) == "absent"


def _capture_exit_status(run, state, wait=False):
    """Read launchd's exit code while the job still exists.

    Does not remove the job. A missing code is not success. Returns the
    code, or None when it is not known yet.
    """
    code = state.get("exit_code")
    if (state.get("exit_known") is True and isinstance(code, int)
            and not isinstance(code, bool)):
        return code
    label = state.get("launch_label")
    if not label:
        return None
    deadline = time.monotonic() + (2.0 if wait else 0.0)
    while True:
        view = _job_view(label)
        if isinstance(view, tuple) and view[0] == "exited":
            found = view[2]
            if isinstance(found, int) and not isinstance(found, bool):
                state["exit_code"] = found
                state["exit_known"] = True
                _remember_identity(run, state)
                return found
        if view == "absent" or time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _terminal_result(state):
    """Structured outcome from a captured exit code. Unknown is not success."""
    code = state.get("exit_code")
    if (state.get("exit_known") is True and isinstance(code, int)
            and not isinstance(code, bool)):
        return {"returncode": code, "timed_out": False,
                "status": "completed" if code == 0 else "died"}
    return {"returncode": 2, "timed_out": False, "status": "died"}


def _publish_pending(run, state):
    """Write result.json from the outcome recorded before job removal."""
    if run is None or (run / "result.json").exists():
        return
    pending = state.get("pending_result")
    if not isinstance(pending, dict) or not pending.get("status"):
        if state.get("exit_known") is not True:
            return
        pending = _terminal_result(state)
    write_json(run / "result.json", pending)


def _stable_identity(pid):
    """(start, coalition) when two reads agree, else None."""
    first = _coalition_id(pid)
    start = _start_time(pid)
    second = _coalition_id(pid)
    start_again = _start_time(pid)
    if (first is None or second is None or start is None
            or first != second or start != start_again or first <= 0):
        return None
    return start, first


_WORKLOAD_WRAPPER = (
    "import json, os, sys, time\n"
    "spec = json.loads(open(sys.argv[1], encoding='utf-8').read())\n"
    "gate = sys.argv[2]\n"
    "while not os.path.exists(gate):\n"
    "    time.sleep(0.02)\n"
    "cwd = spec.get('cwd') or ''\n"
    "if cwd:\n"
    "    os.chdir(cwd)\n"
    "os.execvpe(spec['argv'][0], spec['argv'], spec['env'])\n"
)


def _command_line(pid):
    try:
        proc = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                              capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _await_workload_exec(pid, started):
    """True once this incarnation is no longer the launch wrapper.

    The coalition is recorded before exec. The workload timeout starts after
    exec, so launch delay is not part of the command's deadline.
    """
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _same_process(pid, started) is not True:
            return False
        command = _command_line(pid)
        if command is None:
            return False
        if command and "workload-launch.py" not in command:
            return True
        time.sleep(0.02)
    return False


def _submit_workload(run, state, argv, env, cwd):
    """Start the command as its own launchd job.

    The label is on the lease before submit. The workload waits until the
    coalition id is on that lease. Returns (pid, start, coalition), None if
    the waiter exited before the gate opened, or an error string.
    """
    label = _job_label(state["lease_id"])
    spec_path = run / "workload-spec.json"
    wrapper_path = run / "workload-launch.py"
    gate_path = run / "workload-go"
    spec_path.write_text(json.dumps({"cwd": cwd or "", "argv": list(argv), "env": env}))
    wrapper_path.write_text(_WORKLOAD_WRAPPER)
    stdout = run / "stdout"
    stderr = run / "stderr"
    stdout.touch()
    stderr.touch()
    state["launch_label"] = label
    state["job_remove_pending"] = True
    write_json(run / "lease.json", state)
    try:
        submitted = subprocess.run(
            ["launchctl", "submit", "-l", label, "-o", str(stdout), "-e", str(stderr),
             "--", sys.executable, str(wrapper_path), str(spec_path), str(gate_path)],
            capture_output=True, text=True)
    except OSError as exc:
        return str(exc)
    if submitted.returncode != 0:
        return submitted.stderr.strip() or "launchctl submit failed"
    state["launch_submitted"] = True
    _remember_identity(run, state)
    # A slow launchd must not become "the job never existed". Expiry keeps
    # the label and leaves the tree unknown.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        view = _job_view(label)
        if view == "unknown" or view == "absent":
            time.sleep(0.02)
            continue
        kind, pid, code = view
        state["launch_seen"] = True
        if kind == "exited":
            if isinstance(code, int) and not isinstance(code, bool):
                state["exit_code"] = code
                state["exit_known"] = True
            _remember_identity(run, state)
            return None
        identity = _stable_identity(pid)
        if identity is None:
            time.sleep(0.02)
            continue
        start, cid = identity
        holder_cid = _coalition_id(os.getpid())
        state["workload_pid"] = str(pid)
        state["workload_start"] = [start[0], start[1]]
        state["holder_coalition_id"] = holder_cid
        if holder_cid is None or cid == holder_cid:
            # Not a private coalition. Do not record it as a kill target.
            _remember_identity(run, state)
            return "coalition was not private"
        state["coalition_id"] = cid
        _remember_identity(run, state)
        # The wrapper execs only after the id is on disk and readable again.
        # A failed lease write must not open the gate: recovery cannot kill
        # a tree it cannot name.
        try:
            lease_path = run / "lease.json"
            fd = os.open(str(lease_path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            dirfd = os.open(str(run), os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
            recorded = read_json(lease_path)
        except (OSError, ValueError):
            return "coalition id was not durable"
        if recorded.get("coalition_id") != cid:
            return "coalition id was not durable"
        gate_path.write_text("1")
        return pid, start, cid
    state["job_remove_pending"] = True
    _remember_identity(run, state)
    return None

def past_deadline(state):
    deadline = state.get("deadline_epoch")
    return isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and time.time() >= deadline


def prunable(run, state):
    # Released bodies only. A dead holder with a live workload is unknown and stays.
    # Token sentinels live outside the run directory and are never removed here.
    if state.get("state") != "released":
        return False
    if locked(run / "holder.lock"):
        return False
    return True


def gc_run_bodies(managed, request, apply):
    """Drop terminal bodies that already have a result line. Unknown stays."""
    success_keep = _limit(request, "retain_max_count", SUCCESS_KEEP, int)
    failure_keep = _limit(request, "retain_failure_count", FAILURE_KEEP, int)
    success_age = _limit(request, "retain_max_age_seconds", SUCCESS_AGE_SECONDS, (int, float))
    failure_age = _limit(request, "retain_failure_age_seconds", FAILURE_AGE_SECONDS, (int, float))
    indexed = result_ids(managed)
    rows = []
    if indexed is None:
        return [{"class": "UNKNOWN", "bytes": 0, "path": str(managed / "results" / "index.jsonl")}]
    runs = managed / "runs"
    if not runs.is_dir():
        return rows
    grouped = {"success": [], "failure": []}
    now = time.time()
    for path in sorted(runs.iterdir()):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            state = read_json(path / "lease.json")
        except (OSError, ValueError):
            rows.append({"class": "UNKNOWN", "bytes": directory_bytes(path), "path": str(path)})
            continue
        if not prunable(path, state) or state.get("lease_id") not in indexed:
            continue
        # The slot is reused by the next run. Only this body path can keep it.
        intruders = cwd_intruders([path])
        if intruders is None or intruders:
            rows.append({"class": "UNKNOWN" if intruders is None else "LIVE",
                         "bytes": directory_bytes(path), "path": str(path)})
            continue
        exit_code = None
        if (path / "result.json").exists():
            try:
                exit_code = read_json(path / "result.json").get("returncode")
            except (OSError, ValueError):
                exit_code = None
        kind = "success" if exit_code == 0 else "failure"
        grouped[kind].append((state.get("acquired_at") or 0, path, state))
    doomed = []
    for kind, items in grouped.items():
        keep = success_keep if kind == "success" else failure_keep
        age = success_age if kind == "success" else failure_age
        items.sort()
        overflow = items[:max(0, len(items) - keep)]
        aged = [item for item in items if now - item[0] >= age]
        chosen = {item[1]: item for item in overflow + aged}
        doomed.extend(chosen.values())
    for acquired, path, state in doomed:
        del acquired, state
        rows.append({"class": "ELIGIBLE", "bytes": directory_bytes(path), "path": str(path)})
        if apply:
            shutil.rmtree(path, ignore_errors=True)
    return rows


def iter_run_dirs(managed):
    """Current runs plus leases left under the pre-move admission/runs tree."""
    for root in (managed / "runs", managed / "admission" / "runs"):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.is_symlink():
                yield path


def _run_is_managed(managed, run):
    if run.is_symlink() or not run.is_dir():
        return False
    try:
        parent = run.resolve().parent
    except OSError:
        return False
    for root in (managed / "runs", managed / "admission" / "runs"):
        try:
            if parent == root.resolve():
                return True
        except OSError:
            continue
    return False


def _token_index(lease):
    index = lease.get("token_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return index


def reserved_token_indexes(managed):
    """Indexes a non-released lease still owns, whether or not its flock is held.

    None means UNKNOWN. An unreadable lease does not name its index, so the
    caller holds the whole pool. Skipping the lease would free that capacity.
    """
    found = set()
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            return None
        if lease.get("state") == "released":
            continue
        index = _token_index(lease)
        if index is not None:
            found.add(index)
    return found


def _slot_marker(slot):
    slot = Path(slot)
    if slot.parent.name != "slots":
        return None
    return slot.parent.parent / "slot-meta" / (slot.name + ".json")


def _free_slot_marker(lease):
    slot = lease.get("slot")
    if not slot:
        return
    marker = _slot_marker(slot)
    if marker is None or not marker.is_file():
        return
    try:
        current = read_json(marker)
    except (OSError, ValueError):
        return
    # A successor may already hold this slot. Do not clear their lease.
    if current.get("lease_id") != lease.get("lease_id"):
        return
    write_json(marker, {"state": "free"})


def _mark_released(managed, run, lease, reason):
    """Durable result line first, then the release that makes the body collectable."""
    lease = dict(lease)
    lease["release_reason"] = reason or lease.get("release_reason") or "completed"
    append_result(managed, lease, run)
    lease["state"] = "released"
    write_json(run / "lease.json", lease)
    _free_slot_marker(lease)


def reclaim_dead_prestart(managed):
    """Release an admitted lease whose holder died before any workload existed.

    A running or draining lease is not touched here. Its tree is the reaper's
    job, and a dead parent can no longer name the children it reparented.
    Caller holds the admission lock.
    """
    for run in list(iter_run_dirs(managed)):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            continue
        if lease.get("state") != "admitted" or lease.get("workload_pid"):
            continue
        if locked(run / "holder.lock"):
            continue
        paths = [p for p in (run, lease.get("slot")) if p]
        intruders = cwd_intruders(paths)
        if intruders is None or intruders:
            continue
        _mark_released(managed, run, lease, lease.get("release_reason") or "dead-before-start")


def _with_queue_lock(root):
    handle = (root / "queue.lock").open("a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def finish_dead_workload(root, run, state, action):
    """Drain, prove the tree dead, then release. Anything less stays held."""
    managed = root.parent
    guard = _with_queue_lock(root)
    try:
        lease = read_json(run / "lease.json")
        if lease.get("state") == "released":
            return {"status": "finished"}
        lease["state"] = "draining"
        write_json(run / "lease.json", lease)
    finally:
        guard.close()
    cleared = clear_tree(managed, lease, run)
    guard = _with_queue_lock(root)
    try:
        current = read_json(run / "lease.json")
        if current.get("state") == "released":
            return {"status": "finished"}
        if not cleared:
            current["state"] = "draining"
            write_json(run / "lease.json", current)
            audit(managed / "admission", {"action": action, "result": "kept",
                                          "lease_id": current.get("lease_id"), "at": time.time()})
            return {"status": "unknown"}
        if action == "deadline" and not (run / "result.json").exists():
            pending = current.get("pending_result")
            # A captured exit, including 0, is not this outcome. No captured
            # exit must not leave the watcher with no result at all.
            if not isinstance(pending, dict) or not pending.get("status"):
                current["pending_result"] = {
                    "returncode": EXIT_DEADLINE, "timed_out": True, "status": "deadline",
                }
        _publish_pending(run, current)
        pending = current.get("pending_result")
        if isinstance(pending, dict) and pending.get("status"):
            reason = pending["status"]
        elif action == "deadline":
            reason = "deadline"
        elif action == "operator-clear":
            reason = "operator-clear"
        else:
            reason = current.get("release_reason") or "completed"
        _mark_released(managed, run, current, reason)
        audit(managed / "admission", {"action": action, "result": "cleared",
                                      "lease_id": current.get("lease_id"),
                                      "workload_pid": current.get("workload_pid"),
                                      "at": time.time()})
    finally:
        guard.close()
    return {"status": "cancelled" if action == "deadline" else "cleared", "killed": True}


def _legacy_slot_held(slot):
    """Parent-version ownership: slot/slot.lock and slot/SLOT.json.

    Do not create those files. A missing lock is not ownership. An unreadable
    marker stays reserved.
    """
    lock_path = slot / "slot.lock"
    if lock_path.exists() and locked(lock_path):
        return True
    marker = slot / "SLOT.json"
    if not marker.is_file():
        return False
    try:
        current = read_json(marker)
    except (OSError, ValueError):
        return True
    if current.get("state") == "free":
        return False
    run_dir = current.get("run_dir")
    if not run_dir:
        return True
    try:
        previous = read_json(Path(run_dir) / "lease.json")
    except (OSError, ValueError):
        return True
    return previous.get("state") != "released"


def _drop_legacy_slot_files(slot):
    """Remove a released generation's metadata from inside the checkout."""
    for name in ("SLOT.json", "slot.lock"):
        path = slot / name
        if path.is_file() and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass


def lease_slot(managed, repo, count, state):
    """Reuse s-01..s-N. Never create s-(N+1). An unresolved lease stays reserved."""
    meta_root = managed / "repos" / repo / "slot-meta"
    for index in range(count):
        name = f"s-{index + 1:02d}"
        slot = managed / "repos" / repo / "slots" / name
        slot.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = try_lock(meta_root / (name + ".lock"))
        if lock is None:
            continue
        if _legacy_slot_held(slot):
            lock.close()
            continue
        _drop_legacy_slot_files(slot)
        marker = meta_root / (name + ".json")
        if marker.exists():
            try:
                current = read_json(marker)
            except (OSError, ValueError):
                lock.close()
                continue
            if current.get("state") != "free":
                reusable = False
                run_dir = current.get("run_dir")
                if run_dir:
                    try:
                        previous = read_json(Path(run_dir) / "lease.json")
                        reusable = previous.get("state") == "released"
                    except (OSError, ValueError):
                        reusable = False
                if not reusable:
                    lock.close()
                    continue
        intruders = cwd_intruders([slot])
        if intruders is None or intruders:
            lock.close()
            continue
        write_json(marker, {
            "state": "leased",
            "lease_id": state.get("lease_id"),
            "pid": (state.get("remote_run") or {}).get("pid"),
            "start_token": (state.get("remote_run") or {}).get("start_token"),
            "run_dir": (state.get("remote_run") or {}).get("run_dir"),
        })
        return slot, lock
    return None, None


def quarantine_slot_contents(managed, slot):
    dest = managed / "quarantine" / f"{time.time_ns()}-{slot.name}"
    dest.mkdir(parents=True, mode=0o700)
    for child in list(slot.iterdir()):
        shutil.move(str(child), str(dest / child.name))


def prepare_slot(managed, slot, sha):
    """Reset a reused slot. Dirty git state is moved aside, not deleted."""
    if not (slot / ".git").exists():
        return True
    status = subprocess.run(["git", "-C", str(slot), "status", "--porcelain"],
                            capture_output=True, text=True)
    if status.returncode != 0:
        return False
    if status.stdout.strip():
        quarantine_slot_contents(managed, slot)
        return True
    if sha:
        checkout = subprocess.run(["git", "-C", str(slot), "checkout", "--detach", sha],
                                  capture_output=True, text=True)
        if checkout.returncode != 0:
            quarantine_slot_contents(managed, slot)
            return True
    subprocess.run(["git", "-C", str(slot), "clean", "-fd"], capture_output=True)
    return True


def _release_holder(root, run, state, token):
    """Drain under the admission lock, then release only a proved-dead tree."""
    managed = root.parent
    if token is None and state.get("token_index") is None:
        state.setdefault("release_reason", "completed")
        append_result(root.parent, state, run)
        state["state"] = "released"
        write_json(run / "lease.json", state)
        return
    guard = _with_queue_lock(root)
    try:
        lease = read_json(run / "lease.json")
        if state.get("release_reason"):
            lease["release_reason"] = state["release_reason"]
        for key in ("workload_pid", "workload_start", "launch_label", "coalition_id",
                    "holder_coalition_id", "exit_code", "exit_known", "pending_result"):
            if key in state:
                lease[key] = state[key]
        lease["state"] = "draining"
        write_json(run / "lease.json", lease)
    finally:
        guard.close()
    launched = lease.get("workload_pid") or lease.get("launch_label") or lease.get("coalition_id")
    if launched:
        ok = clear_tree(managed, lease, run)
    else:
        paths = [p for p in (run, lease.get("slot")) if p]
        intruders = cwd_intruders(paths)
        ok = intruders is not None and not intruders
    label = lease.get("launch_label") or state.get("launch_label") or ""
    # Capacity is published only after the job is gone. clear_tree may already
    # have removed it; a failed proof here keeps the lease draining.
    if ok and label and not _remove_job(label):
        ok = False
    guard = _with_queue_lock(root)
    try:
        current = read_json(run / "lease.json")
        if current.get("state") == "released":
            pass
        elif not ok:
            current["state"] = "draining"
            if label:
                current["launch_label"] = label
                current["job_remove_pending"] = True
            if state.get("release_reason"):
                current["release_reason"] = state["release_reason"]
            write_json(run / "lease.json", current)
            return
        else:
            if label:
                current["job_remove_pending"] = False
            _publish_pending(run, current)
            reason = state.get("release_reason") or current.get("release_reason") or "completed"
            _mark_released(managed, run, current, reason)
    finally:
        guard.close()
    if token is not None:
        token.close()


def admission_sleep(run, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not (run / 'release.json').exists():
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def run_holder(root, run, ticket, ticket_lock, holder_lock, request):
    """Own admission and the process group; persist identity before executing."""
    os.setsid()
    # The incarnation nonce is node-issued and fenced by holder.lock. It is
    # never a controller PID or a PID-only liveness claim.
    state = read_json(run / 'lease.json')
    state['remote_run'] = {
        'host': socket.gethostname(), 'pid': str(os.getpid()),
        'start_token': uuid.uuid4().hex, 'run_dir': str(run),
        'lease_id': state['lease_id'], 'lease_token': state['lease_token'],
    }
    write_json(run / 'lease.json', state)
    (run / 'launch.log').write_text('REMOTE_RUN_LAUNCHED ' + ' '.join(
        name + '=' + shlex.quote(value) for name, value in state['remote_run'].items()) + '\n')
    token = None
    slot = None
    slot_lock = None
    tree_started = False
    try:
        while token is None:
            if (run / 'release.json').exists():
                return
            with (root / 'queue.lock').open('a+') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                # A holder killed before start never rewrites its lease. Prove
                # that no workload exists, then release it under this lock.
                reclaim_dead_prestart(root.parent)
                # Tickets are allocated under this same lock. Dead holders
                # cannot strand FIFO: their kernel-held ticket locks are free.
                waiting = []
                for path in sorted((root / 'tickets').iterdir()):
                    if path == ticket or locked(path):
                        waiting.append(path)
                    else:
                        path.unlink(missing_ok=True)
                if waiting and waiting[0] == ticket:
                    try:
                        measured = sample(root)
                        if measured['load1'] <= measured['p_cores']:
                            reserved = reserved_token_indexes(root.parent)
                            # None holds every index. An unreadable lease has
                            # no token number to reserve on its own.
                            if reserved is not None:
                                for index in range(measured['token_pool_size']):
                                    if index in reserved:
                                        continue
                                    token = try_lock(root / 'tokens' / str(index))
                                    if token is not None:
                                        repo = safe_repo(request.get('repo'))
                                        slot, slot_lock = lease_slot(
                                            root.parent, repo, measured['token_pool_size'], state)
                                        if slot is None:
                                            token.close()
                                            token = None
                                            break
                                        state.update(state='admitted', token_index=index,
                                                     sample=measured, slot=str(slot), repo=repo)
                                        write_json(run / 'lease.json', state)
                                        ticket.unlink()
                                        break
                    except (OSError, ValueError):
                        pass  # Unknown load/caps must not grant admission.
            if token is None:
                admission_sleep(run, request['poll_seconds'])
        ticket_lock.close()
        # Admission precedes controller rendering/pushing and command creation.
        # The controller does that bookkeeping and then writes command.json.
        # If the reply was dropped, nobody will. Waiting forever pins the token
        # on a live owner that reap will not touch.
        command_deadline = time.monotonic() + float(request.get('command_wait_seconds', 30))
        while not (run / 'command.json').exists():
            if (run / 'release.json').exists():
                return
            if time.monotonic() >= command_deadline:
                state['release_reason'] = 'command-wait'
                return
            time.sleep(min(request['poll_seconds'], 0.1))
        command = read_json(run / 'command.json')
        sha = (command.get('env') or {}).get('GOALFLIGHT_REMOTE_CI_SHA', '')
        state['sha'] = sha
        if slot is not None and not prepare_slot(root.parent, Path(state['slot']), sha):
            state['release_reason'] = 'slot-unknown'
            return
        state['state'] = 'running'
        state['started_at'] = time.time()
        state['deadline_epoch'] = state['started_at'] + float(command['timeout'])
        write_json(run / 'lease.json', state)
        floor = root.parent / 'admission' / 'resource-floor.json'
        if floor.is_file():
            try:
                decision = read_json(floor)
            except (OSError, ValueError):
                decision = {'refuse': 'capacity'}
            if decision.get('refuse') == 'capacity':
                # The host guard refused the start. This is not a dead workload.
                state['release_reason'] = 'capacity-refused'
                write_json(run / 'result.json', {
                    'returncode': EXIT_CAPACITY, 'timed_out': False,
                    'status': 'capacity-refused'})
                return
        env = os.environ.copy()
        env.update(command['env'])
        env['GOALFLIGHT_REMOTE_CI_REMOTE_PID'] = state['remote_run']['pid']
        env['GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN'] = state['remote_run']['start_token']
        env['GOALFLIGHT_REMOTE_CI_SLOT_DIR'] = state.get('slot') or ''
        env['GOALFLIGHT_REMOTE_CI_RUN_DIR'] = str(run)
        env['GOALFLIGHT_REMOTE_CI_RESULT_INDEX'] = str(root.parent / 'results' / 'index.jsonl')
        # launchd starts a new job, so it cannot inherit the token fd. The
        # holder keeps that fd. Admission follows the lease, not the flock.
        launched = _submit_workload(run, state, command['argv'], env, state.get('slot') or '')
        if isinstance(launched, str):
            state['release_reason'] = 'launch-failed'
            state['pending_result'] = {
                'returncode': 2, 'timed_out': False, 'status': 'died', 'error': launched}
            _remember_identity(run, state)
            write_json(run / 'result.json', state['pending_result'])
        elif launched is None:
            tree_started = False
            if not clear_tree(root.parent, state, run):
                state['release_reason'] = 'unknown-tree'
            else:
                result = _terminal_result(state)
                result['status'] = 'died'
                result['error'] = 'workload exited before its coalition was recorded'
                if result['returncode'] == 0:
                    result['returncode'] = 2
                write_json(run / 'result.json', result)
        else:
            tree_started = True
            leader, leader_start, _cid = launched
            # The lease already names the job. Start the command deadline only
            # once the wrapper has exec'd, so a slow launch is not a timeout.
            if _await_workload_exec(leader, leader_start):
                state['deadline_epoch'] = time.time() + float(command['timeout'])
            write_json(run / 'lease.json', state)
            deadline = time.monotonic() + command['timeout']
            last_scan = 0.0
            while True:
                same = _same_process(leader, leader_start)
                cancel_path = run / 'cancel.json'
                cancelled = cancel_path.exists() and identity_matches(run, read_json(cancel_path))
                if cancelled or time.monotonic() >= deadline:
                    # Keep clearing after the leader dies. Falling through to
                    # the natural-exit result would replace this outcome and
                    # could release nothing while a child is still unknown.
                    pending = ({
                        'returncode': EXIT_CANCELLED, 'timed_out': False,
                        'status': 'cancelled',
                    } if cancelled else {
                        'returncode': EXIT_DEADLINE, 'timed_out': True,
                        'status': 'deadline',
                    })
                    state['pending_result'] = pending
                    _remember_identity(run, state)
                    if not clear_tree(root.parent, state, run):
                        time.sleep(0.2)
                        continue
                    write_json(run / 'result.json', pending)
                    state['release_reason'] = 'cancelled' if cancelled else 'node-timeout'
                    break
                if same is None:
                    # Unreadable is not an exit. Treating it as one starts
                    # cleanup while the leader may still be alive.
                    time.sleep(0.05)
                    continue
                if same is False:
                    _capture_exit_status(run, state, wait=True)
                    state['pending_result'] = _terminal_result(state)
                    _remember_identity(run, state)
                    if not clear_tree(root.parent, state, run):
                        state['release_reason'] = 'unknown-tree'
                    else:
                        write_json(run / 'result.json', state['pending_result'])
                    break
                now = time.monotonic()
                if now - last_scan >= 0.5:
                    scanned = _coalition_members(state['coalition_id'])
                    if scanned is not None:
                        living = {pid: start for pid, start in scanned if start}
                        _store_members(run, state, living)
                    last_scan = now
                time.sleep(0.05)
            if tree_started and state.get('release_reason') != 'unknown-tree':
                while not clear_tree(root.parent, state, run):
                    time.sleep(0.2)
    except BaseException as exc:
        write_json(run / 'result.json', {
            'returncode': 2, 'timed_out': False, 'status': 'died', 'error': str(exc)})
    finally:
        ticket.unlink(missing_ok=True)
        try:
            _release_holder(root, run, state, token)
        finally:
            # Removal is proved before release. Do not take a published
            # release back: that window lets a second run share the token.
            # A holder that is leaving with the job still present stays
            # draining so the next reap retries the removal.
            label = state.get('launch_label')
            try:
                lease = read_json(run / 'lease.json')
            except (OSError, ValueError):
                lease = None
            if label and (lease is None or lease.get('state') != 'released') and not _remove_job(label):
                if lease is not None:
                    lease['launch_label'] = label
                    lease['job_remove_pending'] = True
                    lease['state'] = 'draining'
                    try:
                        write_json(run / 'lease.json', lease)
                    except OSError:
                        pass
            if slot_lock is not None:
                slot_lock.close()
            ticket_lock.close()
            holder_lock.close()


def dispatch(request):
    managed = pin_managed_root(Path(request['managed_root']))
    root = managed / 'admission'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('tokens', 'tickets'):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    for name in ('runs', 'results', 'keep', 'quarantine'):
        (managed / name).mkdir(exist_ok=True, mode=0o700)
    operation = request['operation']
    with (root / 'queue.lock').open('a+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        policy_path = root / 'policy.json'
        policy = {key: request[key] for key in ('p_cores', 'token_pool_size')}
        if not policy_path.exists():
            write_json(policy_path, policy)
        elif operation == 'enqueue' and read_json(policy_path) != policy:
            # A wrong cap must not take a token. Recovery (list, reap, cancel)
            # still has to run, or the corrected config cannot clean the box.
            raise ValueError('shared box policy differs; use the same configured caps')
    if operation == 'enqueue':
        with (root / 'queue.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            counter = root / 'sequence.json'
            sequence = (read_json(counter) if counter.exists() else 0) + 1
            write_json(counter, sequence)
            lease_id = uuid.uuid4().hex
            gc_run_bodies(managed, request, True)
            run = managed / 'runs' / lease_id
            run.mkdir(mode=0o700)
            ticket = root / 'tickets' / f'{sequence:020d}'
            ticket_lock = try_lock(ticket)
            holder_lock = try_lock(run / 'holder.lock')
            state = {
                'schema': 'goalflight.remote-ci.lease.v1', 'lease_id': lease_id,
                'lease_token': uuid.uuid4().hex, 'box': request['box'],
                'managed_run_directory': str(managed), 'run_directory': str(run),
                'state': 'queued', 'acquired_at': time.time(), 'ticket': sequence,
                'request_id': request['request_id'], 'arm': request['arm'],
            }
            write_json(run / 'lease.json', state)
            write_json(run / 'owner.json', request['owner'])
            pid = os.fork()
            if pid == 0:
                # Close the parent's queue descriptor without LOCK_UN (fork
                # shares its open-file description), then detach all stdio.
                guard.close()
                with open(os.devnull, 'r+') as null:
                    for fd in (0, 1, 2):
                        os.dup2(null.fileno(), fd)
                try:
                    run_holder(root, run, ticket, ticket_lock, holder_lock, request)
                finally:
                    os._exit(0)
            ticket_lock.close()
            holder_lock.close()
        return record(run)
    if operation == 'list':
        rows = []
        for path in iter_run_dirs(managed):
            try:
                rows.append(record(path))
            except (OSError, ValueError):
                # An unreadable lease holds the whole pool. Name it so the
                # operator can see which file, instead of an empty list.
                rows.append({
                    "state": "UNREADABLE",
                    "run_directory": str(path),
                    "path": str(path / "lease.json"),
                    "lease_id": None,
                    "lease_token": None,
                })
        return rows
    if operation == 'gc':
        return gc_run_bodies(managed, request, bool(request.get('apply')))
    if operation == 'health':
        measured = sample(root)
        measured['load_within_p_cores'] = measured['load1'] <= measured['p_cores']
        total = measured['token_pool_size']
        guard = _with_queue_lock(root)
        try:
            reclaim_dead_prestart(managed)
            reserved = reserved_token_indexes(managed)
        finally:
            guard.close()
        used = {index for index in range(total) if locked(root / 'tokens' / str(index))}
        if reserved is None:
            used.update(range(total))
        else:
            used.update(index for index in reserved if index < total)
        return {'load': measured, 'tokens': {'total': total, 'free': total - len(used),
                                             'in_use': len(used)}}
    run = Path(request['run_dir'])
    if not _run_is_managed(managed, run):
        raise ValueError('run directory is outside managed admission runs')
    state = record(run)
    if request.get('lease_token') != state['lease_token']:
        raise ValueError('lease token does not match')
    if operation == 'status':
        state['holder_alive'] = locked(run / 'holder.lock')
        if (run / 'result.json').exists():
            state['result'] = read_json(run / 'result.json')
            state['result']['stdout'] = (run / 'stdout').read_text() if (run / 'stdout').exists() else ''
            state['result']['stderr'] = (run / 'stderr').read_text() if (run / 'stderr').exists() else ''
        return state
    if operation == 'start':
        # Serialized existence checks prevent a second command for one lease.
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = record(run)
            if state['state'] != 'admitted' or not locked(run / 'holder.lock'):
                raise ValueError('run does not hold admission')
            if (run / 'release.json').exists() or (run / 'cancel.json').exists():
                raise ValueError('run release is pending')
            if (run / 'command.json').exists():
                raise ValueError('run already started')
            write_json(run / 'command.json', request['command'])
        return state
    if operation == 'attach':
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = record(run)
            if not identity_matches(run, request['identity']):
                raise ValueError('unknown remote identity; admission cannot be inherited')
            if state['state'] == 'queued':
                raise ValueError('queued holder has no admission to inherit')
            if state['state'] != 'released':
                if (run / 'cancel.json').exists() or (run / 'release.json').exists():
                    raise ValueError('cancellation pending; cannot inherit admission')
                if not locked(run / 'holder.lock'):
                    raise ValueError('original admission holder is gone; cannot inherit token')
                if not locked(root / 'tokens' / str(state['token_index'])):
                    raise ValueError('original admission token is not held')
            # A completed result has no workload to admit. Active attachments
            # inherit precisely the original node holder's token.
            write_json(run / 'owner.json', request['owner'])
            return state
    if operation == 'cancel':
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = record(run)
            if not identity_matches(run, request['identity']):
                raise ValueError('unknown identity; refusing cancellation')
            expected = request.get('expected_owner')
            if not isinstance(expected, dict):
                raise ValueError('expected_owner is required')
            if expected != read_json(run / 'owner.json'):
                return {'status': 'owned'}  # A reattach won the race with reaping.
            if not locked(run / 'holder.lock'):
                if state['state'] == 'released':
                    return {'status': 'finished'}
                # The holder was the only timeout enforcer. Once it is dead, a
                # proven child past its deadline is reaped here. Anything less
                # stays unknown and keeps the token.
                fresh = read_json(run / 'lease.json')
                fresh.update(read_json(run / 'owner.json'))
                if past_deadline(fresh):
                    return finish_dead_workload(root, run, fresh, 'deadline')
                # The holder is gone, so nothing else will remove this job.
                # Kill the tree now. Capacity stays until that kill is proved.
                # A running workload is not left in the user launchd domain
                # until the deadline.
                if fresh.get('launch_label') or fresh.get('coalition_id') or fresh.get('state') == 'draining':
                    return finish_dead_workload(root, run, fresh, 'reap')
                return {'status': 'unknown'}
            write_json(run / 'cancel.json', request['identity'])
            # An admitted holder may not have received its command yet.
            if not (run / 'command.json').exists():
                write_json(run / 'release.json', {})
        # clear_tree walks the process table. Under load that exceeds a few
        # seconds. Returning unknown while the holder is still killing leaves
        # the job in this user domain. Wait until the holder drops the lock.
        # Stay under the transport timeout. A slow clear still returns
        # unknown rather than being killed mid-scan.
        deadline = time.monotonic() + 20
        while locked(run / 'holder.lock') and time.monotonic() < deadline:
            time.sleep(0.02)
        return {'status': 'unknown' if locked(run / 'holder.lock') else 'cancelled'}
    if operation == 'release':
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            expected = request.get('expected_owner')
            if expected is not None and expected != read_json(run / 'owner.json'):
                return {'status': 'owned'}  # A reattach won the race.
            if (run / 'command.json').exists():
                raise ValueError('started runs release only on completion or proven cancellation')
            write_json(run / 'release.json', {})
        return {'status': 'releasing'}
    if operation == 'clear':
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = record(run)
            if not identity_matches(run, request.get('identity') or {}):
                raise ValueError('unknown identity; refusing clear')
            if locked(run / 'holder.lock'):
                raise ValueError('holder is alive')
            if state['state'] == 'released':
                return {'status': 'finished'}
            # Operator path: a dead holder may be cleared before the deadline.
            # Automatic cancel will not do this. The audit log is the record.
            return finish_dead_workload(root, run, state, 'operator-clear')
    raise ValueError('unknown operation: ' + operation)


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(base64.b64decode(sys.argv[1])))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
