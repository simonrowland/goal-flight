#!/usr/bin/env bash
# Listener startup-race load test.
#
# This is a measurement instrument, so every prerequisite has a positive
# witness. A cell that cannot prove its journal path, generator work, listener
# arm commits, barrier readiness, or cleanup does not print a measurement row.
#
# USAGE
#   listener_arm_loadtest.sh <N> <spacing_secs> <tag> [load_writers] [load_readers]
#
#   N              listeners to arm (1..200)
#   spacing_secs   delay between barrier releases; 0 releases every listener
#                  from one barrier and reports the observed attempt skew
#   tag            short result label ([A-Za-z0-9._-]+)
#   load_writers   journal-writing generator processes (default 0)
#   load_readers   journal-reading generator processes (default 0)
#
# ISOLATION
#   Each invocation owns its project root, journal, wake ledger, message dir,
#   task store, dispatch dir, and both pidfile-dir spellings. The controller
#   lease holder, listeners, and generators run beneath direct tracked launcher
#   supervisors whose PID-named groups persist until cleanup. No detached
#   controller-lock process is created.
#
# WITNESSES
#   * Every generator publishes its resolved journal path, successful work
#     count, and sequenced monotonic completion log. Only completions between
#     the first product call and final arm boundary qualify; writer counts are
#     additionally reconciled against delivery_events in that exact journal.
#   * A listener counts as armed only when listener_coverage contains its exact
#     PID + process-start-token identity. Terminal return codes are diagnostic.
#   * All listener interpreters import first, publish READY, and wait immediately
#     before goalflight_messages' first journal operation. spacing=0 then uses a
#     common release file; attempt timestamps expose residual scheduler skew.
#   * Cleanup signals only groups pinned by persistent, identity-matched direct
#     supervisors, reaps those children, and refuses a clean exit if
#     reconciliation is nonzero or unprobeable.
set -uo pipefail

SELF="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"

# Each shell-visible launcher is a persistent PID-named process-group supervisor
# for one measured worker. Cleanup can inventory its unreaped worker and any
# unexpected descendants without pkill -P or PID-only signalling.
case "${1:-}" in
  __launch)
    shift
    exec python3 - "$@" <<'PY'
import os, signal, sys

# Stop before fallible setup. The Bash parent records this exact, kernel-pinned
# generation before allowing it to proceed. Cleanup is deferred until this
# launcher either publishes its immortal supervisor record or exits without
# having forked a worker.
registry_bootstrap = sys.argv[2]
if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "launcher-before-ready-exit":
    raise SystemExit("injected launcher exit before bootstrap ready")
bootstrap_ready = os.path.join(
    os.path.dirname(registry_bootstrap), f"launcher-bootstrap.{os.getpid()}.ready"
)
ready_fd = os.open(bootstrap_ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(ready_fd, f"pid={os.getpid()}\n".encode())
finally:
    os.close(ready_fd)
os.kill(os.getpid(), signal.SIGSTOP)
if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "launcher-pre-setpgid-stop":
    os.kill(os.getpid(), signal.SIGSTOP)

# Establish a private process-group anchor before repository imports. Cleanup
# can then adopt this parent-owned launcher even if an interrupt lands before
# its durable registry append.
os.setpgid(0, 0)

import json, time
from pathlib import Path

# This tiny supervisor is the process-group leader and never exits voluntarily.
# It pins the numeric PID/PGID until cleanup's SIGKILL. The measured worker is
# its unreaped direct child, so neither identity can be recycled during cleanup.
for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(signum, signal.SIG_IGN)

skill_dir, registry_text, role, self_path, *worker_args = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

identity = compat.process_start_identity(os.getpid())
if not identity or not identity.get("start_token"):
    raise SystemExit(f"cannot self-register {role} pid={os.getpid()}")
record = {
    "role": f"launcher-supervisor-{role}",
    "worker_role": role,
    "pid": os.getpid(),
    "start_token": identity["start_token"],
    "pgid": os.getpgid(0),
    "worker_state_path": str(Path(registry_text).with_name(f"launcher.{os.getpid()}.json")),
}
payload = (json.dumps(record, sort_keys=True) + "\n").encode()
fd = os.open(registry_text, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try:
    if os.write(fd, payload) != len(payload):
        raise OSError("short process-registry write")
finally:
    os.close(fd)

state_path = Path(record["worker_state_path"])
error_path = Path(registry_text).parent / "SUPERVISOR_ERROR"

def record_supervisor_error(stage, exc):
    try:
        payload = f"role={role} supervisor_pid={os.getpid()} stage={stage} error={type(exc).__name__}:{exc}\n".encode()
        error_fd = os.open(error_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(error_fd, payload)
        finally:
            os.close(error_fd)
    except BaseException:
        pass

def publish(value):
    pending = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, state_path)

def safe_publish(stage, value):
    try:
        publish(value)
        return True
    except BaseException as exc:
        record_supervisor_error(stage, exc)
        return False

def resilient_sleep(stage, seconds):
    try:
        time.sleep(seconds)
    except BaseException as exc:
        record_supervisor_error(stage, exc)

safe_publish("forking-publish", {"state": "forking", "supervisor_pid": os.getpid(), "worker_role": role})
try:
    child_pid = os.fork()
except BaseException as exc:
    record_supervisor_error("fork", exc)
    safe_publish("fork-failed-publish", {"state": "failed", "error": f"{type(exc).__name__}: {exc}"})
    child_pid = None
if child_pid == 0:
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, signal.SIG_DFL)
    os.execv(self_path, [self_path, *worker_args])
if child_pid is not None:
    try:
        child_identity = compat.process_start_identity(child_pid)
    except BaseException as exc:
        record_supervisor_error("child-identity", exc)
        child_identity = None
    safe_publish(
        "running-publish",
        {
            "state": "running",
            "supervisor_pid": os.getpid(),
            "worker_pid": child_pid,
            "worker_start_token": (
                child_identity.get("start_token") if isinstance(child_identity, dict) else None
            ),
            "worker_role": role,
        }
    )
    forced_waitid_error = role == "test-supervisor-waitid-error"
    while True:
        try:
            if forced_waitid_error:
                forced_waitid_error = False
                raise OSError("forced supervisor waitid failure")
            result = os.waitid(
                os.P_PID,
                child_pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except BaseException as exc:
            record_supervisor_error("waitid", exc)
            resilient_sleep("waitid-retry-sleep", 0.02)
            continue
        if result is not None:
            safe_publish(
                "exited-publish",
                {
                    "state": "exited",
                    "supervisor_pid": os.getpid(),
                    "worker_pid": child_pid,
                    "worker_role": role,
                    "wait_code": int(result.si_code),
                    "wait_status": int(result.si_status),
                }
            )
            break
        resilient_sleep("waitid-poll-sleep", 0.02)
while True:
    resilient_sleep("linger-sleep", 1)
PY
    ;;

  __identity)
    shift
    exec python3 - "$@" <<'PY'
import json, os, sys, time
from pathlib import Path

skill_dir, raw_pid, role, registry = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

pid = int(raw_pid)
identity = None
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    identity = compat.process_start_identity(pid)
    if identity and os.getpgid(pid) == pid:
        break
    time.sleep(0.01)
if not identity or not identity.get("start_token"):
    raise SystemExit(f"cannot record process identity for {role} pid={pid}")
record = {
    "role": role,
    "pid": pid,
    "start_token": identity["start_token"],
    "pgid": os.getpgid(pid),
}
with Path(registry).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
PY
    ;;

  __bootstrap_register)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, sys, time
from pathlib import Path

skill_dir, registry_text, raw_pid, role, ready_text, raw_parent = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

pid, parent_pid = int(raw_pid), int(raw_parent)

def raw_process():
    if sys.platform == "darwin":
        info = compat._darwin_process_bsd_info(pid)
        if info is None:
            return None
        return {
            "ppid": int(info["ppid"]),
            "pgid": int(info["pgid"]),
            "stopped": int(info["status"]) == 4,
            "zombie": int(info["status"]) == 5,
        }
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            return {
                "ppid": int(fields[1]),
                "pgid": int(fields[2]),
                "stopped": fields[0] in {"T", "t"},
                "zombie": fields[0] in {"Z", "X"},
            }
        except (IndexError, OSError, ValueError):
            return None
    raise RuntimeError(f"bootstrap process state is unsupported on {sys.platform}")

deadline = time.monotonic() + 3.0
identity = None
while time.monotonic() < deadline:
    raw = raw_process()
    identity = compat.process_start_identity(pid)
    if not raw or raw["zombie"] or not identity or not identity.get("start_token"):
        time.sleep(0.005)
        continue
    if raw["ppid"] != parent_pid or raw["pgid"] != pid:
        raise SystemExit(
            f"bootstrap ownership changed pid={pid} ppid={raw['ppid']} pgid={raw['pgid']}"
        )
    if not Path(ready_text).exists():
        # An external STOP may precede the child's first Python instruction.
        # While stopped, this exact direct-child generation cannot exit or be
        # recycled, so CONT is safe and merely lets it reach its own handshake.
        if raw["stopped"]:
            os.kill(pid, signal.SIGCONT)
        time.sleep(0.005)
        continue
    if raw["stopped"]:
        record = {
            "role": f"launcher-bootstrap-{role}",
            "pid": pid,
            "start_token": identity["start_token"],
            "pgid": pid,
        }
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        fd = os.open(registry_text, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("short bootstrap-registry write")
        finally:
            os.close(fd)
        raise SystemExit(0)
    time.sleep(0.005)
raise SystemExit(f"launcher bootstrap handshake timed out role={role} pid={pid}")
PY
    ;;

  __bootstrap_continue)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, sys
from pathlib import Path

skill_dir, registry_text, raw_pid, raw_parent = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

pid, parent_pid = int(raw_pid), int(raw_parent)
records = [
    json.loads(line)
    for line in Path(registry_text).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
item = next(
    (
        row for row in reversed(records)
        if int(row["pid"]) == pid
        and str(row.get("role") or "").startswith("launcher-bootstrap-")
    ),
    None,
)
if item is None or compat.process_identity_matches(pid, str(item["start_token"])) is not True:
    raise SystemExit(2)

stopped = False
if sys.platform == "darwin":
    raw = compat._darwin_process_bsd_info(pid)
    if raw is None or int(raw["ppid"]) != parent_pid or int(raw["pgid"]) != pid:
        raise SystemExit(2)
    stopped = int(raw["status"]) == 4
elif sys.platform.startswith("linux"):
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat[stat.rfind(")") + 2 :].split()
    if int(fields[1]) != parent_pid or int(fields[2]) != pid:
        raise SystemExit(2)
    stopped = fields[0] in {"T", "t"}
else:
    raise SystemExit(2)
if stopped:
    # The registered generation is currently kernel-stopped and direct-owned;
    # it cannot exit between the identity check and this continuation.
    os.kill(pid, signal.SIGCONT)
PY
    ;;

  __bootstrap_abort)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, sys
from pathlib import Path

skill_dir, registry_text, raw_pid, raw_parent = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

pid, parent_pid = int(raw_pid), int(raw_parent)
records = []
if Path(registry_text).exists():
    records = [
        json.loads(line)
        for line in Path(registry_text).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
item = next(
    (
        row for row in reversed(records)
        if int(row["pid"]) == pid
        and str(row.get("role") or "").startswith("launcher-bootstrap-")
    ),
    None,
)
identity = compat.process_start_identity(pid)
if not identity or not identity.get("start_token"):
    raise SystemExit(2)
if item is not None and str(item["start_token"]) != str(identity["start_token"]):
    raise SystemExit(2)

if sys.platform == "darwin":
    raw = compat._darwin_process_bsd_info(pid)
    valid = bool(
        raw
        and int(raw["ppid"]) == parent_pid
        and int(raw["pgid"]) == pid
        and int(raw["status"]) == 4
    )
elif sys.platform.startswith("linux"):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat[stat.rfind(")") + 2 :].split()
        valid = (
            int(fields[1]) == parent_pid
            and int(fields[2]) == pid
            and fields[0] in {"T", "t"}
        )
    except (IndexError, OSError, ValueError):
        valid = False
else:
    valid = False
if not valid:
    raise SystemExit(2)

# This exact direct child is kernel-stopped, so it cannot exit, fork, or free
# its PID/PGID between validation and KILL.
os.killpg(pid, signal.SIGKILL)
print(f"BOOTSTRAP_ABORTED pid={pid} start_token={identity['start_token']}")
PY
    ;;

  __pid_state)
    shift
    exec python3 - "$@" <<'PY'
import json, sys
from pathlib import Path

skill_dir, registry_text, raw_pid, *snapshot_args = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

