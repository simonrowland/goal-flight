"""Node-side admission authority, sent through the configured remote executor.

Standard library only. One managed root identifies one physical box's pool.
A token stays held until cleanup has verified the workload tree is dead and
written released under the admission lock. Closing the token fd is not
enough: a draining lease keeps the slot and the token index.
"""
from __future__ import annotations

import base64
import builtins
import contextlib
import ctypes
import errno
import fcntl
import gzip
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

# One file per machine, not per project root. The first managed root is pinned
# here; a later request that names a different root is refused so it cannot
# open a second token pool on this box.
AUTHORITY_FILE = Path.home() / ".goal-flight" / "remote-ci" / "authority.json"
SUCCESS_KEEP = 20
SUCCESS_AGE_SECONDS = 7 * 24 * 3600
FAILURE_KEEP = 50
FAILURE_AGE_SECONDS = 14 * 24 * 3600
RUN_BODY_RETENTION_FLOOR_SECONDS = 24 * 3600
MIN_RESULT_RETENTION_SECONDS = RUN_BODY_RETENTION_FLOOR_SECONDS
SUBMITTED_TICKET_TTL_SECONDS = 24 * 3600
UPLOAD_TTL_SECONDS = 24 * 3600
ADMIT_LOOP_INTERVAL_SECONDS = 1.0
PROTOCOL_VERSION = 5
# Owner saturation rule (2026-10-19): refuse at 2x physical cores and 95% busy.
SATURATION_LOAD_FACTOR = 2
SATURATION_BUSY_FRACTION = 0.95
DEFAULT_RESULT_TAIL_BYTES = 16 * 1024
SETUP_TIMEOUT_SECONDS = 300
DEFAULT_REMOTE_RUN_TIMEOUT_SECONDS = 7200
PROCESS_REAP_TIMEOUT_SECONDS = 5.0
WORKLOAD_START_GATE_TIMEOUT_SECONDS = 60.0
# Keep these budgets aligned with the controller's bundle preparation,
# basis-status, and transfer limits. The node helper is installed separately,
# so the existing enqueue protocol carries no new command-window field.
BUNDLE_PREPARATION_TIMEOUT_SECONDS = 300.0
BUNDLE_BASIS_TIMEOUT_SECONDS = 30.0
BUNDLE_TRANSFER_TIMEOUT_SECONDS = 300.0
MAX_SUBMIT_BUNDLE_BYTES = 512 * 1024 * 1024
POOL_LOCK_OPERATION_TIMEOUT_SECONDS = 20.0
# A locked enqueue-GC pass is one filter census plus two fresh attempts;
# 3 x the measured 0.63 s census is 1.89 s.
HEALTH_LOCK_TIMEOUT_SECONDS = 2.0
MAX_RESULT_TAIL_BYTES = 64 * 1024
MAX_FETCH_BYTES = 256 * 1024
TREE_GRACE_SECONDS = 0.3
# Distinct from a dead workload. 75 is the retryable capacity refusal.
# 77 is not used: callers were treating 77 as remote death.
EXIT_CAPACITY = 75
EXIT_CANCELLED = 130
EXIT_DEADLINE = 124
PINNED_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?=="
    r"[A-Za-z0-9][A-Za-z0-9.!+_-]*"
)
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


def _stage_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value))
    return temporary


def _publish_staged_json(temporary, path):
    Path(temporary).replace(Path(path))


def write_json(path, value):
    temporary = _stage_json(path, value)
    _publish_staged_json(temporary, path)


def _fsync_file(path):
    """Fsync a file and its directory. False if that proof cannot be made."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        dirfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        return False
    return True


def read_json(path):
    return json.loads(path.read_text())


_TIMING_RECORD = None
_CWD_SNAPSHOT_UNSET = object()

# This helper is copied to remote nodes as a standalone script. Keep the
# narrow stderr allowlist here instead of adding a runtime import dependency.
_LSOF_MOUNT_STAT_WARNING = re.compile(
    r"lsof: WARNING: can't stat\(\) \S+ file system .+"
)
_LSOF_MOUNT_STAT_CONTINUATIONS = (
    re.compile(r"Output information may be incomplete\."),
    re.compile(r'assuming "dev=[0-9A-Fa-f]+" from mount table'),
)


def _lsof_stderr_is_mount_stat_warnings_only(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    in_group = False
    for raw in (stderr or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _LSOF_MOUNT_STAT_WARNING.fullmatch(line):
            in_group = True
            continue
        if in_group and any(
            pattern.fullmatch(line) for pattern in _LSOF_MOUNT_STAT_CONTINUATIONS
        ):
            continue
        return False
    return True


def _timing_mark(step):
    if _TIMING_RECORD is not None:
        _TIMING_RECORD["events"].append({
            "step": step,
            "monotonic_ns": time.monotonic_ns(),
        })


def _begin_timing_record(request):
    global _TIMING_RECORD
    timing_dir = os.environ.get("GOALFLIGHT_REMOTE_CI_TIMING_DIR")
    if not timing_dir:
        return
    directory = Path(timing_dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        # Opt-in diagnostics must never stop the request they would measure.
        return
    try:
        load_average = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average = None
    call_id = f"{os.getpid()}-{uuid.uuid4().hex}"
    _TIMING_RECORD = {
        "call_id": call_id,
        "operation": request.get("operation"),
        "request_id": request.get("request_id"),
        "hostname": socket.gethostname(),
        "load_average": load_average,
        "events": [],
        "path": directory / f"{call_id}.json",
    }


def _write_timing_record():
    global _TIMING_RECORD
    record = _TIMING_RECORD
    _TIMING_RECORD = None
    if record is None:
        return
    path = record.pop("path")
    try:
        write_json(path, record)
    except OSError:
        # Diagnostics must not turn a completed non-idempotent request into a
        # lost response that a caller might mistake for an unexecuted call.
        pass


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


def locked_existing(path):
    """Check a retained lock without creating a new lock file."""
    try:
        handle = path.open('r+')
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        handle.close()


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


def sample(root, python_interpreter):
    policy = limits(root)
    tools = {
        'python': bool(python_interpreter and os.path.isabs(python_interpreter)
                       and os.access(python_interpreter, os.X_OK)),
        'node': shutil.which('node') is not None,
        'git': shutil.which('git') is not None,
    }
    missing = [name for name, available in tools.items() if not available]
    if missing:
        raise ValueError('required tool unavailable: ' + ', '.join(missing))
    load_average = os.getloadavg()
    load = load_average[0]
    if not math.isfinite(load) or load < 0:
        raise ValueError('unknown node load')
    cpu = subprocess.run(
        ['/usr/bin/top', '-l', '2', '-n', '0'],
        capture_output=True, text=True, timeout=4,
        env={**os.environ, 'LC_ALL': 'C'})
    idle = re.findall(r'CPU usage:.*?([0-9]+(?:\.[0-9]+)?)% idle', cpu.stdout)
    if cpu.returncode != 0 or not idle:
        raise ValueError('unknown node CPU utilization')
    busy = 1.0 - float(idle[-1]) / 100.0
    if not math.isfinite(busy) or not 0 <= busy <= 1:
        raise ValueError('invalid node CPU utilization')
    return {'hostname': socket.gethostname(), 'load1': load,
            'load_average': list(load_average),
            'cpu_busy_fraction': busy, 'tools': tools, **policy}


def ssh_peer_address():
    """Return the address this SSH session reached, never a re-resolved name."""
    connection = os.environ.get('SSH_CONNECTION', '').split()
    if len(connection) != 4:
        raise ValueError('SSH_CONNECTION does not identify the remote peer address')
    try:
        return ipaddress.ip_address(connection[2]).compressed
    except ValueError as exc:
        raise ValueError('SSH_CONNECTION has an invalid remote peer address') from exc


def record(run):
    value = read_json(run / 'lease.json')
    value.update(read_json(run / 'owner.json'))
    return value


def stream_receipt(path, tail_bytes):
    """Hash a completed capture in bounded memory and retain only its tail."""
    digest = hashlib.sha256()
    tail = bytearray()
    try:
        stream = path.open('rb')
    except FileNotFoundError:
        return {
            'available': False,
            'size_bytes': 0,
            'sha256': hashlib.sha256(b'').hexdigest(),
            'tail': {
                'text': '', 'bytes_base64': '', 'truncated': False,
                'omitted_bytes': 0,
            },
        }
    with stream:
        before = os.fstat(stream.fileno())
        size = before.st_size
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OSError(f'{path.name} capture changed while its receipt was read')
            digest.update(chunk)
            tail.extend(chunk)
            if len(tail) > tail_bytes:
                del tail[:-tail_bytes]
            remaining -= len(chunk)
        after = os.fstat(stream.fileno())
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise OSError(f'{path.name} capture changed while its receipt was read')
    tail_bytes_value = bytes(tail)
    omitted = size - len(tail_bytes_value)
    return {
        'available': True,
        'size_bytes': size,
        'sha256': digest.hexdigest(),
        'tail': {
            'text': tail_bytes_value.decode('utf-8', errors='replace'),
            'bytes_base64': base64.b64encode(tail_bytes_value).decode('ascii'),
            'truncated': omitted > 0,
            'omitted_bytes': omitted,
        },
    }


def fetch_stream(run, request):
    name = request.get('stream')
    if name not in ('stdout', 'stderr'):
        raise ValueError('stream must be stdout or stderr')
    last_kb = request.get('last_kb')
    if isinstance(last_kb, bool) or not isinstance(last_kb, int) or last_kb < 1:
        raise ValueError('last_kb must be a positive integer')
    requested = last_kb * 1024
    path = run / name
    stream_size = path.stat().st_size
    if requested > MAX_FETCH_BYTES:
        return {
            'status': 'refused',
            'stream': name,
            'stream_size_bytes': stream_size,
            'requested_bytes': requested,
            'cap_bytes': MAX_FETCH_BYTES,
            'error': (
                f'{name} stream is {stream_size} bytes; requested tail is {requested} '
                f'bytes, above fetch cap {MAX_FETCH_BYTES} bytes'
            ),
        }
    returned = min(stream_size, requested)
    with path.open('rb') as stream:
        stream.seek(stream_size - returned)
        body = stream.read(returned)
    if len(body) != returned:
        raise OSError(f'{name} capture changed while its tail was fetched')
    compressed = gzip.compress(body, mtime=0)
    omitted = stream_size - len(body)
    return {
        'status': 'ok',
        'stream': name,
        'compression': 'gzip',
        'stream_size_bytes': stream_size,
        'requested_bytes': requested,
        'size_bytes': len(body),
        'truncated': omitted > 0,
        'omitted_bytes': omitted,
        'sha256': hashlib.sha256(body).hexdigest(),
        'body_gzip_base64': base64.b64encode(compressed).decode('ascii'),
    }


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
    managed = Path(managed).expanduser()
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
    if isinstance(value, str) and len(value) > 64:
        raise ValueError('repo must be at most 64 characters')
    if not isinstance(value, str) or not value or not value[0].isalnum():
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


def append_result(managed, state, run, *, body_path=None):
    """One durable line. Callers must do this before deleting the body."""
    exit_code = None
    outcome = None
    result_path = run / "result.json"
    if result_path.exists():
        try:
            outcome = read_json(result_path)
            exit_code = outcome.get("returncode")
            tail_bytes = min(
                MAX_RESULT_TAIL_BYTES,
                max(1, int(state.get("result_tail_bytes", DEFAULT_RESULT_TAIL_BYTES))),
            )
            outcome["streams"] = {
                name: stream_receipt(run / name, tail_bytes)
                for name in ("stdout", "stderr")
            }
        except (OSError, ValueError):
            exit_code = None
    line = {
        "run_id": state.get("lease_id"),
        "request_id": state.get("request_id"),
        "arm": state.get("arm"),
        "repo": state.get("repo") or "default",
        "sha": state.get("sha") or "",
        "slot": state.get("slot") or "",
        "host": (state.get("remote_run") or {}).get("host") or socket.gethostname(),
        "start": state.get("started_at"),
        "finish": time.time(),
        "status": state.get("release_reason") or state.get("state"),
        "exit_code": exit_code,
        "body_path": str(body_path or run),
        "bytes": directory_bytes(run),
        "lease_token": state.get("lease_token"),
        "lease": dict(state),
        "result": outcome,
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


def archived_run(managed, lease_id):
    path = managed / "results" / "index.jsonl"
    try:
        rows = path.read_text().splitlines()
    except OSError:
        return None
    for raw in reversed(rows):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("run_id") == lease_id:
            return row
    return None


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


def result_finish_times(managed):
    """Return terminal-body finish times from the durable result index."""
    path = managed / "results" / "index.jsonl"
    if not path.exists():
        return {}
    try:
        rows = path.read_text().splitlines()
    except OSError:
        return None
    finished = {}
    for raw in rows:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        run_id = row.get("run_id")
        finish = row.get("finish")
        if (isinstance(run_id, str) and isinstance(finish, (int, float))
                and not isinstance(finish, bool)):
            finished[run_id] = finish
    return finished


def audit(root, event):
    path = root / "audit.log"
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _report_bundle_basis_pin_release_unknown(managed, lease, *, source):
    lease_id = lease.get("lease_id")
    message = (
        "remote-ci bundle-basis-pin-release=unknown "
        f"lease_id={lease_id} source={source}"
    )
    try:
        audit(Path(managed) / "admission", {
            "action": "bundle-basis-pin-release", "result": "unknown",
            "lease_id": lease_id, "source": source, "at": time.time(),
        })
    except OSError:
        # If the audit write fails, the event cannot reach its normal reader.
        # Stderr remains the fallback for callers whose standard streams live.
        with contextlib.suppress(OSError, ValueError):
            print(message, file=sys.stderr, flush=True)


def _audit_forward_cursor_at_end(root):
    """Return the audit file identity and EOF offset, or an empty-log cursor."""
    path = Path(root) / "audit.log"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return {"device": None, "inode": None, "offset": 0}, None
    except OSError as exc:
        return {"device": None, "inode": None, "offset": 0}, exc
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH)
            stat = os.fstat(handle.fileno())
            return {
                "device": stat.st_dev, "inode": stat.st_ino,
                "offset": stat.st_size,
            }, None
        except OSError as exc:
            return {"device": None, "inode": None, "offset": 0}, exc
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)


def _valid_audit_forward_cursor(cursor):
    if not isinstance(cursor, dict):
        return False
    device, inode, offset = (
        cursor.get("device"), cursor.get("inode"), cursor.get("offset"),
    )
    if type(offset) is not int or offset < 0:
        return False
    return (
        device is None and inode is None
        or type(device) is int and type(inode) is int
    )


def _reset_audit_forward_cursor(root, *, warning):
    cursor, error = _audit_forward_cursor_at_end(root)
    if error is not None:
        return cursor, warning or "audit-read"
    try:
        write_json(Path(root) / "audit-forward.cursor.json", cursor)
    except OSError:
        return cursor, (
            f"{warning}-write-failed" if warning else "cursor-write"
        )
    return cursor, warning


def _load_audit_forward_cursor(root):
    """Load durable position, initializing unreadable state from the current EOF."""
    path = Path(root) / "audit-forward.cursor.json"
    try:
        cursor = read_json(path)
    except FileNotFoundError:
        return _reset_audit_forward_cursor(root, warning=None)
    except (OSError, ValueError, TypeError):
        return _reset_audit_forward_cursor(root, warning="cursor-unreadable")

    if not _valid_audit_forward_cursor(cursor):
        return _reset_audit_forward_cursor(root, warning="cursor-unreadable")

    current, error = _audit_forward_cursor_at_end(root)
    if error is not None:
        return cursor, "audit-read"
    identity_matches = (
        cursor["device"], cursor["inode"]
    ) == (current["device"], current["inode"])
    # A changed inode means rotation/replacement; a smaller file means truncation
    # or a stale offset. Start at the beginning of the new file in either case.
    if not identity_matches or cursor["offset"] > current["offset"]:
        current["offset"] = 0
        try:
            write_json(path, current)
        except OSError:
            return current, "cursor-write"
        return current, None
    return cursor, None


def _print_bundle_basis_pin_release_events(root, cursor, *, force_save=False):
    """Forward new cleanup rows and durably save their audit file position."""
    try:
        handle = (Path(root) / "audit.log").open("rb")
    except FileNotFoundError:
        return cursor, None
    with handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            stat = os.fstat(handle.fileno())
            same_file = (cursor["device"], cursor["inode"]) == (
                stat.st_dev, stat.st_ino,
            )
            offset = cursor["offset"] if same_file else 0
            if offset > stat.st_size:
                offset = 0
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # Keep an incomplete final row for the next pass.
                    offset = line_start
                    break
                offset = handle.tell()
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                result = event.get("result")
                # Only admitted and cleared are known routine successes. Forward
                # absent and all other results, including unknown outcomes.
                if result in ("admitted", "cleared"):
                    continue
                if (event.get("action") == "bundle-basis-pin-release"
                        and result == "unknown"):
                    print(
                        "admit-loop bundle-basis-pin-release=unknown "
                        f"lease_id={event.get('lease_id')} "
                        f"source={event.get('source')}",
                        flush=True,
                    )
                    continue
                action = event.get("action")
                if not isinstance(action, str) or not action:
                    action = "unknown"
                if not isinstance(result, str) or not result:
                    result = "unknown"
                details = [f"action={action}", f"result={result}"]
                for field in ("lease_id", "ticket", "reason"):
                    value = event.get(field)
                    if value is not None:
                        details.append(f"{field}={value}")
                print("admit-loop audit " + " ".join(details), flush=True)
            new_cursor = {
                "device": stat.st_dev, "inode": stat.st_ino, "offset": offset,
            }
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    if new_cursor == cursor and not force_save:
        return cursor, None
    try:
        write_json(Path(root) / "audit-forward.cursor.json", new_cursor)
    except OSError as exc:
        return new_cursor, exc
    return new_cursor, None


def _limit(request, key, default, numeric):
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, numeric) or value < 0:
        return default
    return min(default, value)


# A hung launchctl or lsof must not sit on the capacity path until the
# transport kills it. Timing out is unknown, which keeps the slot.
_COMMAND_TIMEOUT_SECONDS = 10


def _run_command(argv):
    """Completed process, or None if it failed to start or timed out."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _boot_session_id():
    """Return the kernel boot session token, or None when it is unreadable."""
    proc = _run_command(["sysctl", "-n", "kern.bootsessionuuid"])
    if proc is None or proc.returncode != 0:
        return None
    token = proc.stdout.strip()
    return token or None


def awake_monotonic():
    """Monotonic seconds excluding system sleep on supported box platforms."""
    for name in ("CLOCK_UPTIME_RAW", "CLOCK_MONOTONIC"):
        clock_id = getattr(time, name, None)
        if clock_id is not None:
            try:
                return time.clock_gettime(clock_id)
            except OSError:
                pass
    return time.monotonic()


def submitted_ticket_expired(state, *, now=None, boot_session=None,
                             ttl_seconds=SUBMITTED_TICKET_TTL_SECONDS):
    """Measure a queued submit ticket's TTL in awake time, resetting after reboot."""
    now = awake_monotonic() if now is None else now
    boot_session = _boot_session_id() if boot_session is None else boot_session
    started = state.get("ticket_awake_started")
    old_boot = state.get("ticket_boot_session")
    reset = (
        isinstance(started, bool) or not isinstance(started, (int, float))
        or now < started
        or (boot_session is not None and old_boot is not None and boot_session != old_boot)
    )
    if reset:
        state["ticket_awake_started"] = now
        state["ticket_boot_session"] = boot_session
        return False, True
    return now - started >= ttl_seconds, False


def cwd_snapshot():
    """One lsof of every process cwd. None means the snapshot cannot be trusted."""
    _timing_mark("cwd_census_start")
    proc = _run_command(["lsof", "-nP", "-d", "cwd", "-F", "pn"])
    _timing_mark("cwd_census_end")
    if (proc is None or proc.returncode != 0
            or not _lsof_stderr_is_mount_stat_warnings_only(proc.stderr)
            or not proc.stdout or not proc.stdout.strip()):
        return None
    rows = []
    pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            rows.append((pid, line[1:]))
    return rows


def _directory_id(path):
    """(realpath, dev, ino). None if the directory cannot be named.

    realpath spelling is not identity. On a case-insensitive volume the
    same directory keeps whatever case the caller passed.
    """
    try:
        st = os.stat(path)
        text = os.path.realpath(path)
    except OSError:
        return None
    return text, st.st_dev, st.st_ino


def _casefold_parts(path):
    return [part.casefold() for part in path.split(os.sep)]


def _path_is_under(path, root_text, root_dev, root_ino):
    """True if ``path`` is that directory or inside it.

    None means the answer cannot be proved. The decision is the directory
    inode, not the realpath string.
    """
    try:
        text = os.path.realpath(path)
    except OSError:
        return None
    root_parts = _casefold_parts(root_text)
    parts = _casefold_parts(text)
    if parts[:len(root_parts)] != root_parts:
        return False
    boundary = os.sep.join(text.split(os.sep)[:len(root_parts)]) or os.sep
    try:
        st = os.stat(boundary)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) == (root_dev, root_ino)


def cwd_intruders(paths, *, snapshot=_CWD_SNAPSHOT_UNSET):
    """Pids whose cwd is under ``paths``. None means the snapshot failed.

    Slot, GC, and pre-launch checks only. This is not proof a workload tree
    is dead; that proof is the launchd coalition. Only ESRCH means the pid
    is gone. Any other liveness error is unverifiable and keeps the slot.
    """
    snap = cwd_snapshot() if snapshot is _CWD_SNAPSHOT_UNSET else snapshot
    if snap is None:
        return None
    roots = []
    for path in paths:
        if not path:
            continue
        ident = _directory_id(path)
        if ident is None:
            return None
        roots.append(ident)
    found = []
    for pid, cwd in snap:
        if not cwd:
            continue
        matched = False
        for root_text, root_dev, root_ino in roots:
            under = _path_is_under(cwd, root_text, root_dev, root_ino)
            if under is None:
                return None
            if under:
                matched = True
                break
        if not matched:
            continue
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            continue
        except OSError:
            return None
        found.append(pid)
    return found


class _CwdSnapshotGuard:
    """Share one filtering census; require a fresh census before each mutation.

    Filtering callers are gc_run_bodies, _gc_run_scratch, reclaim_dead_prestart,
    lease_slot, and cleanup_legacy_slots. Mutations in those passes, plus
    finish_dead_workload, _release_holder, _mark_released, and
    _remove_run_scratch, prepare first, then take a fresh known, clear census
    immediately before deleting, releasing, reclaiming, leasing, or publishing.
    clear_tree may also use a standalone pre-launch check; it never authorizes
    release by itself.
    The multi-file release group takes one census before its first rename and
    publishes the remaining markers back-to-back.
    Under the admission lock, enqueue GC costs one filtering census plus at
    most two deletion attempts; a holder admission pass costs one filtering
    census plus at most two ownership-change attempts (refusals count). GC
    attempts include resident pairs and scratch deletions. Explicit operator
    GC is outside the lock and remains uncapped. An admitted release can spend
    two attempts if scratch and release publication are separate mutations;
    never-admitted ticket releases need no census. These bounds keep locked
    RPC/pass work to a small constant and leave refused admitted work eligible
    next pass. Health waits 2 s for the same lock, derived from the measured
    1.89 s bound.
    The remaining exposure is the census duration plus the immediate mutation,
    the same shape as the pre-outage per-run check. Unknown results are refused.
    """

    def __init__(self, *, max_fresh_attempts=None):
        self._snapshot = _CWD_SNAPSHOT_UNSET
        self._max_fresh_attempts = max_fresh_attempts
        self._fresh_attempts = 0

    def _refresh(self):
        self._snapshot = cwd_snapshot()

    def for_pass(self):
        if self._snapshot is _CWD_SNAPSHOT_UNSET:
            self._refresh()
        return self._snapshot

    def before_change(self):
        if (self._max_fresh_attempts is not None
                and self._fresh_attempts >= self._max_fresh_attempts):
            return None
        self._fresh_attempts += 1
        return cwd_snapshot()

    def intruders(self, paths, *, candidate=False):
        if candidate:
            return cwd_intruders(paths, snapshot=self.for_pass())
        snapshot = self.before_change()
        return cwd_intruders(paths, snapshot=snapshot)


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


