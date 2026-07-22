#!/usr/bin/env python3
"""Machine-local goal-flight dispatch ledger.

Records process identity next to prompt/session metadata so orchestrators can
recover after sleep, compaction, or parallel session overlap without reading
raw logs into the model context.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import goalflight_compat
import goalflight_compat as fcntl
import goalflight_dispatch_states
import goalflight_terminal

SCHEMA = "goalflight.dispatch.v1"


DEFAULT_STATE_DIR = goalflight_compat.resolve_state_dir()
WORKER_PATTERNS = (
    "codex",
    "codex-acp",
    "grok",
    "cursor-agent",
    "claude-code-cli-acp",
    "opencode",
    "opencode-acp",
    "opencode-bash-tail",
)
KIMI_WORKER_BASENAME = "kimi"
_POSIX_PS_AVAILABLE: bool | None = None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: object) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def state_dir() -> Path:
    return goalflight_compat.resolve_state_dir()


def runs_dir(*, create: bool = True) -> Path:
    path = state_dir() / "runs.d"
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def lock_path() -> Path:
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path / "ledger.lock"


class StateLock:
    def __init__(self):
        self._fh = None
        self._acquired = False

    def __enter__(self):
        if not self._acquired:
            self._fh = lock_path().open("a+")
            fcntl.flock(self._fh, fcntl.LOCK_EX)
            self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def release(self) -> None:
        """Release once; safe for reverse-order transaction teardown."""
        if not self._acquired or self._fh is None:
            return
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            self._acquired = False

    @classmethod
    def try_acquire(cls, deadline_s: float, *, poll_s: float = 0.010) -> "StateLock | None":
        """Acquire against an absolute monotonic deadline without blocking."""
        lock = cls()
        lock._fh = lock_path().open("a+")
        while True:
            try:
                fcntl.flock(lock._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock._acquired = True
                return lock
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in {
                    errno.EACCES,
                    errno.EAGAIN,
                }:
                    lock._fh.close()
                    lock._fh = None
                    raise
                if time.monotonic() >= deadline_s:
                    lock._fh.close()
                    lock._fh = None
                    return None
                time.sleep(min(poll_s, max(0.0, deadline_s - time.monotonic())))


def sha256_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ps_field(pid: int, field: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out or None


def _posix_ps_available() -> bool:
    global _POSIX_PS_AVAILABLE
    if _POSIX_PS_AVAILABLE is None:
        try:
            subprocess.check_call(
                ["ps", "-p", str(os.getpid()), "-o", "pid="],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            _POSIX_PS_AVAILABLE = False
        else:
            _POSIX_PS_AVAILABLE = True
    return _POSIX_PS_AVAILABLE


def process_identity(pid: int | None) -> dict | None:
    if not pid:
        return None
    if goalflight_compat.is_windows():
        # Reject dead PIDs on Windows too, else a dead worker reads as
        # 'identity_indeterminate' instead of 'dead'. Windows lacks the ps
        # probe, so return the probe-only token only for a live PID.
        if not goalflight_compat.pid_alive(pid):
            return None
        return {
            "pid": pid,
            "identity_available": False,
            "identity_source": "windows_pid_probe_only",
        }
    if not _posix_ps_available():
        if not goalflight_compat.pid_alive(pid):
            return None
        return {
            "pid": pid,
            "identity_available": False,
            "identity_source": "posix_pid_probe_only",
        }
    ident = None
    for attempt in range(20):
        if not goalflight_compat.pid_alive(pid):
            return None
        ident = {
            "pid": pid,
            "ppid": _ps_field(pid, "ppid"),
            "pgid": _ps_field(pid, "pgid"),
            "lstart": _ps_field(pid, "lstart"),
            "comm": _ps_field(pid, "comm"),
            "args": _ps_field(pid, "args"),
        }
        if ident.get("lstart") and ident.get("comm"):
            return ident
        if attempt < 19:
            time.sleep(0.1)
    return ident


def identity_matches(record: dict) -> tuple[bool, str]:
    pid = record.get("worker_pid") or record.get("controller_pid")
    if not pid:
        return False, "no_pid"
    current = process_identity(int(pid))
    if current is None:
        return False, "dead"
    prior = record.get("worker_identity") or record.get("controller_identity") or {}
    if goalflight_compat.is_windows() and not current.get("identity_available", True):
        return False, "identity_indeterminate"
    if not current.get("identity_available", True) or not prior.get("identity_available", True):
        return True, "identity_indeterminate"
    for key in ("lstart", "comm"):
        if prior.get(key) and current.get(key) and prior[key] != current[key]:
            return False, f"pid_reused_{key}"
    return True, "live"


def _is_detached_controller_dead_record(record: dict) -> bool:
    if not record.get("detached"):
        return False
    state = record.get("state")
    reason = record.get("reason") or record.get("error")
    return state == "controller_dead" or (state == "orphaned" and reason == "controller_dead")


def classify(record: dict) -> str:
    state = record.get("state", "running")
    if _is_detached_controller_dead_record(record):
        ok, reason = identity_matches(record)
        if ok:
            return "expected_live"
        if reason == "dead":
            return "worker_dead"
        if reason == "no_pid":
            return "unknown_no_pid"
        if reason == "identity_indeterminate":
            return "identity_indeterminate"
        return f"stale_{reason}"
    if goalflight_dispatch_states.is_terminal_state(state):
        return state
    if state in {"queued", "waiting_capacity"}:
        # Queued for a capacity slot: no worker exists yet, so the identity
        # checks below would misread this as unknown/ambiguous. It is a live,
        # expected phase of dispatch (bounded by the capacity-wait deadline).
        return "queued_capacity"
    ok, reason = identity_matches(record)
    if ok:
        return "expected_live"
    if state == "watcher_stopped":
        if reason == "identity_indeterminate":
            return "identity_indeterminate"
        return "watcher_stopped"
    if reason == "no_pid":
        return "unknown_no_pid"
    if reason == "identity_indeterminate":
        return "identity_indeterminate"
    return f"stale_{reason}"


def record_path(dispatch_id: str, *, create: bool = True) -> Path:
    return runs_dir(create=create) / f"{goalflight_compat.safe_dispatch_filename(dispatch_id)}.json"


def write_record(record: dict) -> Path:
    record["updated_at"] = utc_now()
    path = record_path(record["dispatch_id"])
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return path


def read_records() -> list[dict]:
    records: list[dict] = []
    path = runs_dir(create=False)
    if not path.exists():
        return records
    for p in sorted(path.glob("*.json")):
        try:
            records.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            records.append({"schema": SCHEMA, "dispatch_id": p.stem, "state": "unreadable", "path": str(p)})
    return records


def infer_engine(agent: object) -> str:
    if not isinstance(agent, str) or not agent:
        return "unknown"
    for suffix in ("-acp", "-dispatch"):
        if agent.endswith(suffix) and len(agent) > len(suffix):
            return agent[: -len(suffix)]
    return agent


def infer_shape(record: dict) -> str:
    shape = record.get("shape")
    if isinstance(shape, str) and shape in {"bash", "acp"}:
        return shape
    os_sandbox = record.get("os_sandbox")
    if isinstance(os_sandbox, dict):
        sandbox_shape = os_sandbox.get("shape")
        if isinstance(sandbox_shape, str) and sandbox_shape in {"bash", "acp"}:
            return sandbox_shape
    transport = record.get("transport")
    if transport == "acp":
        return "acp"
    if transport == "dispatch":
        return "bash"
    return "unknown"


def terminal_state_for(state: object, reason: object = None) -> str:
    terminal = goalflight_dispatch_states.terminal_state_for(state, reason)
    if terminal != "unknown":
        return terminal
    if state in {None, "", "queued", "running", "starting", "running_quiet", "handshaking", "waiting_capacity"}:
        # queued/waiting_capacity = queued for a capacity slot (pre-spawn, live):
        # non-terminal, so the reused-dispatch-id guard refuses duplicates
        # while a launcher is queued.
        return "unknown"
    return "error"


def _split_task_ids(values: object) -> list[str]:
    out: list[str] = []
    raw_values = values if isinstance(values, list) else [values]
    for value in raw_values:
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            task_id = part.strip()
            if task_id and task_id not in out:
                out.append(task_id)
    return out


def task_ids_from_args(args: argparse.Namespace) -> list[str]:
    values = []
    values.extend(_split_task_ids(getattr(args, "task_id", None)))
    values.extend(_split_task_ids(getattr(args, "task_ids", None)))
    out: list[str] = []
    for task_id in values:
        if task_id not in out:
            out.append(task_id)
    return out


def failure_envelope(reason: object) -> dict | None:
    if reason in (None, ""):
        return None
    if isinstance(reason, dict):
        return {"error": reason}
    if isinstance(reason, list):
        return {"error": reason}
    return {"reason": str(reason)}


def _terminal_key(record: dict) -> str:
    terminal_state = record.get("terminal_state")
    if terminal_state:
        return str(terminal_state)
    return terminal_state_for(record.get("state"), record.get("reason") or record.get("error"))


def elapsed_seconds(record: dict, ended_at: str | None = None) -> float | None:
    raw = record.get("elapsed_s")
    if isinstance(raw, (int, float)):
        return round(float(raw), 3)
    start = parse_utc(record.get("started_at"))
    end = parse_utc(ended_at or record.get("ended_at"))
    if not start or not end:
        return None
    elapsed = (end - start).total_seconds()
    if elapsed < 0:
        return None
    return round(elapsed, 3)


def scan_surplus(records: list[dict], limit: int = 20) -> list[dict]:
    known = {int(r["worker_pid"]) for r in records if r.get("worker_pid")}
    known.update(int(r["controller_pid"]) for r in records if r.get("controller_pid"))
    try:
        out = subprocess.check_output(
            ["ps", "ax", "-o", "pid=", "-o", "comm=", "-o", "args="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    surplus: list[dict] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in known:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        haystack = f"{comm} {args}"
        # Kimi executes from an off-PATH absolute location. Match its executable
        # basename, not arbitrary argv prose mentioning "kimi".
        if Path(comm).name == KIMI_WORKER_BASENAME or any(
            pattern in haystack for pattern in WORKER_PATTERNS
        ):
            surplus.append({"pid": pid, "comm": comm, "args": args[:240]})
        if len(surplus) >= limit:
            break
    return surplus


def cmd_record(args: argparse.Namespace) -> int:
    dispatch_id = args.dispatch_id or str(uuid.uuid4())
    worker_identity = process_identity(args.worker_pid)
    controller_pid = args.controller_pid or os.getpid()
    os_sandbox = None
    if getattr(args, "os_sandbox_json", None):
        try:
            os_sandbox = json.loads(args.os_sandbox_json)
        except json.JSONDecodeError:
            os_sandbox = {"raw": args.os_sandbox_json}
    engine = getattr(args, "engine", None) or infer_engine(args.agent)
    shape = getattr(args, "shape", None) or infer_shape(
        {"shape": getattr(args, "shape", None), "os_sandbox": os_sandbox, "transport": args.transport}
    )
    account = getattr(args, "account", None) or "default"
    record = {
        "schema": SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": args.prompt_id,
        "prompt_path": args.prompt_path,
        "prompt_sha256": sha256_file(args.prompt_path),
        "agent": args.agent,
        "engine": engine,
        "shape": shape,
        "account": account,
        "transport": args.transport,
        "project_root": args.project_root,
        "controller_pid": controller_pid,
        "controller_identity": process_identity(controller_pid),
        "worker_pid": args.worker_pid,
        "worker_identity": worker_identity,
        "worker_pgid": worker_identity.get("pgid") if worker_identity else None,
        "acp_session_id": args.acp_session_id,
        "logical_session_id": args.logical_session_id,
        "lease_id": args.lease_id,
        "remote_lease_id": getattr(args, "remote_lease_id", None) or args.lease_id,
        "stdout_path": args.stdout_path,
        "stderr_path": args.stderr_path,
        "status_path": args.status_path,
        "os_sandbox": os_sandbox,
        "state": args.state,
        "terminal_state": terminal_state_for(args.state),
        "started_at": utc_now(),
        "hostname": socket.gethostname(),
    }
    task_ids = task_ids_from_args(args)
    if task_ids:
        record["task_ids"] = task_ids
    if getattr(args, "detached", False):
        record["detached"] = True
    if getattr(args, "queue_launch_token", None):
        record["queue_launch_token"] = args.queue_launch_token
    with StateLock():
        path = record_path(dispatch_id)
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("started_at"):
                record["started_at"] = existing["started_at"]
        path = write_record(record)
    payload = {"ok": True, "dispatch_id": dispatch_id, "path": str(path), "state": record["state"]}
    print(json.dumps(payload, indent=None if args.json else 2, sort_keys=True))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    path = record_path(args.dispatch_id)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "missing_dispatch", "dispatch_id": args.dispatch_id}))
        return 1
    with StateLock():
        record = json.loads(path.read_text())
        terminal_state = getattr(args, "terminal_state", None) or terminal_state_for(args.state, args.reason)
        existing_terminal = _terminal_key(record)
        if (
            existing_terminal not in {"", "unknown", "watcher_stopped"}
            and terminal_state not in {"", "unknown"}
            and existing_terminal != terminal_state
        ):
            print(json.dumps({
                "ok": True,
                "dispatch_id": args.dispatch_id,
                "state": record.get("state"),
                "idempotent": True,
                "terminal_state": existing_terminal,
            }, sort_keys=True))
            return 0
        record["state"] = args.state
        ended_at = utc_now()
        record["ended_at"] = ended_at
        record["terminal_state"] = terminal_state
        record["liveness_state"] = goalflight_terminal.terminal_liveness_state(args.state)
        elapsed_s = getattr(args, "elapsed_s", None)
        if elapsed_s is None:
            elapsed_s = elapsed_seconds(record, ended_at)
        if elapsed_s is not None:
            record["elapsed_s"] = round(float(elapsed_s), 3)
        if hasattr(args, "worker_still_alive"):
            record["worker_still_alive"] = args.worker_still_alive
        envelope = failure_envelope(args.reason)
        record["outcome"] = {"terminal_state": terminal_state}
        if envelope:
            record.update(envelope)
            record["outcome"].update(envelope)
        write_record(record)
    print(json.dumps({"ok": True, "dispatch_id": args.dispatch_id, "state": args.state}, sort_keys=True))
    return 0


def status_payload() -> dict:
    records = read_records()
    rows = []
    for r in records:
        classification = classify(r)
        terminal_state = r.get("terminal_state") or terminal_state_for(
            r.get("state"), r.get("reason") or r.get("error")
        )
        if _is_detached_controller_dead_record(r):
            if classification == "expected_live" or classification in {
                "unknown_no_pid",
                "identity_indeterminate",
            } or str(classification).startswith("stale_"):
                terminal_state = "unknown"
            elif classification == "worker_dead":
                terminal_state = "worker_dead"
        row = {
            "dispatch_id": r.get("dispatch_id"),
            "prompt_id": r.get("prompt_id"),
            "agent": r.get("agent"),
            "engine": str(r.get("engine") or infer_engine(r.get("agent"))),
            "shape": infer_shape(r),
            "account": r.get("account") or "unknown",
            "transport": r.get("transport"),
            "state": r.get("state"),
            "classification": classification,
            "terminal_state": terminal_state,
            "liveness_state": r.get("liveness_state"),
            "elapsed_s": elapsed_seconds(r),
            "worker_still_alive": r.get("worker_still_alive"),
            "worker_pid": r.get("worker_pid"),
            "worker_identity": r.get("worker_identity"),
            "project_root": r.get("project_root"),
            "prompt_path": r.get("prompt_path"),
            "stdout_path": r.get("stdout_path"),
            "stderr_path": r.get("stderr_path"),
            "status_path": r.get("status_path"),
            "detached": r.get("detached"),
            "os_sandbox": r.get("os_sandbox"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"),
            "updated_at": r.get("updated_at"),
            "reason": r.get("reason"),
            "error": r.get("error"),
            "artifact_path": r.get("artifact_path"),
            "artifact_paths": r.get("artifact_paths"),
            "artifacts": r.get("artifacts"),
            "declared_artifacts": r.get("declared_artifacts"),
            "draft_path": r.get("draft_path"),
            "draft_paths": r.get("draft_paths"),
            "output_path": r.get("output_path"),
            "output_paths": r.get("output_paths"),
            "result_path": r.get("result_path"),
            "result_paths": r.get("result_paths"),
        }
        rows.append(row)
    return {
        "schema": SCHEMA,
        "state_dir": str(state_dir()),
        "records": rows,
        "surplus_processes": scan_surplus(records),
    }


def parse_window(window: str | None) -> tuple[str, int]:
    spec = window or "7d"
    text = spec.strip().lower()
    if not text:
        raise ValueError("empty window")
    if text[-1] in {"h", "d"}:
        number_text = text[:-1]
        unit = text[-1]
    else:
        number_text = text
        unit = "d"
    if not number_text.isdigit():
        raise ValueError(f"malformed window {spec!r}; use <N>h, <N>d, or bare <N> days")
    number = int(number_text)
    if number <= 0:
        raise ValueError(f"malformed window {spec!r}; N must be positive")
    seconds = number * (3600 if unit == "h" else 86400)
    return f"{number}{unit}", seconds


def _record_times(record: dict) -> list[dt.datetime]:
    times = [parse_utc(record.get("started_at")), parse_utc(record.get("ended_at"))]
    return [item for item in times if item is not None]


def _in_window(record: dict, since: dt.datetime) -> bool:
    times = _record_times(record)
    if not times:
        return False
    return any(item >= since for item in times)


def _reason_text(record: dict) -> str | None:
    reason = record.get("reason")
    if reason not in (None, ""):
        return str(reason)
    error = record.get("error")
    if error not in (None, ""):
        if isinstance(error, (dict, list)):
            return json.dumps(error, sort_keys=True)
        return str(error)
    outcome = record.get("outcome")
    if isinstance(outcome, dict):
        for key in ("reason", "error"):
            value = outcome.get(key)
            if value not in (None, ""):
                if isinstance(value, (dict, list)):
                    return json.dumps(value, sort_keys=True)
                return str(value)
    return None


def _new_group() -> dict:
    return {
        "total": 0,
        "outcomes": 0,
        "in_flight": 0,
        "successes": 0,
        "success_rate": 0.0,
        "failure_modes": {},
        "mean_elapsed_s": None,
        "p95_elapsed_s": None,
        "recent_failures": [],
        "_elapsed_values": [],
        "_failure_rows": [],
    }


def _add_to_group(group: dict, record: dict) -> None:
    terminal_state = record.get("terminal_state") or terminal_state_for(
        record.get("state"), record.get("reason") or record.get("error")
    )
    group["total"] += 1
    if terminal_state == "unknown":
        group["in_flight"] += 1
    elif terminal_state == "complete":
        group["outcomes"] += 1
        group["successes"] += 1
    else:
        group["outcomes"] += 1
        failures = group["failure_modes"]
        failures[terminal_state] = failures.get(terminal_state, 0) + 1
        failure_time = parse_utc(record.get("ended_at")) or parse_utc(record.get("started_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        group["_failure_rows"].append(
            {
                "dispatch_id": record.get("dispatch_id") or "unknown",
                "terminal_state": terminal_state,
                "reason": _reason_text(record),
                "_time": failure_time,
            }
        )
    elapsed = elapsed_seconds(record)
    if elapsed is not None:
        group["_elapsed_values"].append(elapsed)


def _finalize_group(group: dict, recent_failures: int) -> dict:
    outcomes = group["outcomes"]
    values = sorted(group.pop("_elapsed_values"))
    failure_rows = sorted(group.pop("_failure_rows"), key=lambda item: item["_time"], reverse=True)
    if outcomes:
        group["success_rate"] = round(group["successes"] / outcomes, 4)
    if values:
        group["mean_elapsed_s"] = round(sum(values) / len(values), 3)
        index = max(0, math.ceil(0.95 * len(values)) - 1)
        group["p95_elapsed_s"] = round(values[index], 3)
    group["recent_failures"] = [
        {key: row.get(key) for key in ("dispatch_id", "terminal_state", "reason")}
        for row in failure_rows[:recent_failures]
    ]
    return group


def stats_payload(window: str | None = None, *, now: dt.datetime | None = None, recent_failures: int = 5) -> dict:
    spec, seconds = parse_window(window)
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    since = now - dt.timedelta(seconds=seconds)
    by_engine: dict[str, dict] = {}
    by_shape: dict[str, dict] = {}
    considered = 0
    for record in read_records():
        if not _in_window(record, since):
            continue
        considered += 1
        engine = str(record.get("engine") or infer_engine(record.get("agent")))
        shape = infer_shape(record)
        _add_to_group(by_engine.setdefault(engine, _new_group()), record)
        _add_to_group(by_shape.setdefault(shape, _new_group()), record)
    return {
        "schema": f"{SCHEMA}.stats.v1",
        "window": {
            "spec": spec,
            "seconds": seconds,
            "since": since.isoformat(timespec="seconds"),
            "until": now.isoformat(timespec="seconds"),
        },
        "records_considered": considered,
        "by_engine": {key: _finalize_group(value, recent_failures) for key, value in sorted(by_engine.items())},
        "by_shape": {key: _finalize_group(value, recent_failures) for key, value in sorted(by_shape.items())},
    }


def _format_recent_failures(rows: list[dict]) -> str:
    if not rows:
        return "-"
    parts = []
    for row in rows:
        reason = row.get("reason") or "-"
        if len(reason) > 60:
            reason = reason[:57] + "..."
        parts.append(f"{row.get('dispatch_id')}:{row.get('terminal_state')}:{reason}")
    return "; ".join(parts)


def format_stats_table(payload: dict) -> str:
    lines = [
        f"window={payload['window']['spec']} records={payload['records_considered']} "
        f"since={payload['window']['since']} until={payload['window']['until']}"
    ]
    for label, key in (("engine", "by_engine"), ("shape", "by_shape")):
        lines.append(f"by {label}:")
        groups = payload.get(key, {})
        if not groups:
            lines.append("  (none)")
            continue
        lines.append("  key total outcomes in_flight success_rate failures mean_s p95_s recent_failures")
        for name, row in groups.items():
            failures = ",".join(
                f"{mode}:{count}" for mode, count in sorted(row.get("failure_modes", {}).items())
            ) or "-"
            mean_s = "-" if row.get("mean_elapsed_s") is None else row["mean_elapsed_s"]
            p95_s = "-" if row.get("p95_elapsed_s") is None else row["p95_elapsed_s"]
            success_pct = round(float(row.get("success_rate", 0.0)) * 100, 1)
            lines.append(
                f"  {name} {row.get('total', 0)} {row.get('outcomes', 0)} "
                f"{row.get('in_flight', 0)} {success_pct}% {failures} "
                f"{mean_s} {p95_s} {_format_recent_failures(row.get('recent_failures', []))}"
            )
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(f"dispatch ledger: {payload['state_dir']}")
    for row in payload["records"][: args.limit]:
        print(
            f"- {row['classification']}: {row.get('dispatch_id')} "
            f"agent={row.get('agent')} pid={row.get('worker_pid')} state={row.get('state')}"
        )
    if payload["surplus_processes"]:
        print("surplus worker-like processes:")
        for proc in payload["surplus_processes"][: args.limit]:
            print(f"- pid={proc['pid']} comm={proc['comm']} args={proc['args']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="goal-flight dispatch ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--dispatch-id")
    rec.add_argument("--prompt-id")
    rec.add_argument("--prompt-path")
    rec.add_argument("--task-id", help="Legacy singular linked task/bug id.")
    rec.add_argument("--task-ids", action="append", help="Comma-separated linked task/bug ids.")
    rec.add_argument("--agent", required=True)
    rec.add_argument("--engine")
    rec.add_argument("--shape", choices=["bash", "acp", "unknown"])
    rec.add_argument("--account")
    rec.add_argument("--transport", default="unknown")
    rec.add_argument("--project-root")
    rec.add_argument("--controller-pid", type=int)
    rec.add_argument("--worker-pid", type=int)
    rec.add_argument("--acp-session-id")
    rec.add_argument("--logical-session-id")
    rec.add_argument("--lease-id")
    rec.add_argument("--stdout-path")
    rec.add_argument("--stderr-path")
    rec.add_argument("--status-path")
    rec.add_argument("--os-sandbox-json")
    rec.add_argument("--state", default="running")
    rec.add_argument("--detached", action="store_true")
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_record)

    fin = sub.add_parser("finish")
    fin.add_argument("--dispatch-id", required=True)
    fin.add_argument("--state", default="complete")
    fin.add_argument("--reason")
    fin.add_argument("--terminal-state", choices=sorted(
        {"complete", "idle_timeout", "watcher_stopped", "unknown"}
        | set(goalflight_dispatch_states.TERMINAL_FAILURE_STATES)
    ))
    fin.add_argument("--elapsed-s", type=float)
    fin.set_defaults(func=cmd_finish)

    stat = sub.add_parser("status")
    stat.add_argument("--json", action="store_true")
    stat.add_argument("--limit", type=int, default=20)
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