pid = int(raw_pid)
records = []
if Path(registry_text).exists():
    records.extend(
        json.loads(line)
        for line in Path(registry_text).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
item = next((row for row in reversed(records) if int(row["pid"]) == pid), None)
if item is None and snapshot_args and Path(snapshot_args[0]).exists():
    snapshot_records = [
        json.loads(line)
        for line in Path(snapshot_args[0]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    item = next((row for row in reversed(snapshot_records) if int(row["pid"]) == pid), None)
if item is None:
    print("unknown")
    raise SystemExit(2)
if str(item.get("start_token") or "").startswith(("unavailable:", "pregroup:")):
    print("unknown")
    raise SystemExit(2)
matches = compat.process_identity_matches(pid, str(item["start_token"]))
if matches is None:
    print("unknown")
    raise SystemExit(2)
if matches is True and str(item.get("role") or "").startswith("launcher-bootstrap-"):
    print("bootstrap")
    raise SystemExit(3)
worker_state_path = item.get("worker_state_path")
if matches is True and worker_state_path:
    try:
        worker_state = json.loads(Path(worker_state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        worker_state = {"state": "starting"}
    if worker_state.get("state") in {"exited", "failed"}:
        print("done")
        raise SystemExit(1)
if matches is True and compat.pid_is_zombie(pid) is not True:
    print("running")
    raise SystemExit(0)
print("done")
raise SystemExit(1)
PY
    ;;

  __worker_pid)
    shift
    exec python3 - "$@" <<'PY'
import json, sys
from pathlib import Path

registry_text, raw_supervisor = sys.argv[1:]
supervisor = int(raw_supervisor)
records = [
    json.loads(line)
    for line in Path(registry_text).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
item = next((row for row in records if int(row["pid"]) == supervisor), None)
if item is None or not item.get("worker_state_path"):
    raise SystemExit(2)
state = json.loads(Path(item["worker_state_path"]).read_text(encoding="utf-8"))
worker_pid = int(state.get("worker_pid") or 0)
if state.get("state") != "running" or worker_pid <= 0:
    raise SystemExit(2)
print(worker_pid)
PY
    ;;

  __scheduler)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, subprocess, sys, time
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test workers forbid descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

root, raw_n, raw_spacing, stop, done_text = (
    Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
)
n, spacing = int(raw_n), float(raw_spacing)
stopping = False

def request_stop(_signum, _frame):
    global stopping
    stopping = True

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
releases = []
if spacing == 0:
    (root / "release.all").touch()
    releases.append(time.monotonic_ns())
else:
    for index in range(1, n + 1):
        if stopping or stop.exists():
            break
        (root / f"release.{index}").touch()
        releases.append(time.monotonic_ns())
        if index < n:
            deadline = time.monotonic() + spacing
            while not stopping and not stop.exists() and time.monotonic() < deadline:
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
pending = done_text.with_name(f".{done_text.name}.{os.getpid()}.tmp")
pending.write_text(
    json.dumps(
        {
            "release_ns": releases,
            "spacing": spacing,
            "spawn_guard": "standard-python-process-apis",
        },
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
if len(releases) == (1 if spacing == 0 else n):
    os.replace(pending, done_text)
elif pending.exists():
    pending.unlink()
while not stopping:
    time.sleep(0.05)
PY
    ;;

  __controller)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, subprocess, sys, time, uuid
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test workers forbid descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

skill_dir, root_text, label, ready_text, stop_text = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat
import goalflight_journal as journal
import goalflight_session_status as sessions
import goalflight_task
import goalflight_wake as wake

# The owned project root is deliberately not a git checkout. Avoid the normal
# synchronous git probe; returning None is exactly that probe's result here.
goalflight_task._git_canonical_root = lambda _start: None

root, ready, stop = Path(root_text), Path(ready_text), Path(stop_text)
stopping = False

def request_stop(_signum, _frame):
    global stopping
    stopping = True

def atomic_json(path, value):
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, path)

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
nonce = uuid.uuid4().hex
record = sessions.claim_session(
    root,
    pid=os.getpid(),
    session_id=nonce,
    label=label,
    hold_lock=False,
)
if record.get("lease_nonce") != nonce:
    raise RuntimeError("controller lease did not retain its requested nonce")
holder = wake.register_lease_holder(
    root,
    controller_label=label,
    lease_nonce=nonce,
)
try:
    wake.publish_lease_generation_event(
        root,
        controller_label=label,
        lease_nonce=nonce,
        generation=int(record["generation"]),
        state=journal.LEASE_ACTIVE,
    )
    live = sessions.live_session(root, label=label, pid=os.getpid())
    if not live or live.get("lease_nonce") != nonce:
        raise RuntimeError("controller lease holder did not become live")
    identity = compat.process_start_identity(os.getpid())
    if not identity or not identity.get("start_token"):
        raise RuntimeError("controller process identity is unavailable")
    atomic_json(
        ready,
        {
            "role": "controller-holder",
            "pid": os.getpid(),
            "start_token": identity["start_token"],
            "lease_nonce": nonce,
            "journal_path": str(journal.resolve_journal_path(root)),
            "generation": int(record["generation"]),
            "spawn_guard": "standard-python-process-apis",
        },
    )
    while not stopping:
        time.sleep(0.05)
finally:
    holder.close()
PY
    ;;

  __writer)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, struct, subprocess, sys, time
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test workers forbid descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

skill_dir, root_text, label, raw_index, stop_text, witness_text, raw_pace = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat
import goalflight_journal as journal
import goalflight_messages as messages
import goalflight_task

goalflight_task._git_canonical_root = lambda _start: None

root, stop, witness = Path(root_text), Path(stop_text), Path(witness_text)
index, pace = int(raw_index), float(raw_pace)
operation_root = (
    root / "fault-wrong-project"
    if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "generator-path"
    else root
)
journal_path = journal.resolve_journal_path(operation_root)
identity = compat.process_start_identity(os.getpid())
if not identity or not identity.get("start_token"):
    raise RuntimeError("writer process identity is unavailable")
stopping = False
successes = failures = 0
last_error = ""
first_success_ns = last_success_ns = None
operation_log = root / f"generator.writer.{index}.operations.bin"

def request_stop(_signum, _frame):
    global stopping
    stopping = True

def publish():
    value = {
        "role": "writer",
        "index": index,
        "stream_id": f"listener-load-w{index}",
        "pid": os.getpid(),
        "start_token": identity["start_token"],
        "journal_path": str(journal_path),
        "committed_operations": successes,
        "first_success_ns": first_success_ns,
        "last_success_ns": last_success_ns,
        "operation_log": str(operation_log),
        "failures": failures,
        "last_error": last_error[-240:],
        "stop_file_observed": stop.exists(),
        "spawn_guard": "standard-python-process-apis",
    }
    pending = witness.with_name(f".{witness.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, witness)

def append_operation(sequence, completed_ns):
    payload = struct.pack("!QQ", sequence, completed_ns)
    fd = os.open(operation_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short generator operation-log write")
    finally:
        os.close(fd)

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
operation_log.write_bytes(b"")
publish()
while not stopping and not stop.exists():
    fault = os.environ.get("GOALFLIGHT_LOADTEST_FAULT")
    if (
        fault in {
            "generator-pause-in-window",
            "generator-after-arm-only",
            "generator-pre-attempt-only",
        }
        and (root / "PAUSE_GENERATORS").exists()
        and not (
            (fault == "generator-after-arm-only" and (root / "ALLOW_POST_ARM_WORK").exists())
            or (fault == "generator-pre-attempt-only" and (root / "ALLOW_PRE_ATTEMPT_WORK").exists())
        )
    ):
        time.sleep(min(0.02, pace))
        continue
    try:
        if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "generator-spawn":
            os.fork()
        result = messages.post_message(
            dispatch_id=f"listener-load-w{index}",
            msg_type="note",
            payload={
                "text": f"synthetic listener load writer={index} operation={successes + failures + 1}",
                "project_root": str(operation_root),
            },
            messages_dir=Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "local", "adapter": "loadtest", "transport": "controller"},
        )
        delivery = result.get("controller_delivery")
        if messages.post_result_is_error(result) or not isinstance(delivery, dict):
            raise RuntimeError("journal delivery was not committed")
        delivered_root = Path(str(delivery.get("project_root") or "")).resolve(strict=False)
        if not delivery.get("delivered") or delivered_root != operation_root.resolve(strict=False):
            raise RuntimeError(
                f"journal delivery target mismatch: delivered={delivered_root} expected={operation_root}"
            )
        successes += 1
        now_ns = time.time_ns()
        if first_success_ns is None:
            first_success_ns = now_ns
        last_success_ns = now_ns
        last_error = ""
        append_operation(successes, time.monotonic_ns())
        if fault == "generator-after-arm-only" and (root / "ALLOW_POST_ARM_WORK").exists():
            (root / f"generator.writer.{index}.post-arm-success").touch()
        if fault == "generator-pre-attempt-only" and (root / "ALLOW_PRE_ATTEMPT_WORK").exists():
            (root / f"generator.writer.{index}.pre-attempt-success").touch()
    except Exception as exc:
        failures += 1
        last_error = f"{type(exc).__name__}: {exc}"
    publish()
    deadline = time.monotonic() + pace
    while not stopping and not stop.exists() and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
publish()
while not stopping:
    time.sleep(0.05)
PY
    ;;

  __reader)
    shift
    exec python3 - "$@" <<'PY'
import json, os, signal, struct, subprocess, sys, time
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test workers forbid descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

skill_dir, root_text, label, nonce, raw_index, stop_text, witness_text, raw_pace = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat
import goalflight_journal as journal
import goalflight_task

goalflight_task._git_canonical_root = lambda _start: None

root, stop, witness = Path(root_text), Path(stop_text), Path(witness_text)
index, pace = int(raw_index), float(raw_pace)
operation_root = (
    root / "fault-wrong-project"
    if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "generator-path"
    else root
)
journal_path = journal.resolve_journal_path(operation_root)
identity = compat.process_start_identity(os.getpid())
if not identity or not identity.get("start_token"):
    raise RuntimeError("reader process identity is unavailable")
stopping = False
successes = failures = 0
last_error = ""
first_success_ns = last_success_ns = None
operation_log = root / f"generator.reader.{index}.operations.bin"

def request_stop(_signum, _frame):
    global stopping
    stopping = True

def publish():
    value = {
        "role": "reader",
        "index": index,
        "pid": os.getpid(),
        "start_token": identity["start_token"],
        "journal_path": str(journal_path),
        "successful_operations": successes,
        "first_success_ns": first_success_ns,
        "last_success_ns": last_success_ns,
        "operation_log": str(operation_log),
        "failures": failures,
        "last_error": last_error[-240:],
        "stop_file_observed": stop.exists(),
        "spawn_guard": "standard-python-process-apis",
    }
    pending = witness.with_name(f".{witness.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, witness)

def append_operation(sequence, completed_ns):
    payload = struct.pack("!QQ", sequence, completed_ns)
    fd = os.open(operation_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short generator operation-log write")
    finally:
        os.close(fd)

signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
operation_log.write_bytes(b"")
publish()
while not stopping and not stop.exists():
    fault = os.environ.get("GOALFLIGHT_LOADTEST_FAULT")
    if (
        fault in {
            "generator-pause-in-window",
            "generator-after-arm-only",
            "generator-pre-attempt-only",
        }
        and (root / "PAUSE_GENERATORS").exists()
        and not (
            (fault == "generator-after-arm-only" and (root / "ALLOW_POST_ARM_WORK").exists())
            or (fault == "generator-pre-attempt-only" and (root / "ALLOW_PRE_ATTEMPT_WORK").exists())
        )
    ):
        time.sleep(min(0.02, pace))
        continue
    try:
        authority = journal.Journal.open_reader(operation_root)
        authority.cursor_peek(label, nonce=nonce, limit=1000)
        successes += 1
        now_ns = time.time_ns()
        if first_success_ns is None:
            first_success_ns = now_ns
        last_success_ns = now_ns
        last_error = ""
        append_operation(successes, time.monotonic_ns())
        if fault == "generator-after-arm-only" and (root / "ALLOW_POST_ARM_WORK").exists():
            (root / f"generator.reader.{index}.post-arm-success").touch()
        if fault == "generator-pre-attempt-only" and (root / "ALLOW_PRE_ATTEMPT_WORK").exists():
            (root / f"generator.reader.{index}.pre-attempt-success").touch()
    except Exception as exc:
        failures += 1
        last_error = f"{type(exc).__name__}: {exc}"
    publish()
    deadline = time.monotonic() + pace
    while not stopping and not stop.exists() and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
publish()
while not stopping:
    time.sleep(0.05)
PY
    ;;

  __listener)
    shift
    exec python3 - "$@" <<'PY'
import json, os, subprocess, sys, time, traceback
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test workers forbid descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

(
    skill_dir, root_text, label, nonce, raw_slots, raw_index,
    ready_text, release_text, release_all_text, abort_text,
    identity_text, attempt_text, rc_text, raw_timeout,
) = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]

# Import before READY. The next product call is command parsing followed by
# _resolve_listen_auto_lease(), the listener's first journal operation.
import goalflight_compat as compat
import goalflight_journal as journal
import goalflight_messages as messages
import goalflight_task

goalflight_task._git_canonical_root = lambda _start: None

ready = Path(ready_text)
release = Path(release_text)
release_all = Path(release_all_text)
abort = Path(abort_text)
identity_path = Path(identity_text)
attempt_path = Path(attempt_text)
rc_path = Path(rc_text)

def atomic(path, value):
    pending = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(pending, path)

def generator_snapshot(path, boundary, *, arm_committed=None):
    captured_ns = time.monotonic_ns()
    value = {
        "state": "ready",
        "boundary": boundary,
        "captured_ns": captured_ns,
        "counter_boundary": "monotonic-operation-log-v1",
        "listener_index": int(raw_index),
    }
    if arm_committed is not None:
        value["arm_committed"] = bool(arm_committed)
    atomic(path, value)

identity = compat.process_start_identity(os.getpid())
if not identity or not identity.get("start_token"):
    raise RuntimeError("listener process identity is unavailable")
atomic(
    identity_path,
    {
        "index": int(raw_index),
        "pid": os.getpid(),
        "start_token": identity["start_token"],
        "spawn_guard": "standard-python-process-apis",
    },
)
rc = 98
gate_entered = False
arm_end_path = Path(root_text) / f"listener.{raw_index}.window-end.json"
arm_transaction_active = False
arm_transaction_boundary_ns = None
try:
    original_resolver = messages._resolve_listen_auto_lease
    original_arm_listener = journal.Journal.arm_listener
    original_domain_write = journal.Journal._domain_write

    def witnessed_domain_write(authority, action):
        global arm_transaction_boundary_ns
        if not arm_transaction_active:
            return original_domain_write(authority, action)

        def action_with_boundary(connection):
            global arm_transaction_boundary_ns
            value = action(connection)
            # One monotonic clock read after the coverage INSERT is the only
            # in-transaction instrumentation. All operation-log I/O and
            # analysis happen after production commits.
            arm_transaction_boundary_ns = time.monotonic_ns()
            return value

        return original_domain_write(authority, action_with_boundary)

    def witnessed_arm_listener(authority, *args, **kwargs):
        global arm_transaction_active, arm_transaction_boundary_ns

        def publish_arm_end(committed):
            captured_ns = arm_transaction_boundary_ns
            if captured_ns is None:
                if committed:
                    raise RuntimeError("committed arm has no in-transaction load boundary")
                # No coverage action ran. Sample immediately after the failed
                # production call so a genuine pre-arm failure still has an
                # explicit load-window boundary without becoming arm success.
                captured_ns = time.monotonic_ns()
            atomic(
                arm_end_path,
                {
                    "state": "ready",
                    "boundary": "end",
                    "captured_ns": captured_ns,
                    "counter_boundary": "monotonic-operation-log-v1",
                    "listener_index": int(raw_index),
                    "arm_committed": committed,
                },
            )

        arm_transaction_boundary_ns = None
        arm_transaction_active = True
        try:
            if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "listener-pre-arm-error":
                raise RuntimeError("injected listener failure before arm")
            result = original_arm_listener(authority, *args, **kwargs)
        except BaseException:
            arm_transaction_active = False
            publish_arm_end(False)
            raise
        arm_transaction_active = False
        committed = bool(result.committed and result.value)
        publish_arm_end(committed)
        if committed and os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "listener-post-arm-error":
            raise RuntimeError("injected listener failure after arm")
        return result

    def gated_resolver(*args, **kwargs):
        global gate_entered
        if not gate_entered:
            # _run_cli has completed parsing and listen setup. This resolver is
            # the first production operation that can touch the journal/lease.
            atomic(ready, {"index": int(raw_index), "ready_ns": time.time_ns()})
            while not release.exists() and not release_all.exists():
                if abort.exists():
                    raise RuntimeError("listener barrier aborted")
                time.sleep(0.002)
            start_path = Path(root_text) / "generator.listener-window-start.json"
            claim_path = Path(root_text) / "generator.listener-window-start.claim"
            try:
                claim_fd = os.open(
                    claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except FileExistsError:
                claim_fd = None
            if claim_fd is not None:
                os.close(claim_fd)
                generator_snapshot(start_path, "start")
            else:
                while not start_path.exists():
                    if abort.exists():
                        raise RuntimeError("listener start-snapshot barrier aborted")
                    time.sleep(0.002)
            if os.environ.get("GOALFLIGHT_LOADTEST_FAULT") == "generator-pre-attempt-only":
                done_path = Path(root_text) / "PRE_ATTEMPT_WORK_DONE"
                if claim_fd is not None:
                    expected = int(
                        (Path(root_text) / "PRE_ATTEMPT_EXPECTED").read_text(encoding="utf-8")
                    )
                    (Path(root_text) / "ALLOW_PRE_ATTEMPT_WORK").touch()
                    deadline = time.monotonic() + 5.0
                    while len(list(Path(root_text).glob("generator.*.pre-attempt-success"))) < expected:
                        if abort.exists() or time.monotonic() >= deadline:
                            raise RuntimeError("pre-attempt-only work witness timeout")
                        time.sleep(0.002)
                    (Path(root_text) / "ALLOW_PRE_ATTEMPT_WORK").unlink()
                    done_path.touch()
                else:
                    while not done_path.exists():
                        if abort.exists():
                            raise RuntimeError("pre-attempt-only barrier aborted")
                        time.sleep(0.002)
            attempt_ns = time.monotonic_ns()
            gate_entered = True
            try:
                return original_resolver(*args, **kwargs)
            finally:
                # Persist after the call so file I/O cannot open a pre-call
                # qualification gap. The saved value is the call boundary.
                atomic(
                    attempt_path,
                    {"index": int(raw_index), "journal_attempt_ns": attempt_ns},
                )
        return original_resolver(*args, **kwargs)

    messages._resolve_listen_auto_lease = gated_resolver
    journal.Journal._domain_write = witnessed_domain_write
    journal.Journal.arm_listener = witnessed_arm_listener
    os.environ.pop("GOALFLIGHT_DISPATCH_ID", None)
    os.environ["GOALFLIGHT_PROCESS_ROLE"] = "controller"
    rc = messages._run_cli(
        [
            "listen",
            "--project-root", root_text,
            "--controller-label", label,
            "--lease-nonce", nonce,
            "--listener-slots", raw_slots,
            "--report-pending",
            "--timeout-s", raw_timeout,
            "--json",
        ]
    )
except BaseException as exc:
    print(f"listener worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    rc = 97
finally:
    if gate_entered and not arm_end_path.exists():
        try:
            # An unarmed startup returns promptly. Its terminal snapshot bounds
            # the failed arm attempt without extending a successful arm through
            # the listener's post-arm wait.
            generator_snapshot(arm_end_path, "end", arm_committed=False)
        except BaseException as exc:
            atomic(
                arm_end_path,
                {
                    "state": "fatal",
                    "boundary": "end",
                    "listener_index": int(raw_index),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
    atomic(rc_path, {"index": int(raw_index), "rc": int(rc)})
raise SystemExit(rc)
PY
    ;;

  __generator_check)
    shift
    exec python3 - "$@" <<'PY'
import json, sys
from pathlib import Path

root, expected_text, raw_minimum, raw_writers, raw_readers = sys.argv[1:]
root, expected = Path(root), Path(expected_text).resolve(strict=False)
minimum, writers, readers = int(raw_minimum), int(raw_writers), int(raw_readers)
items = []
fatal = []
for role, count in (("writer", writers), ("reader", readers)):
    for index in range(1, count + 1):
        path = root / f"generator.{role}.{index}.json"
        if not path.exists():
            items.append({"role": role, "index": index, "state": "missing"})
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            items.append({"role": role, "index": index, "state": "unreadable", "error": str(exc)})
            continue
        actual = Path(str(item.get("journal_path") or "")).resolve(strict=False)
        item["path_matches"] = actual == expected
        if actual != expected:
            fatal.append(f"{role}-{index}:journal_path={actual}")
        key = "committed_operations" if role == "writer" else "successful_operations"
        item["work_count"] = int(item.get(key) or 0)
        item["state"] = "ready" if item["work_count"] >= minimum else "warming"
        items.append(item)
state = "fatal" if fatal else (
    "ready" if len(items) == writers + readers and all(x.get("state") == "ready" for x in items)
    else "pending"
)
print(json.dumps({"state": state, "expected_journal": str(expected), "items": items, "fatal": fatal}, sort_keys=True))
raise SystemExit(2 if fatal else 0 if state == "ready" else 1)
PY
    ;;

  __generator_report)
    shift
    exec python3 - "$@" <<'PY'
import json, sys
from pathlib import Path

root, raw_writers, raw_readers = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
for role, count in (("writer", raw_writers), ("reader", raw_readers)):
    for index in range(1, count + 1):
        item = json.loads((root / f"generator.{role}.{index}.json").read_text(encoding="utf-8"))
        key = "committed_operations" if role == "writer" else "successful_operations"
        print(
            f"WORK_WITNESS generator={role}-{index} journal={item['journal_path']} "
            f"{key}={int(item.get(key) or 0)} failures={int(item.get('failures') or 0)}"
        )
PY
    ;;

  __generator_stop_check)
    shift
    exec python3 - "$@" <<'PY'
import json, sys
from pathlib import Path

root, writers, readers = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
items = []
try:
    for role, count in (("writer", writers), ("reader", readers)):
        for index in range(1, count + 1):
            value = json.loads(
                (root / f"generator.{role}.{index}.json").read_text(encoding="utf-8")
            )
            items.append(
                {
                    "role": role,
                    "index": index,
                    "stop_file_observed": value.get("stop_file_observed") is True,
                }
            )
except (OSError, json.JSONDecodeError) as exc:
    print(json.dumps({"state": "fatal", "error": str(exc), "items": items}, sort_keys=True))
    raise SystemExit(2)
ready = len(items) == writers + readers and all(item["stop_file_observed"] for item in items)
print(json.dumps({"state": "ready" if ready else "pending", "items": items}, sort_keys=True))
raise SystemExit(0 if ready else 1)
PY
    ;;

  __cleanup)
    shift
    exec python3 - "$@" <<'PY'
import ctypes, json, os, signal, sys, time
from pathlib import Path

skill_dir, registry_text, snapshot_text, owned_text, *fault_args = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

registry, snapshot = Path(registry_text), Path(snapshot_text)
records = []
seen = set()
errors = []
force_kill_groups = set()
retired_groups = set()
reported_errors = set()
harness_pid = os.getppid()
helper_pid = os.getpid()
parent_owned_pids = {
    int(value) for value in owned_text.split() if value.isdigit() and int(value) > 0
}
fault = fault_args[0] if fault_args else ""
if fault not in {"", "direct-identity-unknown", "bootstrap-identity-unknown"}:
    raise SystemExit(f"unsupported cleanup fault: {fault}")
fault_pid = min(parent_owned_pids) if fault == "direct-identity-unknown" and parent_owned_pids else None

def record_error(value):
    if value not in reported_errors:
        errors.append(value)
        reported_errors.add(value)

def load_registry():
    try:
        lines = registry.read_text(encoding="utf-8").splitlines() if registry.exists() else ()
        for line in lines:
            item = json.loads(line)
            key = (int(item["pid"]), str(item["start_token"]))
            if key not in seen:
                records.append(item)
                seen.add(key)
            elif str(item.get("role") or "").startswith("launcher-supervisor-"):
                # Promote the parent-written, stopped-bootstrap record in
                # place. The exact generation is unchanged, but it is now the
                # persistent supervisor that cleanup may safely signal.
                existing = next(
                    candidate
                    for candidate in records
                    if (int(candidate["pid"]), str(candidate["start_token"])) == key
                )
                existing.update(item)
            if str(item.get("role") or "").startswith("launcher-supervisor-"):
                force_kill_groups.add(int(item.get("pgid") or 0))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        record_error(f"registry_unreadable:{type(exc).__name__}:{exc}")

load_registry()

def matches(item):
    if str(item.get("start_token") or "").startswith(("unavailable:", "pregroup:")):
        try:
            os.kill(int(item["pid"]), 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None
    state = compat.process_identity_matches(int(item["pid"]), str(item["start_token"]))
    if fault_pid == int(item["pid"]) and state is not False:
        try:
            os.kill(fault_pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        return None
    return state

def raw_process(pid):
    if sys.platform == "darwin":
        info = compat._darwin_process_bsd_info(pid)
        if info is None:
            return None
        return {
            "ppid": int(info["ppid"]),
            "pgid": int(info["pgid"]),
            "zombie": int(info["status"]) == 5,
            "stopped": int(info["status"]) == 4,
        }
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            return {
                "ppid": int(fields[1]),
                "pgid": int(fields[2]),
                "zombie": fields[0] in {"Z", "X"},
                "stopped": fields[0] in {"T", "t"},
            }
        except (IndexError, OSError, ValueError):
            return None
    return None

def pinned_direct(item):
    pid = int(item["pid"])
    pgid = int(item.get("pgid") or 0)
    if (
        pid not in parent_owned_pids
        or pid != pgid
        or str(item.get("role") or "").startswith("discovered-descendant-")
    ):
        return False
    raw = raw_process(pid)
    return bool(
        raw
        and raw["ppid"] == harness_pid
        and raw["pgid"] == pgid
        and not raw["zombie"]
    )

def pinned_bootstrap(item):
    pid = int(item["pid"])
    if (
        pid == helper_pid
        or pid not in parent_owned_pids
        or not str(item.get("role") or "").startswith("adopted-pregroup-launcher-")
    ):
        return False
    raw = raw_process(pid)
    return bool(raw and raw["ppid"] == harness_pid and not raw["zombie"])

def direct_anchor_valid(item):
    state = matches(item)
    retry_deadline = time.monotonic() + 0.25
    while state is None and time.monotonic() < retry_deadline:
        time.sleep(0.002)
        state = matches(item)
    if state is True:
        return True
    if state is None and pinned_direct(item):
        pid = int(item["pid"])
        pgid = int(item.get("pgid") or 0)
        record_error(f"direct_identity_unavailable:pid={pid}:pgid={pgid}")
        force_kill_groups.add(pgid)
        return True
    return False

def running(item):
    state = matches(item)
    alive = state is True or (
        state is None and (pinned_direct(item) or pinned_bootstrap(item))
    )
    return alive and compat.pid_is_zombie(int(item["pid"])) is not True

def process_ids():
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        libproc.proc_listallpids.restype = ctypes.c_int
        count = libproc.proc_listallpids(None, 0)
        if count <= 0:
            raise OSError(ctypes.get_errno(), "proc_listallpids sizing failed")
        values = (ctypes.c_int * (count + 64))()
        found = libproc.proc_listallpids(values, ctypes.sizeof(values))
        if found < 0:
            raise OSError(ctypes.get_errno(), "proc_listallpids failed")
        return [int(values[index]) for index in range(found) if int(values[index]) > 0]
    if sys.platform.startswith("linux"):
        return [int(path.name) for path in Path("/proc").iterdir() if path.name.isdigit()]
    raise RuntimeError(f"process inventory is unsupported on {sys.platform}")

# Direct leaders are deliberately left unreaped until this helper exits. Their
# exact PID/start-token identities pin their process-group IDs against reuse,
# allowing one group signal to avoid a compare-then-kill race. Inventory also
# follows the harness's live ancestry so a descendant that creates a new
# process group/session is brought under an independently pinned group.
def discover_owned():
    # A launcher may finish repository imports and append its registry record
    # after cleanup starts. Merge the append-only registry on every inventory
    # pass so that bootstrap window cannot escape the later signal sweeps.
    load_registry()
    try:
        inventory = process_ids()
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"process inventory failed: {exc}") from exc
    snapshots = {}
    for pid in inventory:
        if pid == helper_pid:
            continue
        identity = (
            None
            if fault == "bootstrap-identity-unknown" and pid in parent_owned_pids
            else compat.process_start_identity(pid, include_ancestry=True)
        )
        if identity and identity.get("start_token"):
            snapshots[pid] = identity

    # Before a launcher's durable append, its shell-owned PID plus live parent
    # relationship and PID-named process group safely pin the generation. Adopt
    # it as a direct anchor; a reused/unrelated PID cannot satisfy that tuple.
    for pid in parent_owned_pids:
        if pid == helper_pid:
            continue
        identity = snapshots.get(pid)
        raw = raw_process(pid)
        if not raw or raw["ppid"] != harness_pid or raw["zombie"]:
            continue
        if raw["pgid"] != pid:
            start_token = f"pregroup:{pid}"
            generation = (pid, start_token)
            if generation not in seen:
                records.append(
                    {
                        "role": f"adopted-pregroup-launcher-of-{harness_pid}",
                        "pid": pid,
                        "start_token": start_token,
                        "pgid": raw["pgid"],
                    }
                )
                seen.add(generation)
                record_error(
                    f"bootstrap_before_process_group:pid={pid}:pgid={raw['pgid']}"
                )
            continue
        if any(
            int(item["pid"]) == pid and int(item.get("pgid") or 0) == pid
            for item in records
        ):
            continue
        start_token = (
            str(identity["start_token"])
            if identity and int(identity.get("ppid") or 0) == harness_pid
            else f"unavailable:{pid}"
        )
        generation = (pid, start_token)
        if generation not in seen:
            records.append(
                {
                    "role": (
                        f"adopted-direct-launcher-of-{harness_pid}"
                        if identity
                        else f"adopted-unknown-launcher-of-{harness_pid}"
                    ),
                    "pid": pid,
                    "start_token": start_token,
                    "pgid": pid,
                }
            )
            seen.add(generation)
            if not identity:
                record_error(f"bootstrap_identity_unavailable:pid={pid}:pgid={pid}")

    owned_pids = {
        int(item["pid"])
        for item in records
        if matches(item) is True or pinned_direct(item) or pinned_bootstrap(item)
    }
    changed = True
    while changed:
        changed = False
        for pid, identity in snapshots.items():
            if pid in owned_pids:
                continue
            if int(identity.get("ppid") or 0) == harness_pid or int(identity.get("ppid") or 0) in owned_pids:
                owned_pids.add(pid)
                changed = True

    active_groups = {
        int(item.get("pgid") or 0)
        for item in records
        if (matches(item) is True or pinned_direct(item))
        and int(item.get("pgid") or 0) > 0
    }
    # A validated direct leader remains an unreaped child of the harness after
    # SIGKILL, so its PID still pins the numeric process-group generation. Keep
    # inventorying that retired group until every already-signalled member is
    # gone; this also catches a fork that completed at the STOP boundary.
    active_groups.update(retired_groups)
    for pid in inventory:
        try:
            pgid = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            continue
        if pid not in owned_pids and pgid not in active_groups:
            continue
        identity = snapshots.get(pid)
        if not identity or not identity.get("start_token"):
            record_error(f"identity_unavailable:pid={pid}:pgid={pgid}")
            if pgid in active_groups:
                force_kill_groups.add(pgid)
            continue
        generation = (pid, str(identity["start_token"]))
        if generation in seen:
            continue
        item = {
            "role": f"discovered-descendant-of-{harness_pid}",
            "pid": pid,
            "start_token": identity["start_token"],
            "pgid": pgid,
        }
        records.append(item)
        seen.add(generation)
    return owned_pids

def anchored_groups():
    groups = {}
    for item in records:
        role = str(item.get("role") or "")
        if role.startswith(
            (
                "launcher-bootstrap-",
                "adopted-pregroup-launcher-",
                "adopted-direct-launcher-",
                "adopted-unknown-launcher-",
            )
        ):
            continue
        pgid = int(item.get("pgid") or 0)
        if pgid <= 0 or pgid in groups:
            continue
        anchor = next(
            (
                candidate
                for candidate in records
                if not str(candidate.get("role") or "").startswith("discovered-descendant-")
                and int(candidate["pid"]) == pgid
                and int(candidate.get("pgid") or 0) == pgid
                and direct_anchor_valid(candidate)
            ),
            None,
        )
        if anchor is None and pgid in retired_groups:
            continue
        if anchor is None and any(running(candidate) and int(candidate.get("pgid") or 0) == pgid for candidate in records):
            record_error(f"unanchored_process_group:{pgid}")
        elif anchor is not None:
            groups[pgid] = anchor
    return groups

def signal_bootstrap_individuals():
    for item in records:
        if not pinned_bootstrap(item):
            continue
        pid = int(item["pid"])
        try:
            # Freeze the exact direct child first. It cannot fork before its
            # setpgid call; if it already advanced, the persistent supervisor
            # remains the live group anchor. Re-reading after STOP therefore
            # chooses safely between one pre-group PID and its complete group.
            os.kill(pid, signal.SIGSTOP)
            raw = None
            stop_deadline = time.monotonic() + 0.25
            while time.monotonic() < stop_deadline:
                raw = raw_process(pid)
                if raw is None or raw["zombie"] or raw.get("stopped"):
                    break
                time.sleep(0.002)
            if raw is None or raw["zombie"]:
                continue
            if not raw.get("stopped"):
                record_error(f"bootstrap_stop_unconfirmed:pid={pid}")
            if raw["pgid"] == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            record_error(f"bootstrap_kill_failed:pid={pid}:{exc}")

def signal_groups(sig, only=None):
    for pgid, anchor in anchored_groups().items():
        if only is not None and pgid not in only:
            continue
        # Revalidate the unreaped group leader immediately before killpg.
        anchor_state = matches(anchor)
        retry_deadline = time.monotonic() + 0.25
        while anchor_state is None and time.monotonic() < retry_deadline:
            time.sleep(0.002)
            anchor_state = matches(anchor)
        if anchor_state is False:
            # The direct leader can acknowledge STOP and exit between group
            # enumeration and this check. With no live tracked member, absence
            # is proof that there is no longer a harness group to signal.
            if any(
                running(candidate) and int(candidate.get("pgid") or 0) == pgid
                for candidate in records
            ):
                record_error(f"group_anchor_lost_with_live_member:pgid={pgid}")
            continue
        if anchor_state is not True and not pinned_direct(anchor):
            record_error(f"group_anchor_changed:pgid={pgid}")
            continue
        if anchor_state is None:
            record_error(f"direct_identity_unavailable:pid={anchor['pid']}:pgid={pgid}")
            force_kill_groups.add(pgid)
        try:
            if sig == signal.SIGKILL:
                # Freeze the complete group before retiring its persistent
                # leader. Either an in-flight fork precedes this group STOP and
                # joins the stopped group, or the stopped leader cannot fork.
                os.killpg(pgid, signal.SIGSTOP)
                stop_deadline = time.monotonic() + 0.25
                raw = None
                while time.monotonic() < stop_deadline:
                    raw = raw_process(int(anchor["pid"]))
                    if raw is None or raw["zombie"] or raw.get("stopped"):
                        break
                    time.sleep(0.002)
                if not raw or raw["zombie"] or not raw.get("stopped"):
                    record_error(f"group_stop_unconfirmed:pgid={pgid}")
            os.killpg(pgid, sig)
            if sig == signal.SIGKILL:
                retired_groups.add(pgid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            record_error(f"killpg_failed:pgid={pgid}:signal={sig}:{exc}")

inventory_ok = True
try:
    discover_owned()
except RuntimeError as exc:
    record_error(str(exc))
    inventory_ok = False
signal_bootstrap_individuals()
signal_groups(signal.SIGKILL, force_kill_groups)
signal_groups(signal.SIGTERM)

deadline = time.monotonic() + 5.0
stable = 0
while inventory_ok and time.monotonic() < deadline:
    try:
        before = len(records)
        discover_owned()
    except RuntimeError as exc:
        record_error(str(exc))
        inventory_ok = False
        break
    signal_bootstrap_individuals()
    signal_groups(signal.SIGKILL, force_kill_groups)
    signal_groups(signal.SIGTERM)
    live = any(running(item) for item in records)
    if not live and len(records) == before:
        stable += 1
        if stable >= 2:
            break
    else:
        stable = 0
    time.sleep(0.02)

if any(running(item) for item in records):
    signal_bootstrap_individuals()
    signal_groups(signal.SIGKILL)
    kill_deadline = time.monotonic() + 3.0
    stable = 0
    while inventory_ok and time.monotonic() < kill_deadline:
        try:
            before = len(records)
            discover_owned()
        except RuntimeError as exc:
            record_error(str(exc))
            inventory_ok = False
            break
        signal_bootstrap_individuals()
        signal_groups(signal.SIGKILL)
        live = any(running(item) for item in records)
        if not live and len(records) == before:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        time.sleep(0.02)

for item in records:
    if matches(item) is None:
        record_error(f"identity_reconciliation_unknown:pid={item['pid']}")
snapshot.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in records), encoding="utf-8")
print(json.dumps({"tracked_identities": len(records), "cleanup_errors": errors}, sort_keys=True))
raise SystemExit(0 if inventory_ok and not errors else 3)
PY
    ;;

  __reconcile)
    shift
    exec python3 - "$@" <<'PY'
import json, sys, time
from pathlib import Path

skill_dir, snapshot_text = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_compat as compat

records = [
    json.loads(line)
    for line in Path(snapshot_text).read_text(encoding="utf-8").splitlines()
    if line.strip()
]
deadline = time.monotonic() + 3.0
states = []
while True:
    states = [
        compat.process_identity_matches(int(item["pid"]), str(item["start_token"]))
        for item in records
    ]
    if not any(state is True for state in states) or time.monotonic() >= deadline:
        break
    time.sleep(0.02)
survivors = [records[i] for i, state in enumerate(states) if state is True]
unknown = [records[i] for i, state in enumerate(states) if state is None]
print(
    f"CLEANUP identity_reconciled={len(records)} survivors={len(survivors)} "
    f"unknown={len(unknown)}"
)
if survivors:
    print("CLEANUP_SURVIVORS " + json.dumps(survivors, sort_keys=True))
if unknown:
    print("CLEANUP_UNKNOWN " + json.dumps(unknown, sort_keys=True))
raise SystemExit(0 if not survivors and not unknown else 3)
PY
    ;;

  __analyse)
    shift
    exec python3 - "$@" <<'PY'
import collections, json, os, sqlite3, struct, subprocess, sys
from pathlib import Path

def refuse_spawn(*_args, **_kwargs):
    marker_root = os.environ.get("GOALFLIGHT_LOADTEST_ROOT")
    if marker_root:
        marker = Path(marker_root) / "SPAWN_ATTEMPTED"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"pid={os.getpid()}\n".encode())
        finally:
            os.close(fd)
    raise RuntimeError("listener load-test analysis forbids descendant process creation")