def _verify_incarnation(pid, started):
    """True only when ``pid`` still has the persisted start token.

    False is ESRCH or a different process. None means the check cannot be
    read. A mismatch is not adopted and not signalled.
    """
    return _same_process(pid, started)


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
    same = _verify_incarnation(pid, started)
    if same is not True:
        return False if same is False else None
    found = _coalition_id(pid)
    if found is None:
        return None
    after = _verify_incarnation(pid, started)
    if after is not True:
        return False if after is False else None
    if found != cid:
        return None
    return True


def _private_tree_proof(state, cid, members=None, run=None):
    """True when the durable job coalition is private to this cleanup."""
    del members
    if (not isinstance(state, dict)
            or not _positive_cid(state.get("coalition_id"))
            or state.get("coalition_id") != cid
            or not _positive_cid(cid)):
        return False
    # XNU initializes coalition_next_id once per boot and assigns
    # coalition_next_id++ under the coalition list lock; coalition.id is
    # documented as monotonically increasing. That makes the boot-session
    # fence the incarnation fence for a coalition ID within one boot.
    # Source:
    # https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/coalition.c#L88-L90
    # https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/coalition.c#L258-L260
    # https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/coalition.c#L1228-L1268
    boot = state.get("coalition_boot_session")
    if not isinstance(boot, str) or not boot or _boot_session_id() != boot:
        return False
    holder = state.get("holder_coalition_id")
    holder_boot = state.get("holder_coalition_boot_session")
    if (not _positive_cid(holder) or cid == holder
            or not isinstance(holder_boot, str) or holder_boot != boot):
        return False
    mine = _coalition_id(os.getpid())
    if not _positive_cid(mine) or cid == mine:
        return False
    if run is None:
        identity = state.get("wrapper_identity")
    else:
        identity = _read_wrapper_identity(Path(run))
    if (not isinstance(identity, dict)
            or identity.get("coalition_id") != cid
            or identity.get("boot_session") != boot):
        return False
    if "workload_pid" in state:
        try:
            recorded_pid = int(state["workload_pid"])
        except (TypeError, ValueError):
            return False
        if identity.get("pid") != recorded_pid:
            return False
    if "workload_start" in state:
        recorded_start = _as_start(state["workload_start"])
        if recorded_start is None or tuple(identity.get("start") or ()) != recorded_start:
            return False
    return True


def _signal_incarnation(state, pid, started, cid, run=None):
    """TERM, then KILL only after a positive private-tree proof.

    Returns True when that incarnation is gone. A reused pid is not signalled.
    This is the only helper that sends a signal to a workload process.
    """
    pid = int(pid)
    if (pid <= 1 or pid == os.getpid() or started is None
            or not _private_tree_proof(state, cid, run=run)):
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
    if state.get("coalition_boot_session"):
        lease["coalition_boot_session"] = state["coalition_boot_session"]
    if state.get("workload_start"):
        lease["workload_start"] = state["workload_start"]
    if state.get("holder_coalition_id"):
        lease["holder_coalition_id"] = state["holder_coalition_id"]
    if state.get("holder_coalition_boot_session"):
        lease["holder_coalition_boot_session"] = state["holder_coalition_boot_session"]
    write_json(run / "lease.json", lease)


_LEASE_KEYS = (
    "launch_label", "coalition_id", "coalition_boot_session",
    "holder_coalition_id", "holder_coalition_boot_session", "workload_pid",
    "first_workload_pid",
    "workload_start", "workload_relaunched", "exit_code", "exit_known",
    "members", "job_remove_pending",
    "pending_result", "launch_submitted", "launch_seen", "launch_intent_at",
    "identity_status", "launch_rejected",
)

# How long a recorded submit may lack a wrapper identity before recovery
# says the tree is unproven. Absence after that still keeps the slot.
_INTENT_HOLD_SECONDS = 60
_WRAPPER_IDENTITY = "wrapper-identity.json"


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


def _positive_cid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _private_coalition(state, run=None):
    """A coalition id proven different from the holder and this process."""
    cid = state.get("coalition_id")
    if not _private_tree_proof(state, cid, run=run):
        return None
    return cid


def _gate_open(run):
    return run is not None and (run / "workload-go").is_file()


def _workload_released(run, state):
    """True when the real command was allowed to exec."""
    return _gate_open(run) or bool(state.get("members"))


def clear_tree(managed, state, run, *, cwd_guard=None):
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
    if label and _private_coalition(state, run) is None:
        adopted = _adopt_launch(run, state)
        if adopted == "unknown":
            return False
        if adopted == "empty":
            return True
    _capture_exit_status(run, state)
    cid = _private_coalition(state, run)
    if cid is None:
        if label or state.get("workload_pid") or state.get("members") or _gate_open(run):
            state["job_remove_pending"] = bool(label)
            _remember_identity(run, state)
            return False
        # No launch identity is not proof the slot is empty. A pre-start
        # holder can be draining with a live cwd and nothing else to name.
        paths = [p for p in (run, state.get("slot")) if p]
        cwd_guard = cwd_guard or _CwdSnapshotGuard()
        intruders = cwd_guard.intruders(paths)
        if intruders is None or intruders:
            return False
        return True
    return _kill_members(run, state, cid, label)


def _read_wrapper_identity(run):
    """The wrapper's own pid, start token, and coalition.

    None if the file is not there yet. 'unreadable' if it is not a usable
    identity. A dict is the record.
    """
    if run is None:
        return None
    path = run / _WRAPPER_IDENTITY
    if not path.is_file():
        return None
    try:
        body = read_json(path)
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(body, dict):
        return "unreadable"
    pid = body.get("pid")
    start = _as_start(body.get("start"))
    cid = body.get("coalition_id")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or start is None:
        return "unreadable"
    if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
        cid = None
    boot = body.get("boot_session")
    if not isinstance(boot, str) or not boot:
        boot = None
    return {"pid": pid, "start": start, "coalition_id": cid,
            "boot_session": boot}


def _identity_allows_release(run, state):
    """True only when a recorded launch is positively gone.

    No wrapper record and no submit intent: the coalition scan is the proof.
    A recorded submit with no identity, or an identity that is still alive
    or whose coalition cannot be listed, is not proof.
    """
    ident = _read_wrapper_identity(run)
    if ident is None and not state.get("launch_submitted"):
        return True
    if not isinstance(ident, dict):
        return False
    if _verify_incarnation(ident["pid"], ident["start"]) is not False:
        return False
    cid = ident.get("coalition_id")
    boot = ident.get("boot_session")
    if not cid or not isinstance(boot, str) or not boot or _boot_session_id() != boot:
        return False
    if state.get("coalition_id") not in (None, cid):
        return False
    if state.get("coalition_boot_session") not in (None, boot):
        return False
    members = _coalition_members(cid)
    if members is None or members:
        return False
    return True


def _hold_unproven(run, state):
    """Keep the slot. After the intent window, say the wrapper was not proved."""
    started = state.get("launch_intent_at")
    if (isinstance(started, bool) or not isinstance(started, (int, float))
            or time.time() >= started + _INTENT_HOLD_SECONDS):
        state["identity_status"] = "unproven"
    state["job_remove_pending"] = True
    _remember_identity(run, state)
    return "unknown"


def _recover_absent(run, state):
    """Absent from launchd is not an empty tree.

    Release only when the wrapper's incarnation is dead and its coalition
    has no members. A missing identity is unknown, during the wait and after.
    """
    # Only the explicit launchd rejection proof can bypass the recorded-intent
    # path. An ambiguous submit failure may have accepted the job already.
    if (state.get("launch_rejected") is True
            and _job_view(state.get("launch_label") or "") == "absent"):
        ident = _read_wrapper_identity(run)
        if not isinstance(ident, dict):
            state["job_remove_pending"] = False
            _remember_identity(run, state)
            return "empty"
    ident = _read_wrapper_identity(run)
    if ident == "unreadable":
        return _hold_unproven(run, state)
    if not isinstance(ident, dict):
        return _hold_unproven(run, state)
    verdict = _verify_incarnation(ident["pid"], ident["start"])
    if verdict is None:
        return _hold_unproven(run, state)
    state["workload_pid"] = str(ident["pid"])
    state.setdefault("first_workload_pid", str(ident["pid"]))
    state["workload_start"] = [ident["start"][0], ident["start"][1]]
    if ident.get("coalition_id"):
        state["coalition_id"] = ident["coalition_id"]
    if ident.get("boot_session"):
        state["coalition_boot_session"] = ident["boot_session"]
    if verdict is True or not _identity_allows_release(run, state):
        state["job_remove_pending"] = True
        if verdict is True and not _positive_cid(state.get("holder_coalition_id")):
            state["identity_status"] = "holder-coalition-unknown"
            _remember_identity(run, state)
            return "unknown"
        _remember_identity(run, state)
        if verdict is True and _private_coalition(state, run):
            return "ready"
        return "unknown"
    state["job_remove_pending"] = False
    state["identity_status"] = "dead"
    _remember_identity(run, state)
    return "empty"


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
        return _recover_absent(run, state)
    kind, pid, code = view
    if kind == "exited":
        ident = _read_wrapper_identity(run)
        if isinstance(ident, dict):
            state["workload_pid"] = str(ident["pid"])
            state.setdefault("first_workload_pid", str(ident["pid"]))
        _capture_exit_status(run, state)
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
    if not _positive_cid(state.get("holder_coalition_id")):
        state["identity_status"] = "holder-coalition-unknown"
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    ident = _read_wrapper_identity(run)
    started = ident["start"] if isinstance(ident, dict) else _as_start(state.get("workload_start"))
    persisted_cid = ident.get("coalition_id") if isinstance(ident, dict) else state.get("coalition_id")
    if isinstance(ident, dict) and ident["pid"] != pid:
        started = None
    if started is None:
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    identity = _identity_for_job(label, pid, started, persisted_cid,
                                 ident.get("boot_session") if isinstance(ident, dict) else None)
    if identity is None:
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    start, cid, boot = identity
    holder = state.get("holder_coalition_id")
    state["workload_pid"] = str(pid)
    state.setdefault("first_workload_pid", str(pid))
    state["workload_start"] = [start[0], start[1]]
    if not _positive_cid(holder):
        state["identity_status"] = "holder-coalition-unknown"
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    holder_boot = state.get("holder_coalition_boot_session")
    if (cid == holder or not _positive_cid(cid)
            or not isinstance(boot, str) or not boot
            or holder_boot != boot):
        state["identity_status"] = "coalition-not-private"
        state["job_remove_pending"] = True
        _remember_identity(run, state)
        return "unknown"
    state["coalition_id"] = cid
    state["coalition_boot_session"] = boot
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
        if living and not _positive_cid(state.get("holder_coalition_id")):
            state["identity_status"] = "holder-coalition-unknown"
            state["job_remove_pending"] = bool(label)
            _remember_identity(run, state)
            return False
        if living and not _private_tree_proof(state, cid, run=run):
            state["identity_status"] = "coalition-not-private"
            state["job_remove_pending"] = bool(label)
            _remember_identity(run, state)
            return False
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
            if not _identity_allows_release(run, state):
                state["job_remove_pending"] = bool(label)
                _remember_identity(run, state)
                return False
            state["job_remove_pending"] = False
            _remember_identity(run, state)
            return True
        for pid, start in living.items():
            _signal_incarnation(state, pid, start, cid, run=run)
        persisted = living
    state["job_remove_pending"] = bool(label)
    _store_members(run, state, persisted)
    _remember_identity(run, state)
    return False


def _job_label(lease_id):
    return "com.goalflight.remote-ci." + str(lease_id)


def _launchctl_jobs():
    """label -> (pid or None, exit code or None). None if the list failed."""
    listed = _run_command(["launchctl", "list"])
    if listed is None or listed.returncode != 0:
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
        if code is not None and code > 255 and code & 0xff == 0:
            code >>= 8
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
    if _run_command(["launchctl", "remove", label]) is None:
        return False
    return _job_view(label) == "absent"


def _capture_exit_status(run, state, wait=False):
    """Read the workload's own exit record, then first-launch status if safe.

    Does not remove the job. A missing code is not success. Returns the
    code, or None when it is not known yet.
    """
    exit_path = run / "exit.json" if run is not None else None
    if exit_path is not None and exit_path.exists():
        try:
            outcome = read_json(exit_path)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(outcome, dict):
            return None
        prior_pid = state.get("first_workload_pid", state.get("workload_pid"))
        try:
            prior_pid = int(prior_pid)
        except (TypeError, ValueError):
            prior_pid = None
        code = outcome.get("returncode")
        if (prior_pid is None or outcome.get("pid") != prior_pid
                or isinstance(code, bool) or not isinstance(code, int)):
            return None
        state["exit_code"] = code
        state["exit_known"] = True
        _remember_identity(run, state)
        return code
    code = state.get("exit_code")
    if (state.get("exit_known") is True and isinstance(code, int)
            and not isinstance(code, bool)):
        return code
    if state.get("workload_relaunched") is True:
        return None
    if run is not None and (run / "relaunch-noop").exists():
        state["workload_relaunched"] = True
        _remember_identity(run, state)
        return None
    label = state.get("launch_label")
    if not label:
        return None
    deadline = time.monotonic() + (2.0 if wait else 0.0)
    while True:
        view = _job_view(label)
        prior_pid = state.get("first_workload_pid", state.get("workload_pid"))
        relaunched = (
            isinstance(view, tuple) and view[0] == "running"
            and prior_pid is not None and view[1] is not None
            and str(view[1]) != str(prior_pid)
        )
        if relaunched:
            state["workload_relaunched"] = True
            _remember_identity(run, state)
            return None
        if isinstance(view, tuple) and view[0] == "exited":
            identity = _read_wrapper_identity(run) if run is not None else None
            if (prior_pid is None or not isinstance(identity, dict)
                    or str(identity.get("pid")) != str(prior_pid)
                    or (run / "relaunch-noop").exists()):
                return None
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
                "status": "completed" if code == 0 else "died", "exit_known": True,
                "command_started": state.get("command_started") is True}
    return {"returncode": 2, "timed_out": False, "status": "died",
            "exit_known": False,
            "command_started": state.get("command_started") is True}


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
    """(start, coalition, boot) when two reads agree, else None."""
    first = _coalition_id(pid)
    start = _start_time(pid)
    second = _coalition_id(pid)
    start_again = _start_time(pid)
    boot = _boot_session_id()
    if (first is None or second is None or start is None
            or first != second or start != start_again or first <= 0 or not boot):
        return None
    return start, first, boot


def _identity_for_job(label, pid, started, coalition, boot_session):
    """(start, coalition, boot) only when the live identity is persisted.

    ``started`` is the first observation (the wrapper's own record). The
    same pid with a different start is a different process: do not adopt it.
    """
    if _verify_incarnation(pid, started) is not True:
        return None
    identity = _stable_identity(pid)
    if identity is None or identity[0] != started:
        return None
    if not _positive_cid(coalition) or identity[1] != coalition:
        return None
    if not isinstance(boot_session, str) or not boot_session or identity[2] != boot_session:
        return None
    view = _job_view(label)
    if not (isinstance(view, tuple) and view[0] == "running" and view[1] == pid):
        return None
    if _verify_incarnation(pid, started) is not True:
        return None
    return started, coalition, boot_session


# Field layout matches _ProcBsdInfo and _CoalInfo. The wrapper is a separate
# process and cannot import this module when the node is exec'd from a string.
_WORKLOAD_WRAPPER = (
    "import ctypes, json, os, resource, re, shlex, socket, subprocess, sys, time, uuid\n"
    "import xml.etree.ElementTree as ET\n"
    "from pathlib import Path\n"
    "def identity():\n"
    "    pid = os.getpid()\n"
    "    start = None\n"
    "    coalition = None\n"
    "    boot = None\n"
    "    try:\n"
    "        lib = ctypes.CDLL('/usr/lib/libproc.dylib')\n"
    "        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]\n"
    "        lib.proc_pidinfo.restype = ctypes.c_int\n"
    "        class BSD(ctypes.Structure):\n"
    "            _fields_ = [\n"
    "                ('pbi_flags', ctypes.c_uint32), ('pbi_status', ctypes.c_uint32),\n"
    "                ('pbi_xstatus', ctypes.c_uint32), ('pbi_pid', ctypes.c_uint32),\n"
    "                ('pbi_ppid', ctypes.c_uint32), ('pbi_uid', ctypes.c_uint32),\n"
    "                ('pbi_gid', ctypes.c_uint32), ('pbi_ruid', ctypes.c_uint32),\n"
    "                ('pbi_rgid', ctypes.c_uint32), ('pbi_svuid', ctypes.c_uint32),\n"
    "                ('pbi_svgid', ctypes.c_uint32), ('rfu_1', ctypes.c_uint32),\n"
    "                ('pbi_comm', ctypes.c_char * 16), ('pbi_name', ctypes.c_char * 32),\n"
    "                ('pbi_nfiles', ctypes.c_uint32), ('pbi_pgid', ctypes.c_uint32),\n"
    "                ('pbi_pjobc', ctypes.c_uint32), ('e_tdev', ctypes.c_uint32),\n"
    "                ('e_tpgid', ctypes.c_uint32), ('pbi_nice', ctypes.c_int32),\n"
    "                ('pbi_start_tvsec', ctypes.c_uint64), ('pbi_start_tvusec', ctypes.c_uint64)]\n"
    "        class Coal(ctypes.Structure):\n"
    "            _fields_ = [('ids', ctypes.c_uint64 * 2), ('reserved', ctypes.c_uint64 * 3)]\n"
    "        info = BSD()\n"
    "        size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))\n"
    "        if size == ctypes.sizeof(info) and int(info.pbi_pid) == pid:\n"
    "            start = [int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)]\n"
    "        coal = Coal()\n"
    "        size = lib.proc_pidinfo(pid, 20, 0, ctypes.byref(coal), ctypes.sizeof(coal))\n"
    "        if size == ctypes.sizeof(coal):\n"
    "            coalition = int(coal.ids[0])\n"
    "        result = subprocess.run(['sysctl', '-n', 'kern.bootsessionuuid'],\n"
    "                                  capture_output=True, text=True, timeout=5)\n"
    "        if result.returncode == 0:\n"
    "            boot = result.stdout.strip() or None\n"
    "    except (OSError, subprocess.TimeoutExpired):\n"
    "        pass\n"
    "    return {'pid': pid, 'start': start, 'coalition_id': coalition,\n"
    "            'boot_session': boot}\n"
    "spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
    "run = Path(sys.argv[1]).resolve().parent\n"
    "if (run / 'command-started').exists():\n"
    "    try:\n"
    "        fd = os.open(str(run / 'relaunch-noop'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
    "        os.close(fd)\n"
    "    except FileExistsError:\n"
    "        pass\n"
    "    sys.exit(0)\n"
    "raw = json.dumps(identity()).encode()\n"
    "tmp = run / ('wrapper-identity.json.' + str(os.getpid()))\n"
    "fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)\n"
    "os.write(fd, raw)\n"
    "os.fsync(fd)\n"
    "os.close(fd)\n"
    "os.replace(tmp, run / 'wrapper-identity.json')\n"
    "gate = sys.argv[2]\n"
    f"gate_deadline = time.monotonic() + {WORKLOAD_START_GATE_TIMEOUT_SECONDS!r}\n"
    "while not os.path.exists(gate) and time.monotonic() < gate_deadline:\n"
    "    time.sleep(min(0.02, max(0.0, gate_deadline - time.monotonic())))\n"
    "if not os.path.exists(gate):\n"
    "    sys.exit(124)\n"
    "try:\n"
    "    started_fd = os.open(str(run / 'command-started'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
    "except FileExistsError:\n"
    "    sys.exit(0)\n"
    "else:\n"
    "    os.close(started_fd)\n"
    "cwd = spec.get('cwd') or ''\n"
    "emit_receipt = spec.get('emit_receipt')\n"
    "env = dict(spec['env'])\n"
    "try:\n"
    "    load_start = list(os.getloadavg())\n"
    "except (AttributeError, OSError):\n"
    "    load_start = []\n"
    "usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
    "wall_start = time.monotonic()\n"
    "try:\n"
    "    lease = json.loads((run / 'lease.json').read_text(encoding='utf-8'))\n"
    "    command_timeout = float(lease['deadline_epoch']) - float(lease['started_at'])\n"
    "    started_at = (run / 'command-started').stat().st_mtime\n"
    "    remaining = max(0.0, command_timeout - max(0.0, time.time() - started_at))\n"
    "    if emit_receipt:\n"
    "        # This fresh per-run JUnit file separates executed cases from stale stdout summaries.\n"
    "        # It addresses accidental echoed output; hostile command tampering is out of scope.\n"
    "        junitxml_path = run / 'pytest-results.xml'\n"
    "        junitxml_option = '--junitxml=' + shlex.quote(str(junitxml_path))\n"
    "        env['PYTEST_ADDOPTS'] = ' '.join(filter(None, (env.get('PYTEST_ADDOPTS', ''), junitxml_option)))\n"
    "    child = subprocess.Popen(spec['argv'], env=env, cwd=cwd or None)\n"
    "    try:\n"
    "        returncode = child.wait(timeout=remaining)\n"
    "    except subprocess.TimeoutExpired:\n"
    "        try:\n"
    "            child.kill()\n"
    "        except OSError:\n"
    "            pass\n"
    "        try:\n"
    "            child.wait(timeout=5)\n"
    "        except subprocess.TimeoutExpired:\n"
    "            pass\n"
    "        returncode = 124\n"
    "except OSError as exc:\n"
    "    print('remote command could not start: ' + str(exc), file=sys.stderr, flush=True)\n"
    "    returncode = 127\n"
    "else:\n"
    "    exit_record = {'pid': os.getpid(), 'returncode': returncode,\n"
    "                   'signal': -returncode if returncode < 0 else None}\n"
    "    temporary = run / ('exit.json.' + str(os.getpid()) + '.' + uuid.uuid4().hex + '.tmp')\n"
    "    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
    "    try:\n"
    "        with os.fdopen(fd, 'w', encoding='utf-8') as output:\n"
    "            json.dump(exit_record, output, sort_keys=True)\n"
    "            output.write('\\n')\n"
    "            output.flush()\n"
    "            os.fsync(output.fileno())\n"
    "        try:\n"
    "            os.link(str(temporary), str(run / 'exit.json'))\n"
    "        except FileExistsError:\n"
    "            pass\n"
    "    finally:\n"
    "        temporary.unlink(missing_ok=True)\n"
    "if not emit_receipt:\n"
    "    sys.exit(returncode)\n"
    "passed, failed, skipped, errors = 0, 0, 0, 0\n"
    "failure_node_ids, collection_errors, first_error_lines, failure_excerpt = [], [], [], []\n"
    "failure_summary_complete = False\n"
    "# JUnit testcase elements include xfails (skipped), ordinary xpasses (passed), and strict xpasses (failures).\n"
    "# Strict xpass remains red because pytest's own return code and JUnit failure both say so.\n"
    "try:\n"
    "    junit_root = ET.parse(run / 'pytest-results.xml').getroot()\n"
    "    for testcase in junit_root.iter('testcase'):\n"
    "        if testcase.find('failure') is not None:\n"
    "            failed += 1\n"
    "        elif testcase.find('error') is not None:\n"
    "            errors += 1\n"
    "        elif testcase.find('skipped') is not None:\n"
    "            skipped += 1\n"
    "        else:\n"
    "            passed += 1\n"
    "except (OSError, ET.ParseError):\n"
    "    pass\n"
    "usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
    "wall_seconds = time.monotonic() - wall_start\n"
    "try:\n"
    "    load_end = list(os.getloadavg())\n"
    "except (AttributeError, OSError):\n"
    "    load_end = []\n"
    "cpu_seconds = (usage_after.ru_utime + usage_after.ru_stime - usage_before.ru_utime - usage_before.ru_stime)\n"
    "if emit_receipt:\n"
    "    try:\n"
    "        with (run / 'stdout').open('rb') as output:\n"
    "            output.seek(max(0, output.seek(0, os.SEEK_END) - 65536))\n"
    "            tail = output.read().decode('utf-8', errors='replace')\n"
    "        error_block_lines = 0\n"
    "        for line in tail.splitlines():\n"
    "            if re.match(r'^\\s*E(?:\\s|$)', line):\n"
    "                if len(failure_excerpt) < 20 and error_block_lines < 5:\n"
    "                    failure_excerpt.append(line.strip()[:500])\n"
    "                error_block_lines += 1\n"
    "            else:\n"
    "                error_block_lines = 0\n"
    "        lines = tail.splitlines()\n"
    "        summary_header = next((index for index, line in enumerate(lines)\n"
    "                               if 'short test summary info' in line.lower()), None)\n"
    "        summary_end = next((index for index in range(len(lines) - 1, -1, -1)\n"
    "                            if re.search(r'\\bin\\s+[0-9]+(?:\\.[0-9]+)?s(?:\\s+\\([^)]*\\))?\\s*(?:=+\\s*)?$', lines[index])), None)\n"
    "        if summary_header is not None and summary_end is not None and summary_end > summary_header:\n"
    "            failure_summary_complete = True\n"
    "            for line in lines[summary_header + 1:summary_end]:\n"
    "                match = re.match(r'^\\s*(FAILED|ERROR)\\s+(.+?)\\s*$', line)\n"
    "                if not match:\n"
    "                    continue\n"
    "                kind, summary_item = match.groups()\n"
    "                if kind == 'ERROR' and line.strip() not in first_error_lines:\n"
    "                    first_error_lines.append(line.strip())\n"
    "                if kind == 'ERROR' and summary_item.startswith('collecting '):\n"
    "                    path = summary_item[len('collecting '):]\n"
    "                    if path and path not in collection_errors:\n"
    "                        collection_errors.append(path)\n"
    "                    continue\n"
    "                node_id = summary_item.rsplit(' - ', 1)[0] if ' - ' in summary_item else summary_item\n"
    "                if '::' not in node_id or not all(part.strip() for part in node_id.split('::', 1)):\n"
    "                    continue\n"
    "                if node_id not in failure_node_ids:\n"
    "                    failure_node_ids.append(node_id)\n"
    "    except OSError:\n"
    "        pass\n"
    "receipt = {'schema': 'goalflight.remote-ci.receipt.v1', 'hostname': socket.gethostname(),\n"
    "    'status': 'pass' if returncode == 0 else 'fail', 'returncode': returncode,\n"
    "    'passed': passed, 'failed': failed, 'skipped': skipped, 'errors': errors,\n"
    "    'failing_node_ids': failure_node_ids, 'collection_import_errors': collection_errors,\n"
    "    'first_error_lines': first_error_lines,\n"
    "    'failure_summary': {'failing_node_ids': failure_node_ids, 'collection_import_errors': collection_errors},\n"
    "    'failure_excerpt': failure_excerpt, 'failure_summary_complete': failure_summary_complete,\n"
    "    'stdout_path': str((run / 'stdout').resolve()),\n"
    "    'box_id': env.get('GOALFLIGHT_REMOTE_CI_BOX', ''),\n"
    "    'expected_hostname': env.get('GOALFLIGHT_REMOTE_CI_EXPECTED_HOSTNAME', ''),\n"
    "    'measured_hostname': socket.gethostname(),\n"
    "    'tip_sha': env.get('GOALFLIGHT_REMOTE_CI_TIP_SHA', ''),\n"
    "    'candidate_sha': env.get('GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA', ''),\n"
    "    'head_sha': env.get('GOALFLIGHT_REMOTE_CI_HEAD_SHA', ''),\n"
    "    'snapshot_sha': env.get('GOALFLIGHT_REMOTE_CI_SNAPSHOT_SHA', ''),\n"
    "    'dirty': env.get('GOALFLIGHT_REMOTE_CI_DIRTY', 'false') == 'true',\n"
    "    'command_id': env.get('GOALFLIGHT_REMOTE_CI_COMMAND_ID', ''),\n"
    "    'process_cpu_seconds': max(0.0, cpu_seconds), 'wall_seconds': max(0.0, wall_seconds),\n"
    "    'duration_seconds': max(0.0, wall_seconds), 'load_average_start': load_start,\n"
    "    'load_average_end': load_end, 'route': 'studio',\n"
    "    'route_reason': env.get('GOALFLIGHT_REMOTE_CI_ROUTE_REASON', ''),\n"
    "    'project_id': env.get('GOALFLIGHT_REMOTE_CI_PROJECT_ID', ''),\n"
    "    'policy_outcome': env.get('GOALFLIGHT_REMOTE_CI_POLICY_OUTCOME', 'ran_remote'),\n"
    "    'fallback_reason': None,\n"
    "    'requirements_sha256': env.get('GOALFLIGHT_REMOTE_CI_REQUIREMENTS_SHA256', ''),\n"
    "    'venv_path': env.get('GOALFLIGHT_REMOTE_CI_VENV_PATH', '')}\n"
    "command_override_id = env.get('GOALFLIGHT_REMOTE_CI_COMMAND_OVERRIDE_ID', '')\n"
    "if command_override_id:\n"
    "    receipt['command_override_id'] = command_override_id\n"
    "profile = json.loads(env.get('GOALFLIGHT_REMOTE_CI_PROFILE_RECEIPT_JSON', '{}'))\n"
    "if profile:\n"
    "    receipt['profile'] = profile\n"
    "print(json.dumps(receipt, sort_keys=True), flush=True)\n"
    "sys.exit(returncode)\n"
)


