"""Node-side admission authority, sent through the configured remote executor.

Standard library only. One managed root identifies one physical box's pool.
The holder keeps the incarnation lock. The workload inherits the token flock,
so the slot stays taken after the holder exits until that workload exits.
"""
from __future__ import annotations

import base64
import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid


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
                                    state.update(state='admitted', token_index=index,
                                                 sample=measured)
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
        state['state'] = 'running'
        write_json(run / 'lease.json', state)
        env = os.environ.copy()
        env.update(command['env'])
        env['GOALFLIGHT_REMOTE_CI_REMOTE_PID'] = state['remote_run']['pid']
        env['GOALFLIGHT_REMOTE_CI_REMOTE_START_TOKEN'] = state['remote_run']['start_token']
        with (run / 'stdout').open('w') as out, (run / 'stderr').open('w') as err:
            # Same process group as this holder. The token fd is inherited so
            # SIGKILL of the holder does not free the slot while the workload
            # still runs. holder.lock is not inherited.
            token_fd = token.fileno()
            os.set_inheritable(token_fd, True)
            child = subprocess.Popen(command['argv'], env=env, stdout=out,
                                     stderr=err, stdin=subprocess.DEVNULL,
                                     pass_fds=(token_fd,))
            deadline = time.monotonic() + command['timeout']
            while child.poll() is None:
                cancel_path = run / 'cancel.json'
                cancelled = cancel_path.exists() and identity_matches(run, read_json(cancel_path))
                if cancelled or time.monotonic() >= deadline:
                    write_json(run / 'result.json', {'returncode': 124, 'timed_out': True})
                    state['state'] = 'released'
                    state['release_reason'] = 'cancelled' if cancelled else 'node-timeout'
                    write_json(run / 'lease.json', state)
                    # Self-cancellation avoids a PID-reuse check/kill race.
                    os.killpg(os.getpid(), signal.SIGKILL)
                time.sleep(0.02)
            write_json(run / 'result.json', {'returncode': child.returncode, 'timed_out': False})
    except BaseException as exc:
        write_json(run / 'result.json', {'returncode': 2, 'timed_out': False, 'error': str(exc)})
    finally:
        ticket.unlink(missing_ok=True)
        state['state'] = 'released'
        state.setdefault('release_reason', 'completed')
        write_json(run / 'lease.json', state)
        if token is not None:
            token.close()
        ticket_lock.close()
        holder_lock.close()


def dispatch(request):
    managed = Path(request['managed_root'])
    root = managed / 'admission'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('runs', 'tokens', 'tickets'):
        (root / name).mkdir(exist_ok=True, mode=0o700)
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
            run = root / 'runs' / lease_id
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
        return [record(path) for path in sorted((root / 'runs').iterdir())]
    if operation == 'health':
        measured = sample(root)
        measured['load_within_p_cores'] = measured['load1'] <= measured['p_cores']
        total = measured['token_pool_size']
        used = sum(locked(root / 'tokens' / str(index)) for index in range(total))
        return {'load': measured, 'tokens': {'total': total, 'free': total-used, 'in_use': used}}
    run = Path(request['run_dir'])
    if run.parent != root / 'runs' or run.is_symlink() or not run.is_dir():
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
            if expected is not None and expected != read_json(run / 'owner.json'):
                return {'status': 'owned'}  # A reattach won the race with reaping.
            if not locked(run / 'holder.lock'):
                return {'status': 'finished' if state['state'] == 'released' else 'unknown'}
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
    raise ValueError('unknown operation: ' + operation)


if __name__ == '__main__':
    try:
        print(json.dumps(dispatch(json.loads(base64.b64decode(sys.argv[1])))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