class RefusingPopen:
    def __init__(self, *_args, **_kwargs):
        refuse_spawn()

subprocess.Popen = RefusingPopen
for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system"):
    if hasattr(os, name):
        setattr(os, name, refuse_spawn)

(
    skill_dir, root_text, expected_text, raw_n, spacing, tag,
    raw_writers, raw_readers, raw_minimum, raw_window_minimum, nonce, label,
    wall_start, wall_end, load_before, load_after,
    code_hash, code_revision, code_tree_state, code_file_count,
    harness_hash, harness_tree_state,
    runtime_info, verify_only, load_pace, warmup_timeout, ready_timeout,
    listener_timeout, cell_timeout, release_timeout,
) = sys.argv[1:]
sys.path[:0] = [str(Path(skill_dir) / "scripts"), skill_dir]
import goalflight_journal as journal
import goalflight_task

goalflight_task._git_canonical_root = lambda _start: None

root, expected = Path(root_text), Path(expected_text).resolve(strict=False)
n, writers, readers = int(raw_n), int(raw_writers), int(raw_readers)
minimum = int(raw_minimum)
window_minimum = int(raw_window_minimum)
try:
    spawn_attempt = root / "SPAWN_ATTEMPTED"
    if spawn_attempt.exists():
        detail = spawn_attempt.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"descendant process creation was attempted: {detail}")
    supervisor_error = root / "SUPERVISOR_ERROR"
    if supervisor_error.exists():
        detail = supervisor_error.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"launcher supervisor error: {detail}")
    resolved = journal.resolve_journal_path(root)
    if resolved != expected:
        raise RuntimeError(f"listener journal resolved to {resolved}, expected {expected}")
    authority = journal.Journal.open_reader(root)
    if authority.path != expected:
        raise RuntimeError(f"opened journal {authority.path}, expected {expected}")

    controller = json.loads((root / "controller.ready.json").read_text(encoding="utf-8"))
    if controller.get("spawn_guard") != "standard-python-process-apis":
        raise RuntimeError("controller descendant-spawn guard is unproved")

    window_start = json.loads(
        (root / "generator.listener-window-start.json").read_text(encoding="utf-8")
    )
    if window_start.get("state") != "ready" or window_start.get("boundary") != "start":
        raise RuntimeError("listener-window start snapshot is invalid")
    if window_start.get("counter_boundary") != "monotonic-operation-log-v1":
        raise RuntimeError("listener-window start counter boundary is unproved")
    end_snapshots = []
    arm_committed_by_listener = {}
    for listener_index in range(1, n + 1):
        end_snapshot = json.loads(
            (root / f"listener.{listener_index}.window-end.json").read_text(encoding="utf-8")
        )
        if end_snapshot.get("state") != "ready" or end_snapshot.get("boundary") != "end":
            raise RuntimeError(f"listener-{listener_index} end snapshot is invalid")
        if end_snapshot.get("counter_boundary") != "monotonic-operation-log-v1":
            raise RuntimeError(f"listener-{listener_index} end counter boundary is unproved")
        if int(end_snapshot.get("listener_index") or 0) != listener_index:
            raise RuntimeError(f"listener-{listener_index} end snapshot identity is invalid")
        if not isinstance(end_snapshot.get("arm_committed"), bool):
            raise RuntimeError(f"listener-{listener_index} arm boundary is unproved")
        arm_committed_by_listener[listener_index] = end_snapshot["arm_committed"]
        end_snapshots.append(end_snapshot)
    window_end = max(end_snapshots, key=lambda item: int(item["captured_ns"]))
    window_start_ns = int(window_start["captured_ns"])
    window_end_ns = int(window_end["captured_ns"])
    if window_end_ns < window_start_ns:
        raise RuntimeError("listener-window timestamps are reversed")

    attempts_by_listener = {}
    for listener_index in range(1, n + 1):
        attempt = json.loads(
            (root / f"listener.{listener_index}.attempt.json").read_text(encoding="utf-8")
        )
        if int(attempt.get("index") or 0) != listener_index:
            raise RuntimeError(f"listener-{listener_index} journal-attempt identity is invalid")
        attempts_by_listener[listener_index] = int(attempt["journal_attempt_ns"])
    attempts = list(attempts_by_listener.values())
    if len(attempts) != n:
        raise RuntimeError("journal-attempt timestamps are incomplete")
    attempt_floor_ns = min(attempts)
    if window_start_ns > attempt_floor_ns:
        raise RuntimeError("generator start snapshot follows the first journal attempt")
    if window_end_ns < max(attempts):
        raise RuntimeError("generator end snapshot precedes a journal attempt")

    generator_details = []
    for role, count in (("writer", writers), ("reader", readers)):
        for index in range(1, count + 1):
            item = json.loads((root / f"generator.{role}.{index}.json").read_text(encoding="utf-8"))
            actual = Path(str(item.get("journal_path") or "")).resolve(strict=False)
            if actual != expected:
                raise RuntimeError(f"{role}-{index} journal mismatch: {actual} != {expected}")
            if item.get("spawn_guard") != "standard-python-process-apis":
                raise RuntimeError(f"{role}-{index} descendant-spawn guard is unproved")
            key = "committed_operations" if role == "writer" else "successful_operations"
            count_value = int(item.get(key) or 0)
            if count_value < minimum:
                raise RuntimeError(
                    f"{role}-{index} work witness {count_value} is below minimum {minimum}"
                )
            expected_log = (root / f"generator.{role}.{index}.operations.bin").resolve()
            actual_log = Path(str(item.get("operation_log") or "")).resolve(strict=False)
            if actual_log != expected_log:
                raise RuntimeError(
                    f"{role}-{index} operation log mismatch: {actual_log} != {expected_log}"
                )
            log_data = expected_log.read_bytes()
            if len(log_data) % 16 != 0:
                raise RuntimeError(f"{role}-{index} operation log has a partial record")
            operation_records = list(struct.iter_unpack("!QQ", log_data))
            if len(operation_records) != count_value:
                raise RuntimeError(
                    f"{role}-{index} operation log count={len(operation_records)} "
                    f"witness={count_value}"
                )
            expected_sequences = list(range(1, count_value + 1))
            sequences = [int(sequence) for sequence, _completed_ns in operation_records]
            if sequences != expected_sequences:
                raise RuntimeError(f"{role}-{index} operation log sequence is invalid")
            completion_times = [int(completed_ns) for _sequence, completed_ns in operation_records]
            if completion_times != sorted(completion_times):
                raise RuntimeError(f"{role}-{index} operation times are nonmonotonic")
            # start_count excludes all operations that completed before the
            # first product resolver call; end_count excludes everything after
            # the last committed-arm (or failed-attempt) transaction boundary.
            start_count = sum(value < attempt_floor_ns for value in completion_times)
            end_count = sum(value <= window_end_ns for value in completion_times)
            if not start_count <= end_count <= count_value:
                raise RuntimeError(
                    f"{role}-{index} nonmonotonic counts start={start_count} "
                    f"end={end_count} final={count_value}"
                )
            window_delta = end_count - start_count
            if window_delta < window_minimum:
                raise RuntimeError(
                    f"{role}-{index} listener-window work delta {window_delta} "
                    f"is below minimum {window_minimum}"
                )
            if item.get("stop_file_observed") is not True:
                raise RuntimeError(f"{role}-{index} did not acknowledge the cell STOP file")
            detail = {
                "role": role,
                "index": index,
                "work_count": count_value,
                "window_start_count": start_count,
                "window_end_count": end_count,
                "window_delta": window_delta,
                "stop_ack": True,
            }
            if role == "writer":
                rows = authority.read_all(
                    "SELECT COUNT(*) AS count FROM delivery_events WHERE stream_id = ?",
                    (str(item["stream_id"]),),
                )
                committed_rows = int(rows[0]["count"])
                if committed_rows != count_value:
                    raise RuntimeError(
                        f"writer-{index} witness={count_value} journal_rows={committed_rows}"
                    )
                window_rows = authority.read_all(
                    """
                    SELECT COUNT(*) AS count
                    FROM delivery_events
                    WHERE stream_id = ? AND stream_seq > ? AND stream_seq <= ?
                    """,
                    (str(item["stream_id"]), start_count, end_count),
                )
                committed_window_rows = int(window_rows[0]["count"])
                if committed_window_rows != window_delta:
                    raise RuntimeError(
                        f"writer-{index} listener-window witness={window_delta} "
                        f"journal_rows={committed_window_rows}"
                    )
                detail["journal_rows"] = committed_rows
                detail["listener_window_rows"] = committed_window_rows
            generator_details.append(detail)

    coverage_rows = authority.read_all(
        """
        SELECT coverage_id, pid, start_token, armed_at, state, exit_reason
        FROM listener_coverage
        WHERE project_root = ? AND label = ? AND lease_nonce = ?
        """,
        (str(authority.project_root), label, nonce),
    )
    by_identity = collections.defaultdict(list)
    for row in coverage_rows:
        by_identity[(int(row["pid"]), str(row["start_token"]))].append(dict(row))

    armed = 0
    missing = []
    terminal = collections.Counter()
    for index in range(1, n + 1):
        identity_path = root / f"listener.{index}.identity.json"
        attempt_path = root / f"listener.{index}.attempt.json"
        rc_path = root / f"listener.{index}.rc.json"
        if not identity_path.exists() or not attempt_path.exists() or not rc_path.exists():
            raise RuntimeError(f"listener-{index} instrumentation witness is incomplete")
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("spawn_guard") != "standard-python-process-apis":
            raise RuntimeError(f"listener-{index} descendant-spawn guard is unproved")
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        if int(attempt.get("journal_attempt_ns") or 0) != attempts_by_listener[index]:
            raise RuntimeError(f"listener-{index} journal-attempt witness changed")
        rc = int(json.loads(rc_path.read_text(encoding="utf-8"))["rc"])
        terminal[str(rc)] += 1
        rows = by_identity[(int(identity["pid"]), str(identity["start_token"]))]
        positively_armed = len(rows) == 1 and bool(rows[0].get("armed_at"))
        if positively_armed != arm_committed_by_listener[index]:
            raise RuntimeError(
                f"listener-{index} arm callback and coverage witness disagree"
            )
        if positively_armed:
            armed += 1
        elif len(rows) > 1:
            raise RuntimeError(f"listener-{index} has {len(rows)} coverage rows")
        else:
            err_path = root / f"listener.{index}.err"
            last_error = ""
            if err_path.exists():
                lines = [line.strip() for line in err_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
                last_error = lines[-1][:120] if lines else ""
            missing.append({"listener": index, "rc": rc, "error": last_error})

    skew_ms = (max(attempts) - min(attempts)) / 1_000_000 if attempts else 0.0
    window_duration_ms = (window_end_ns - attempt_floor_ns) / 1_000_000

    scheduler = json.loads((root / "scheduler.done.json").read_text(encoding="utf-8"))
    if scheduler.get("spawn_guard") != "standard-python-process-apis":
        raise RuntimeError("release scheduler descendant-spawn guard is unproved")
    releases = [int(value) for value in scheduler.get("release_ns", [])]
    expected_release_count = 1 if float(spacing) == 0 else n
    if len(releases) != expected_release_count:
        raise RuntimeError(
            f"release witness count {len(releases)} != {expected_release_count}"
        )
    release_span_ms = (max(releases) - min(releases)) / 1_000_000 if releases else 0.0
except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error, journal.JournalError, RuntimeError) as exc:
    print(f"{tag} CELL_FAILED instrument_witness={type(exc).__name__}:{exc}")
    raise SystemExit(10)