def _await_command_started(run, pid, started):
    """Wait for the workload wrapper's existing command-start signal."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (run / 'command-started').is_file():
            return True
        if _same_process(pid, started) is not True:
            return False
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
    spec_path.write_text(json.dumps({
        "cwd": cwd or "", "argv": list(argv), "env": env,
        "emit_receipt": env.get("GOALFLIGHT_REMOTE_CI_EMIT_RECEIPT") == "1",
    }))
    wrapper_path.write_text(_WORKLOAD_WRAPPER)
    stdout = run / "stdout"
    stderr = run / "stderr"
    stdout.touch()
    stderr.touch()
    holder_cid = state.get("holder_coalition_id")
    if not _positive_cid(holder_cid):
        holder_cid = _coalition_id(os.getpid())
        holder_boot = _boot_session_id()
        if _positive_cid(holder_cid) and holder_boot:
            state["holder_coalition_id"] = holder_cid
            state["holder_coalition_boot_session"] = holder_boot
    state["launch_label"] = label
    # Intent is durable before the syscall. A holder killed during submit
    # must not recover as "the job never existed".
    state["launch_submitted"] = True
    state["launch_intent_at"] = time.time()
    state["job_remove_pending"] = True
    write_json(run / "lease.json", state)
    if not _fsync_file(run / "lease.json"):
        return None
    try:
        recorded = read_json(run / "lease.json")
    except (OSError, ValueError):
        return None
    if recorded.get("launch_submitted") is not True or recorded.get("launch_label") != label:
        return None
    before_submit = _job_view(label)
    try:
        submitted = subprocess.run(
            ["launchctl", "submit", "-l", label, "-o", str(stdout), "-e", str(stderr),
             "--", sys.executable, str(wrapper_path), str(spec_path), str(gate_path)],
            capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        return str(exc)
    if submitted.returncode != 0:
        output = (f"{getattr(submitted, 'stderr', '') or ''}\n"
                  f"{getattr(submitted, 'stdout', '') or ''}").lower()
        # A failed submit is empty only when launchd explicitly rejected it as
        # not loaded and the label was absent both before and after submit.
        # Every other failure follows the durable recorded-intent path.
        if ("not loaded" in output and before_submit == "absent"
                and _job_view(label) == "absent"):
            state["launch_rejected"] = True
            _remember_identity(run, state)
        return submitted.stderr.strip() or "launchctl submit failed"
    _remember_identity(run, state)
    # A slow launchd must not become "the job never existed". Expiry keeps
    # the label and leaves the tree unknown.
    deadline = time.monotonic() + WORKLOAD_START_GATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        view = _job_view(label)
        if view == "unknown" or view == "absent":
            time.sleep(0.02)
            continue
        kind, pid, code = view
        state["launch_seen"] = True
        if kind == "exited":
            ident = _read_wrapper_identity(run)
            if isinstance(ident, dict):
                state["workload_pid"] = str(ident["pid"])
                state.setdefault("first_workload_pid", str(ident["pid"]))
            _capture_exit_status(run, state)
            _remember_identity(run, state)
            return None
        ident = _read_wrapper_identity(run)
        if not isinstance(ident, dict) or ident["pid"] != pid:
            time.sleep(0.02)
            continue
        identity = _identity_for_job(
            label, pid, ident["start"], ident.get("coalition_id"),
            ident.get("boot_session"))
        if identity is None:
            time.sleep(0.02)
            continue
        start, cid, boot = identity
        state["workload_pid"] = str(pid)
        state.setdefault("first_workload_pid", str(pid))
        state["workload_start"] = [start[0], start[1]]
        holder_cid = state.get("holder_coalition_id")
        holder_boot = state.get("holder_coalition_boot_session")
        if (not _positive_cid(holder_cid) or cid == holder_cid
                or not isinstance(boot, str) or not boot or holder_boot != boot):
            # Not a private coalition. Do not record it as a kill target.
            _remember_identity(run, state)
            return "coalition was not private"
        state["coalition_id"] = cid
        state["coalition_boot_session"] = boot
        _remember_identity(run, state)
        # The wrapper execs only after the id is on disk and readable again.
        # A failed lease write must not open the gate: recovery cannot kill
        # a tree it cannot name.
        try:
            lease_path = run / "lease.json"
            if not _fsync_file(lease_path):
                return "coalition id was not durable"
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
    deadline = state.get("phase_deadline_epoch")
    if (state.get("phase") not in {"setup", "execution", "finalization"}
            or not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)):
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


# Enqueue GC makes at most two fresh pre-deletion census ATTEMPTS (including
# refusals), so lock time stays at one filtering census plus two fresh ones.
MAX_GC_DELETIONS_PER_PASS = 2
# Bounds holder admission lock time to one filter census plus two fresh
# admitted ownership-change attempts; refusals spend an attempt too.
MAX_OWNERSHIP_CENSUS_ATTEMPTS_PER_PASS = 2


def gc_run_bodies(managed, request, apply, *, cap_deletions=False):
    """Drop terminal bodies that already have a result line. Unknown stays."""
    success_keep = _limit(request, "retain_max_count", SUCCESS_KEEP, int)
    failure_keep = _limit(request, "retain_failure_count", FAILURE_KEEP, int)
    success_age = max(
        RUN_BODY_RETENTION_FLOOR_SECONDS,
        _limit(request, "retain_max_age_seconds", SUCCESS_AGE_SECONDS, (int, float)),
    )
    failure_age = max(
        RUN_BODY_RETENTION_FLOOR_SECONDS,
        _limit(request, "retain_failure_age_seconds", FAILURE_AGE_SECONDS, (int, float)),
    )
    indexed = result_ids(managed)
    finished_at = result_finish_times(managed)
    rows = []
    if indexed is None or finished_at is None:
        rows.append({"class": "UNKNOWN", "bytes": 0, "path": str(managed / "results" / "index.jsonl")})
    grouped = {"success": [], "failure": []}
    cwd_guard = _CwdSnapshotGuard()
    now = time.time()
    runs = managed / "runs"
    bodies = (*(sorted(runs.iterdir()) if runs.is_dir() else ()), *iter_retained_dirs(managed))
    for path in bodies if indexed is not None and finished_at is not None else ():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            state = read_json(path / "lease.json")
        except (OSError, ValueError):
            rows.append({"class": "UNKNOWN", "bytes": directory_bytes(path), "path": str(path)})
            continue
        if (state.get("resident_submission") is True
                and path.parent != managed / "resident-results"
                and (managed / "resident-results" / path.name).exists()):
            # Its terminal body is retained independently of legacy GC. If the
            # retained partner is already gone (a pair deletion removed it and
            # then failed on this original), fall through and collect this
            # original as an ordinary body, so it is retried rather than stranded.
            continue
        original = Path(state.get("run_directory", str(path)))
        if original != path and original.exists():
            try:
                original_state = read_json(original / "lease.json")
            except (OSError, ValueError):
                continue
            if original_state.get("state") != "released" or locked(original / "holder.lock"):
                continue
        if not prunable(path, state) or state.get("lease_id") not in indexed:
            continue
        # Seat contents are re-created before reuse; only this run body is retained.
        paths = [path, original] if original != path else [path]
        intruders = cwd_guard.intruders(paths, candidate=True)
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
        terminal_at = finished_at.get(state.get("lease_id"))
        if terminal_at is None:
            rows.append({"class": "UNKNOWN", "bytes": directory_bytes(path), "path": str(path)})
            continue
        grouped[kind].append((terminal_at, path, state))
    doomed = []
    for kind, items in grouped.items():
        keep = success_keep if kind == "success" else failure_keep
        age = success_age if kind == "success" else failure_age
        items.sort()
        count_eligible = [item for item in items
                          if now - item[0] >= RUN_BODY_RETENTION_FLOOR_SECONDS]
        overflow = count_eligible[:max(0, len(count_eligible) - keep)]
        aged = [item for item in count_eligible if now - item[0] >= age]
        chosen = {item[1]: item for item in overflow + aged}
        doomed.extend(chosen.values())
    deletions = 0
    for acquired, path, state in doomed:
        del acquired
        if apply:
            if cap_deletions and deletions >= MAX_GC_DELETIONS_PER_PASS:
                break
            original = Path(state.get("run_directory", str(path)))
            if (path.parent == managed / "resident-results"
                    and original == managed / "runs" / path.name and original.is_dir()
                    and not original.is_symlink()):
                # A resident pair (runs/<id> plus resident-results/<id>) is ONE
                # deletion: one fresh census over both paths, then both removed
                # back-to-back, so the cap can never split the pair and strand
                # the retained body as UNKNOWN on later passes.
                body_bytes = directory_bytes(path)
                deletions += 1
                intruders = cwd_guard.intruders([path, original])
                if intruders is None or intruders:
                    rows.append({"class": "UNKNOWN" if intruders is None else "LIVE",
                                 "bytes": body_bytes, "path": str(path)})
                    continue
                rows.append({"class": "ELIGIBLE", "bytes": body_bytes, "path": str(path)})
                try:
                    shutil.rmtree(path)
                except OSError:
                    rows[-1] = {"class": "UNKNOWN", "bytes": body_bytes,
                                "path": str(path)}
                    continue
                shutil.rmtree(original)
                continue
            body_bytes = directory_bytes(path)
            deletions += 1
            intruders = cwd_guard.intruders([path])
            if intruders is None or intruders:
                rows.append({"class": "UNKNOWN" if intruders is None else "LIVE",
                             "bytes": body_bytes, "path": str(path)})
                continue
            rows.append({"class": "ELIGIBLE", "bytes": body_bytes, "path": str(path)})
            shutil.rmtree(path, ignore_errors=True)
        else:
            rows.append({"class": "ELIGIBLE", "bytes": directory_bytes(path), "path": str(path)})
    rows.extend(_gc_run_scratch(
        managed, apply, cwd_guard=cwd_guard,
        deletion_budget=(MAX_GC_DELETIONS_PER_PASS - deletions)
        if cap_deletions else None,
    ))
    return rows


def _gc_run_scratch(managed, apply, *, cwd_guard=None, deletion_budget=None):
    cwd_guard = cwd_guard or _CwdSnapshotGuard()
    scratch_root = Path(managed) / "tmp"
    if not scratch_root.is_dir():
        return []
    leases = {}
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            leases[run.name] = None
            continue
        leases[lease.get("lease_id", run.name)] = lease
    now = time.time()
    rows = []
    deletions = 0
    for path in sorted(scratch_root.iterdir()):
        if (apply and deletion_budget is not None
                and deletions >= deletion_budget):
            break
        if path.is_symlink() or not path.is_dir():
            continue
        lease = leases.get(path.name)
        if lease is None and path.name in leases:
            continue
        if lease is not None and lease.get("state") != "released":
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if lease is None:
            deadline_age = DEFAULT_REMOTE_RUN_TIMEOUT_SECONDS
        else:
            acquired = lease.get("acquired_at")
            deadline = lease.get("deadline_epoch")
            duration = (deadline - acquired if isinstance(deadline, (int, float))
                        and isinstance(acquired, (int, float)) else 0)
            deadline_age = max(SETUP_TIMEOUT_SECONDS, duration)
        if age < deadline_age:
            continue
        if apply:
            intruders = cwd_guard.intruders([path], candidate=True)
            if intruders is None or intruders:
                rows.append({"class": "UNKNOWN" if intruders is None else "LIVE",
                             "bytes": directory_bytes(path), "path": str(path)})
                continue
            body_bytes = directory_bytes(path)
            deletions += 1
            intruders = cwd_guard.intruders([path])
            if intruders is None or intruders:
                rows.append({"class": "UNKNOWN" if intruders is None else "LIVE",
                             "bytes": body_bytes, "path": str(path)})
                continue
            rows.append({"class": "SCRATCH", "bytes": body_bytes, "path": str(path)})
            shutil.rmtree(path, ignore_errors=True)
        else:
            rows.append({"class": "SCRATCH", "bytes": directory_bytes(path), "path": str(path)})
    return rows


def iter_run_dirs(managed):
    """Current runs plus leases left under the pre-move admission/runs tree."""
    for root in (managed / "runs", managed / "admission" / "runs"):
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.is_symlink():
                yield path


def iter_upload_dirs(managed):
    root = Path(managed) / "uploads"
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.is_symlink():
                yield path


def iter_retained_dirs(managed):
    root = Path(managed) / "resident-results"
    if root.is_dir():
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
    for root in (managed / "runs", managed / "resident-results", managed / "admission" / "runs"):
        try:
            if parent == root.resolve():
                return True
        except OSError:
            continue
    return False


def _retained_run(managed, run):
    """Resolve a terminal resident body even during a legacy GC's unlink pass."""
    if run != managed / "runs" / run.name:
        return run
    retained = managed / "resident-results" / run.name
    if not _run_is_managed(managed, retained):
        return run
    try:
        snapshot = read_json(retained / "lease.json")
    except (OSError, ValueError):
        return run
    if (not isinstance(snapshot, dict) or snapshot.get("resident_submission") is not True
            or snapshot.get("state") != "released" or snapshot.get("run_directory") != str(run)):
        return run
    try:
        original = read_json(run / "lease.json")
    except (OSError, ValueError):
        return retained
    return retained if isinstance(original, dict) and original.get("state") == "released" else run


def _token_index(lease):
    index = lease.get("token_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return index


def _resident_pre_admission(lease):
    return (lease.get("resident_submission") is True
            and lease.get("state") in {"queued", "submitting", "uploading"}
            and all(not lease.get(key) for key in (
                "slot", "launch_label", "launch_submitted", "workload_pid", "command_started"))
            and lease.get("token_index") is None)


def _lease_has_admission(lease):
    """Whether this lease has ever acquired a seat or started a workload."""
    return (
        lease.get("state") in {"admitted", "running"}
        or _token_index(lease) is not None
        or bool(lease.get("slot"))
        or any(lease.get(key) for key in (
            "started_at", "command_started", "workload_pid", "workload_start",
            "launch_label", "launch_submitted", "coalition_id", "members",
        ))
    )


def reserved_token_indexes(managed, *, token_pool_size):
    """Return reserved lease indexes, or an unknown-ownership reason."""
    if type(token_pool_size) is not int or token_pool_size <= 0:
        return None, 'invalid-token-pool-size'
    found = set()
    unresolved_runs = []
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            unresolved_runs.append(run)
            continue
        if lease.get("state") in {"queued", "released"} or _resident_pre_admission(lease):
            continue
        index = _token_index(lease)
        if index is not None:
            found.add(index)
        else:
            unresolved_runs.append(run)
    if unresolved_runs:
        unresolved_ids = {run.name for run in unresolved_runs}
        marker_ids = set()
        ownership_unknown = False
        repos = managed / "repos"
        if repos.is_dir():
            for repo in repos.iterdir():
                if repo.is_symlink() or not repo.is_dir():
                    continue
                meta_root = repo / "slot-meta"
                if not meta_root.is_dir():
                    continue
                for marker in meta_root.glob("s-*.json"):
                    if marker.is_symlink():
                        ownership_unknown = True
                        continue
                    try:
                        state = read_json(marker)
                    except (OSError, ValueError):
                        ownership_unknown = True
                        continue
                    if (not isinstance(state, dict)
                            or state.get("state") != "leased"):
                        continue
                    lease_id = state.get("lease_id")
                    if lease_id not in unresolved_ids:
                        continue
                    marker_ids.add(lease_id)
                    index = _token_index(state)
                    if index is not None and index < token_pool_size:
                        found.add(index)
                    else:
                        ownership_unknown = True
        if unresolved_ids - marker_ids:
            ownership_unknown = True
        if ownership_unknown:
            return None, 'unknown-token-ownership'
        # Keep a live holder's token reserved while its workload runs. The
        # workload does not inherit the holder's token-lock descriptor.
        for index in range(token_pool_size):
            token_lock = managed / 'admission' / 'tokens' / str(index)
            if token_lock.exists() and locked(token_lock):
                found.add(index)
    return found, None


def admission_queue_state(managed):
    """Return active repo seats and queued ticket caps from readable leases."""
    repo_seats = {}
    ticket_limits = {}
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / 'lease.json')
        except (OSError, ValueError):
            continue
        if lease.get('state') == 'released':
            continue
        repo = lease.get('repo')
        if isinstance(repo, str) and _token_index(lease) is not None:
            repo_seats[repo] = repo_seats.get(repo, 0) + 1
        ticket = lease.get('ticket')
        if lease.get('state') == 'queued' and type(ticket) is int:
            max_seats = lease.get('max_concurrent_seats')
            ticket_limits[ticket] = (
                (repo, max_seats)
                if isinstance(repo, str) and type(max_seats) is int
                else None
            )
    return repo_seats, ticket_limits


def _submitted_ticket_runs(managed):
    """Map queued resident ticket numbers to their durable run directories."""
    found = {}
    for run in iter_run_dirs(managed):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            continue
        ticket = lease.get("ticket")
        if not _resident_pre_admission(lease) or type(ticket) is not int:
            continue
        found[ticket] = (run, lease)
    return found


def _new_ticket_lease(
    managed, root, request, sequence, run, max_seats, checkout_basename, *,
    state="queued", resident_submission=False, measured_hostname=None,
    destination_address=None,
):
    lease = {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": run.name,
        "lease_token": uuid.uuid4().hex,
        "box": request["box"],
        "managed_run_directory": str(managed),
        "run_directory": str(run),
        "state": state,
        "acquired_at": time.time(),
        "ticket": sequence,
        "request_id": request["request_id"],
        "arm": request["arm"],
        "repo": safe_repo(request.get("repo")),
        "max_concurrent_seats": max_seats,
        "checkout_basename": checkout_basename,
        "result_tail_bytes": request.get("result_tail_bytes", DEFAULT_RESULT_TAIL_BYTES),
    }
    if measured_hostname is not None:
        lease["measured_hostname"] = measured_hostname
    if destination_address is not None:
        lease["destination_address"] = destination_address
    holder_cid = _coalition_id(os.getpid())
    holder_boot = _boot_session_id()
    if resident_submission:
        lease["resident_submission"] = True
        lease["ticket_awake_started"] = awake_monotonic()
        lease["ticket_boot_session"] = holder_boot
    if _positive_cid(holder_cid) and holder_boot:
        lease["holder_coalition_id"] = holder_cid
        lease["holder_coalition_boot_session"] = holder_boot
    write_json(run / "lease.json", lease)
    durable_lease = _fsync_file(run / "lease.json")
    if resident_submission and not durable_lease:
        raise ValueError("submitted ticket lease could not be made durable")
    if not resident_submission and not durable_lease:
        lease.pop("holder_coalition_id", None)
        lease.pop("holder_coalition_boot_session", None)
        write_json(run / "lease.json", lease)
    write_json(run / "owner.json", request["owner"])
    if resident_submission and not _fsync_file(run / "owner.json"):
        raise ValueError("submitted ticket owner could not be made durable")
    return lease


def _slot_marker(slot):
    slot = Path(slot)
    if slot.parent.name != "seats":
        return None
    return slot.parent.parent / "slot-meta" / (slot.name + ".json")


