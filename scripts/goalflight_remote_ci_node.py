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
# Exported into the workload only. A descendant that scrubs this and is
# reparented before the snapshot cannot be found; that lease stays draining.
RUN_MARKER = "GOALFLIGHT_REMOTE_CI_RUN"
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


def cwd_intruders(paths, allowed_pgid):
    snap = cwd_snapshot()
    if snap is None:
        return None
    roots = [os.path.normpath(str(path)) for path in paths if path]
    found = []
    for pid, cwd in snap:
        cwd_n = os.path.normpath(cwd) if cwd else ""
        if not any(cwd_n == root or cwd_n.startswith(root + os.sep) for root in roots):
            continue
        try:
            pgid = os.getpgid(pid)
        except OSError:
            continue
        if allowed_pgid is not None and pgid == int(allowed_pgid):
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
    try:
        os.kill(int(pid), 0)
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


def _proven_dead(pid, started):
    return _same_process(pid, started) is False


def _as_start(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _coalition_id(pid):
    """Resource coalition, or None when this pid cannot be queried."""
    try:
        lib = _libproc()
        info = _CoalInfo()
        size = lib.proc_pidinfo(int(pid), PROC_PIDCOALITIONINFO, 0,
                                ctypes.byref(info), ctypes.sizeof(info))
    except OSError:
        return None
    if size != ctypes.sizeof(info):
        return None
    cid = int(info.ids[0])
    return cid or None


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


def _coalition_members(cid):
    """(pid, start) for every process in this resource coalition.

    None means the pid list failed. An empty list means the coalition has
    no live members we can see. Pids we cannot query are not members of a
    job we launched; those jobs are same-user and readable.
    """
    pids = _all_pids()
    if pids is None:
        return None
    found = []
    for pid in pids:
        if _coalition_id(pid) != cid:
            continue
        found.append((pid, _start_time(pid)))
    return found


def _signal_incarnation(pid, started):
    """TERM, then KILL only if the pid is still that same start time.

    Returns True when that incarnation is gone. A reused pid is not signalled.
    """
    pid = int(pid)
    if pid <= 1 or pid == os.getpid() or started is None:
        return False
    if _same_process(pid, started) is not True:
        return _proven_dead(pid, started)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + TREE_GRACE_SECONDS
    while time.monotonic() < deadline:
        same = _same_process(pid, started)
        if same is False:
            return True
        if same is None:
            return False
        time.sleep(0.02)
    # The grace elapsed. The pid may have been reused since TERM.
    if _same_process(pid, started) is not True:
        return _proven_dead(pid, started)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    time.sleep(0.05)
    return _proven_dead(pid, started)


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


def clear_tree(managed, state, run):
    """Kill every member of this run's coalition. False keeps the lease.

    Membership is the launchd resource coalition recorded at launch. A child
    reparented to launchd, or a grandchild spawned while its parent handles
    SIGTERM, stays in that coalition. The session coalition is never a
    target: signalling it would hit every process in the login session.
    Incarnations are re-checked before SIGTERM and again before SIGKILL.
    """
    del managed
    _remove_job(state.get("launch_label"))
    cid = state.get("coalition_id")
    holder_cid = state.get("holder_coalition_id")
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0 or cid == holder_cid:
        # No private coalition was recorded. A run that never launched has
        # no tree. Anything else is incomplete and stays held.
        return not state.get("workload_pid") and not state.get("members")
    persisted = _member_map(state)
    for _ in range(5):
        scanned = _coalition_members(cid)
        if scanned is None:
            _store_members(run, state, persisted)
            return False
        current = dict(persisted)
        for pid, start in scanned:
            if pid <= 1 or pid == os.getpid():
                continue
            if start is None:
                _store_members(run, state, persisted)
                return False
            current[pid] = start
        living = {}
        for pid, start in current.items():
            same = _same_process(pid, start)
            if same is False:
                continue
            if same is None:
                _store_members(run, state, current)
                return False
            living[pid] = start
        _store_members(run, state, living)
        if not living:
            return True
        for pid, start in living.items():
            _signal_incarnation(pid, start)
        persisted = living
    _store_members(run, state, persisted)
    return False


def _job_label(lease_id):
    return "com.goalflight.remote-ci." + str(lease_id)


def _launchctl_job(label):
    """(pid or None, status) from `launchctl list`, or None if the job is absent."""
    try:
        listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3 or parts[-1] != label:
            continue
        pid_text, status = parts[0], parts[1]
        pid = int(pid_text) if pid_text.isdigit() else None
        code = int(status) if status.lstrip("-").isdigit() else None
        return pid, code
    return None


def _remove_job(label):
    if not label:
        return
    try:
        subprocess.run(["launchctl", "remove", label], capture_output=True, text=True)
    except OSError:
        pass


_WORKLOAD_WRAPPER = (
    "import json, os, sys\n"
    "spec = json.loads(open(sys.argv[1], encoding='utf-8').read())\n"
    "cwd = spec.get('cwd') or ''\n"
    "if cwd:\n"
    "    os.chdir(cwd)\n"
    "os.execvpe(spec['argv'][0], spec['argv'], spec['env'])\n"
)


def _submit_workload(run, state, argv, env, cwd):
    """Start the command as its own launchd job. Returns (pid, start, coalition) or an error string."""
    label = _job_label(state["lease_id"])
    spec_path = run / "workload-spec.json"
    wrapper_path = run / "workload-launch.py"
    spec_path.write_text(json.dumps({"cwd": cwd or "", "argv": list(argv), "env": env}))
    wrapper_path.write_text(_WORKLOAD_WRAPPER)
    stdout = run / "stdout"
    stderr = run / "stderr"
    stdout.touch()
    stderr.touch()
    try:
        submitted = subprocess.run(
            ["launchctl", "submit", "-l", label, "-o", str(stdout), "-e", str(stderr),
             "--", sys.executable, str(wrapper_path), str(spec_path)],
            capture_output=True, text=True)
    except OSError as exc:
        return str(exc)
    if submitted.returncode != 0:
        return submitted.stderr.strip() or "launchctl submit failed"
    state["launch_label"] = label
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = _launchctl_job(label)
        if job is None:
            time.sleep(0.02)
            continue
        pid, code = job
        if pid:
            start = _start_time(pid)
            cid = _coalition_id(pid)
            holder_cid = _coalition_id(os.getpid())
            if start is None or cid is None or cid == holder_cid:
                state["workload_pid"] = str(pid)
                if start is not None:
                    state["workload_start"] = [start[0], start[1]]
                return "coalition was not private"
            state["workload_pid"] = str(pid)
            state["workload_start"] = [start[0], start[1]]
            state["coalition_id"] = cid
            state["holder_coalition_id"] = holder_cid
            return pid, start, cid
        state["exit_code"] = code
        return None
    return "launchctl job did not appear"

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
        intruders = cwd_intruders([path], None)
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
    """Indexes a non-released lease still owns, whether or not its flock is held."""
    found = set()
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            continue
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
        intruders = cwd_intruders(paths, None)
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
        reason = "deadline" if action == "deadline" else "operator-clear"
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
        intruders = cwd_intruders([slot], None)
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
        if state.get("workload_pid"):
            lease["workload_pid"] = state["workload_pid"]
        lease["state"] = "draining"
        write_json(run / "lease.json", lease)
    finally:
        guard.close()
    if lease.get("workload_pid"):
        ok = clear_tree(managed, lease, run)
    else:
        paths = [p for p in (run, lease.get("slot")) if p]
        intruders = cwd_intruders(paths, None)
        ok = intruders is not None and not intruders
    guard = _with_queue_lock(root)
    try:
        current = read_json(run / "lease.json")
        if current.get("state") == "released":
            pass
        elif not ok:
            current["state"] = "draining"
            if state.get("release_reason"):
                current["release_reason"] = state["release_reason"]
            write_json(run / "lease.json", current)
            return
        else:
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
        # Set after the command env so a caller cannot substitute another run.
        env[RUN_MARKER] = state['lease_id']
        # launchd starts a new job, so it cannot inherit the token fd. The
        # holder keeps that fd. Admission follows the lease, not the flock.
        launched = _submit_workload(run, state, command['argv'], env, state.get('slot') or '')
        if isinstance(launched, str):
            state['release_reason'] = 'launch-failed'
            write_json(run / 'result.json', {
                'returncode': 2, 'timed_out': False, 'status': 'died', 'error': launched})
        elif launched is None:
            tree_started = False
            code = state.get('exit_code')
            code = 0 if code is None else code
            write_json(run / 'result.json', {
                'returncode': code, 'timed_out': False,
                'status': 'completed' if code == 0 else 'died'})
            if not clear_tree(root.parent, state, run):
                state['release_reason'] = 'unknown-tree'
        else:
            tree_started = True
            write_json(run / 'lease.json', state)
            leader, leader_start, _cid = launched
            deadline = time.monotonic() + command['timeout']
            last_scan = 0.0
            while _same_process(leader, leader_start) is True:
                cancel_path = run / 'cancel.json'
                cancelled = cancel_path.exists() and identity_matches(run, read_json(cancel_path))
                if cancelled or time.monotonic() >= deadline:
                    if not clear_tree(root.parent, state, run):
                        time.sleep(0.2)
                        continue
                    if cancelled:
                        write_json(run / 'result.json', {
                            'returncode': EXIT_CANCELLED, 'timed_out': False,
                            'status': 'cancelled'})
                        state['release_reason'] = 'cancelled'
                    else:
                        write_json(run / 'result.json', {
                            'returncode': EXIT_DEADLINE, 'timed_out': True,
                            'status': 'deadline'})
                        state['release_reason'] = 'node-timeout'
                    break
                now = time.monotonic()
                if now - last_scan >= 0.5:
                    scanned = _coalition_members(state['coalition_id'])
                    if scanned is not None:
                        living = {pid: start for pid, start in scanned if start}
                        _store_members(run, state, living)
                    last_scan = now
                time.sleep(0.05)
            else:
                if not clear_tree(root.parent, state, run):
                    state['release_reason'] = 'unknown-tree'
                else:
                    job = _launchctl_job(state.get('launch_label'))
                    code = job[1] if job is not None else state.get('exit_code')
                    code = 0 if code is None else code
                    write_json(run / 'result.json', {
                        'returncode': code, 'timed_out': False,
                        'status': 'completed' if code == 0 else 'died'})
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
            _remove_job(state.get('launch_label'))
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
                continue
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
                return {'status': 'unknown'}
            write_json(run / 'cancel.json', request['identity'])
            # An admitted holder may not have received its command yet.
            if not (run / 'command.json').exists():
                write_json(run / 'release.json', {})
        deadline = time.monotonic() + 5
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