for detail in generator_details:
    suffix = f" journal_rows={detail['journal_rows']}" if "journal_rows" in detail else ""
    if "listener_window_rows" in detail:
        suffix += f" listener_window_rows={detail['listener_window_rows']}"
    print(
        f"WORK_RECONCILED generator={detail['role']}-{detail['index']} "
        f"journal={expected} work_count={detail['work_count']} "
        f"window_start_count={detail['window_start_count']} "
        f"window_end_count={detail['window_end_count']} "
        f"listener_window_delta={detail['window_delta']} "
        f"counter_boundary=monotonic-operation-log-v1"
        f" stop_ack=yes{suffix}"
    )

wall = float(wall_end) - float(wall_start)
load = f"w{writers}/r{readers}"
work_status = "verified" if writers + readers else "not_applicable"
environment = (
    f"env[dispatch_id=unset,process_role=controller,test_mode=0,"
    f"allow_journal_migration=0,listener_slots={n},supervised=unset,"
    f"startup_grace=default,wake_entry_poll=default,listener_low_water=default,"
    f"descendant_policy=persistent_supervisor+forbid_standard_python_process_apis,"
    f"descendant_attempts=0,supervisor_errors=0]"
)
storage = (
    f"storage[state={root / 'state'},journal_dir={root / 'journal'},"
    f"wake_dir={root / 'wake'},messages={root / 'messages'},"
    f"task_store={root / 'tasks'},dispatch={root / 'state' / 'dispatch'},"
    f"pidfile_both={root / 'pids'}]"
)
fault = __import__("os").environ.get("GOALFLIGHT_LOADTEST_FAULT") or "none"
keep_root = __import__("os").environ.get("GOALFLIGHT_LOADTEST_KEEP_ROOT", "0")
treatment = (
    f"treatment[load_pace_s={load_pace},min_warmup_ops={minimum},"
    f"min_window_ops={window_minimum},generator_warmup_timeout_s={warmup_timeout},"
    f"listener_ready_timeout_s={ready_timeout},listener_timeout_s={listener_timeout},"
    f"cell_timeout_s={cell_timeout},release_timeout_s={release_timeout},"
    f"counter_boundary=monotonic-operation-log-v1,fault={fault},"
    f"verify_only={verify_only},keep_root={keep_root}]"
)
provenance = (
    f"product_sha256={code_hash} product_files={code_file_count} "
    f"revision={code_revision} product_tree={code_tree_state} "
    f"harness_sha256={harness_hash} harness_tree={harness_tree_state} "
    f"runtime[{runtime_info}] skill_dir={skill_dir} journal_history=fresh"
)
if verify_only == "1":
    print(
        f"{tag} VERIFY_OK journal={expected} work_witness={work_status} "
        f"arm_witness=listener_coverage:{armed}/{n} journal_attempt_skew_ms={skew_ms:.3f} "
        f"release_span_ms={release_span_ms:.3f} listener_window_ms={window_duration_ms:.3f} "
        f"terminal_rc={dict(terminal)} "
        f"{environment} {storage} {treatment} {provenance}"
    )