def _slot_marker_to_free(lease):
    slot = lease.get("slot")
    if not slot:
        return None
    marker = _slot_marker(slot)
    if marker is None or not marker.is_file():
        return None
    try:
        current = read_json(marker)
    except (OSError, ValueError):
        return None
    # A successor may already hold this slot. Do not clear their lease.
    if current.get("lease_id") != lease.get("lease_id"):
        return None
    return marker


def _mark_released(managed, run, lease, reason, *, cwd_guard=None):
    """Prepare durable results, then freshly prove and publish the release."""
    lease = dict(lease)
    lease["release_reason"] = reason or lease.get("release_reason") or "completed"
    body_path = run
    if lease.get("resident_submission") is True:
        body_path = managed / "resident-results" / run.name
        body_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A v4 GC may unlink the original run as soon as it is released.
        # Terminal captures share inodes, so that cannot remove retained data.
        for name in ("stdout", "stderr", "result.json", "command.json", "request.json"):
            source, target = run / name, body_path / name
            if not source.exists():
                continue
            if target.exists():
                if source.samefile(target):
                    continue
                target.unlink()
            os.link(source, target)
        write_json(body_path / "owner.json", read_json(run / "owner.json"))
        write_json(body_path / "lease.json", {**lease, "state": "draining"})
        for path in body_path.iterdir():
            if not _fsync_file(path):
                raise ValueError("resident result body could not be made durable")
        if not _fsync_file(body_path):
            raise ValueError("resident result directory could not be made durable")
    append_result(managed, lease, run, body_path=body_path)

    needs_cwd_proof = _lease_has_admission(lease)
    cwd_guard = cwd_guard or _CwdSnapshotGuard()
    free_marker = _slot_marker_to_free(lease)
    paths = [path for path in (run, lease.get("slot"), body_path) if path]
    lease_id = lease.get("lease_id")
    scratch = None
    if isinstance(lease_id, str) and lease_id:
        scratch = Path(managed) / "tmp" / lease_id
    lease["state"] = "released"
    staged = []
    try:
        if body_path != run:
            staged.append((
                _stage_json(body_path / "lease.json", lease),
                body_path / "lease.json",
            ))
        staged.append((_stage_json(run / "lease.json", lease), run / "lease.json"))
        if free_marker is not None:
            staged.append((_stage_json(free_marker, {"state": "free"}), free_marker))
        for temporary, _target in staged:
            if not _fsync_file(temporary):
                raise ValueError("release marker could not be made durable")
        # Scratch removal is a separate mutation: prove the run, seat, retained
        # body, and scratch clear immediately before deleting it. Publication
        # below takes its own fresh census.
        if not _remove_run_scratch(
                managed, lease_id, cwd_guard=cwd_guard, related_paths=paths,
                verify_cwd=needs_cwd_proof):
            return None
        if scratch is not None and (scratch.exists() or scratch.is_symlink()):
            paths.append(scratch)
        if needs_cwd_proof:
            intruders = cwd_guard.intruders(paths)
            if intruders is None or intruders:
                return None
        for temporary, target in staged:
            _publish_staged_json(temporary, target)
        return lease
    finally:
        for temporary, _target in staged:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def _remove_run_scratch(
        managed, lease_id, *, cwd_guard=None, related_paths=(), verify_cwd=True):
    if not isinstance(lease_id, str) or not lease_id:
        return True
    scratch = Path(managed) / "tmp" / lease_id
    is_symlink = scratch.is_symlink()
    is_directory = not is_symlink and scratch.is_dir()
    if not is_symlink and not is_directory:
        return True
    if verify_cwd:
        cwd_guard = cwd_guard or _CwdSnapshotGuard()
        intruders = cwd_guard.intruders([*related_paths, scratch])
        if intruders is None or intruders:
            return False
    if is_symlink:
        scratch.unlink(missing_ok=True)
    elif is_directory:
        shutil.rmtree(scratch, ignore_errors=True)
    return True


def reclaim_dead_prestart(managed, cwd_guard=None):
    """Release admitted leases whose holders died before starting a workload.

    A running or draining lease is left to its holder or the reaper. Caller
    holds the admission lock.
    """
    released = []
    candidates = []
    for run in list(iter_run_dirs(managed)):
        try:
            lease = read_json(run / "lease.json")
        except (OSError, ValueError):
            continue
        if lease.get("state") != "admitted" or lease.get("workload_pid"):
            continue
        if locked(run / "holder.lock"):
            continue
        candidates.append((run, lease))
    cwd_guard = cwd_guard or _CwdSnapshotGuard(
        max_fresh_attempts=MAX_OWNERSHIP_CENSUS_ATTEMPTS_PER_PASS,
    )
    for run, lease in candidates:
        paths = [p for p in (run, lease.get("slot")) if p]
        intruders = cwd_guard.intruders(paths, candidate=True)
        if intruders is None or intruders:
            continue
        released_lease = _mark_released(
            managed, run, lease, lease.get("release_reason") or "dead-before-start",
            cwd_guard=cwd_guard,
        )
        if released_lease is not None:
            released.append(released_lease)
    return released


def _with_queue_lock(root, *, deadline=None):
    handle = (root / "queue.lock").open("a+")
    try:
        if deadline is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SetupDeadlineExceeded(
                        "remote setup deadline expired waiting for the admission queue lock")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.05, remaining))
        return handle
    except BaseException:
        handle.close()
        raise


def finish_dead_workload(root, run, state, action):
    """Drain, prove the tree dead, then release. Anything less stays held."""
    managed = root.parent
    released_lease = None
    cwd_guard = _CwdSnapshotGuard()
    guard = _with_queue_lock(root)
    try:
        lease = read_json(run / "lease.json")
        if lease.get("state") == "released":
            return {"status": "finished"}
        lease["state"] = "draining"
        write_json(run / "lease.json", lease)
    finally:
        guard.close()
    has_launch_identity = any(lease.get(key) for key in (
        "workload_pid", "launch_label", "members",
    )) or _private_coalition(lease, run) is not None
    launch_pending = lease.get("launch_submitted") or _gate_open(run)
    cleared = (
        clear_tree(managed, lease, run, cwd_guard=cwd_guard)
        if has_launch_identity else not launch_pending
    )
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
        released_lease = _mark_released(
            managed, run, current, reason, cwd_guard=cwd_guard,
        )
        if released_lease is None:
            current["state"] = "draining"
            write_json(run / "lease.json", current)
            audit(managed / "admission", {
                "action": action, "result": "kept",
                "lease_id": current.get("lease_id"), "at": time.time(),
            })
            return {"status": "unknown"}
        audit(managed / "admission", {"action": action, "result": "cleared",
                                      "lease_id": current.get("lease_id"),
                                      "workload_pid": current.get("workload_pid"),
                                      "at": time.time()})
    finally:
        guard.close()
    if released_lease is not None and not _release_bundle_basis_pin(managed, released_lease):
        # The lease is already released; a leftover pin holds no seat and a
        # later GC can sweep it. Record unknown while keeping cancellation clear.
        _report_bundle_basis_pin_release_unknown(
            managed, released_lease, source="finish-dead-workload",
        )
    return {"status": "cancelled" if action == "deadline" else "cleared", "killed": True}


def lease_slot(managed, repo, count, state, *, token_index, cwd_guard=None):
    """Reuse s-01..s-N. Never create s-(N+1). An unresolved lease stays reserved."""
    repo_root = managed / "repos" / repo
    seats_root = repo_root / "seats"
    meta_root = repo_root / "slot-meta"
    if repo_root.is_symlink() or seats_root.is_symlink() or meta_root.is_symlink():
        return None, None
    seats_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd_guard = cwd_guard or _CwdSnapshotGuard()
    for index in range(count):
        name = f"s-{index + 1:02d}"
        slot = seats_root / name
        if slot.is_symlink():
            continue
        slot.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = try_lock(meta_root / (name + ".lock"))
        if lock is None:
            continue
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
        intruders = cwd_guard.intruders([slot], candidate=True)
        if intruders is None or intruders:
            lock.close()
            continue
        marker_value = {
            "state": "leased",
            "lease_id": state.get("lease_id"),
            "token_index": token_index,
            "pid": (state.get("remote_run") or {}).get("pid"),
            "start_token": (state.get("remote_run") or {}).get("start_token"),
            "run_dir": (state.get("remote_run") or {}).get("run_dir"),
        }
        temporary = _stage_json(marker, marker_value)
        try:
            if not _fsync_file(temporary):
                lock.close()
                continue
            # The pass snapshot only filters candidates. Stage and fsync the
            # marker first, then revalidate immediately before publishing it.
            intruders = cwd_guard.intruders([slot])
            if intruders is None or intruders:
                lock.close()
                continue
            _publish_staged_json(temporary, marker)
            return slot, lock
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
    return None, None


class SetupFailure(RuntimeError):
    failure_type = "setup-failed"


class SetupDeadlineExceeded(SetupFailure):
    failure_type = "setup-timeout"


def _check_setup_deadline(run, deadline):
    if run is not None and (Path(run) / 'release.json').exists():
        raise RuntimeError('setup cancelled')
    if deadline is not None and time.monotonic() >= deadline:
        raise SetupDeadlineExceeded('remote setup deadline expired')


def _remove_setup_tree(path, *, run=None, deadline=None):
    """Remove a setup tree while checking the shared budget between entries."""
    path = Path(path)
    _check_setup_deadline(run, deadline)
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    pending = [(path, False)]
    while pending:
        current, remove_directory = pending.pop()
        _check_setup_deadline(run, deadline)
        if remove_directory:
            current.rmdir()
            continue
        if current.is_symlink() or not current.is_dir():
            current.unlink(missing_ok=True)
            continue
        pending.append((current, True))
        directories = []
        with os.scandir(current) as entries:
            for entry in entries:
                _check_setup_deadline(run, deadline)
                child = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    directories.append(child)
                else:
                    child.unlink(missing_ok=True)
        pending.extend((directory, False) for directory in directories)


def _run_setup(argv, *, cwd=None, env=None, run=None, deadline=None, pass_fds=()):
    """Run one bounded provisioning command, interruptible by a release."""
    if deadline is None:
        deadline = time.monotonic() + 30
    if run is not None and (run / 'release.json').exists():
        raise RuntimeError('setup cancelled')
    remaining = (deadline - time.monotonic()) if deadline is not None else 30
    if remaining <= 0:
        raise SetupDeadlineExceeded('remote setup deadline expired')
    process = subprocess.Popen(
        list(argv), cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', start_new_session=True,
        pass_fds=tuple(pass_fds),
    )
    while True:
        if run is not None and (run / 'release.json').exists():
            reason = RuntimeError('setup cancelled')
        elif time.monotonic() >= deadline:
            reason = SetupDeadlineExceeded('remote setup deadline expired')
        else:
            reason = None
        if reason is not None:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=PROCESS_REAP_TIMEOUT_SECONDS)
            raise reason
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, deadline - time.monotonic()))
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


def _minimal_environment():
    allowed = {"PATH", "HOME", "TMPDIR", "USER", "SHELL", "LANG"}
    return {
        name: value for name, value in os.environ.items()
        if name in allowed or name.startswith("LC_")
    }


class SeatSetupFailure(SetupFailure):
    failure_type = "seat-clone-failed"


class BaseOverlayFailure(SetupFailure):
    failure_type = "base-overlay-failed"


class ProfileProbeFailure(SetupFailure):
    failure_type = "profile-probe-failed"

def _clean_git_environment(env=None):
    values = dict(os.environ if env is None else env)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_NAMESPACE"):
        values.pop(name, None)
    return values


@contextlib.contextmanager
def _repo_pool_lock(managed, repo, *, run=None, deadline=None):
    repos_root = Path(managed) / "repos"
    repo_root = repos_root / safe_repo(repo)
    if repos_root.is_symlink() or repo_root.is_symlink():
        raise ValueError("managed repository path must not be a symlink")
    repo_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = repo_root / "pool.lock"
    if lock_path.is_symlink():
        raise ValueError("managed repository lock must not be a symlink")
    lock = lock_path.open("a+")
    try:
        if deadline is None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            while True:
                _check_setup_deadline(run, deadline)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SetupDeadlineExceeded(
                            'remote setup deadline expired waiting for the repo pool lock')
                    time.sleep(min(0.05, remaining))
        yield repo_root, lock.fileno()
    finally:
        lock.close()


def _pool_git(mirror, args, *, env=None, run=None, deadline=None, lock_fd=None):
    return _run_setup(
        ["git", "--git-dir", str(mirror), *args],
        env=_clean_git_environment(env), run=run, deadline=deadline,
        pass_fds=(() if lock_fd is None else (lock_fd,)),
    )


