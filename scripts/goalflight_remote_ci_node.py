"""Node-side admission authority, sent through the configured remote executor.

Standard library only. One managed root identifies one physical box's pool.
The holder keeps the incarnation lock. The workload inherits the token flock,
so the slot stays taken after the holder exits until that workload exits.
"""
from __future__ import annotations

import base64
import builtins
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
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(line, sort_keys=True) + "\n")


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


def token_held(root, state):
    index = state.get("token_index")
    if index is None:
        return False
    return locked(root / "tokens" / str(index))


def group_alive(pgid):
    try:
        os.killpg(int(pgid), 0)
        return True
    except (OSError, ValueError):
        return False


def signal_one(pid, group=False):
    """SIGTERM, a short grace, then SIGKILL. True when the target is gone."""
    killer = os.killpg if group else os.kill
    try:
        killer(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + TREE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if group:
            if not group_alive(pid):
                return True
        else:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
        time.sleep(0.02)
    try:
        killer(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    time.sleep(0.05)
    if group:
        return not group_alive(pid)
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


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


def clear_tree(pgid, paths):
    """Kill the workload group and any escaped descendant whose cwd is in paths.

    False means something may still be alive, or lsof could not prove otherwise.
    Callers must keep the token in that case.
    """
    if pgid is not None:
        signal_one(pgid, group=True)
    intruders = cwd_intruders(paths, pgid)
    if intruders is None:
        return False
    for pid in intruders:
        signal_one(pid, group=False)
    if pgid is not None and group_alive(pgid):
        return False
    again = cwd_intruders(paths, pgid)
    return again is not None and not again


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


def finish_dead_workload(root, run, state, action):
    """Kill the whole workload tree, or keep the token when that cannot be proved."""
    managed = root.parent
    pgid = state.get("workload_pid")
    paths = [run, state.get("slot")]
    cleared = clear_tree(int(pgid) if pgid else None, paths)
    if not cleared:
        audit(managed / "admission", {"action": action, "result": "kept",
                                      "lease_id": state.get("lease_id"), "at": time.time()})
        return {"status": "unknown"}
    lease = read_json(run / "lease.json")
    lease["state"] = "released"
    lease["release_reason"] = "deadline" if action == "deadline" else "operator-clear"
    write_json(run / "lease.json", lease)
    append_result(managed, lease, run)
    audit(managed / "admission", {"action": action, "result": "cleared",
                                  "lease_id": lease.get("lease_id"),
                                  "workload_pid": lease.get("workload_pid"), "at": time.time()})
    return {"status": "cancelled" if action == "deadline" else "cleared", "killed": True}


def lease_slot(managed, repo, count, state):
    """Reuse s-01..s-N. Never create s-(N+1). Unknown or busy slots are skipped."""
    for index in range(count):
        slot = managed / "repos" / repo / "slots" / f"s-{index + 1:02d}"
        slot.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = try_lock(slot / "slot.lock")
        if lock is None:
            continue
        marker = slot / "SLOT.json"
        if marker.exists():
            try:
                current = read_json(marker)
            except (OSError, ValueError):
                lock.close()
                continue
            if current.get("state") != "free":
                run_dir = current.get("run_dir")
                busy = True
                if run_dir:
                    try:
                        lease = read_json(Path(run_dir) / "lease.json")
                        # A dead holder before start never rewrites the lease.
                        # The slot can be reused once nothing is still in it.
                        busy = locked(Path(run_dir) / "holder.lock") and lease.get("state") != "released"
                    except (OSError, ValueError):
                        busy = True
                if busy:
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
        if child.name == "slot.lock":
            continue
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
                            for index in range(measured['token_pool_size']):
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
        env = os.environ.copy()
        env.update(command['env'])
        env['GOALFLIGHT_REMOTE_CI_REMOTE_PID'] = state['remote_run']['pid']
        env['GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN'] = state['remote_run']['start_token']
        env['GOALFLIGHT_REMOTE_CI_SLOT_DIR'] = state.get('slot') or ''
        env['GOALFLIGHT_REMOTE_CI_RUN_DIR'] = str(run)
        env['GOALFLIGHT_REMOTE_CI_RESULT_INDEX'] = str(root.parent / 'results' / 'index.jsonl')
        with (run / 'stdout').open('w') as out, (run / 'stderr').open('w') as err:
            # The workload is its own session so killing the holder does not
            # silently reap it, and so cancel can signal that group alone.
            # The token fd is inherited and stays held until the tree is gone.
            token_fd = token.fileno()
            os.set_inheritable(token_fd, True)
            child = subprocess.Popen(command['argv'], env=env, stdout=out,
                                     stderr=err, stdin=subprocess.DEVNULL,
                                     cwd=state.get('slot') or None,
                                     start_new_session=True, pass_fds=(token_fd,))
            tree_started = True
            state['workload_pid'] = str(child.pid)
            write_json(run / 'lease.json', state)
            deadline = time.monotonic() + command['timeout']
            paths = [run, state.get('slot')]
            while child.poll() is None:
                cancel_path = run / 'cancel.json'
                cancelled = cancel_path.exists() and identity_matches(run, read_json(cancel_path))
                if cancelled or time.monotonic() >= deadline:
                    if not clear_tree(child.pid, paths):
                        time.sleep(0.2)
                        continue
                    write_json(run / 'result.json', {'returncode': 124, 'timed_out': True})
                    state['release_reason'] = 'cancelled' if cancelled else 'node-timeout'
                    break
                time.sleep(0.02)
            else:
                write_json(run / 'result.json', {'returncode': child.returncode, 'timed_out': False})
            if tree_started:
                while not clear_tree(int(state['workload_pid']), paths):
                    time.sleep(0.2)
    except BaseException as exc:
        write_json(run / 'result.json', {'returncode': 2, 'timed_out': False, 'error': str(exc)})
    finally:
        ticket.unlink(missing_ok=True)
        if state.get('release_reason') == 'unknown-descendants':
            # A live descendant still owns this capacity.
            write_json(run / 'lease.json', state)
        else:
            state['state'] = 'released'
            state.setdefault('release_reason', 'completed')
            write_json(run / 'lease.json', state)
            append_result(root.parent, state, run)
            if token is not None:
                token.close()
            if slot is not None:
                write_json(slot / 'SLOT.json', {'state': 'free'})
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
        runs_root = managed / 'runs'
        return [record(path) for path in sorted(path for path in runs_root.iterdir()
                                                if path.is_dir() and not path.is_symlink())]
    if operation == 'gc':
        return gc_run_bodies(managed, request, bool(request.get('apply')))
    if operation == 'health':
        measured = sample(root)
        measured['load_within_p_cores'] = measured['load1'] <= measured['p_cores']
        total = measured['token_pool_size']
        used = sum(locked(root / 'tokens' / str(index)) for index in range(total))
        return {'load': measured, 'tokens': {'total': total, 'free': total-used, 'in_use': used}}
    run = Path(request['run_dir'])
    if run.parent != managed / 'runs' or run.is_symlink() or not run.is_dir():
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