else:
    pct = 100.0 * armed / n
    extra = f" missing_arm_witness={missing}" if missing else ""
    print(
        f"{tag:>12} N={n:<3d} spacing={spacing}s load={load:<8} "
        f"armed={armed}/{n} ({pct:.0f}%) work_witness={work_status} "
        f"arm_witness=listener_coverage "
        f"journal_attempt_skew_ms={skew_ms:.3f} release_span_ms={release_span_ms:.3f} "
        f"listener_window_ms={window_duration_ms:.3f} wall={wall:.1f}s "
        f"loadavg[{load_before.strip()} -> {load_after.strip()}] "
        f"{environment} {storage} {treatment} {provenance}{extra}"
    )
PY
    ;;
esac

N="${1:?usage: listener_arm_loadtest.sh <N> <spacing_secs> <tag> [writers] [readers]}"
SPACING="${2:-0}"
TAG="${3:-run}"
LOAD_WRITERS="${4:-0}"
LOAD_READERS="${5:-0}"

if ! VALIDATED_ARGS=$(python3 - "$N" "$SPACING" "$TAG" "$LOAD_WRITERS" "$LOAD_READERS" <<'PY'
import re, sys
n_text, spacing_text, tag, writers_text, readers_text = sys.argv[1:]
try:
    n, spacing = int(n_text), float(spacing_text)
    writers, readers = int(writers_text), int(readers_text)
    if not 1 <= n <= 200:
        raise ValueError("N must be in 1..200")
    if not 0 <= spacing <= 60:
        raise ValueError("spacing must be in 0..60")
    if not 0 <= writers <= 50 or not 0 <= readers <= 50:
        raise ValueError("generator counts must be in 0..50")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,48}", tag):
        raise ValueError("tag must match [A-Za-z0-9._-]{1,48}")