def _ensure_mirror(repo_root, *, env=None, run=None, deadline=None, lock_fd=None):
    mirror = repo_root / "mirror.git"
    if mirror.is_symlink():
        raise ValueError("managed Git mirror must not be a symlink")
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = _run_setup(
            ["git", "init", "--quiet", "--bare", str(mirror)],
            env=_clean_git_environment(env), run=run, deadline=deadline,
            pass_fds=(() if lock_fd is None else (lock_fd,)),
        )
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or "could not create bare Git mirror")
    if not mirror.is_dir():
        raise ValueError("managed Git mirror is not a directory")
    bare = _pool_git(mirror, ["rev-parse", "--is-bare-repository"],
                     env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    if bare.returncode != 0 or bare.stdout.strip() != "true":
        raise ValueError("managed Git mirror is not bare")
    return mirror


def _mirror_has_commit(mirror, sha, *, env=None, run=None, deadline=None, lock_fd=None):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
        return False
    result = _pool_git(mirror, ["cat-file", "-e", f"{sha}^{{commit}}"],
                       env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    return result.returncode == 0


def _update_pool_ref(mirror, ref, sha, *, env=None, run=None, deadline=None, lock_fd=None):
    result = _pool_git(mirror, ["update-ref", ref, sha], env=env, run=run,
                       deadline=deadline, lock_fd=lock_fd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"could not update mirror ref {ref}")


def _write_current_base(bases, sha, *, run=None, deadline=None):
    _check_setup_deadline(run, deadline)
    current = bases / "current"
    if current.is_symlink():
        raise ValueError("current base pointer must not be a symlink")
    temporary = bases / f".current-{uuid.uuid4().hex}.tmp"
    temporary.write_text(sha + "\n", encoding="ascii")
    _check_setup_deadline(run, deadline)
    if not _fsync_file(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("current base pointer could not be made durable")
    os.replace(temporary, current)
    _check_setup_deadline(run, deadline)
    if not _fsync_file(current):
        raise RuntimeError("current base pointer replacement could not be made durable")


def _build_base_generation(
    mirror, bases, sha, checkout_basename, *, env=None, run=None, deadline=None, lock_fd=None,
):
    generation = bases / sha
    if generation.is_symlink():
        raise ValueError("base generation must not be a symlink")
    if generation.exists():
        if not generation.is_dir():
            raise ValueError("base generation is not a directory")
        return generation
    temporary = bases / f".{sha}.{uuid.uuid4().hex}.tmp"
    index_path = bases / f".{sha}.{uuid.uuid4().hex}.index"
    build_env = _clean_git_environment(env)
    build_env["GIT_INDEX_FILE"] = str(index_path)
    try:
        _check_setup_deadline(run, deadline)
        temporary.mkdir(mode=0o700)
        checkout = temporary / checkout_basename if checkout_basename else temporary
        checkout.mkdir(parents=True, exist_ok=True, mode=0o700)
        build_env["GIT_WORK_TREE"] = str(checkout)
        for args in (["read-tree", sha], ["checkout-index", "--all", "--force"]):
            result = _run_setup(
                ["git", "--git-dir", str(mirror), *args], env=build_env,
                run=run, deadline=deadline,
                pass_fds=(() if lock_fd is None else (lock_fd,)),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "could not materialize base generation")
        _check_setup_deadline(run, deadline)
        os.replace(temporary, generation)
        return generation
    except BaseException:
        with contextlib.suppress(OSError):
            _remove_setup_tree(temporary)
        raise
    finally:
        index_path.unlink(missing_ok=True)


def _clonefile(source, destination):
    if sys.platform != "darwin":
        raise OSError("APFS clonefile is only available on macOS")
    try:
        function = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
    except (OSError, AttributeError) as exc:
        raise OSError("clonefile is unavailable on this filesystem") from exc
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    result = function(os.fsencode(source), os.fsencode(destination), 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _stop_setup_child(process):
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=PROCESS_REAP_TIMEOUT_SECONDS)


def _run_clonefile_process(source, destination, *, env=None, run=None,
                           deadline=None, lock_fd=None):
    """Run clonefile outside the holder so an expired setup can stop it."""
    if deadline is None:
        deadline = time.monotonic() + 30
    _check_setup_deadline(run, deadline)
    module_path = str(Path(__file__).resolve())
    module_root = str(Path(module_path).parent)
    child_env = dict(os.environ if env is None else env)
    search_path = [child_env.get("PYTHONPATH", ""), module_root]
    child_env["PYTHONPATH"] = os.pathsep.join(value for value in search_path if value)
    child_code = (
        "import importlib.util,sys\n"
        "sys.dont_write_bytecode=True\n"
        "spec=importlib.util.spec_from_file_location('_goalflight_clone_child',sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module._clonefile(sys.argv[2],sys.argv[3])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, module_path,
         str(source), str(destination)],
        env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, start_new_session=True,
        pass_fds=(() if lock_fd is None else (lock_fd,)),
    )
    try:
        while True:
            _check_setup_deadline(run, deadline)
            if process.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupDeadlineExceeded('remote setup deadline expired during APFS clone')
            time.sleep(min(0.05, remaining))
    except BaseException:
        _stop_setup_child(process)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.communicate(timeout=PROCESS_REAP_TIMEOUT_SECONDS)
        raise
    _stdout, stderr = process.communicate(timeout=PROCESS_REAP_TIMEOUT_SECONDS)
    _check_setup_deadline(run, deadline)
    if process.returncode != 0:
        raise OSError(stderr.decode(errors="replace").strip() or
                      "APFS clone child failed")


def _install_base_overlays(
    generation, overlays, *, checkout_basename="", run=None, deadline=None,
):
    generation = Path(generation)
    checkout = generation / checkout_basename if checkout_basename else generation
    try:
        root = checkout.resolve(strict=True)
        for index, overlay in enumerate(overlays):
            _check_setup_deadline(run, deadline)
            if not isinstance(overlay, dict):
                raise ValueError(f"base overlay {index} must be an object")
            kind = overlay.get("kind")
            source_value = overlay.get("source")
            destination_value = overlay.get("destination")
            if (kind not in {"copy", "symlink"}
                    or not isinstance(source_value, str)
                    or not isinstance(destination_value, str)):
                raise ValueError(f"base overlay {index} is malformed")
            relative = Path(destination_value)
            if (not destination_value or relative.is_absolute()
                    or not relative.parts
                    or "\\" in destination_value
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or relative.parts[0] == ".git"):
                raise ValueError(f"base overlay {index} has an unsafe destination")
            destination = checkout / relative
            destination_parent = destination.parent.resolve(strict=False)
            if destination_parent != root and root not in destination_parent.parents:
                raise ValueError(f"base overlay {index} destination escapes the checkout")
            source = Path(source_value).expanduser()
            if not source.is_absolute() or not source.exists():
                raise ValueError(f"base overlay {index} source is unavailable")
            if destination.is_symlink() or destination.exists():
                _remove_setup_tree(destination, run=run, deadline=deadline)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _check_setup_deadline(run, deadline)
            if kind == "symlink":
                os.symlink(str(source), destination)
            elif source.is_symlink():
                raise ValueError(f"base overlay {index} copy source is a symlink")
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            elif source.is_file():
                shutil.copy2(source, destination)
            else:
                raise ValueError(f"base overlay {index} source is not a file or directory")
            _check_setup_deadline(run, deadline)
    except SetupFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise BaseOverlayFailure(f"base overlay installation failed: {exc}") from exc


def _clone_base_generation(
    generation, seat, *, env=None, checkout_basename="", run=None,
    deadline=None, lock_fd=None,
):
    generation = Path(generation)
    seat = Path(seat)
    if generation.is_symlink() or not generation.is_dir():
        raise SeatSetupFailure("base generation is missing or unsafe")
    if seat.parent.is_symlink():
        raise SeatSetupFailure("seat root is a symlink")
    if seat.is_symlink():
        raise SeatSetupFailure("seat path is a symlink")
    try:
        _check_setup_deadline(run, deadline)
        seat.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.stat(generation, follow_symlinks=False).st_dev != os.stat(
                seat.parent, follow_symlinks=False).st_dev:
            raise SeatSetupFailure("base generation and seat are on different volumes")
        if checkout_basename:
            if (checkout_basename in {".", ".."} or "/" in checkout_basename
                    or "\\" in checkout_basename):
                raise SeatSetupFailure("checkout name is unsafe")
            source = generation / checkout_basename
            if source.is_symlink() or not source.is_dir():
                raise SeatSetupFailure("base checkout is missing or unsafe")
            if seat.exists():
                if not seat.is_dir():
                    raise SeatSetupFailure("seat path is not a directory")
            else:
                seat.mkdir(mode=0o700)
            target = seat / checkout_basename
            if target.is_symlink():
                raise SeatSetupFailure("seat checkout path is a symlink")
            if checkout_basename == "retained":
                raise SeatSetupFailure("checkout name conflicts with preserved seat data")
            for child in seat.iterdir():
                _check_setup_deadline(run, deadline)
                if child.name == "retained":
                    # Legacy output directories may contain user data; preserve them by name only.
                    continue
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    _remove_setup_tree(child, run=run, deadline=deadline)
            stale_glob = f".{seat.name}.checkout-clone-*"
            for stale in seat.parent.glob(stale_glob):
                _check_setup_deadline(run, deadline)
                if stale.is_symlink():
                    stale.unlink()
                elif stale.is_dir():
                    _remove_setup_tree(stale, run=run, deadline=deadline)
            temporary = seat.parent / f".{seat.name}.checkout-clone-{uuid.uuid4().hex}"
            try:
                _run_clonefile_process(
                    source, temporary, env=env, run=run, deadline=deadline,
                    lock_fd=lock_fd,
                )
                _check_setup_deadline(run, deadline)
                source_stat = os.stat(source, follow_symlinks=False)
                clone_stat = os.stat(temporary, follow_symlinks=False)
                if (not temporary.is_dir() or source_stat.st_dev != clone_stat.st_dev
                        or source_stat.st_ino == clone_stat.st_ino):
                    raise OSError("clonefile did not produce a same-volume checkout clone")
                if target.exists():
                    _remove_setup_tree(target)
                temporary.rename(target)
                _check_setup_deadline(run, deadline)
            except BaseException:
                with contextlib.suppress(OSError):
                    _remove_setup_tree(temporary)
                raise
            return
        seat_exists = seat.exists()
        preserved_retained_child = False
        if seat_exists:
            if not seat.is_dir():
                raise SeatSetupFailure("seat path is not a directory")
            for child in seat.iterdir():
                _check_setup_deadline(run, deadline)
                if child.name == "retained":
                    # Do not stat or remove data left by the retired output feature.
                    preserved_retained_child = True
                    continue
                if child.is_symlink() or not child.is_dir():
                    child.unlink()
                else:
                    _remove_setup_tree(child, run=run, deadline=deadline)
        for stale in seat.parent.glob(f".{seat.name}.clone-*"):
            _check_setup_deadline(run, deadline)
            if stale.is_symlink():
                stale.unlink()
            elif stale.is_dir():
                _remove_setup_tree(stale)
        temporary = seat.parent / f".{seat.name}.clone-{uuid.uuid4().hex}"
        try:
            _check_setup_deadline(run, deadline)
            _run_clonefile_process(
                generation, temporary, env=env, run=run,
                deadline=deadline, lock_fd=lock_fd,
            )
            _check_setup_deadline(run, deadline)
            source_stat = os.stat(generation, follow_symlinks=False)
            clone_stat = os.stat(temporary, follow_symlinks=False)
            if (not temporary.is_dir() or source_stat.st_dev != clone_stat.st_dev
                    or source_stat.st_ino == clone_stat.st_ino):
                raise OSError("clonefile did not produce a same-volume clone")
            _check_setup_deadline(run, deadline)
            if seat_exists:
                for child in temporary.iterdir():
                    if child.name == "retained" and preserved_retained_child:
                        raise OSError("base checkout conflicts with preserved seat data")
                    child.rename(seat / child.name)
                temporary.rmdir()
            else:
                temporary.rename(seat)
        except BaseException:
            with contextlib.suppress(OSError):
                _remove_setup_tree(temporary)
            raise
    except SetupFailure:
        raise
    except (OSError, RuntimeError) as exc:
        raise SeatSetupFailure(f"APFS clone failed: {exc}") from exc


def _initialize_seat_git(checkout, mirror, sha, *, env=None, run=None, deadline=None, lock_fd=None):
    checkout = Path(checkout)
    setup_env = _clean_git_environment(env)
    initialized = _run_setup(["git", "-C", str(checkout), "init", "--quiet"],
                             env=setup_env, run=run, deadline=deadline,
                             pass_fds=(() if lock_fd is None else (lock_fd,)))
    if initialized.returncode != 0:
        return False
    alternates = checkout / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(str(Path(mirror).resolve() / "objects") + "\n", encoding="utf-8")
    for args in (["read-tree", sha], ["update-ref", "--no-deref", "HEAD", sha]):
        result = _run_setup(["git", "-C", str(checkout), *args], env=setup_env,
                            run=run, deadline=deadline,
                            pass_fds=(() if lock_fd is None else (lock_fd,)))
        if result.returncode != 0:
            return False
    actual = _run_setup(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                        env=setup_env, run=run, deadline=deadline,
                        pass_fds=(() if lock_fd is None else (lock_fd,)))
    shallow = _run_setup(["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"],
                         env=setup_env, run=run, deadline=deadline,
                         pass_fds=(() if lock_fd is None else (lock_fd,)))
    return (actual.returncode == 0 and actual.stdout.strip().lower() == sha.lower()
            and shallow.returncode == 0 and shallow.stdout.strip() == "false")


def _repo_gc_locked(managed, repo, repo_root, mirror, lock_fd, *, env=None,
                    run=None, deadline=None):
    # The caller holds pool.lock. Clone children inherit its descriptor, so a
    # staging path is orphaned only after no setup process can still use it.
    bases = repo_root / "bases"
    active_generations = set()
    active_lease_ids = set()
    unknown = False
    for run_dir in (*iter_run_dirs(managed), *iter_upload_dirs(managed)):
        _check_setup_deadline(run, deadline)
        try:
            state = read_json(run_dir / "lease.json")
        except (OSError, ValueError):
            unknown = True
            continue
        if state.get("repo") != repo:
            continue
        if state.get("state") == "released":
            continue
        lease_id = state.get("lease_id")
        if not isinstance(lease_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            unknown = True
            continue
        active_lease_ids.add(lease_id)
        generation = state.get("base_generation")
        commit = state.get("base_commit")
        if generation is None and commit is None:
            continue
        if (not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit)
                or not isinstance(generation, str)
                or Path(generation) != bases / commit.lower()):
            unknown = True
            continue
        active_generations.add(commit.lower())
    if unknown:
        return {"status": "skipped", "reason": "unreadable-or-unknown-lease"}
    _check_setup_deadline(run, deadline)
    pins = _pool_git(mirror, ["for-each-ref", "--format=%(refname)",
                             "refs/goalflight/remote-ci/bundle-bases",
                             "refs/goalflight/remote-ci/resident-bundle-bases"],
                     env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    if pins.returncode != 0:
        return {"status": "failed", "reason": pins.stderr.strip()}
    for ref in pins.stdout.splitlines():
        _check_setup_deadline(run, deadline)
        lease_id = ref.rsplit("/", 1)[-1]
        if lease_id not in active_lease_ids:
            removed = _pool_git(mirror, ["update-ref", "-d", ref], env=env,
                                run=run, deadline=deadline, lock_fd=lock_fd)
            if removed.returncode != 0:
                return {"status": "failed", "reason": removed.stderr.strip()}
    current_path = bases / "current"
    current_commit = None
    if current_path.exists():
        try:
            current_commit = current_path.read_text(encoding="ascii").strip().lower()
        except OSError:
            return {"status": "skipped", "reason": "unreadable-current-base"}
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", current_commit):
            return {"status": "skipped", "reason": "invalid-current-base"}
    keep = active_generations | ({current_commit} if current_commit else set())
    for commit in keep:
        _check_setup_deadline(run, deadline)
        if (not (bases / commit).is_dir()
                or not _mirror_has_commit(mirror, commit, env=env, run=run,
                                          deadline=deadline, lock_fd=lock_fd)):
            return {"status": "skipped", "reason": "pinned-commit-missing"}
    base_temporary = re.compile(
        r"^\.(?:[0-9a-f]{40}|[0-9a-f]{64})\.[0-9a-f]{32}\.(?:tmp|index)$"
        r"|^\.current-[0-9a-f]{32}\.tmp$"
    )
    for generation in bases.iterdir() if bases.is_dir() else ():
        _check_setup_deadline(run, deadline)
        if generation.is_symlink():
            continue
        if base_temporary.fullmatch(generation.name):
            _remove_setup_tree(generation, run=run, deadline=deadline)
            continue
        if not generation.is_dir():
            continue
        commit = generation.name
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) or commit in keep:
            continue
        _remove_setup_tree(generation, run=run, deadline=deadline)
        _pool_git(mirror, ["update-ref", "-d",
                           f"refs/goalflight/remote-ci/base-generations/{commit}"],
                  env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    seats = repo_root / "seats"
    clone_temporary = re.compile(r"^\.s-[0-9]{2,}\.clone-[0-9a-f]{32}$")
    for staging in seats.iterdir() if seats.is_dir() else ():
        _check_setup_deadline(run, deadline)
        if (clone_temporary.fullmatch(staging.name) and not staging.is_symlink()
                and staging.is_dir()):
            _remove_setup_tree(staging, run=run, deadline=deadline)
    for commit in keep:
        _check_setup_deadline(run, deadline)
        _update_pool_ref(mirror, f"refs/goalflight/remote-ci/base-generations/{commit}",
                         commit, env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    collected = _pool_git(
        mirror, ["gc", "--auto", "--prune=now", "--quiet"], env=env,
        run=run, deadline=deadline, lock_fd=lock_fd,
    )
    if collected.returncode != 0:
        return {"status": "failed", "reason": collected.stderr.strip()}
    return {"status": "complete", "retained_generations": sorted(keep)}


def _set_confirmed_basis(mirror, sha, *, env=None, run=None, deadline=None, lock_fd=None):
    if not _mirror_has_commit(mirror, sha, env=env, run=run,
                              deadline=deadline, lock_fd=lock_fd):
        return False
    _update_pool_ref(mirror, "refs/goalflight/remote-ci/basis", sha,
                     env=env, run=run, deadline=deadline, lock_fd=lock_fd)
    return True

def _begin_finalization(run, state):
    if (state.get("phase") == "finalization"
            and isinstance(state.get("phase_deadline_epoch"), (int, float))):
        return
    started_at = time.time()
    state["phase"] = "finalization"
    state["phase_started_at"] = started_at
    state["phase_deadline_epoch"] = started_at + SETUP_TIMEOUT_SECONDS
    write_json(Path(run) / "lease.json", state)

def _publish_terminal_result(managed, run, state, pending):
    state["pending_result"] = pending
    _remember_identity(run, state)
    _begin_finalization(run, state)
    if not clear_tree(managed, state, run):
        return False
    write_json(Path(run) / "result.json", pending)
    return True

def prepare_slot(
    managed, repo, slot, sha, *, checkout_basename="", repo_url="",
    source_bundle="", candidate_sha=None, base_overlays=(), env=None, run=None,
    lease_path=None, deadline=None,
):
    """Re-clone a managed CoW seat from the confirmed base generation."""
    _check_setup_deadline(run, deadline)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
        return False
    repo = safe_repo(repo)
    slot = Path(slot)
    seats_root = Path(managed) / "repos" / repo / "seats"
    checkout_basename = str(checkout_basename or "")
    if seats_root.is_symlink():
        raise SeatSetupFailure("seat root is a symlink")
    if (slot.parent != seats_root or not re.fullmatch(r"s-[0-9]{2,}", slot.name)
            or checkout_basename in {".", ".."} or "/" in checkout_basename
            or "\\" in checkout_basename):
        return False
    if slot.is_symlink():
        raise SeatSetupFailure("seat path is a symlink")
    seats_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_setup_deadline(run, deadline)
    with _repo_pool_lock(managed, repo, run=run, deadline=deadline) as (repo_root, lock_fd):
        mirror = _ensure_mirror(repo_root, env=env, run=run,
                                deadline=deadline, lock_fd=lock_fd)
        bases = repo_root / "bases"
        bases.mkdir(parents=True, exist_ok=True, mode=0o700)
        if bases.is_symlink():
            raise ValueError("base generations path must not be a symlink")
        try:
            if source_bundle:
                bundle = Path(source_bundle)
                if run is not None and bundle.resolve(strict=False).parent != Path(run).resolve():
                    return False
                try:
                    verified = _pool_git(mirror, ["bundle", "verify", str(bundle)],
                                         env=env, run=run, deadline=deadline,
                                         lock_fd=lock_fd)
                    if verified.returncode != 0:
                        return False
                    commits = [sha]
                    if candidate_sha is not None:
                        if (not isinstance(candidate_sha, str)
                                or not re.fullmatch(
                                    r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}",
                                    candidate_sha,
                                )):
                            return False
                        commits.append(candidate_sha)
                    for commit in dict.fromkeys(commits):
                        if _mirror_has_commit(
                                mirror, commit, env=env, run=run,
                                deadline=deadline, lock_fd=lock_fd):
                            continue
                        fetched = _pool_git(
                            mirror, ["fetch", "--no-tags", str(bundle), commit],
                            env=env, run=run, deadline=deadline, lock_fd=lock_fd,
                        )
                        if fetched.returncode != 0:
                            return False
                finally:
                    bundle.unlink(missing_ok=True)
            elif not _mirror_has_commit(mirror, sha, env=env, run=run,
                                        deadline=deadline, lock_fd=lock_fd):
                if not repo_url:
                    return False
                fetched = _pool_git(mirror, ["fetch", "--no-tags", repo_url, sha],
                                    env=env, run=run, deadline=deadline,
                                    lock_fd=lock_fd)
                if fetched.returncode != 0:
                    return False
            if not _set_confirmed_basis(mirror, sha, env=env, run=run,
                                        deadline=deadline, lock_fd=lock_fd):
                return False
            generation = _build_base_generation(
                mirror, bases, sha.lower(), checkout_basename,
                env=env, run=run, deadline=deadline, lock_fd=lock_fd,
            )
            if base_overlays:
                _install_base_overlays(
                    generation, base_overlays,
                    checkout_basename=checkout_basename,
                    run=run, deadline=deadline,
                )
            _update_pool_ref(
                mirror, f"refs/goalflight/remote-ci/base-generations/{sha.lower()}",
                sha.lower(), env=env, run=run, deadline=deadline, lock_fd=lock_fd,
            )
            _write_current_base(bases, sha.lower(), run=run, deadline=deadline)
            if lease_path is not None:
                lease = read_json(Path(lease_path))
                lease["base_generation"] = str(generation)
                lease["base_commit"] = sha.lower()
                write_json(Path(lease_path), lease)
                if not _fsync_file(Path(lease_path)):
                    raise RuntimeError("base generation lease pin could not be made durable")
            _clone_base_generation(
                generation, slot, env=env, checkout_basename=checkout_basename,
                run=run, deadline=deadline, lock_fd=lock_fd,
            )
            checkout = slot / checkout_basename if checkout_basename else slot
            if not _initialize_seat_git(
                    checkout, mirror, sha.lower(), env=env, run=run,
                    deadline=deadline, lock_fd=lock_fd):
                return False
            _repo_gc_locked(managed, repo, repo_root, mirror, lock_fd,
                            env=env, run=run, deadline=deadline)
            return True
        except SetupFailure:
            raise
        except (OSError, RuntimeError, ValueError, TimeoutError):
            return False


def confirmed_bundle_basis(managed, repo):
    """Read the confirmed repo basis without reserving it for a lease."""
    repo = safe_repo(repo)
    deadline = time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS
    with _repo_pool_lock(managed, repo, deadline=deadline) as (repo_root, lock_fd):
        mirror = repo_root / "mirror.git"
        if mirror.is_symlink() or not mirror.is_dir():
            return None
        result = _pool_git(
            mirror, ["rev-parse", "--verify", "refs/goalflight/remote-ci/basis"],
            deadline=deadline, lock_fd=lock_fd,
        )
        if result.returncode != 0:
            return None
        commit = result.stdout.strip().lower()
        return commit if _mirror_has_commit(
            mirror, commit, deadline=deadline, lock_fd=lock_fd,
        ) else None


def pin_bundle_basis(managed, repo, lease_id, lease_token, basis_commit=None, *, run=None):
    repo = safe_repo(repo)
    if not isinstance(lease_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lease_id):
        raise ValueError("bundle basis requires an active lease id")
    root = Path(managed) / "admission"
    with _with_queue_lock(root):
        if run is None:
            run = Path(managed) / "runs" / lease_id
        lease_path = run / "lease.json"
        if run.is_symlink() or lease_path.is_symlink():
            raise ValueError("bundle basis lease path is unsafe")
        lease = read_json(lease_path)
        if (lease.get("lease_id") != lease_id or lease.get("lease_token") != lease_token
                or lease.get("repo") != repo or lease.get("state") == "released"):
            raise ValueError("bundle basis lease is not active or does not match")
    deadline = time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS
    commit = None
    with _repo_pool_lock(managed, repo, deadline=deadline) as (repo_root, lock_fd):
        mirror = repo_root / "mirror.git"
        if mirror.is_symlink() or not mirror.is_dir():
            return None
        if basis_commit is None:
            result = _pool_git(
                mirror, ["rev-parse", "--verify", "refs/goalflight/remote-ci/basis"],
                deadline=deadline, lock_fd=lock_fd,
            )
            if result.returncode != 0:
                return None
            commit = result.stdout.strip().lower()
        else:
            if (not isinstance(basis_commit, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", basis_commit)):
                raise ValueError("bundle basis commit is invalid")
            commit = basis_commit.lower()
        if not _mirror_has_commit(mirror, commit, deadline=deadline, lock_fd=lock_fd):
            return None
        namespace = "resident-bundle-bases" if lease.get("resident_submission") is True else "bundle-bases"
        _update_pool_ref(
            mirror, f"refs/goalflight/remote-ci/{namespace}/{lease_id}",
            commit, deadline=deadline, lock_fd=lock_fd,
        )
    # Pin creation and lease validation use different locks. Revalidate only
    # after the pool lock is closed so no git work can extend the admission lock.
    with _with_queue_lock(root):
        current = read_json(lease_path)
        active = (
            current.get("lease_id") == lease_id
            and current.get("lease_token") == lease_token
            and current.get("repo") == repo
            and current.get("state") != "released"
        )
    if active:
        return commit
    _release_bundle_basis_pin(managed, lease)
    return None


def _release_bundle_basis_pin(managed, lease):
    repo = lease.get("repo")
    lease_id = lease.get("lease_id")
    if (not isinstance(repo, str) or not isinstance(lease_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", lease_id)):
        return False
    try:
        repo = safe_repo(repo)
    except ValueError:
        return False
    repo_root = Path(managed) / "repos" / repo
    mirror = repo_root / "mirror.git"
    if repo_root.is_symlink() or mirror.is_symlink():
        return False
    if not mirror.is_dir():
        return True
    try:
        deadline = time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS
        with _repo_pool_lock(managed, repo, deadline=deadline) as (_repo_root, lock_fd):
            namespace = "resident-bundle-bases" if lease.get("resident_submission") is True else "bundle-bases"
            result = _pool_git(
                mirror, ["update-ref", "-d",
                         f"refs/goalflight/remote-ci/{namespace}/{lease_id}"],
                deadline=deadline, lock_fd=lock_fd,
            )
            return result.returncode == 0
    except (OSError, RuntimeError, ValueError):
        return False


def cleanup_legacy_slots(managed, repo, *, apply=False):
    repo = safe_repo(repo)
    admission_root = Path(managed) / "admission"
    admission_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    repo_root = Path(managed) / "repos" / repo
    legacy = repo_root / "slots"
    tombstone = repo_root / ".slots-cleanup"
    deadline = time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS
    cleanup_path = None
    result = None
    cwd_guard = _CwdSnapshotGuard()
    try:
        # Never wait for provisioning's pool lock while holding box-wide
        # admission. The queue lock then protects the ownership check and
        # atomic rename that fences future legacy-slot admissions.
        with _repo_pool_lock(managed, repo, deadline=deadline):
            try:
                guard = _with_queue_lock(admission_root, deadline=deadline)
            except SetupDeadlineExceeded:
                return {"status": "unknown", "path": str(legacy),
                        "reason": "admission-lock-timeout"}
            try:
                if tombstone.is_symlink() or (tombstone.exists() and not tombstone.is_dir()):
                    return {"status": "unknown", "path": str(legacy),
                            "reason": "unknown-cleanup-tombstone"}
                if tombstone.exists():
                    # A prior cleanup stopped after detaching the old tree.
                    # Only this command can name the tombstone; admissions use
                    # the original slots path and cannot enter it.
                    cleanup_path = tombstone
                    result = {"status": "eligible", "path": str(legacy),
                              "cleanup_path": str(tombstone)}
                else:
                    if legacy.is_symlink():
                        return {"status": "unknown", "path": str(legacy)}
                    if not legacy.exists():
                        return {"status": "absent", "path": str(legacy)}
                    if not legacy.is_dir():
                        return {"status": "unknown", "path": str(legacy)}

                    legacy_abs = Path(os.path.abspath(legacy))
                    for run_dir in iter_run_dirs(managed):
                        try:
                            lease = read_json(run_dir / "lease.json")
                        except (OSError, ValueError):
                            return {"status": "unknown", "path": str(legacy),
                                    "reason": "unreadable-lease"}
                        if lease.get("state") == "released":
                            continue
                        lease_repo = lease.get("repo")
                        slot = lease.get("slot")
                        if lease_repo is not None and not isinstance(lease_repo, str):
                            return {"status": "unknown", "path": str(legacy),
                                    "lease_id": lease.get("lease_id"),
                                    "reason": "invalid-lease-repo"}
                        slot_path = None
                        if isinstance(slot, str) and slot:
                            candidate = Path(slot)
                            if not candidate.is_absolute():
                                candidate = Path(managed) / candidate
                            slot_path = Path(os.path.abspath(candidate))
                            if slot_path == legacy_abs or legacy_abs in slot_path.parents:
                                return {"status": "active", "path": str(legacy),
                                        "slot": slot, "lease_id": lease.get("lease_id"),
                                        "reason": "active-lease"}
                        if lease_repo is None:
                            # Pre-repo leases used the default pool. A recorded slot
                            # path can also identify a non-default legacy pool.
                            if repo == "default" and slot_path is None:
                                return {"status": "active", "path": str(legacy),
                                        "lease_id": lease.get("lease_id"),
                                        "reason": "legacy-default-lease-without-slot"}
                            continue
                        if lease_repo != repo:
                            continue
                        if slot_path is None and lease.get("state") != "queued":
                            return {"status": "unknown", "path": str(legacy),
                                    "lease_id": lease.get("lease_id"),
                                    "reason": "active-lease-without-slot"}

                    try:
                        slots = sorted(legacy.iterdir())
                    except OSError:
                        return {"status": "unknown", "path": str(legacy),
                                "reason": "unreadable-legacy-slots"}
                    for slot in slots:
                        if slot.is_symlink() or not slot.is_dir():
                            return {"status": "unknown", "path": str(legacy),
                                    "slot": str(slot), "reason": "unknown-legacy-slot"}
                        lock_path = slot / "slot.lock"
                        if lock_path.is_symlink():
                            return {"status": "unknown", "path": str(legacy),
                                    "slot": str(slot), "reason": "unknown-legacy-slot-lock"}
                        try:
                            if lock_path.exists() and locked(lock_path):
                                return {"status": "active", "path": str(legacy),
                                        "slot": str(slot), "reason": "legacy-slot-lock-held"}
                        except OSError:
                            return {"status": "unknown", "path": str(legacy),
                                    "slot": str(slot), "reason": "unreadable-legacy-slot-lock"}

                        marker = slot / "SLOT.json"
                        if marker.is_symlink():
                            return {"status": "unknown", "path": str(legacy),
                                    "slot": str(slot), "reason": "unknown-legacy-slot-marker"}
                        if not marker.exists():
                            continue
                        try:
                            current = read_json(marker)
                            if not isinstance(current, dict):
                                raise ValueError("legacy slot marker is not an object")
                            if current.get("state") == "free":
                                continue
                            run = current.get("run_dir")
                            if not isinstance(run, str) or not run:
                                raise ValueError("legacy slot marker has no run directory")
                            previous = read_json(Path(run) / "lease.json")
                            if not isinstance(previous, dict):
                                raise ValueError("legacy slot lease is not an object")
                        except (OSError, ValueError, TypeError):
                            return {"status": "unknown", "path": str(legacy),
                                    "slot": str(slot),
                                    "reason": "unreadable-legacy-slot-marker"}
                        if previous.get("state") != "released":
                            return {"status": "active", "path": str(legacy),
                                    "slot": str(slot), "lease_id": previous.get("lease_id"),
                                    "reason": "active-legacy-slot-marker"}

                    intruders = cwd_guard.intruders([legacy], candidate=True)
                    if intruders is None or intruders:
                        return {"status": "unknown" if intruders is None else "active",
                                "path": str(legacy)}
                    result = {"status": "eligible", "path": str(legacy)}
                    if apply:
                        intruders = cwd_guard.intruders([legacy])
                        if intruders is None or intruders:
                            return {"status": "unknown" if intruders is None else "active",
                                    "path": str(legacy)}
                        legacy.rename(tombstone)
                        cleanup_path = tombstone
            finally:
                guard.close()
            if cleanup_path is None:
                cleanup_path = legacy
            result["bytes"] = directory_bytes(cleanup_path)
            if apply and cleanup_path == tombstone:
                intruders = cwd_guard.intruders([tombstone])
                if intruders is None or intruders:
                    return {"status": "unknown" if intruders is None else "active",
                            "path": str(legacy), "cleanup_path": str(tombstone),
                            "bytes": result["bytes"]}
                try:
                    # Keep the repo lock to serialize recovery of this detached
                    # tree, but never hold box-wide admission during deletion.
                    shutil.rmtree(tombstone)
                except OSError:
                    return {"status": "unknown", "path": str(legacy),
                            "cleanup_path": str(tombstone),
                            "reason": "cleanup-failed", "bytes": result["bytes"]}
                result["status"] = "removed"
            return result
    except SetupDeadlineExceeded:
        return {"status": "unknown", "path": str(legacy),
                "reason": "pool-lock-timeout"}


def resource_floor_refuses(root):
    floor = Path(root).parent / 'admission' / 'resource-floor.json'
    if not floor.is_file():
        return False
    try:
        return read_json(floor).get('refuse') == 'capacity'
    except (OSError, ValueError, AttributeError):
        return True


def overlay_candidate_tests(slot, candidate_sha, test_files, *, env=None, run=None, deadline=None):
    """Overlay only the candidate's changed tests onto the checked-out base."""
    normalized = []
    for relative in test_files:
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('candidate test path escapes the checkout')
        normalized.append(path.as_posix())
    if not normalized:
        return True
    listing = _run_setup(
        ['git', '-C', str(slot), 'ls-tree', '-r', '--name-only', '-z', candidate_sha],
        env=env, run=run, deadline=deadline,
    )
    if listing.returncode != 0:
        return False
    candidate_paths = set(listing.stdout.split('\0'))
    root = Path(slot).resolve()
    for relative in normalized:
        path = Path(slot) / relative
        parent = path.parent.resolve(strict=False)
        if parent != root and root not in parent.parents:
            raise ValueError('candidate test path escapes the checkout')
        _check_setup_deadline(run, deadline)
        if relative in candidate_paths:
            if path.is_symlink():
                path.unlink()
            restored = _run_setup(
                ['git', '-C', str(slot), 'restore', '--source', candidate_sha,
                 '--worktree', '--', relative],
                env=env, run=run, deadline=deadline,
            )
            if restored.returncode != 0:
                return False
            continue
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            _remove_setup_tree(path, run=run, deadline=deadline)
        else:
            path.unlink(missing_ok=True)
    return True


def _requirement_inputs(source_root, relative):
    found = {}

    def visit(path):
        resolved = path.resolve()
        if resolved in found:
            return
        if source_root not in resolved.parents or not resolved.is_file():
            raise ValueError('included test requirements file is missing or escapes the checkout')
        content = resolved.read_bytes()
        found[resolved] = content
        for raw in content.decode('utf-8').splitlines():
            line = raw.split('#', 1)[0].strip()
            match = re.match(
                r'^(?:-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+)?(\S+)', line,
            )
            if match:
                visit(resolved.parent / match.group(1))

    visit(source_root / relative)
    return sorted(found.items(), key=lambda item: str(item[0]))


def _check_pinned_requirements(inputs):
    for _path, content in inputs:
        for raw in content.decode('utf-8').splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or re.match(
                    r'^(?:-r|--requirement|-c|--constraint)(?:\s*=\s*|\s+)?\S+',
                    line):
                continue
            if not PINNED_REQUIREMENT.fullmatch(line):
                raise ValueError(
                    f'profile requirements must use exact == pins: {line!r}')


def prepare_venv(managed, repo, slot, requirements, interpreter, *, env=None,
                 run=None, deadline=None):
    """Create or reuse a project venv keyed by every requirement input."""
    if not interpreter or not Path(interpreter).is_absolute() or not os.access(interpreter, os.X_OK):
        raise ValueError('configured Python interpreter is unavailable')
    source_root = slot.resolve()
    if isinstance(requirements, str):
        relative = Path(requirements)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('test requirements must be repository-relative')
        requirements_path = (source_root / relative).resolve()
        if source_root not in requirements_path.parents or not requirements_path.is_file():
            raise ValueError('test requirements file is missing or escapes the checkout')
        inputs = _requirement_inputs(source_root, relative)
        if env is not None and env.get('GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED') == '1':
            _check_pinned_requirements(inputs)
        install_requirements = ['-r', str(relative)]
        digest_inputs = [(str(path.relative_to(source_root)), content) for path, content in inputs]
    elif isinstance(requirements, (list, tuple)):
        if (not requirements
                or any(not isinstance(item, str) or not item or item.startswith('-')
                       or '\x00' in item or '\n' in item or '\r' in item
                       for item in requirements)):
            raise ValueError('inline requirements must be package requirement strings')
        install_requirements = list(requirements)
        if env is not None and env.get('GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED') == '1':
            _check_pinned_requirements([
                (Path('<inline>'), '\n'.join(install_requirements).encode())])
        digest_inputs = [('inline', json.dumps(install_requirements, separators=(',', ':')).encode())]
    else:
        raise ValueError('test requirements must be a repository path or package list')
    setup_kwargs = {'run': run, 'deadline': deadline}
    if env is not None:
        setup_kwargs['env'] = env
    version = _run_setup([interpreter, '--version'], **setup_kwargs)
    if version.returncode != 0:
        raise ValueError('configured Python interpreter could not report its version')
    digest_builder = hashlib.sha256()
    digest_builder.update(str(Path(interpreter).resolve()).encode())
    digest_builder.update(version.stdout.encode())
    for path, content in digest_inputs:
        digest_builder.update(path.encode())
        digest_builder.update(content)
    digest = digest_builder.hexdigest()
    venv_root = managed / 'venvs' / safe_repo(repo)
    venv_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = venv_root / digest
    lock_path = venv_root / (digest + '.lock')
    with lock_path.open('a+') as lock:
        while True:
            _check_setup_deadline(run, deadline)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = (deadline - time.monotonic()) if deadline is not None else 0.05
                if remaining <= 0:
                    raise SetupDeadlineExceeded(
                        'remote setup deadline expired waiting for the test venv lock')
                time.sleep(min(0.05, remaining))
        python = target / 'bin' / 'python'
        marker = target / '.goalflight-complete'
        try:
            complete = marker.read_text(encoding='ascii').strip() == digest and python.is_file()
        except OSError:
            complete = False
        if not complete:
            _check_setup_deadline(run, deadline)
            _remove_setup_tree(target, run=run, deadline=deadline)
            try:
                created = _run_setup([interpreter, '-m', 'venv', str(target)], **setup_kwargs)
                if created.returncode != 0:
                    raise ValueError('could not create project virtualenv: ' + created.stderr[-2000:])
                installed = _run_setup(
                    [str(python), '-m', 'pip', 'install',
                     '--disable-pip-version-check', *install_requirements],
                    cwd=str(source_root), **setup_kwargs)
                if installed.returncode != 0:
                    raise ValueError('could not install project test requirements: ' + installed.stderr[-2000:])
                marker.write_text(digest + '\n', encoding='ascii')
                if not _fsync_file(marker):
                    raise ValueError('could not verify completed project virtualenv')
            except BaseException:
                with contextlib.suppress(OSError):
                    _remove_setup_tree(target)
                raise
        return target, digest


def _is_python_interpreter(value):
    return re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(value).name) is not None


def _release_holder(root, run, state, token):
    """Drain under the admission lock, then release only a proved-dead tree."""
    managed = root.parent
    cwd_guard = _CwdSnapshotGuard()
    if token is None and state.get("token_index") is None:
        released_lease = None
        guard = _with_queue_lock(root)
        try:
            current = read_json(run / "lease.json")
            if state.get("release_reason"):
                current["release_reason"] = state["release_reason"]
            if state.get("pending_result") and not (run / "result.json").exists():
                write_json(run / "result.json", state["pending_result"])
            if current.get("state") != "released":
                released_lease = _mark_released(
                    managed, run, current,
                    current.get("release_reason") or "completed",
                    cwd_guard=cwd_guard,
                )
                if released_lease is None:
                    if (_lease_has_admission(current)
                            and not current.get("workload_pid")):
                        current["state"] = "admitted"
                    write_json(run / "lease.json", current)
                    return
            else:
                released_lease = current
        finally:
            guard.close()
        if released_lease is not None and released_lease.get("state") == "released":
            _remove_run_scratch(
                managed, state.get("lease_id"), cwd_guard=cwd_guard,
                related_paths=[p for p in (run, current.get("slot")) if p],
                verify_cwd=_lease_has_admission(released_lease),
            )
        if not _release_bundle_basis_pin(managed, released_lease):
            # This detached holder has no response channel and the result is
            # already durable. Audit the uncertainty; the leftover ref holds
            # no seat and a later setup GC can sweep it.
            _report_bundle_basis_pin_release_unknown(
                managed, released_lease, source="tokenless-holder-release",
            )
        return
    released_lease = None
    guard = _with_queue_lock(root)
    try:
        lease = read_json(run / "lease.json")
        if state.get("release_reason"):
            lease["release_reason"] = state["release_reason"]
        for key in ("workload_pid", "workload_start", "launch_label", "coalition_id",
                    "coalition_boot_session", "holder_coalition_id",
                    "holder_coalition_boot_session", "exit_code", "exit_known",
                    "pending_result"):
            if key in state:
                lease[key] = state[key]
        lease["state"] = "draining"
        write_json(run / "lease.json", lease)
    finally:
        guard.close()
    has_launch_identity = any(lease.get(key) for key in (
        "workload_pid", "launch_label", "members",
    )) or _private_coalition(lease, run) is not None
    launch_pending = lease.get("launch_submitted") or _gate_open(run)
    if has_launch_identity:
        ok = clear_tree(managed, lease, run, cwd_guard=cwd_guard)
    else:
        # _mark_released performs the fresh census after result preparation.
        ok = not launch_pending
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
        else:
            if not ok:
                current["state"] = "draining"
                if label:
                    current["launch_label"] = label
                    current["job_remove_pending"] = True
                if state.get("release_reason"):
                    current["release_reason"] = state["release_reason"]
                write_json(run / "lease.json", current)
                return
            if label:
                current["job_remove_pending"] = False
            _publish_pending(run, current)
            reason = state.get("release_reason") or current.get("release_reason") or "completed"
            released_lease = _mark_released(
                managed, run, current, reason, cwd_guard=cwd_guard,
            )
            if released_lease is None:
                # clear_tree and job removal succeeded, so a refused final
                # census leaves a dead prestart lease for the next admission
                # pass to retry, not an unselected draining lease.
                current["state"] = "admitted"
                current.pop("workload_pid", None)
                current.pop("workload_start", None)
                write_json(run / "lease.json", current)
                return
    finally:
        guard.close()
    if token is not None:
        token.close()
    if released_lease is not None:
        if not _release_bundle_basis_pin(managed, released_lease):
            # This detached holder has no response channel and its result is
            # already durable; audit the uncertainty without failing completion.
            _report_bundle_basis_pin_release_unknown(
                managed, released_lease, source="holder-release",
            )


def admission_sleep(run, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not (run / 'release.json').exists():
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def admission_command_window_seconds(controller_poll_seconds):
    if (isinstance(controller_poll_seconds, bool)
            or not isinstance(controller_poll_seconds, (int, float))
            or not math.isfinite(controller_poll_seconds)
            or controller_poll_seconds <= 0):
        raise ValueError('controller_poll_seconds must be a positive finite number')
    return (
        BUNDLE_BASIS_TIMEOUT_SECONDS
        + BUNDLE_PREPARATION_TIMEOUT_SECONDS
        + BUNDLE_TRANSFER_TIMEOUT_SECONDS
        + max(30.0, 2.0 * controller_poll_seconds + 10.0)
    )


def _run_scratch_directory(admission_root, lease_id):
    """Create per-lease TMPDIR outside every Git worktree and slot."""
    scratch = (Path(admission_root).parent / 'tmp' / str(lease_id)).resolve()
    for parent in (scratch, *scratch.parents):
        if (parent / '.git').exists():
            raise ValueError('remote TMPDIR resolves inside a Git worktree')
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = scratch.resolve()
    for parent in (scratch, *scratch.parents):
        if (parent / '.git').exists():
            raise ValueError('remote TMPDIR resolves inside a Git worktree')
    return scratch


def run_holder(root, run, ticket, ticket_lock, holder_lock, request):
    """Own admission and the process group; persist identity before executing."""
    os.setsid()
    # The incarnation nonce is node-issued and fenced by holder.lock. It is
    # never a controller PID or a PID-only liveness claim.
    state = read_json(run / 'lease.json')
    remote_run = {
        'host': socket.gethostname(), 'pid': str(os.getpid()),
        'start_token': uuid.uuid4().hex, 'run_dir': str(run),
        'lease_id': state['lease_id'], 'lease_token': state['lease_token'],
    }
    if state.get('resident_submission') is not True:
        remote_run['address'] = state['destination_address']
    state['remote_run'] = remote_run
    write_json(run / 'lease.json', state)
    token = None
    slot = None
    slot_lock = None
    scratch = None
    tree_started = False
    queue_waited = False
    reservation_reason = None
    resident_submission = request.get("resident_submission") is True
    admission_deadline = (
        None if resident_submission else
        time.monotonic() + float(request.get('queue_wait_seconds', 30))
    )
    current_boot_session = _boot_session_id() if resident_submission else None
    last_decision = None

    def note_decision(reason):
        nonlocal last_decision
        if reason != last_decision:
            audit(root, {
                "action": "admission-decision", "ticket": state.get("ticket"),
                "lease_id": state.get("lease_id"), "result": reason, "at": time.time(),
            })
            last_decision = reason

    try:
        while token is None:
            cwd_guard = _CwdSnapshotGuard(
                max_fresh_attempts=MAX_OWNERSHIP_CENSUS_ATTEMPTS_PER_PASS,
            )
            reservation_reason = None
            if (run / 'release.json').exists():
                return
            reclaimed = []
            decision = "waiting"
            with (root / 'queue.lock').open('a+') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                # Release and admission serialize on this lock. Recheck after
                # acquiring it so a queued lease cannot be admitted after a
                # controller has confirmed its withdrawal.
                if (run / 'release.json').exists():
                    return
                if resident_submission:
                    state = read_json(run / "lease.json")
                    expired, reset = submitted_ticket_expired(
                        state, now=awake_monotonic(),
                        boot_session=current_boot_session,
                        ttl_seconds=float(request.get(
                            "queue_wait_seconds", SUBMITTED_TICKET_TTL_SECONDS,
                        )),
                    )
                    if reset:
                        write_json(run / "lease.json", state)
                    if expired:
                        state["release_reason"] = "ticket-expired"
                        state["pending_result"] = {
                            "returncode": EXIT_DEADLINE, "timed_out": True,
                            "status": "expired", "command_started": False,
                        }
                        write_json(run / "result.json", state["pending_result"])
                        write_json(run / "lease.json", state)
                        note_decision("expired")
                        return
                # A holder killed before start never rewrites its lease. Prove
                # that no workload exists, then release it under this lock.
                reclaimed = reclaim_dead_prestart(
                    root.parent, cwd_guard,
                )
                # Tickets are allocated under this same lock. Dead holders
                # cannot strand FIFO: their kernel-held ticket locks are free.
                waiting = []
                submitted_tickets = _submitted_ticket_runs(root.parent)
                for path in sorted((root / 'tickets').iterdir()):
                    if (not path.name.isdecimal()):
                        continue
                    if (path == ticket or locked(path)
                            or int(path.name) in submitted_tickets):
                        waiting.append(path)
                    else:
                        path.unlink(missing_ok=True)
                queue_state = admission_queue_state(root.parent)
                eligible_ticket = None
                if queue_state is not None and waiting:
                    repo_seats_by_project, ticket_limits = queue_state
                    eligible_ticket = waiting[0]
                    for candidate_ticket in waiting:
                        ticket_number = int(candidate_ticket.name)
                        policy = ticket_limits.get(ticket_number)
                        if policy is None:
                            eligible_ticket = candidate_ticket
                            break
                        queued_repo, queued_cap = policy
                        if repo_seats_by_project.get(queued_repo, 0) < queued_cap:
                            eligible_ticket = candidate_ticket
                            break
                        eligible_ticket = None
                if eligible_ticket == ticket:
                    try:
                        measured = sample(root, request.get('python_interpreter', ''))
                        saturated = (
                            measured['load1'] >= SATURATION_LOAD_FACTOR * measured['p_cores']
                            and measured['cpu_busy_fraction'] >= SATURATION_BUSY_FRACTION
                        )
                        floor_refused = resource_floor_refuses(root)
                        if saturated:
                            decision = "saturated"
                        elif floor_refused:
                            decision = "resource-floor"
                        else:
                            reserved, reservation_reason = reserved_token_indexes(
                                root.parent, token_pool_size=measured['token_pool_size'],
                            )
                            repo = safe_repo(request.get('repo'))
                            repo_seats = (
                                queue_state[0].get(repo, 0)
                                if queue_state is not None else None
                            )
                            max_seats = request.get(
                                'max_concurrent_seats', measured['token_pool_size'],
                            )
                            # Unknown ownership holds every index.
                            if reserved is None:
                                decision = reservation_reason or "token-ownership-unknown"
                            elif repo_seats is None:
                                decision = "repo-seat-unknown"
                            elif repo_seats >= max_seats:
                                decision = "project-cap"
                            else:
                                for index in range(measured['token_pool_size']):
                                    if index in reserved:
                                        continue
                                    token = try_lock(root / 'tokens' / str(index))
                                    if token is not None:
                                        slot, slot_lock = lease_slot(
                                            root.parent, repo, max_seats, state,
                                            token_index=index,
                                            cwd_guard=cwd_guard)
                                        if slot is None:
                                            token.close()
                                            token = None
                                            break
                                        checkout = slot / request['checkout_basename'] \
                                            if request.get('checkout_basename') else slot
                                        state.update(state='admitted', token_index=index,
                                                     sample=measured, slot=str(slot), repo=repo,
                                                     checkout_dir=str(checkout),
                                                     queue_waited=queue_waited)
                                        write_json(run / 'lease.json', state)
                                        ticket.unlink()
                                        decision = "admitted"
                                        audit(root, {
                                            "action": "admission-decision",
                                            "ticket": state.get("ticket"),
                                            "lease_id": state.get("lease_id"),
                                            "result": decision, "at": time.time(),
                                        })
                                        break
                                if token is None:
                                    decision = "tokens"
                    except (OSError, ValueError):
                        decision = "sample-or-policy-unknown"
                elif waiting:
                    decision = "project-cap" if eligible_ticket is None else "earlier-ticket"
            for released_lease in reclaimed:
                if not _release_bundle_basis_pin(root.parent, released_lease):
                    # Reclamation is already published and this holder has no
                    # response channel. Audit the uncertainty without delaying
                    # admission; the leftover ref holds no seat and later GC sweeps it.
                    _report_bundle_basis_pin_release_unknown(
                        root.parent, released_lease, source="run-holder-reclaimed",
                    )
            if token is None:
                note_decision(decision)
            if token is None:
                if (admission_deadline is not None
                        and time.monotonic() >= admission_deadline):
                    state['queue_waited'] = queue_waited
                    state['release_reason'] = 'capacity-refused'
                    result = {
                        'returncode': EXIT_CAPACITY, 'timed_out': False,
                        'status': 'capacity-refused', 'queue_waited': queue_waited,
                    }
                    if reservation_reason is not None:
                        result['reason'] = reservation_reason
                        audit(root, {
                            'action': 'token-reservation',
                            'result': 'capacity-refused',
                            'reason': reservation_reason,
                            'lease_id': state.get('lease_id'),
                            'at': time.time(),
                        })
                    state['pending_result'] = result
                    write_json(run / 'result.json', result)
                    return
                queue_waited = True
                admission_sleep(run, request['poll_seconds'])
        ticket_lock.close()
        scratch = _run_scratch_directory(root, state['lease_id'])
        # Admission precedes controller rendering/pushing and command creation.
        # The controller does that bookkeeping and then writes command.json.
        # If the reply was dropped, nobody will. Waiting forever pins the token
        # on a live owner that reap will not touch.
        command_deadline = time.monotonic() + admission_command_window_seconds(
            request['controller_poll_seconds'])
        while not (run / 'command.json').exists():
            if (run / 'release.json').exists():
                return
            if time.monotonic() >= command_deadline:
                state['release_reason'] = 'command-wait'
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False,
                    'error': 'controller did not submit the command before the setup deadline',
                }
                write_json(run / 'result.json', state['pending_result'])
                return
            time.sleep(min(request['poll_seconds'], 0.1))
        command = read_json(run / 'command.json')
        sha = (command.get('env') or {}).get('GOALFLIGHT_REMOTE_CI_SHA', '')
        state['sha'] = sha
        setup_deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
        state['phase'] = 'setup'
        state['phase_started_at'] = time.time()
        state['state'] = 'provisioning'
        state['setup_deadline_epoch'] = time.time() + max(0, setup_deadline - time.monotonic())
        state['phase_deadline_epoch'] = state['setup_deadline_epoch']
        write_json(run / 'lease.json', state)
        env = command.setdefault('env', {})
        if resident_submission:
            box_cap = state['sample'].get('self_cap')
            if type(box_cap) is not int or box_cap <= 0:
                box_cap = 1
            try:
                requested_cap = int(env.get('GOALFLIGHT_REMOTE_CI_SELF_CAP', box_cap))
            except (TypeError, ValueError):
                requested_cap = 1
            if requested_cap <= 0:
                requested_cap = 1
            requested_cap = min(requested_cap, box_cap)
            env['GOALFLIGHT_REMOTE_CI_SELF_CAP'] = str(requested_cap)
            cap_env_name = env.pop('GOALFLIGHT_REMOTE_CI_SELF_CAP_ENV_NAME', None)
            if isinstance(cap_env_name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', cap_env_name):
                env[cap_env_name] = str(requested_cap)
            write_json(run / 'command.json', command)
        profile_enabled = env.get('GOALFLIGHT_REMOTE_CI_PROFILE_ENABLED') == '1'
        base_env = _minimal_environment() if profile_enabled else os.environ.copy()
        env_set = command.get('env_set', {})
        env_unset = command.get('env_unset', [])
        if (not isinstance(env_set, dict)
                or any(not isinstance(name, str) or not isinstance(value, str)
                       for name, value in env_set.items())
                or not isinstance(env_unset, list)
                or any(not isinstance(name, str) for name in env_unset)):
            raise ValueError('command environment override is invalid')
        for name in env_unset:
            env.pop(name, None)
            base_env.pop(name, None)
        env.update(env_set)
        if profile_enabled:
            base_env['TMPDIR'] = str(scratch)
        if slot is not None:
            repo_url = (command.get('env') or {}).get('GOALFLIGHT_REMOTE_CI_REPO_URL') or ''
            source_bundle = (command.get('env') or {}).get('GOALFLIGHT_REMOTE_CI_SOURCE_BUNDLE') or ''
            checkout = Path(state.get('checkout_dir') or state['slot'])
            try:
                try:
                    base_overlays = json.loads(
                        env.get('GOALFLIGHT_REMOTE_CI_BASE_OVERLAYS_JSON', '[]')
                    )
                except (TypeError, ValueError) as exc:
                    raise BaseOverlayFailure('base overlay configuration is invalid') from exc
                if not isinstance(base_overlays, list):
                    raise BaseOverlayFailure('base overlay configuration must be an array')
                prepared = prepare_slot(
                    root.parent, safe_repo(request.get('repo')), slot, sha,
                    checkout_basename=request.get('checkout_basename', ''),
                    repo_url=repo_url, source_bundle=source_bundle,
                    candidate_sha=(env.get('GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA')
                                   if env.get('GOALFLIGHT_REMOTE_CI_OVERLAY') == '1'
                                   else None),
                    base_overlays=base_overlays,
                    env=base_env if profile_enabled else None,
                    run=run, lease_path=run / 'lease.json', deadline=setup_deadline,
                )
            except SetupFailure as exc:
                state['release_reason'] = exc.failure_type
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False, 'setup_failure_type': exc.failure_type,
                    'error': str(exc),
                }
                write_json(run / 'result.json', state['pending_result'])
                return
            if not prepared:
                state['release_reason'] = 'source-unavailable'
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False,
                    'error': 'requested SHA could not be fetched and verified'}
                write_json(run / 'result.json', state['pending_result'])
                return
            state.update(read_json(run / 'lease.json'))
            if env.get('GOALFLIGHT_REMOTE_CI_OVERLAY') == '1':
                try:
                    test_files = json.loads(env.get('GOALFLIGHT_REMOTE_CI_TEST_FILES_JSON', '[]'))
                    overlay_ok = overlay_candidate_tests(
                        checkout,
                        env.get('GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA', ''),
                        test_files, env=base_env if profile_enabled else None,
                        run=run, deadline=setup_deadline,
                    )
                except SetupFailure as exc:
                    state['release_reason'] = exc.failure_type
                    state['pending_result'] = {
                        'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                        'command_started': False, 'setup_failure_type': exc.failure_type,
                        'error': str(exc),
                    }
                    write_json(run / 'result.json', state['pending_result'])
                    return
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    overlay_ok = False
                    overlay_error = str(exc)
                else:
                    overlay_error = 'candidate test overlay could not be applied'
                if not overlay_ok:
                    state['release_reason'] = 'overlay-unavailable'
                    state['pending_result'] = {
                        'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                        'command_started': False, 'error': overlay_error,
                    }
                    write_json(run / 'result.json', state['pending_result'])
                    return
        if resident_submission and env.get('GOALFLIGHT_REMOTE_CI_RESIDENT_CWD') == '1':
            checkout = Path(state.get('checkout_dir') or state.get('slot') or '').resolve()
            relative_cwd = env.get('GOALFLIGHT_REMOTE_CI_CWD_RELATIVE', '')
            if (not isinstance(relative_cwd, str) or Path(relative_cwd).is_absolute()
                    or '..' in Path(relative_cwd).parts):
                raise ValueError('submitted working directory is not checkout-relative')
            command['cwd'] = str((checkout / relative_cwd).resolve())
            owner = read_json(run / 'owner.json')
            values = {
                'box': state.get('box', ''),
                'host': env.get('GOALFLIGHT_REMOTE_CI_HOST', ''),
                'arm': env.get('GOALFLIGHT_REMOTE_CI_ARM', ''),
                'sha': env.get('GOALFLIGHT_REMOTE_CI_SHA', ''),
                'tip_sha': env.get('GOALFLIGHT_REMOTE_CI_TIP_SHA', ''),
                'candidate_sha': env.get('GOALFLIGHT_REMOTE_CI_CANDIDATE_SHA', ''),
                'request_id': state.get('request_id', ''),
                'run_id': state.get('lease_id', ''),
                'lease_id': state.get('lease_id', ''),
                'lease_token': state.get('lease_token', ''),
                'owner_identity': owner.get('owner_identity', ''),
                'run_dir': str(run),
                'managed_run_directory': str(root.parent),
                'token_index': state.get('token_index', ''),
                'overlay': env.get('GOALFLIGHT_REMOTE_CI_OVERLAY', '0'),
                'slot_dir': state.get('slot', ''),
                'checkout_dir': state.get('checkout_dir') or state.get('slot') or '',
            }
            if env.get('GOALFLIGHT_REMOTE_CI_EXPAND_RUNNER_TEMPLATE') == '1':
                command['argv'] = _expand_resident_argv(command.get('argv'), values)
            for name, value in values.items():
                env['GOALFLIGHT_REMOTE_CI_' + name.upper()] = str(value)
            lease_record = record(run)
            env['GOALFLIGHT_REMOTE_CI_LEASE_RECORD_JSON'] = json.dumps(lease_record)
            env['GOALFLIGHT_REMOTE_CI_RUN_DIR'] = str(run)
            env['GOALFLIGHT_REMOTE_CI_SLOT_DIR'] = str(state.get('slot') or '')
            env['GOALFLIGHT_REMOTE_CI_RESULT_INDEX'] = str(
                root.parent / 'results' / 'index.jsonl')
            env['GOALFLIGHT_REMOTE_CI_POLICY_OUTCOME'] = (
                'queued' if state.get('queue_waited') is True else 'ran_remote')
            if profile_enabled and request.get('checkout_basename'):
                profile_paths = [str(Path(values['checkout_dir']).parent)]
                box_pythonpath = env.get('GOALFLIGHT_REMOTE_CI_BOX_PYTHONPATH', '')
                if box_pythonpath:
                    profile_paths.append(box_pythonpath)
                env['PYTHONPATH'] = os.pathsep.join(profile_paths)
            env.pop('GOALFLIGHT_REMOTE_CI_RESIDENT_CWD', None)
            env.pop('GOALFLIGHT_REMOTE_CI_EXPAND_RUNNER_TEMPLATE', None)
            env.pop('GOALFLIGHT_REMOTE_CI_CWD_RELATIVE', None)
            env.pop('GOALFLIGHT_REMOTE_CI_BOX_PYTHONPATH', None)
            write_json(run / 'command.json', command)
        if command.get('emit_receipt') is True:
            try:
                checkout = Path(state.get('checkout_dir') or state['slot'])
                setup_env = base_env.copy()
                setup_env.update(env)
                if profile_enabled:
                    setup_env['TMPDIR'] = str(scratch)
                environment = {}
                receipt_probes = json.loads(
                    env.get('GOALFLIGHT_REMOTE_CI_RECEIPT_PROBES_JSON', '{}')
                )
                profile_receipt = {}
                profile_interpreter = None
                environment = json.loads(
                    env.get('GOALFLIGHT_REMOTE_CI_ENVIRONMENT_JSON',
                            '{"requirements_file":"requirements-test.txt"}')
                )
                if set(environment) in ({'requirements_file'}, {'requirements'}):
                    interpreter = env.get('GOALFLIGHT_REMOTE_CI_PYTHON_INTERPRETER', '')
                    requirements = environment.get('requirements_file', environment.get('requirements'))
                    venv_path, requirements_hash = prepare_venv(
                        root.parent, safe_repo(request.get('repo')), checkout,
                        requirements, interpreter, env=setup_env,
                        run=run, deadline=setup_deadline,
                    )
                    version = _run_setup(
                        [interpreter, '--version'], cwd=checkout, env=setup_env,
                        run=run, deadline=setup_deadline,
                    )
                    if version.returncode != 0:
                        raise ValueError('configured Python interpreter could not report its version')
                    profile_receipt['environment_version'] = version.stdout.strip()
                    profile_interpreter = str(venv_path / 'bin' / 'python')
                    env['GOALFLIGHT_REMOTE_CI_VENV_PATH'] = str(venv_path)
                    env['GOALFLIGHT_REMOTE_CI_REQUIREMENTS_SHA256'] = requirements_hash
                    env['VIRTUAL_ENV'] = str(venv_path)
                    env['PATH'] = str(venv_path / 'bin') + os.pathsep + setup_env.get('PATH', '')
                    state['venv_path'] = str(venv_path)
                    state['requirements_sha256'] = requirements_hash
                elif set(environment) == {'interpreter', 'version_probe'}:
                    interpreter = environment['interpreter']
                    if (not isinstance(interpreter, str) or not Path(interpreter).is_absolute()
                            or not os.access(interpreter, os.X_OK)):
                        raise ValueError('declared project interpreter is unavailable')
                    version = _run_setup(
                        environment['version_probe'], cwd=checkout, env=setup_env,
                        run=run, deadline=setup_deadline,
                    )
                    if version.returncode != 0:
                        raise ValueError('declared project interpreter version probe failed: '
                                         + version.stderr[-2000:])
                    profile_receipt['environment_version'] = version.stdout.strip()
                    profile_receipt['environment_interpreter'] = interpreter
                    profile_interpreter = interpreter
                    interpreter_bin = str(Path(interpreter).parent)
                    env['PATH'] = interpreter_bin + os.pathsep + setup_env.get('PATH', '')
                    env.pop('VIRTUAL_ENV', None)
                    env.pop('GOALFLIGHT_REMOTE_CI_VENV_PATH', None)
                    setup_env.pop('VIRTUAL_ENV', None)
                    setup_env.pop('GOALFLIGHT_REMOTE_CI_VENV_PATH', None)
                    env['GOALFLIGHT_REMOTE_CI_REQUIREMENTS_SHA256'] = ''
                    state['requirements_sha256'] = ''
                else:
                    raise ValueError('invalid project environment recipe')

                outputs = {}
                for name, probe in receipt_probes.items():
                    setup_env.update(env)
                    try:
                        result = _run_setup(
                            probe['command'], cwd=checkout, env=setup_env,
                            run=run, deadline=setup_deadline,
                        )
                    except SetupFailure:
                        raise
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        raise ProfileProbeFailure(
                            f"receipt probe {name!r} could not run: {exc}"
                        ) from exc
                    output = result.stdout.strip()
                    if result.returncode != 0:
                        raise ProfileProbeFailure(
                            f"receipt probe {name!r} failed: {result.stderr[-2000:]}"
                        )
                    if 'expected' in probe and output != probe['expected']:
                        raise ProfileProbeFailure(
                            f"receipt probe {name!r} expected {probe['expected']!r}, got {output!r}"
                        )
                    outputs[name] = output
                if outputs:
                    profile_receipt['receipt_probes'] = outputs
                if profile_receipt:
                    env['GOALFLIGHT_REMOTE_CI_PROFILE_RECEIPT_JSON'] = json.dumps(profile_receipt)
                if profile_enabled:
                    argv = command.get('argv')
                    if (not isinstance(argv, list) or len(argv) < 3
                            or not isinstance(argv[0], str)
                            or not _is_python_interpreter(argv[0])
                            or argv[1:3] != ['-m', 'pytest']
                            or not profile_interpreter):
                        raise ValueError(
                            'profile command must use a leading Python interpreter and python -m pytest'
                        )
                    argv[0] = profile_interpreter
                write_json(run / 'command.json', command)
            except ProfileProbeFailure as exc:
                state['release_reason'] = exc.failure_type
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False, 'setup_failure_type': exc.failure_type,
                    'error': str(exc),
                }
                write_json(run / 'result.json', state['pending_result'])
                return
            except SetupFailure as exc:
                state['release_reason'] = exc.failure_type
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False, 'setup_failure_type': exc.failure_type,
                    'error': 'test virtualenv setup failed: ' + str(exc),
                }
                write_json(run / 'result.json', state['pending_result'])
                return
            except (OSError, ValueError, RuntimeError, KeyError, TypeError,
                    subprocess.SubprocessError) as exc:
                state['release_reason'] = 'venv-unavailable'
                state['pending_result'] = {
                    'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                    'command_started': False,
                    'error': 'test virtualenv setup failed: ' + str(exc)}
                write_json(run / 'result.json', state['pending_result'])
                return
        state['state'] = 'running'
        state['started_at'] = time.time()
        state['deadline_epoch'] = state['started_at'] + float(command['timeout'])
        write_json(run / 'lease.json', state)
        if resource_floor_refuses(root):
            # The host guard refused the start. This is not a dead workload.
            state['release_reason'] = 'capacity-refused'
            write_json(run / 'result.json', {
                'returncode': EXIT_CAPACITY, 'timed_out': False,
                'status': 'capacity-refused'})
            return
        env = base_env.copy()
        env.update(command['env'])
        if (command.get('emit_receipt') is True
                and set(environment) == {'interpreter', 'version_probe'}):
            env.pop('VIRTUAL_ENV', None)
            env.pop('GOALFLIGHT_REMOTE_CI_VENV_PATH', None)
        env['GOALFLIGHT_REMOTE_CI_REMOTE_PID'] = state['remote_run']['pid']
        env['GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN'] = state['remote_run']['start_token']
        env['GOALFLIGHT_REMOTE_CI_SLOT_DIR'] = state.get('slot') or ''
        env['GOALFLIGHT_REMOTE_CI_RUN_DIR'] = str(run)
        env['GOALFLIGHT_REMOTE_CI_RESULT_INDEX'] = str(root.parent / 'results' / 'index.jsonl')
        env['TMPDIR'] = str(scratch)
        checkout_root = Path(state.get('checkout_dir') or state.get('slot') or '').resolve()
        requested_cwd = Path(command.get('cwd') or checkout_root).resolve()
        if ((checkout_root not in requested_cwd.parents and requested_cwd != checkout_root)
                or not requested_cwd.is_dir()):
            state['release_reason'] = 'cwd-unavailable'
            state['pending_result'] = {
                'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                'command_started': False,
                'error': 'forwarded working directory is outside the prepared checkout or missing',
            }
            write_json(run / 'result.json', state['pending_result'])
            return
        # launchd starts a new job, so it cannot inherit the token fd. The
        # holder keeps that fd. Admission follows the lease, not the flock.
        launched = _submit_workload(run, state, command['argv'], env, str(requested_cwd))
        if isinstance(launched, str):
            state['release_reason'] = 'launch-failed'
            state['pending_result'] = {
                'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
                'command_started': False, 'error': launched}
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
            # The lease already names the job. The wrapper's marker identifies
            # when its command starts, so launch delay is not in the deadline.
            if _await_command_started(run, leader, leader_start):
                state['started_at'] = (run / 'command-started').stat().st_mtime
                state['deadline_epoch'] = state['started_at'] + float(command['timeout'])
            state['command_started'] = (run / 'command-started').is_file()
            if state['command_started']:
                state['phase'] = 'execution'
                state['phase_started_at'] = state['started_at']
                state['phase_deadline_epoch'] = state['deadline_epoch']
            write_json(run / 'lease.json', state)
            deadline = time.monotonic() + max(0, state['deadline_epoch'] - time.time())
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
                        'command_started': (run / 'command-started').is_file(),
                    } if cancelled else {
                        'returncode': EXIT_DEADLINE, 'timed_out': True,
                        'status': 'deadline',
                        'command_started': (run / 'command-started').is_file(),
                    })
                    if not _publish_terminal_result(
                            root.parent, run, state, pending):
                        time.sleep(0.2)
                        continue
                    state['release_reason'] = 'cancelled' if cancelled else 'node-timeout'
                    break
                if same is None:
                    # Unreadable is not an exit. Treating it as one starts
                    # cleanup while the leader may still be alive.
                    time.sleep(0.05)
                    continue
                if same is False:
                    _capture_exit_status(run, state, wait=True)
                    state['command_started'] = (run / 'command-started').is_file()
                    pending = _terminal_result(state)
                    if not _publish_terminal_result(
                            root.parent, run, state, pending):
                        state['release_reason'] = 'unknown-tree'
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
            'returncode': 2, 'timed_out': False, 'status': 'setup-failed',
            'command_started': (run / 'command-started').is_file(), 'error': str(exc)})
    finally:
        ticket.unlink(missing_ok=True)
        try:
            _release_holder(root, run, state, token)
        finally:
            # Removal is proved before release. Do not take a published
            # release back: that window lets a second run share the token.
            # A holder that is leaving with the job still present stays
            # draining so the next reap retries the removal.
            if slot_lock is not None:
                slot_lock.close()
            ticket_lock.close()
            holder_lock.close()