except ValueError as exc:
    print(exc)
    raise SystemExit(1)
print(n, repr(spacing), tag, writers, readers)
PY
); then
  echo "$TAG SETUP_FAILED invalid_arguments:$VALIDATED_ARGS"
  exit 9
fi
read -r N SPACING TAG LOAD_WRITERS LOAD_READERS <<<"$VALIDATED_ARGS"

# A dispatched worker is forbidden from listening by the product before it
# reaches the nonce or journal. Refuse the entire cell rather than normalizing
# this one variable invisibly and publishing a plausible 0/N.
if [ -n "${GOALFLIGHT_DISPATCH_ID:-}" ]; then
  echo "$TAG N=$N spacing=$SPACING SETUP_FAILED ambient_GOALFLIGHT_DISPATCH_ID=present effective_dispatch_id=refused"
  exit 9
fi
case "${GOALFLIGHT_LOADTEST_FAULT:-}" in
  ""|generator-path|generator-exit-after-warmup|generator-spawn|generator-pause-in-window|generator-after-arm-only|generator-pre-attempt-only|listener-pre-arm-error|listener-post-arm-error|launcher-pre-setpgid-stop|launcher-before-ready-exit) ;;
  *)
    echo "$TAG SETUP_FAILED unknown_fault_injection:${GOALFLIGHT_LOADTEST_FAULT}"
    exit 9
    ;;
esac
VERIFY_ONLY="${GOALFLIGHT_LOADTEST_VERIFY_ONLY:-0}"
KEEP_ROOT="${GOALFLIGHT_LOADTEST_KEEP_ROOT:-0}"
case "$VERIFY_ONLY" in
  0|1) ;;
  *) echo "$TAG SETUP_FAILED GOALFLIGHT_LOADTEST_VERIFY_ONLY_must_be_0_or_1"; exit 9 ;;
esac
case "$KEEP_ROOT" in
  0|1) ;;
  *) echo "$TAG SETUP_FAILED GOALFLIGHT_LOADTEST_KEEP_ROOT_must_be_0_or_1"; exit 9 ;;
esac
export GOALFLIGHT_LOADTEST_VERIFY_ONLY="$VERIFY_ONLY"
export GOALFLIGHT_LOADTEST_KEEP_ROOT="$KEEP_ROOT"

SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd -P)"
CHECKOUT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
SKILL_DIR="${GOALFLIGHT_SKILL_DIR:-$CHECKOUT_ROOT}"
if [ -z "$SKILL_DIR" ]; then
  echo "$TAG SETUP_FAILED cannot_resolve_skill_dir"
  exit 9
fi
if ! SKILL_DIR="$(cd "$SKILL_DIR" 2>/dev/null && pwd -P)"; then
  echo "$TAG SETUP_FAILED invalid_skill_dir:${GOALFLIGHT_SKILL_DIR:-$CHECKOUT_ROOT}"
  exit 9
fi

S="$SKILL_DIR/scripts/goalflight_messages.py"
SESS="$SKILL_DIR/scripts/goalflight_session_status.py"
JOURNAL="$SKILL_DIR/scripts/goalflight_journal.py"
WAKE="$SKILL_DIR/scripts/goalflight_wake.py"
COMPAT="$SKILL_DIR/scripts/goalflight_compat.py"
TASK="$SKILL_DIR/goalflight_task.py"
for file in "$S" "$SESS" "$JOURNAL" "$WAKE" "$COMPAT" "$TASK"; do
  if [ ! -f "$file" ]; then
    echo "$TAG SETUP_FAILED missing:$file"
    exit 9
  fi
done

# Normalize all listener-behaviour inputs after the dispatch-ID refusal above.
# Storage variables are then replaced with paths under the cell root.
unset GOALFLIGHT_PROCESS_ROLE GOALFLIGHT_CONTROLLER_LABEL
unset GOALFLIGHT_CONTROLLER_LEASE_NONCE GOALFLIGHT_CONTROLLER_SESSION_ID
unset GOALFLIGHT_CONTROLLER_PID GOALFLIGHT_LISTENER_SLOTS
unset GOALFLIGHT_TEST_MODE GOALFLIGHT_TEST_LISTENER_START_TOKEN
unset GOALFLIGHT_ALLOW_JOURNAL_MIGRATION GOALFLIGHT_LISTENER_STARTUP_GRACE_S
unset GOALFLIGHT_SUPERVISED GOALFLIGHT_WAKE_ENTRY_POLL_S
unset GOALFLIGHT_LISTENER_LOW_WATER
unset GOALFLIGHT_LOADTEST_ROOT
unset GOALFLIGHT_PROMPT_FILE GOALFLIGHT_STEER_FILE GOALFLIGHT_DISPATCH_SCRIPT
export GOALFLIGHT_PROCESS_ROLE=controller

BASE="${TMPDIR:-/tmp}"
BASE="${BASE%/}/gf-listener-loadtest"
if ! mkdir -p "$BASE" || ! BASE="$(cd "$BASE" && pwd -P)"; then
  echo "$TAG SETUP_FAILED cannot_create_base:$BASE"
  exit 9
fi
if ! ROOT="$(mktemp -d "$BASE/root-XXXXXX")" || [ -z "$ROOT" ]; then
  echo "$TAG SETUP_FAILED cannot_create_isolated_root base=$BASE"
  exit 9
fi
case "$ROOT" in
  "$BASE"/root-*) ;;
  *)
    echo "$TAG SETUP_FAILED unsafe_isolated_root:$ROOT"
    exit 9
    ;;
esac
if ! touch "$ROOT/.listener-arm-loadtest-root"; then
  echo "$TAG SETUP_FAILED cannot_mark_isolated_root:$ROOT"
  rmdir "$ROOT" 2>/dev/null || true
  exit 9
fi

early_setup_cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  case "$ROOT" in
    "$BASE"/root-*)
      if [ -f "$ROOT/.listener-arm-loadtest-root" ]; then
        rm -rf -- "$ROOT"
      fi
      ;;
  esac
  exit "$status"
}
trap early_setup_cleanup EXIT HUP INT TERM

ROOT="$(cd "$ROOT" && pwd -P)" || {
  echo "$TAG SETUP_FAILED cannot_resolve_isolated_root"
  exit 9
}
case "$ROOT" in
  "$BASE"/root-*) ;;
  *)
    echo "$TAG SETUP_FAILED unsafe_canonical_root:$ROOT"
    exit 9
    ;;
esac

export GOALFLIGHT_STATE_DIR="$ROOT/state"
export GOALFLIGHT_JOURNAL_DIR="$ROOT/journal"
export GOALFLIGHT_WAKE_LEDGER_DIR="$ROOT/wake"
export GOALFLIGHT_WAKE_LEDGER="$ROOT/wake/wake-ledger.jsonl"
export GOALFLIGHT_MESSAGES_DIR="$ROOT/messages"
export GOALFLIGHT_TASK_STORE_DIR="$ROOT/tasks"
export GOALFLIGHT_TASK_STORE="$ROOT/tasks"
export GOALFLIGHT_DISPATCH_DIR="$ROOT/state/dispatch"
export GOAL_FLIGHT_PIDFILE_DIR="$ROOT/pids"
export GOALFLIGHT_PIDFILE_DIR="$ROOT/pids"
export GOALFLIGHT_CAPACITY_CONF=/dev/null
export GOALFLIGHT_LOADTEST_ROOT="$ROOT"
if ! mkdir -p "$ROOT"/{state,journal,wake,messages,tasks,pids,state/dispatch}; then
  echo "$TAG SETUP_FAILED cannot_create_isolated_storage root=$ROOT"
  exit 9
fi
if ! python3 - "$ROOT" \
  "$GOALFLIGHT_STATE_DIR" "$GOALFLIGHT_JOURNAL_DIR" "$GOALFLIGHT_WAKE_LEDGER_DIR" "$GOALFLIGHT_WAKE_LEDGER" \
  "$GOALFLIGHT_MESSAGES_DIR" "$GOALFLIGHT_TASK_STORE_DIR" "$GOALFLIGHT_TASK_STORE" \
  "$GOALFLIGHT_DISPATCH_DIR" "$GOAL_FLIGHT_PIDFILE_DIR" "$GOALFLIGHT_PIDFILE_DIR" <<'PY'
import os, sys
root = os.path.realpath(sys.argv[1])
for raw in sys.argv[2:]:
    resolved = os.path.realpath(raw)
    if os.path.commonpath((root, resolved)) != root or resolved == root:
        raise SystemExit(f"storage path escaped root: {raw} -> {resolved}")
PY
then
  echo "$TAG SETUP_FAILED isolated_storage_validation_failed root=$ROOT"
  exit 9
fi

STOP="$ROOT/STOP"
ABORT="$ROOT/ABORT"
REGISTRY="$ROOT/process-identities.jsonl"
CLEANUP_SNAPSHOT="$ROOT/cleanup-identities.jsonl"
RESULT_PENDING="$ROOT/result.pending"
RESULT_READY=0
ALL_PIDS=()
LOAD_PIDS=()
LISTENER_PIDS=()
REAPED_PIDS=()
LAUNCH_CRITICAL=0
DEFERRED_SIGNAL_NAME=""
DEFERRED_SIGNAL_STATUS=""

begin_launcher() {
  LAUNCH_CRITICAL=1
  # Bash establishes the private group from the parent side before returning.
  set -m
}

finish_launcher() {
  LAUNCH_CRITICAL=0
  if [ -n "$DEFERRED_SIGNAL_NAME" ]; then
    signal_name="$DEFERRED_SIGNAL_NAME"
    signal_status="$DEFERRED_SIGNAL_STATUS"
    DEFERRED_SIGNAL_NAME=""
    DEFERRED_SIGNAL_STATUS=""
    interrupted "$signal_name" "$signal_status"
  fi
}

make_launcher_safe() {
  pid="$1"
  while true; do
    if abort_witness=$(
      "$SELF" __bootstrap_abort "$SKILL_DIR" "$REGISTRY" "$pid" "$$" 2>/dev/null
    ); then
      printf '%s\n' "$abort_witness"
      wait "$pid" 2>/dev/null || true
      REAPED_PIDS+=("$pid")
      return 0
    fi
    state="$(pid_state "$pid")"
    if [ "$state" = "done" ]; then
      wait "$pid" 2>/dev/null || true
      REAPED_PIDS+=("$pid")
      return 0
    fi
    if [ "$state" = "running" ]; then
      # The append-only registry's last row is the persistent supervisor.
      return 0
    fi
    if [ "$state" = "unknown" ]; then
      job_active=0
      while IFS= read -r job_pid; do
        [ "$job_pid" = "$pid" ] && job_active=1 && break
      done < <({ jobs -pr; jobs -ps; } 2>/dev/null)
      if [ "$job_active" -eq 0 ]; then
        # Bash's job table, not a numeric liveness probe, proves the original
        # direct child has exited. Consume its cached status generation-safely.
        wait "$pid" 2>/dev/null || true
        REAPED_PIDS+=("$pid")
        return 0
      fi
    fi
    if [ "$state" = "bootstrap" ]; then
      "$SELF" __bootstrap_continue "$SKILL_DIR" "$REGISTRY" "$pid" "$$" >/dev/null 2>&1 || true
    fi
    # Remain launch-critical: no cleanup signal may target an unproved numeric
    # generation. The pre-fork launcher must become persistent, exit, or be
    # killed under the stopped-generation proof above.
    sleep 0.01
  done
}

bootstrap_launcher() {
  role="$1"
  pid="$2"
  ready="$ROOT/launcher-bootstrap.$pid.ready"
  set +m
  ALL_PIDS+=("$pid")
  if ! "$SELF" __bootstrap_register "$SKILL_DIR" "$REGISTRY" "$pid" "$role" "$ready" "$$"; then
    echo "$TAG SETUP_FAILED launcher_bootstrap_unavailable role=$role pid=$pid"
    make_launcher_safe "$pid"
    finish_launcher
    return 1
  fi
  if ! "$SELF" __bootstrap_continue "$SKILL_DIR" "$REGISTRY" "$pid" "$$"; then
    echo "$TAG SETUP_FAILED launcher_bootstrap_release_failed role=$role pid=$pid"
    make_launcher_safe "$pid"
    finish_launcher
    return 1
  fi
  if ! track_pid "$role" "$pid"; then
    make_launcher_safe "$pid"
    finish_launcher
    return 1
  fi
  finish_launcher
}

track_pid() {
  role="$1"
  pid="$2"
  registration_deadline=$(( $(date +%s) + 3 ))
  while true; do
    state="$(pid_state "$pid")"
    [ "$state" = "running" ] && return 0
    if [ "$state" = "bootstrap" ]; then
      if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" != "launcher-pre-setpgid-stop" ] || \
         [ -n "$DEFERRED_SIGNAL_NAME" ]; then
        "$SELF" __bootstrap_continue "$SKILL_DIR" "$REGISTRY" "$pid" "$$" >/dev/null 2>&1 || true
      fi
    fi
    if [ "$state" = "done" ]; then
      echo "$TAG SETUP_FAILED worker_exited_during_self_registration role=$role pid=$pid"
      return 1
    fi
    if [ "$(date +%s)" -ge "$registration_deadline" ]; then
      echo "$TAG SETUP_FAILED identity_unavailable role=$role pid=$pid"
      return 1
    fi
    sleep 0.01
  done
}

pid_state() {
  if [ -n "${2:-}" ]; then
    "$SELF" __pid_state "$SKILL_DIR" "$REGISTRY" "$1" "$2" 2>/dev/null || true
  else
    "$SELF" __pid_state "$SKILL_DIR" "$REGISTRY" "$1" 2>/dev/null || true
  fi
}

cleanup() {
  status=$?
  trap - EXIT
  trap '' HUP INT TERM
  set +u
  touch "$STOP" "$ABORT" "$ROOT/release.all" 2>/dev/null
  cleanup_rc=0
  result_payload=""
  # A signal may arrive after `command &` but before track_pid appends $!.
  # Fold Bash's still-live direct job table into the owned set before cleanup.
  while IFS= read -r job_pid; do
    case "$job_pid" in
      ''|*[!0-9]*) continue ;;
    esac
    already_owned=0
    for pid in "${ALL_PIDS[@]}"; do
      [ "$pid" = "$job_pid" ] && already_owned=1 && break
    done
    [ "$already_owned" -eq 1 ] || ALL_PIDS+=("$job_pid")
  done < <(jobs -p)
  {
    "$SELF" __cleanup "$SKILL_DIR" "$REGISTRY" "$CLEANUP_SNAPSHOT" "${ALL_PIDS[*]}" || cleanup_rc=$?
    for pid in "${ALL_PIDS[@]}"; do
      already_reaped=0
      for reaped_pid in "${REAPED_PIDS[@]}"; do
        [ "$pid" = "$reaped_pid" ] && already_reaped=1 && break
      done
      [ "$already_reaped" -eq 1 ] && continue
      state="$(pid_state "$pid" "$CLEANUP_SNAPSHOT")"
      if [ "$state" = "done" ]; then
        wait "$pid" || true
      elif [ "$state" = "running" ]; then
        echo "CLEANUP_REAP_SKIPPED pid=$pid state=running"
        cleanup_rc=3
      else
        echo "CLEANUP_REAP_SKIPPED pid=$pid state=unknown"
        cleanup_rc=3
      fi
    done
  } 2>/dev/null
  if [ -f "$CLEANUP_SNAPSHOT" ]; then
    "$SELF" __reconcile "$SKILL_DIR" "$CLEANUP_SNAPSHOT" || cleanup_rc=$?
  else
    echo "CLEANUP identity_reconciled=0 survivors=0 unknown=1"
    cleanup_rc=3
  fi
  if [ -s "$ROOT/SUPERVISOR_ERROR" ]; then
    echo "$TAG CLEANUP_FAILED supervisor_error=$(sed -n '1p' "$ROOT/SUPERVISOR_ERROR")"
    cleanup_rc=3
  fi
  if [ "$cleanup_rc" -ne 0 ]; then
    echo "$TAG CLEANUP_FAILED root=$ROOT reconciliation_rc=$cleanup_rc"
    status=10
  fi
  if [ "$RESULT_READY" -eq 1 ] && [ "$cleanup_rc" -eq 0 ]; then
    if [ -s "$RESULT_PENDING" ]; then
      result_payload="$(sed -n '1,240p' "$RESULT_PENDING")"
    else
      echo "$TAG CELL_FAILED result_witness_missing"
      cleanup_rc=3
      status=10
    fi
  fi
  case "$ROOT" in
    "$BASE"/root-*)
      if [ -f "$ROOT/.listener-arm-loadtest-root" ] && [ "$cleanup_rc" -eq 0 ] && [ "$KEEP_ROOT" != "1" ]; then
        if ! rm -rf -- "$ROOT" || [ -e "$ROOT" ]; then
          echo "$TAG CLEANUP_FAILED root_removal=$ROOT"
          cleanup_rc=3
          status=10
        fi
      fi
      ;;
    *)
      echo "$TAG CLEANUP_FAILED unsafe_root=$ROOT"
      status=10
      ;;
  esac
  if [ "$RESULT_READY" -eq 1 ] && [ "$cleanup_rc" -eq 0 ]; then
    printf '%s\n' "$result_payload"
  fi
  exit "$status"
}

interrupted() {
  signal_name="$1"
  signal_status="$2"
  if [ "$LAUNCH_CRITICAL" -eq 1 ]; then
    if [ -z "$DEFERRED_SIGNAL_NAME" ]; then
      DEFERRED_SIGNAL_NAME="$signal_name"
      DEFERRED_SIGNAL_STATUS="$signal_status"
    fi
    return 0
  fi
  echo "$TAG INTERRUPTED signal=$signal_name"
  exit "$signal_status"
}

trap cleanup EXIT
trap 'interrupted HUP 129' HUP
trap 'interrupted INT 130' INT
trap 'interrupted TERM 143' TERM

LOAD_PACE_S="${LOAD_PACE_S:-0.1}"
MIN_GENERATOR_SUCCESSES="${MIN_GENERATOR_SUCCESSES:-3}"
MIN_GENERATOR_WINDOW_OPERATIONS="${MIN_GENERATOR_WINDOW_OPERATIONS:-1}"
GENERATOR_WARMUP_TIMEOUT_S="${GENERATOR_WARMUP_TIMEOUT_S:-15}"
LISTENER_READY_TIMEOUT_S="${LISTENER_READY_TIMEOUT_S:-20}"
LISTENER_TIMEOUT_S="${LISTENER_TIMEOUT_S:-6}"
CELL_TIMEOUT_S="${CELL_TIMEOUT_S:-30}"
if ! VALIDATED_TUNING=$(python3 - "$LOAD_PACE_S" "$MIN_GENERATOR_SUCCESSES" "$MIN_GENERATOR_WINDOW_OPERATIONS" "$GENERATOR_WARMUP_TIMEOUT_S" "$LISTENER_READY_TIMEOUT_S" "$LISTENER_TIMEOUT_S" "$CELL_TIMEOUT_S" <<'PY'
import sys
try:
    pace = float(sys.argv[1]); minimum = int(sys.argv[2]); window_minimum = int(sys.argv[3])
    warmup = int(sys.argv[4]); ready = int(sys.argv[5])
    listener = float(sys.argv[6]); cell = int(sys.argv[7])
    if not 0.001 <= pace <= 10:
        raise ValueError("LOAD_PACE_S must be in 0.001..10")
    if not 1 <= minimum <= 10000:
        raise ValueError("MIN_GENERATOR_SUCCESSES must be in 1..10000")
    if not 1 <= window_minimum <= 10000:
        raise ValueError("MIN_GENERATOR_WINDOW_OPERATIONS must be in 1..10000")
    if min(warmup, ready, listener, cell) <= 0:
        raise ValueError("timeouts must be positive")
    if cell <= listener:
        raise ValueError("CELL_TIMEOUT_S must exceed LISTENER_TIMEOUT_S")
except ValueError as exc:
    print(exc)
    raise SystemExit(1)
print(
    repr(pace), minimum, window_minimum,
    warmup, ready, repr(listener), cell,
)
PY
); then
  echo "$TAG SETUP_FAILED invalid_tuning:$VALIDATED_TUNING"
  exit 9
fi
read -r LOAD_PACE_S MIN_GENERATOR_SUCCESSES MIN_GENERATOR_WINDOW_OPERATIONS \
  GENERATOR_WARMUP_TIMEOUT_S LISTENER_READY_TIMEOUT_S LISTENER_TIMEOUT_S CELL_TIMEOUT_S \
  <<<"$VALIDATED_TUNING"
if ! CODE_PROVENANCE=$(python3 - "$SKILL_DIR" <<'PY'
import hashlib, importlib, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / "scripts"), str(root)]
for name in (
    "goalflight_messages", "goalflight_session_status", "goalflight_journal",
    "goalflight_wake", "goalflight_compat", "goalflight_task",
):
    importlib.import_module(name)

files = set()
for module in tuple(sys.modules.values()):
    raw = getattr(module, "__file__", None)
    if not raw:
        continue
    path = Path(raw).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        continue
    if path.suffix == ".py":
        files.add((relative.as_posix(), path))

digest = hashlib.sha256()
for relative, path in sorted(files):
    data = path.read_bytes()
    digest.update(relative.encode())
    digest.update(b"\0")
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
print(digest.hexdigest(), len(files))
PY
); then
  echo "$TAG SETUP_FAILED product_hash_unavailable"
  exit 9
fi
read -r CODE_HASH CODE_FILE_COUNT <<<"$CODE_PROVENANCE"
if [ -z "$CODE_HASH" ] || [ -z "$CODE_FILE_COUNT" ]; then
  echo "$TAG SETUP_FAILED product_hash_incomplete"
  exit 9
fi
if ! HARNESS_HASH=$(python3 - "$SELF" <<'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
); then
  echo "$TAG SETUP_FAILED harness_hash_unavailable"
  exit 9
fi
CODE_REVISION="$(git -C "$SKILL_DIR" rev-parse HEAD 2>/dev/null || printf 'not-a-git-checkout')"
if git -C "$SKILL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! CODE_STATUS="$(git -C "$SKILL_DIR" status --porcelain -- scripts goalflight_task.py)"; then
    echo "$TAG SETUP_FAILED product_tree_state_unavailable"
    exit 9
  elif [ -n "$CODE_STATUS" ]; then
    CODE_TREE_STATE=dirty
  else
    CODE_TREE_STATE=clean
  fi
else
  CODE_TREE_STATE=installed
fi
if [ -n "$CHECKOUT_ROOT" ] && [ -n "$(git -C "$CHECKOUT_ROOT" status --porcelain -- "$SELF" 2>/dev/null)" ]; then
  HARNESS_TREE_STATE=dirty
else
  HARNESS_TREE_STATE=clean
fi
if ! RUNTIME_INFO=$(python3 - <<'PY'
import platform, sqlite3
os_name = "-".join((platform.system(), platform.release(), platform.machine()))
print(f"python={platform.python_version()},sqlite={sqlite3.sqlite_version},os={os_name}")
PY
); then
  echo "$TAG SETUP_FAILED runtime_provenance_unavailable"
  exit 9
fi
LABEL="listener-loadtest-$TAG"

# The controller helper itself holds the lease flock. This intentionally avoids
# controller-startup's detached helper, so there is no untracked holder to leak.
# Monitor mode is enabled only across each asynchronous fork. Bash establishes
# the private process group from the parent side before returning; disabling it
# immediately avoids changing the rest of the harness's job semantics.
begin_launcher
"$SELF" __launch "$SKILL_DIR" "$REGISTRY" controller-holder "$SELF" \
  __controller "$SKILL_DIR" "$ROOT" "$LABEL" "$ROOT/controller.ready.json" "$STOP" \
  >"$ROOT/controller.out" 2>"$ROOT/controller.err" &
CONTROLLER_PID=$!
bootstrap_launcher controller-holder "$CONTROLLER_PID" || exit 9

deadline=$(( $(date +%s) + 15 ))
while [ ! -f "$ROOT/controller.ready.json" ]; do
  if [ "$(pid_state "$CONTROLLER_PID")" != "running" ]; then
    echo "$TAG SETUP_FAILED controller_holder_exited:$(tail -1 "$ROOT/controller.err" 2>/dev/null)"
    exit 9
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "$TAG SETUP_FAILED controller_holder_timeout"
    exit 9
  fi
  sleep 0.02
done

read -r NONCE EXPECTED_JOURNAL CONTROLLER_WITNESS < <(python3 - "$ROOT/controller.ready.json" "$SKILL_DIR" "$ROOT" <<'PY'
import json, sys
from pathlib import Path
skill, root = Path(sys.argv[2]), Path(sys.argv[3])
sys.path[:0] = [str(skill / "scripts"), str(skill)]
import goalflight_journal as journal
import goalflight_task
item = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if item.get("spawn_guard") != "standard-python-process-apis":
    raise RuntimeError("controller descendant-spawn guard is unavailable")
goalflight_task._git_canonical_root = lambda _start: None
expected = journal.resolve_journal_path(root)
print(item["lease_nonce"], expected, item["journal_path"])
PY
)
if [ -z "$NONCE" ] || [ "$EXPECTED_JOURNAL" != "$CONTROLLER_WITNESS" ]; then
  echo "$TAG SETUP_FAILED controller_journal_mismatch expected=$EXPECTED_JOURNAL actual=$CONTROLLER_WITNESS"
  exit 9
fi
echo "JOURNAL_WITNESS listeners=$EXPECTED_JOURNAL controller=$CONTROLLER_WITNESS match=yes"
echo "ENVIRONMENT effective_dispatch_id=unset process_role=$GOALFLIGHT_PROCESS_ROLE test_mode=${GOALFLIGHT_TEST_MODE:-0} allow_journal_migration=${GOALFLIGHT_ALLOW_JOURNAL_MIGRATION:-0} fault_injection=${GOALFLIGHT_LOADTEST_FAULT:-none} skill_dir=$SKILL_DIR code_sha256=$CODE_HASH revision=$CODE_REVISION journal_history=fresh"