def _fork_run_holder(root, run, ticket, request, ticket_lock, holder_lock,
                     queue_guard=None, close_handles=()):
    """Transfer the flocks to a detached run holder and return its child PID."""
    try:
        pid = os.fork()
    except OSError:
        ticket_lock.close()
        holder_lock.close()
        return False
    if pid == 0:
        if queue_guard is not None:
            queue_guard.close()
        for handle in close_handles:
            if isinstance(handle, int):
                os.close(handle)
            elif handle is not None:
                handle.close()
        with open(os.devnull, "r+") as null:
            for fd in (0, 1, 2):
                os.dup2(null.fileno(), fd)
        try:
            run_holder(root, run, ticket, ticket_lock, holder_lock, request)
        finally:
            os._exit(0)
    ticket_lock.close()
    holder_lock.close()
    return pid


def _finish_queued_submission(root, run, state, status, reason, *, cwd_guard=None):
    result = {
        "returncode": EXIT_CANCELLED if status == "cancelled" else EXIT_DEADLINE,
        "timed_out": status != "cancelled",
        "status": status,
        "command_started": False,
    }
    state["release_reason"] = reason
    state["pending_result"] = result
    write_json(run / "result.json", result)
    return _mark_released(
        root.parent, run, state, reason, cwd_guard=cwd_guard,
    )