for ((index = 1; index <= LOAD_WRITERS; index++)); do
  witness="$ROOT/generator.writer.$index.json"
  begin_launcher
  "$SELF" __launch "$SKILL_DIR" "$REGISTRY" "writer-$index" "$SELF" \
    __writer "$SKILL_DIR" "$ROOT" "$LABEL" "$index" "$STOP" "$witness" "$LOAD_PACE_S" \
    >"$ROOT/generator.writer.$index.out" 2>"$ROOT/generator.writer.$index.err" &
  pid=$!
  LOAD_PIDS+=("$pid")
  bootstrap_launcher "writer-$index" "$pid" || exit 9
done
for ((index = 1; index <= LOAD_READERS; index++)); do
  witness="$ROOT/generator.reader.$index.json"
  begin_launcher
  "$SELF" __launch "$SKILL_DIR" "$REGISTRY" "reader-$index" "$SELF" \
    __reader "$SKILL_DIR" "$ROOT" "$LABEL" "$NONCE" "$index" "$STOP" "$witness" "$LOAD_PACE_S" \
    >"$ROOT/generator.reader.$index.out" 2>"$ROOT/generator.reader.$index.err" &
  pid=$!
  LOAD_PIDS+=("$pid")
  bootstrap_launcher "reader-$index" "$pid" || exit 9
done

if [ "$LOAD_WRITERS" -ne 0 ] || [ "$LOAD_READERS" -ne 0 ]; then
  warmup_deadline=$(( $(date +%s) + GENERATOR_WARMUP_TIMEOUT_S ))
  while true; do
    generator_state=$("$SELF" __generator_check "$ROOT" "$EXPECTED_JOURNAL" "$MIN_GENERATOR_SUCCESSES" "$LOAD_WRITERS" "$LOAD_READERS")
    generator_rc=$?
    if [ "$generator_rc" -eq 0 ]; then
      break
    fi
    if [ "$generator_rc" -eq 2 ]; then
      echo "$TAG CELL_FAILED generator_journal_mismatch $generator_state"
      exit 10
    fi
    for pid in "${LOAD_PIDS[@]}"; do
      if [ "$(pid_state "$pid")" != "running" ]; then
        echo "$TAG CELL_FAILED generator_exited_during_warmup pid=$pid witness=$generator_state"
        exit 10
      fi
    done
    if [ "$(date +%s)" -ge "$warmup_deadline" ]; then
      echo "$TAG CELL_FAILED generator_work_unproven minimum=$MIN_GENERATOR_SUCCESSES witness=$generator_state"
      exit 10
    fi
    sleep 0.05
  done
  "$SELF" __generator_report "$ROOT" "$LOAD_WRITERS" "$LOAD_READERS"
fi

if ! LOAD_BEFORE="$(uptime | sed 's/.*load averages*: //')" || [ -z "$LOAD_BEFORE" ]; then
  echo "$TAG CELL_FAILED load_average_before_unavailable"
  exit 10
fi
WALL_START="$(python3 -c 'import time; print(time.time())')"

# Start every interpreter first. READY is written only after imports and just
# before the first product/journal call.
for ((index = 1; index <= N; index++)); do
  begin_launcher
  "$SELF" __launch "$SKILL_DIR" "$REGISTRY" "listener-$index" "$SELF" __listener \
    "$SKILL_DIR" "$ROOT" "$LABEL" "$NONCE" "$N" "$index" \
    "$ROOT/listener.$index.ready.json" "$ROOT/release.$index" "$ROOT/release.all" "$ABORT" \
    "$ROOT/listener.$index.identity.json" "$ROOT/listener.$index.attempt.json" \
    "$ROOT/listener.$index.rc.json" "$LISTENER_TIMEOUT_S" \
    >"$ROOT/listener.$index.out" 2>"$ROOT/listener.$index.err" &
  pid=$!
  LISTENER_PIDS+=("$pid")
  bootstrap_launcher "listener-$index" "$pid" || exit 9
done

ready_deadline=$(( $(date +%s) + LISTENER_READY_TIMEOUT_S ))
while true; do
  ready_count=$(find "$ROOT" -maxdepth 1 -name 'listener.*.ready.json' -type f | wc -l | tr -d ' ')
  [ "$ready_count" -eq "$N" ] && break
  for pid in "${LISTENER_PIDS[@]}"; do
    if [ "$(pid_state "$pid")" != "running" ]; then
      echo "$TAG CELL_FAILED listener_died_before_barrier pid=$pid ready=$ready_count/$N"
      exit 10
    fi
  done
  if [ "$(date +%s)" -ge "$ready_deadline" ]; then
    echo "$TAG CELL_FAILED listener_barrier_timeout ready=$ready_count/$N"
    exit 10
  fi
  sleep 0.02
done
echo "BARRIER_WITNESS ready=$ready_count/$N boundary=before_first_journal_operation spacing=${SPACING}s"

if [ "$LOAD_WRITERS" -ne 0 ] || [ "$LOAD_READERS" -ne 0 ]; then
  if ! "$SELF" __generator_check \
    "$ROOT" "$EXPECTED_JOURNAL" "$MIN_GENERATOR_SUCCESSES" \
    "$LOAD_WRITERS" "$LOAD_READERS" >"$ROOT/generator.pre-release-admission.json"
  then
    echo "$TAG CELL_FAILED generator_admission_unavailable:$(sed -n '1p' "$ROOT/generator.pre-release-admission.json")"
    exit 10
  fi
  echo "LOAD_ADMISSION witness=$(sed -n '1p' "$ROOT/generator.pre-release-admission.json")"
  for pid in "${LOAD_PIDS[@]}"; do
    if [ "$(pid_state "$pid")" != "running" ]; then
      echo "$TAG CELL_FAILED generator_not_live_at_arm_window pid=$pid"
      exit 10
    fi
  done
  if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-pause-in-window" ] || \
     [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-after-arm-only" ] || \
     [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-pre-attempt-only" ]; then
    touch "$ROOT/PAUSE_GENERATORS" || {
      echo "$TAG CELL_FAILED generator_pause_fault_unavailable"
      exit 10
    }
    if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-pre-attempt-only" ]; then
      printf '%s\n' "$((LOAD_WRITERS + LOAD_READERS))" > "$ROOT/PRE_ATTEMPT_EXPECTED" || {
        echo "$TAG CELL_FAILED pre_attempt_fault_setup_unavailable"
        exit 10
      }
    fi
  fi
  if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-exit-after-warmup" ]; then
    if ! fault_worker_pid=$("$SELF" __worker_pid "$REGISTRY" "${LOAD_PIDS[0]}"); then
      echo "$TAG CELL_FAILED generator_fault_worker_identity_unavailable"
      exit 10
    fi
    kill -TERM "$fault_worker_pid" 2>/dev/null || true
  fi
fi

# One persistent, tracked scheduler owns every release. It is armed only after
# every listener is waiting at the exact first-journal-operation boundary and
# remains alive until STOP, so even sub-millisecond spacing has no spawn gap.
begin_launcher
"$SELF" __launch "$SKILL_DIR" "$REGISTRY" release-scheduler "$SELF" \
  __scheduler "$ROOT" "$N" "$SPACING" "$STOP" "$ROOT/scheduler.done.json" &
SCHEDULER_PID=$!
bootstrap_launcher release-scheduler "$SCHEDULER_PID" || exit 9
RELEASE_TIMEOUT_S="$(python3 - "$N" "$SPACING" <<'PY'
import math, sys
print(int(math.ceil(max(10.0, (int(sys.argv[1]) - 1) * float(sys.argv[2]) + 10.0))))
PY
)"
release_deadline=$(( $(date +%s) + RELEASE_TIMEOUT_S ))
while [ ! -f "$ROOT/scheduler.done.json" ]; do
  if [ "$(pid_state "$SCHEDULER_PID")" != "running" ]; then
    echo "$TAG CELL_FAILED release_scheduler_exited_before_completion"
    exit 10
  fi
  if [ "$(date +%s)" -ge "$release_deadline" ]; then
    echo "$TAG CELL_FAILED release_scheduler_timeout timeout_s=$RELEASE_TIMEOUT_S"
    exit 10
  fi
  sleep 0.02
done

cell_deadline=$(( $(date +%s) + CELL_TIMEOUT_S ))
while true; do
  running=0
  for pid in "${LISTENER_PIDS[@]}"; do
    if [ "$(pid_state "$pid")" = "running" ]; then
      running=$((running + 1))
    fi
  done
  [ "$running" -eq 0 ] && break
  if [ "$(date +%s)" -ge "$cell_deadline" ]; then
    echo "$TAG CELL_FAILED listener_completion_timeout still_running=$running"
    exit 10
  fi
  sleep 0.05
done

if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-pre-attempt-only" ]; then
  pre_attempt_count=$(find "$ROOT" -maxdepth 1 -name 'generator.*.pre-attempt-success' -type f | wc -l | tr -d ' ')
  expected_pre_attempt=$((LOAD_WRITERS + LOAD_READERS))
  if [ "$pre_attempt_count" -ne "$expected_pre_attempt" ] || [ ! -f "$ROOT/PRE_ATTEMPT_WORK_DONE" ]; then
    echo "$TAG CELL_FAILED pre_attempt_fault_witness_missing count=$pre_attempt_count/$expected_pre_attempt"
    exit 10
  fi
  echo "PRE_ATTEMPT_ONLY_WITNESS generators=$pre_attempt_count/$expected_pre_attempt"
fi

if [ "${GOALFLIGHT_LOADTEST_FAULT:-}" = "generator-after-arm-only" ]; then
  touch "$ROOT/ALLOW_POST_ARM_WORK" || {
    echo "$TAG CELL_FAILED post_arm_fault_release_unavailable"
    exit 10
  }
  post_arm_deadline=$(( $(date +%s) + 5 ))
  expected_post_arm=$((LOAD_WRITERS + LOAD_READERS))
  while true; do
    post_arm_count=$(find "$ROOT" -maxdepth 1 -name 'generator.*.post-arm-success' -type f | wc -l | tr -d ' ')
    [ "$post_arm_count" -eq "$expected_post_arm" ] && break
    for pid in "${LOAD_PIDS[@]}"; do
      if [ "$(pid_state "$pid")" != "running" ]; then
        echo "$TAG CELL_FAILED generator_exited_before_post_arm_fault_witness pid=$pid"
        exit 10
      fi
    done
    if [ "$(date +%s)" -ge "$post_arm_deadline" ]; then
      echo "$TAG CELL_FAILED post_arm_fault_witness_timeout count=$post_arm_count/$expected_post_arm"
      exit 10
    fi
    sleep 0.02
  done
  echo "POST_ARM_ONLY_WITNESS generators=$post_arm_count/$expected_post_arm"
fi

WALL_END="$(python3 -c 'import time; print(time.time())')"
if [ "$LOAD_WRITERS" -ne 0 ] || [ "$LOAD_READERS" -ne 0 ]; then
  for pid in "${LOAD_PIDS[@]}"; do
    if [ "$(pid_state "$pid")" != "running" ]; then
      echo "$TAG CELL_FAILED generator_load_not_sustained pid=$pid boundary=before_STOP"
      exit 10
    fi
  done
fi
touch "$STOP"

generator_stop_deadline=$(( $(date +%s) + 10 ))
if [ "$LOAD_WRITERS" -ne 0 ] || [ "$LOAD_READERS" -ne 0 ]; then
  while true; do
    generator_stop_state=$("$SELF" __generator_stop_check "$ROOT" "$LOAD_WRITERS" "$LOAD_READERS")
    generator_stop_rc=$?
    [ "$generator_stop_rc" -eq 0 ] && break
    if [ "$generator_stop_rc" -eq 2 ]; then
      echo "$TAG CELL_FAILED generator_stop_witness_unreadable:$generator_stop_state"
      exit 10
    fi
    for pid in "${LOAD_PIDS[@]}"; do
      if [ "$(pid_state "$pid")" != "running" ]; then
        echo "$TAG CELL_FAILED generator_exited_before_stop_ack pid=$pid witness=$generator_stop_state"
        exit 10
      fi
    done
    if [ "$(date +%s)" -ge "$generator_stop_deadline" ]; then
      echo "$TAG CELL_FAILED generator_stop_ack_timeout witness=$generator_stop_state"
      exit 10
    fi
    sleep 0.05
  done
fi
if ! LOAD_AFTER="$(uptime | sed 's/.*load averages*: //')" || [ -z "$LOAD_AFTER" ]; then
  echo "$TAG CELL_FAILED load_average_after_unavailable"
  exit 10
fi

"$SELF" __analyse \
  "$SKILL_DIR" "$ROOT" "$EXPECTED_JOURNAL" "$N" "$SPACING" "$TAG" \
  "$LOAD_WRITERS" "$LOAD_READERS" "$MIN_GENERATOR_SUCCESSES" \
  "$MIN_GENERATOR_WINDOW_OPERATIONS" "$NONCE" "$LABEL" \
  "$WALL_START" "$WALL_END" "$LOAD_BEFORE" "$LOAD_AFTER" \
  "$CODE_HASH" "$CODE_REVISION" "$CODE_TREE_STATE" "$CODE_FILE_COUNT" "$HARNESS_HASH" \
  "$HARNESS_TREE_STATE" "$RUNTIME_INFO" "$VERIFY_ONLY" \
  "$LOAD_PACE_S" "$GENERATOR_WARMUP_TIMEOUT_S" "$LISTENER_READY_TIMEOUT_S" \
  "$LISTENER_TIMEOUT_S" "$CELL_TIMEOUT_S" "$RELEASE_TIMEOUT_S" \
  >"$RESULT_PENDING"
analysis_rc=$?
if [ "$analysis_rc" -ne 0 ]; then
  sed -n '1,240p' "$RESULT_PENDING"
  exit "$analysis_rc"
fi
RESULT_READY=1
exit 0