def _gc_uploads(managed, *, apply=True, boot_session=None):
    """Expire interrupted private uploads; an open upload flock always wins."""
    events = []
    for path in iter_upload_dirs(managed):
        try:
            handle = try_lock(path / "upload.lock")
        except FileNotFoundError:
            continue  # The submitter atomically published it after our scan.
        if handle is None:
            continue
        try:
            try:
                lease = read_json(path / "lease.json")
                if not isinstance(lease, dict):
                    raise ValueError("invalid private upload metadata")
            except (OSError, ValueError):
                try:
                    lease = read_json(path / "upload-start.json")
                    if (not isinstance(lease, dict)
                            or isinstance(lease.get("ticket_awake_started"), bool)
                            or not isinstance(lease.get("ticket_awake_started"), (int, float))):
                        lease = None
                except (OSError, ValueError):
                    lease = None
                if lease is None:
                    # A crash can happen between mkdir and the first durable
                    # write. Start the TTL on recovery; wall mtime includes
                    # sleep and cannot prove awake time.
                    expired = False
                    if apply:
                        lease = {
                            "lease_id": path.name,
                            "repo": None,
                            "resident_submission": True,
                            "ticket_awake_started": awake_monotonic(),
                            "ticket_boot_session": boot_session,
                        }
                        write_json(path / "upload-start.json", lease)
                        if not _fsync_file(path / "upload-start.json"):
                            lease = None
                else:
                    expired, reset = submitted_ticket_expired(
                        lease, boot_session=boot_session,
                        ttl_seconds=UPLOAD_TTL_SECONDS,
                    )
                    if reset and apply:
                        write_json(path / "upload-start.json", lease)
            else:
                expired, reset = submitted_ticket_expired(
                    lease, boot_session=boot_session, ttl_seconds=UPLOAD_TTL_SECONDS,
                )
                if reset and apply:
                    write_json(path / "lease.json", lease)
            if not expired:
                continue
            if apply:
                if (lease is not None and lease.get("repo") is not None
                        and not _release_bundle_basis_pin(managed, lease)):
                    events.append((None, "upload-cleanup-deferred"))
                    continue
                shutil.rmtree(path)
            events.append((None, "upload-expired"))
        finally:
            handle.close()
    return events


def _admit_once(managed, *, boot_session=None, singleton_lock=None, holder_pids=None):
    root = managed / "admission"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tickets = root / "tickets"
    tickets.mkdir(parents=True, exist_ok=True, mode=0o700)
    events = _gc_uploads(managed, boot_session=boot_session)
    released = []
    cwd_guard = _CwdSnapshotGuard(
        max_fresh_attempts=MAX_OWNERSHIP_CENSUS_ATTEMPTS_PER_PASS,
    )
    guard = _with_queue_lock(
        root, deadline=time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS,
    )
    try:
        submitted = _submitted_ticket_runs(managed)
        for number, (_run, _state) in submitted.items():
            ticket_path = tickets / f"{number:020d}"
            if not ticket_path.exists():
                ticket_path.touch()
                if not _fsync_file(ticket_path):
                    ticket_path.unlink(missing_ok=True)
                    events.append((number, "ticket-restore-failed"))
        for ticket in sorted(tickets.iterdir()):
            if not ticket.name.isdecimal():
                continue
            number = int(ticket.name)
            entry = submitted.get(number)
            if entry is None:
                stale_lock = try_lock(ticket)
                if stale_lock is None:
                    events.append((number, "legacy-ticket-held"))
                else:
                    stale_lock.close()
                    ticket.unlink(missing_ok=True)
                    events.append((number, "legacy-ticket-stale"))
                continue

            run, state = entry
            ticket_lock = try_lock(ticket)
            if ticket_lock is None:
                continue
            holder_lock = try_lock(run / "holder.lock")
            if holder_lock is None:
                ticket_lock.close()
                events.append((number, "holder-active"))
                continue
            current = read_json(run / "lease.json")
            if not _resident_pre_admission(current):
                ticket.unlink(missing_ok=True)
                ticket_lock.close()
                holder_lock.close()
                continue
            if (run / "release.json").exists() or (run / "cancel.json").exists():
                released_lease = _finish_queued_submission(
                    root, run, current, "cancelled", "cancelled",
                    cwd_guard=cwd_guard,
                )
                if released_lease is None:
                    ticket_lock.close()
                    holder_lock.close()
                    events.append((number, "cancelled-release-deferred"))
                    continue
                released.append(released_lease)
                ticket.unlink(missing_ok=True)
                ticket_lock.close()
                holder_lock.close()
                events.append((number, "cancelled"))
                continue
            expired, reset = submitted_ticket_expired(
                current, now=awake_monotonic(), boot_session=boot_session,
            )
            if reset:
                write_json(run / "lease.json", current)
            if expired:
                released_lease = _finish_queued_submission(
                    root, run, current, "expired", "ticket-expired",
                    cwd_guard=cwd_guard,
                )
                if released_lease is None:
                    ticket_lock.close()
                    holder_lock.close()
                    events.append((number, "expired-release-deferred"))
                    continue
                released.append(released_lease)
                ticket.unlink(missing_ok=True)
                ticket_lock.close()
                holder_lock.close()
                events.append((number, "expired"))
                continue
            try:
                request = read_json(run / "request.json")
                if (current.get("state") != "queued" or not isinstance(request, dict)
                        or request.get("resident_submission") is not True
                        or not (run / "command.json").is_file()):
                    raise ValueError("submitted request is incomplete")
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                state = current
                state["pending_result"] = {
                    "returncode": 2, "timed_out": False,
                    "status": "setup-failed", "command_started": False,
                    "error": str(exc),
                }
                write_json(run / "result.json", state["pending_result"])
                released_lease = _mark_released(
                    managed, run, state, "invalid-submission",
                    cwd_guard=cwd_guard,
                )
                if released_lease is None:
                    ticket_lock.close()
                    holder_lock.close()
                    events.append((number, "invalid-release-deferred"))
                    continue
                released.append(released_lease)
                ticket.unlink(missing_ok=True)
                ticket_lock.close()
                holder_lock.close()
                events.append((number, "invalid-submission"))
                continue
            pid = _fork_run_holder(
                root, run, ticket, request, ticket_lock, holder_lock,
                queue_guard=guard, close_handles=(singleton_lock,))
            if pid:
                if holder_pids is not None:
                    holder_pids.add(pid)
                events.append((number, "holder-started"))
            else:
                events.append((number, "holder-start-failed"))
    finally:
        guard.close()
    for lease in released:
        if not _release_bundle_basis_pin(managed, lease):
            # The admission pass reports through events; include the lease id
            # there while preserving the already-published cancellation/expiry.
            events.append((
                lease.get("ticket"),
                f"bundle-basis-pin-release-unknown lease_id={lease.get('lease_id')}",
            ))
    return events


def admit_once(managed):
    """Run one resident admission pass, fenced by the per-box flock."""
    managed = Path(managed)
    root = managed / "admission"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = try_lock(root / "admit-loop.lock")
    if lock is None:
        return [(None, "already-running")]
    try:
        return _admit_once(managed, boot_session=_boot_session_id(), singleton_lock=lock)
    finally:
        lock.close()


def admit_loop(managed, *, interval=ADMIT_LOOP_INTERVAL_SECONDS):
    """Keep queued submitted tickets moving without a controller connection."""
    managed = pin_managed_root(Path(managed))
    root = managed / "admission"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = try_lock(root / "admit-loop.lock")
    if lock is None:
        print("admit-loop already running", flush=True)
        return
    holder_pids = set()
    audit_cursor, cursor_warning = _load_audit_forward_cursor(root)
    cursor_warning_reported = cursor_warning is not None
    cursor_needs_persist = cursor_warning in (
        "cursor-write", "cursor-unreadable-write-failed",
    )
    if cursor_warning in ("cursor-unreadable", "cursor-unreadable-write-failed"):
        print(
            "admit-loop audit-cursor-unreadable=1 forwarding_from=end",
            flush=True,
        )
    elif cursor_warning is not None:
        print(f"admit-loop audit-cursor-error kind={cursor_warning}", flush=True)
    try:
        while True:
            started = awake_monotonic()
            for pid in tuple(holder_pids):
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                except OSError as exc:
                    if exc.errno == errno.ECHILD:
                        reaped = pid
                    else:
                        print(
                            f"admit-loop waitpid-error pid={pid} errno={exc.errno}",
                            flush=True,
                        )
                        continue
                if reaped:
                    holder_pids.remove(pid)
            try:
                events = _admit_once(
                    managed, boot_session=_boot_session_id(), singleton_lock=lock,
                    holder_pids=holder_pids,
                )
            except Exception as exc:
                print(f"admit-loop decision=error detail={exc}", flush=True)
                events = []
            for ticket, result in events:
                print(f"admit-loop ticket={ticket if ticket is not None else '-'} decision={result}",
                      flush=True)
            try:
                audit_cursor, cursor_error = _print_bundle_basis_pin_release_events(
                    root, audit_cursor, force_save=cursor_needs_persist,
                )
                cursor_needs_persist = cursor_error is not None
                if cursor_error is not None and not cursor_warning_reported:
                    print(
                        f"admit-loop audit-cursor-write-error errno={cursor_error.errno}",
                        flush=True,
                    )
                    cursor_warning_reported = True
            except OSError as exc:
                print(f"admit-loop audit-read-error errno={exc.errno}", flush=True)
            remaining = interval - (awake_monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        lock.close()


def _find_submission(managed, request_id, arm):
    seen = set()
    for path in (*iter_run_dirs(managed), *iter_retained_dirs(managed)):
        if path.name in seen:
            continue
        seen.add(path.name)
        try:
            lease = read_json(path / "lease.json")
            lease.update(read_json(path / "owner.json"))
        except (OSError, ValueError):
            continue
        if (lease.get("resident_submission") is True
                and lease.get("request_id") == request_id
                and lease.get("arm") == arm):
            return _retained_run(managed, path), lease
    return None


def _command_identity(command):
    value = dict(command)
    env = dict(value.get("env") or {})
    env.pop("GOALFLIGHT_REMOTE_CI_SOURCE_BUNDLE", None)
    value["env"] = env
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _expand_resident_argv(argv, values):
    if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
        raise ValueError("submitted runner argv template must be a string array")
    expanded = []
    for token in argv:
        value = token
        for name, replacement in values.items():
            value = value.replace("{" + name + "}", str(replacement))
        if "{" in value or "}" in value:
            raise ValueError("submitted runner argv contains an unknown placeholder")
        expanded.append(value)
    return expanded


def _consume_submit_bundle(stream, size, expected_digest):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(64 * 1024, remaining))
        if not isinstance(chunk, bytes) or not chunk:
            raise ValueError("candidate bundle stream ended before its declared size")
        if len(chunk) > remaining:
            raise ValueError("candidate bundle stream exceeded its declared size")
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise ValueError("candidate bundle stream exceeded its declared size")
    if digest.hexdigest() != expected_digest:
        raise ValueError("candidate bundle SHA-256 digest mismatch")
    return digest.hexdigest()


def _submission_matches(run, lease, request, command_hash, bundle_digest, basis_commit):
    try:
        stored = read_json(run / "request.json")
        owner = read_json(run / "owner.json")
    except (OSError, ValueError):
        return False
    if not isinstance(stored, dict) or not isinstance(owner, dict):
        return False
    incoming_owner = request.get("owner")
    if not isinstance(incoming_owner, dict):
        return False
    if (stored.get("candidate_bundle_sha256") != bundle_digest
            or stored.get("command_sha256") not in (None, command_hash)
            or stored.get("basis_commit") != (basis_commit.lower() if basis_commit else None)
            or stored.get("repo") != safe_repo(request.get("repo"))
            or stored.get("max_concurrent_seats") != request.get("max_concurrent_seats")
            or stored.get("checkout_basename") != request.get("checkout_basename", "")
            or lease.get("owner_identity") != incoming_owner.get("owner_identity")
            or lease.get("owner_pid") != incoming_owner.get("owner_pid")
            or lease.get("owner_start_token") != incoming_owner.get("owner_start_token")
            or lease.get("owner_host") != incoming_owner.get("owner_host")):
        return False
    if stored.get("command_sha256") is None:
        try:
            current = read_json(run / "command.json")
        except (OSError, ValueError):
            return False
        return _command_identity(current) == command_hash
    return True


def _submit_ticket(managed, root, request, max_seats, checkout_basename):
    request_id = request.get("request_id")
    arm = request.get("arm")
    owner = request.get("owner")
    repo = safe_repo(request.get("repo"))
    if not isinstance(request_id, str) or not request_id or not isinstance(arm, str) or not arm:
        raise ValueError("submit requires request_id and arm")
    if not isinstance(owner, dict) or not owner:
        raise ValueError("submit requires an owner identity")
    command = request.get("command")
    if not isinstance(command, dict):
        raise ValueError("submit requires a command object")
    argv = command.get("argv")
    env = command.get("env")
    timeout = command.get("timeout")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)):
        raise ValueError("submit command argv must be a non-empty string array")
    if (not isinstance(env, dict)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   or "\x00" in key or "\x00" in value for key, value in env.items())):
        raise ValueError("submit command env must contain string values")
    submitted_self_cap = env.get("GOALFLIGHT_REMOTE_CI_SELF_CAP")
    if submitted_self_cap is not None and (
            not re.fullmatch(r"[0-9]+", submitted_self_cap)
            or int(submitted_self_cap) <= 0):
        raise ValueError("GOALFLIGHT_REMOTE_CI_SELF_CAP must be a positive integer")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("submit command timeout must be positive and finite")
    sha = env.get("GOALFLIGHT_REMOTE_CI_SHA")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", sha):
        raise ValueError("submit command must name GOALFLIGHT_REMOTE_CI_SHA")
    bundle_stream = request.get("_candidate_bundle_stream")
    bundle_size = request.get("candidate_bundle_size")
    if (request.get("candidate_bundle_stream") is not True
            or not hasattr(bundle_stream, "read")
            or isinstance(bundle_size, bool) or not isinstance(bundle_size, int)):
        raise ValueError("submit requires a streamed candidate bundle")
    if bundle_size > MAX_SUBMIT_BUNDLE_BYTES:
        raise ValueError("candidate bundle exceeds the 512 MiB limit")
    if bundle_size < 1:
        raise ValueError("candidate bundle is empty")
    expected_digest = request.get("candidate_bundle_sha256")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("submit requires a candidate bundle SHA-256 digest")
    basis_commit = request.get("basis_commit")
    if basis_commit is not None and (
            not isinstance(basis_commit, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", basis_commit)):
        raise ValueError("basis_commit must be a full Git commit id")
    controller_poll_seconds = request.get("controller_poll_seconds", 1.0)
    admission_command_window_seconds(controller_poll_seconds)
    poll_seconds = request.get("poll_seconds", 1.0)
    if (isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds) or poll_seconds <= 0):
        raise ValueError("poll_seconds must be positive and finite")
    queue_wait_seconds = request.get("queue_wait_seconds", SUBMITTED_TICKET_TTL_SECONDS)
    if (isinstance(queue_wait_seconds, bool)
            or not isinstance(queue_wait_seconds, (int, float))
            or not math.isfinite(queue_wait_seconds) or queue_wait_seconds <= 0):
        raise ValueError("queue_wait_seconds must be positive and finite")

    command_hash = _command_identity(command)
    existing = _find_submission(managed, request_id, arm)
    if existing is not None:
        existing_run, existing_lease = existing
        duplicate_digest = _consume_submit_bundle(
            bundle_stream, bundle_size, expected_digest,
        )
        if not _submission_matches(
                existing_run, existing_lease, request, command_hash,
                duplicate_digest, basis_commit):
            raise ValueError("request_id/arm already names a different submitted ticket")
        return record(existing_run)

    stored_request = {
        "resident_submission": True,
        "request_id": request_id,
        "arm": arm,
        "owner": owner,
        "repo": repo,
        "max_concurrent_seats": max_seats,
        "checkout_basename": checkout_basename,
        "python_interpreter": request.get("python_interpreter", ""),
        "poll_seconds": poll_seconds,
        "controller_poll_seconds": controller_poll_seconds,
        "queue_wait_seconds": queue_wait_seconds,
        "result_tail_bytes": request.get("result_tail_bytes", DEFAULT_RESULT_TAIL_BYTES),
    }
    run = None
    basis_pinned = False
    ticket_path = None
    published = False
    upload_lock = None
    try:
        uploads = managed / "uploads"
        uploads.mkdir(mode=0o700, exist_ok=True)
        runs = managed / "runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        lease_id = uuid.uuid4().hex
        run = uploads / lease_id
        run.mkdir(mode=0o700)
        upload_lock = try_lock(run / "upload.lock")
        if upload_lock is None:
            raise ValueError("could not lock private upload")
        upload_start = {
            "lease_id": lease_id,
            "repo": repo,
            "resident_submission": True,
            "ticket_awake_started": awake_monotonic(),
            "ticket_boot_session": _boot_session_id(),
        }
        write_json(run / "upload-start.json", upload_start)
        if not _fsync_file(run / "upload-start.json"):
            raise ValueError("private upload start time could not be made durable")
        for name in ("stdout", "stderr"):
            (run / name).touch()
        _new_ticket_lease(
            managed, root, request, None, run, max_seats,
            checkout_basename, state="uploading", resident_submission=True,
        )

        if basis_commit is not None:
            basis_pinned = True
            pinned = pin_bundle_basis(
                managed, repo, run.name, read_json(run / "lease.json")["lease_token"],
                basis_commit=basis_commit, run=run,
            )
            if pinned is None:
                raise ValueError(
                    "basis commit is no longer available; retry submit with a full candidate bundle"
                )

        bundle_path = run / "source.bundle"
        bundle_digest = hashlib.sha256()
        remaining = bundle_size
        with bundle_path.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            while remaining:
                chunk = bundle_stream.read(min(64 * 1024, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise ValueError("candidate bundle stream ended before its declared size")
                if len(chunk) > remaining:
                    raise ValueError("candidate bundle stream exceeded its declared size")
                output.write(chunk)
                bundle_digest.update(chunk)
                remaining -= len(chunk)
            if bundle_stream.read(1):
                raise ValueError("candidate bundle stream exceeded its declared size")
            output.flush()
        if bundle_digest.hexdigest() != expected_digest:
            raise ValueError("candidate bundle SHA-256 digest mismatch")
        if not _fsync_file(bundle_path):
            raise ValueError("candidate bundle could not be made durable")
        stored_request.update({
            "protocol_version": PROTOCOL_VERSION,
            "basis_commit": basis_commit.lower() if basis_commit else None,
            "candidate_bundle": bundle_path.name,
            "candidate_bundle_sha256": bundle_digest.hexdigest(),
            "command_sha256": command_hash,
        })
        command = dict(command)
        command["env"] = dict(env)

        # GC scans uploads and published leases under pool.lock. Keep the
        # namespace move inside that lock so a scan cannot miss both owners.
        publication_guard = (
            _repo_pool_lock(managed, repo,
                            deadline=time.monotonic() + POOL_LOCK_OPERATION_TIMEOUT_SECONDS)
            if basis_pinned else contextlib.nullcontext()
        )
        duplicate_run = None
        with publication_guard as publication, _with_queue_lock(root) as guard:
            duplicate = _find_submission(managed, request_id, arm)
            if duplicate is not None:
                duplicate_run, duplicate_lease = duplicate
                if not _submission_matches(
                        duplicate_run, duplicate_lease, request, command_hash,
                        bundle_digest.hexdigest(), basis_commit):
                    raise ValueError(
                        "request_id/arm already names a different submitted ticket"
                    )
            else:
                counter = root / "sequence.json"
                sequence = (read_json(counter) if counter.exists() else 0) + 1
                write_json(counter, sequence)
                if not _fsync_file(counter):
                    raise ValueError("ticket sequence could not be made durable")
                destination = runs / lease_id
                stored_request["ticket"] = sequence
                write_json(run / "request.json", stored_request)
                command["env"]["GOALFLIGHT_REMOTE_CI_SOURCE_BUNDLE"] = str(destination / "source.bundle")
                write_json(run / "command.json", command)
                lease = read_json(run / "lease.json")
                lease.update(state="queued", ticket=sequence, run_directory=str(destination),
                             ticket_awake_started=awake_monotonic(),
                             ticket_boot_session=_boot_session_id())
                if basis_commit is not None:
                    lease["basis_commit"] = basis_commit.lower()
                write_json(run / "lease.json", lease)
                for name in ("lease.json", "request.json", "command.json"):
                    if not _fsync_file(run / name):
                        raise ValueError("submitted ticket metadata could not be made durable")
                run.rename(destination)
                run = destination
                published = True
                if not _fsync_file(run) or not _fsync_file(uploads):
                    raise ValueError("submitted ticket publication could not be made durable")
                ticket_path = root / "tickets" / f"{sequence:020d}"
                if ticket_path.exists():
                    raise ValueError("ticket sequence already exists")
                ticket_lock = try_lock(ticket_path)
                holder_lock = try_lock(run / "holder.lock")
                if ticket_lock is None or holder_lock is None:
                    if ticket_lock is not None:
                        ticket_lock.close()
                    if holder_lock is not None:
                        holder_lock.close()
                    raise ValueError("could not reserve submitted ticket locks")
                if not _fsync_file(ticket_path):
                    ticket_lock.close()
                    holder_lock.close()
                    raise ValueError("submitted ticket could not be made durable")
                _fork_run_holder(
                        root, run, ticket_path, stored_request, ticket_lock,
                        holder_lock, queue_guard=guard,
                        close_handles=(upload_lock, publication[1] if publication else None))
        if duplicate_run is not None:
            if basis_pinned and not _release_bundle_basis_pin(
                    managed, read_json(run / "lease.json")):
                raise ValueError("duplicate upload basis hold could not be released")
            shutil.rmtree(run, ignore_errors=True)
            return record(duplicate_run)
    except BaseException:
        if not published and run is not None:
            if ticket_path is not None:
                ticket_path.unlink(missing_ok=True)
            if (not basis_pinned
                    or _release_bundle_basis_pin(managed, read_json(run / "lease.json"))):
                shutil.rmtree(run, ignore_errors=True)
        raise
    finally:
        if upload_lock is not None:
            upload_lock.close()
    return record(run)


def dispatch(request):
    _timing_mark("handler_entry")
    configured_path = request.get('node_path')
    previous_path = os.environ.get('PATH')
    if isinstance(configured_path, str):
        os.environ['PATH'] = configured_path
    try:
        return _dispatch(request)
    finally:
        if previous_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = previous_path


def _dispatch(request):
    managed = pin_managed_root(Path(request['managed_root']))
    root = managed / 'admission'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('tokens', 'tickets'):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    for name in ('runs', 'results', 'keep', 'quarantine'):
        (managed / name).mkdir(exist_ok=True, mode=0o700)
    operation = request['operation']
    if operation in {'enqueue', 'submit'}:
        admission_command_window_seconds(request.get('controller_poll_seconds'))
        token_pool_size = request.get('token_pool_size')
        max_seats = request.get('max_concurrent_seats', request.get('token_pool_size'))
        if (type(token_pool_size) is not int or token_pool_size <= 0
                or type(max_seats) is not int or max_seats < 0
                or max_seats > token_pool_size):
            raise ValueError('max_concurrent_seats must be between zero and token_pool_size')
        request['max_concurrent_seats'] = max_seats
        request['repo'] = safe_repo(request.get('repo'))
        checkout_basename = request.get('checkout_basename', '')
        if (not isinstance(checkout_basename, str)
                or checkout_basename in {'.', '..'}
                or '/' in checkout_basename or '\\' in checkout_basename):
            raise ValueError('checkout_basename must be one directory name')
    try:
        _timing_mark("admission_lock_wait_start")
        guard = _with_queue_lock(
            root,
            deadline=(time.monotonic() + HEALTH_LOCK_TIMEOUT_SECONDS
                      if operation == 'health' else None),
        )
        _timing_mark("admission_lock_acquired")
    except SetupDeadlineExceeded:
        if operation == 'health':
            return {'status': 'unknown', 'reason': 'admission-lock-timeout'}
        raise
    try:
        policy_path = root / 'policy.json'
        policy = {key: request[key] for key in ('p_cores', 'token_pool_size')}
        if not policy_path.exists():
            write_json(policy_path, policy)
        elif operation in {'enqueue', 'submit'} and read_json(policy_path) != policy:
            # A wrong cap must not take a token. Recovery (list, reap, cancel)
            # still has to run, or the corrected config cannot clean the box.
            raise ValueError('shared box policy differs; use the same configured caps')
    finally:
        guard.close()
    if operation == 'bundle-basis':
        if request.get('lease_id') is None:
            return {'commit': confirmed_bundle_basis(managed, request.get('repo'))}
        return {'commit': pin_bundle_basis(
            managed, request.get('repo'), request.get('lease_id'),
            request.get('lease_token'), request.get('basis_commit'),
        )}
    if operation == 'cleanup-legacy-slots':
        if type(request.get('apply', False)) is not bool:
            raise ValueError('apply must be a boolean')
        return cleanup_legacy_slots(
            managed, request.get('repo'), apply=request.get('apply', False),
        )
    if operation == 'submit':
        return _submit_ticket(managed, root, request, max_seats, checkout_basename)
    if operation == 'enqueue':
        _timing_mark("enqueue_sample_start")
        sample(root, request.get('python_interpreter', ''))
        _timing_mark("enqueue_sample_end")
        destination_address = ssh_peer_address()
        measured_hostname = socket.gethostname()
        _timing_mark("queue_lock_wait_start")
        with (root / 'queue.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            _timing_mark("queue_lock_acquired")
            counter = root / 'sequence.json'
            sequence = (read_json(counter) if counter.exists() else 0) + 1
            write_json(counter, sequence)
            lease_id = uuid.uuid4().hex
            gc_run_bodies(managed, request, True, cap_deletions=True)
            run = managed / 'runs' / lease_id
            run.mkdir(mode=0o700)
            (run / 'stdout').touch()
            (run / 'stderr').touch()
            ticket = root / 'tickets' / f'{sequence:020d}'
            ticket_lock = try_lock(ticket)
            holder_lock = try_lock(run / 'holder.lock')
            _timing_mark("lease_create_start")
            state = _new_ticket_lease(
                managed, root, request, sequence, run, max_seats,
                checkout_basename, measured_hostname=measured_hostname,
                destination_address=destination_address,
            )
            _timing_mark("lease_create_end")
            _timing_mark("holder_fork_start")
            if not _fork_run_holder(
                    root, run, ticket, request, ticket_lock, holder_lock,
                    queue_guard=guard):
                ticket.unlink(missing_ok=True)
                raise ValueError('could not start the enqueue holder')
            _timing_mark("holder_fork_end")
        return record(run)
    if operation == 'list':
        rows = []
        seen = set()
        for path in (*iter_run_dirs(managed), *iter_retained_dirs(managed)):
            if path.name in seen:
                continue
            seen.add(path.name)
            path = _retained_run(managed, path)
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
        measured = sample(root, request.get('python_interpreter', ''))
        peer_address = ssh_peer_address()
        measured['load_within_p_cores'] = measured['load1'] <= measured['p_cores']
        required_paths = request.get('required_paths', [])
        if (not isinstance(required_paths, list)
                or any(not isinstance(path, str) or not Path(path).is_absolute()
                       for path in required_paths)):
            raise ValueError('required_paths must contain absolute paths')
        missing_paths = [path for path in required_paths if not Path(path).exists()]
        total = measured['token_pool_size']
        # Health is an advisory snapshot. Do not reclaim leases or hold the
        # admission lock while scanning ownership; admission remains authoritative.
        reserved, reservation_reason = reserved_token_indexes(
            managed, token_pool_size=measured['token_pool_size'],
        )
        used = {index for index in range(total) if locked(root / 'tokens' / str(index))}
        if reserved is None:
            used.update(range(total))
        else:
            used.update(index for index in reserved if index < total)
        tokens = {'total': total, 'free': total - len(used), 'in_use': len(used)}
        if reservation_reason is not None:
            tokens['reservation_reason'] = reservation_reason
        return {'load': measured, 'peer_address': peer_address, 'tokens': tokens,
                'missing_required_paths': missing_paths}
    run = _retained_run(managed, Path(request['run_dir']))
    if not _run_is_managed(managed, run):
        if operation == 'status':
            archived = archived_run(managed, run.name)
            if (archived is not None
                    and isinstance(archived.get('lease'), dict)
                    and (archived.get('body_path') == str(run)
                         or archived['lease'].get('run_directory') == str(run))
                    and archived.get('lease_token') == request.get('lease_token')
                    and isinstance(archived.get('result'), dict)):
                cached = dict(archived['lease'])
                cached.update(
                    state='released', run_directory=str(run),
                    result=archived['result'], holder_alive=False, command_present=True,
                )
                return cached
        raise ValueError('run directory is outside managed admission runs')
    _timing_mark("run_lease_read_start")
    state = record(run)
    _timing_mark("run_lease_read_end")
    if request.get('lease_token') != state['lease_token']:
        raise ValueError('lease token does not match')
    if operation == 'status':
        _timing_mark("status_snapshot_start")
        state['holder_alive'] = locked_existing(run / 'holder.lock')
        state['command_present'] = (run / 'command.json').is_file()
        result_path = run / 'result.json'
        if result_path.exists():
            tail_bytes = request.get('result_tail_bytes', DEFAULT_RESULT_TAIL_BYTES)
            if (isinstance(tail_bytes, bool) or not isinstance(tail_bytes, int)
                    or tail_bytes < 1):
                raise ValueError('result_tail_bytes must be a positive integer')
            if tail_bytes > MAX_RESULT_TAIL_BYTES:
                raise ValueError(
                    f'result tail request {tail_bytes} bytes exceeds cap '
                    f'{MAX_RESULT_TAIL_BYTES} bytes'
                )
            state['result'] = read_json(result_path)
            state['result']['streams'] = {
                name: stream_receipt(run / name, tail_bytes)
                for name in ('stdout', 'stderr')
            }
        _timing_mark("status_snapshot_end")
        return state
    if operation == 'fetch':
        if not (run / 'result.json').is_file():
            raise ValueError('stream fetch is available only after result.json exists')
        return fetch_stream(run, request)
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
                existing = read_json(run / 'command.json')
                if existing != request['command']:
                    raise ValueError('run already started with different command content')
                return state
            write_json(run / 'command.json', request['command'])
        return state
    if operation == 'cancel':
        queued_cancel = False
        queued_cancel_deferred = False
        released_lease = None
        with (run / 'command.lock').open('a+') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            state = record(run)
            expected = request.get('expected_owner')
            if not isinstance(expected, dict):
                raise ValueError('expected_owner is required')
            if expected != read_json(run / 'owner.json'):
                return {'status': 'owned'}
            identity = request.get('identity') or state.get('remote_run')
            if ((state.get('state') == 'queued' or _resident_pre_admission(state))
                    and (identity is not None or state.get('resident_submission') is True)):
                queue_guard = _with_queue_lock(root)
                try:
                    state = record(run)
                    if state.get('state') == 'queued' or _resident_pre_admission(state):
                        if identity is not None and not identity_matches(run, identity):
                            raise ValueError('unknown identity; refusing cancellation')
                        cancel_identity = identity or {
                            'lease_id': state.get('lease_id'),
                            'lease_token': state.get('lease_token'),
                        }
                        write_json(run / 'cancel.json', cancel_identity)
                        write_json(run / 'release.json', {})
                        queued_ticket = root / 'tickets' / f"{state.get('ticket', -1):020d}"
                        if not locked(run / 'holder.lock'):
                            released_lease = _finish_queued_submission(
                                root, run, state, 'cancelled', 'cancelled',
                            )
                            if released_lease is None:
                                queued_cancel_deferred = True
                            else:
                                queued_ticket.unlink(missing_ok=True)
                        else:
                            queued_ticket.unlink(missing_ok=True)
                        queued_cancel = True
                    else:
                        identity = request.get('identity') or state.get('remote_run')
                finally:
                    queue_guard.close()
            if not queued_cancel and not identity_matches(run, identity):
                raise ValueError('unknown identity; refusing cancellation')
            if not queued_cancel and not locked(run / 'holder.lock'):
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
            if not queued_cancel:
                write_json(run / 'cancel.json', identity)
                # An admitted holder may not have received its command yet.
                if not (run / 'command.json').exists():
                    write_json(run / 'release.json', {})
        if released_lease is not None and not _release_bundle_basis_pin(
                managed, released_lease):
            # The cancel response describes holder completion, not pin cleanup.
            # Report that separately: this ref holds no seat and a later setup
            # GC can sweep it, so preserve the already-published cancellation.
            _report_bundle_basis_pin_release_unknown(
                managed, released_lease, source="queued-cancel",
            )
        if queued_cancel_deferred:
            return {'status': 'unknown'}
        if queued_cancel:
            deadline = time.monotonic() + 20
            while locked(run / 'holder.lock') and time.monotonic() < deadline:
                time.sleep(0.02)
            return {'status': 'unknown' if locked(run / 'holder.lock') else 'cancelled'}
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
                return {'status': 'owned'}
            with (root / 'queue.lock').open('a+') as queue_guard:
                fcntl.flock(queue_guard, fcntl.LOCK_EX)
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
        if len(sys.argv) >= 2 and sys.argv[1] == "admit-loop":
            if len(sys.argv) != 4 or sys.argv[2] != "--managed-root":
                raise ValueError(
                    "usage: goalflight_remote_ci_node.py admit-loop --managed-root <path>"
                )
            admit_loop(sys.argv[3])
            raise SystemExit(0)
        request = json.loads(base64.b64decode(sys.argv[1]))
        if len(sys.argv) == 3 and sys.argv[2] == "--stream-submit":
            if (request.get("operation") != "submit"
                    or request.get("candidate_bundle_stream") is not True):
                raise ValueError("streamed bundle input is valid only for submit")
            request["_candidate_bundle_stream"] = sys.stdin.buffer
        elif len(sys.argv) != 2:
            raise ValueError("invalid remote CI helper arguments")
        version = request.get('protocol_version')
        if version != PROTOCOL_VERSION:
            raise ValueError(
                f'remote CI protocol mismatch: expected {PROTOCOL_VERSION}, received {version!r}'
            )
        _begin_timing_record(request)
        result = dispatch(request)
        _timing_mark("response_write_start")
        response = json.dumps({
            'protocol_version': PROTOCOL_VERSION,
            'result': result,
        }) + '\n'
        sys.stdout.write(response)
        sys.stdout.flush()
        _timing_mark("response_write_end")
        _write_timing_record()
    except Exception as exc:
        _timing_mark("response_write_start")
        sys.stderr.write(str(exc) + '\n')
        sys.stderr.flush()
        _timing_mark("response_write_end")
        _write_timing_record()
        sys.exit(2)
