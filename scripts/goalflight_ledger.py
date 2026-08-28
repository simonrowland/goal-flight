#!/usr/bin/env python3
"""Machine-local goal-flight dispatch ledger.

Records process identity next to prompt/session metadata so orchestrators can
recover after sleep, compaction, or parallel session overlap without reading
raw logs into the model context.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

import goalflight_compat
import goalflight_compat as fcntl
import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_fleet_console_history
import goalflight_journal
import goalflight_output_redact
import goalflight_task
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


def canonicalize_project_root_on_store(project_root: object) -> str:
    """Return the worktree-collapsed project identity persisted in the ledger.

    Delivery targeting deliberately trusts stored roots and must not spawn git
    while reading a record.  Every ledger persistence therefore crosses this
    write-side boundary; repeating it for updates is intentionally idempotent.
    """
    return str(goalflight_task.resolve_project_root(str(project_root)))


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


def _ps_field(pid: int, field: str) -> tuple[str | None, bool]:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None, False
    return out or None, True


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
    liveness = goalflight_compat.pid_liveness(pid)
    if liveness is False:
        return None
    if liveness is None:
        return {
            "pid": pid,
            "identity_available": False,
            "identity_probe_error": True,
            "identity_source": "pid_probe_error",
        }
    start_identity = goalflight_compat.process_start_identity(pid)
    start_token = (
        start_identity.get("start_token") if isinstance(start_identity, dict) else None
    )
    if goalflight_compat.is_windows():
        # Reject dead PIDs on Windows too, else a dead worker reads as
        # 'identity_indeterminate' instead of 'dead'. Windows lacks the ps
        # probe, so return the probe-only token only for a live PID.
        ident = {
            "pid": pid,
            "identity_available": False,
            "identity_source": "windows_pid_probe_only",
        }
        if start_token:
            ident["start_token"] = start_token
        return ident
    if not _posix_ps_available():
        ident = {
            "pid": pid,
            "identity_available": False,
            "identity_source": "posix_pid_probe_only",
        }
        if start_token:
            ident["start_token"] = start_token
        return ident
    ident = None
    for attempt in range(20):
        liveness = goalflight_compat.pid_liveness(pid)
        if liveness is False:
            return None
        if liveness is None:
            ident = {
                "pid": pid,
                "identity_available": False,
                "identity_probe_error": True,
                "identity_source": "pid_probe_error",
            }
            if start_token:
                ident["start_token"] = start_token
            return ident
        fields = {
            field: _ps_field(pid, field)
            for field in ("ppid", "pgid", "lstart", "comm", "args")
        }
        ident = {
            "pid": pid,
            **{field: result[0] for field, result in fields.items()},
        }
        if start_token:
            ident["start_token"] = start_token
        if not all(result[1] for result in fields.values()):
            liveness = goalflight_compat.pid_liveness(pid)
            if liveness is False:
                return None
            ident.update(
                {
                    "identity_available": False,
                    "identity_probe_error": True,
                    "identity_source": (
                        "ps_probe_error" if liveness is True else "pid_probe_error"
                    ),
                }
            )
            return ident
        if ident.get("lstart"):
            return ident
        if attempt < 19:
            time.sleep(0.1)
    if ident is not None:
        ident.update(
            {
                "identity_available": False,
                "identity_probe_error": True,
                "identity_source": "ps_identity_incomplete",
            }
        )
    return ident


def compare_process_identities(
    pid: int,
    expected_identity: dict | None,
    current_identity: dict | None,
) -> tuple[bool, str]:
    """Pure worker identity comparison shared by verdict and reap paths."""
    if current_identity is None:
        return False, "dead"
    if current_identity.get("identity_probe_error"):
        # A failed probe is absence of evidence. Keep boolean consumers on the
        # conservative maybe-live side while carrying the explicit reason to
        # classifiers that implement bounded indeterminate handling.
        return True, "identity_indeterminate"
    if expected_identity:
        if expected_identity.get("pid") and int(expected_identity["pid"]) != int(pid):
            return False, "identity_pid_mismatch"

        expected_lstart = expected_identity.get("lstart")
        actual_lstart = current_identity.get("lstart")
        expected_start_token = expected_identity.get("start_token")
        actual_start_token = current_identity.get("start_token")
        if expected_start_token and actual_start_token:
            if actual_start_token != expected_start_token:
                return False, "pid_reused_start_token"
            if expected_lstart and actual_lstart and actual_lstart != expected_lstart:
                return False, "pid_reused_lstart"
            # exec(2) preserves the process generation while replacing comm.
            # A matching fine-grained start token is decisive, unlike lstart's
            # second-granularity wall-clock representation.
            return True, "live"

        if expected_lstart and actual_lstart:
            if actual_lstart != expected_lstart:
                return False, "pid_reused_lstart"
            # exec(2) preserves pid and lstart while replacing comm. The
            # launcher records its Python identity immediately before execing
            # the worker CLI, so comm is diagnostic only and cannot disprove
            # this durable identity pair.
            return True, "live"

        missing_sides = []
        if not expected_lstart:
            missing_sides.append("expected")
        if not actual_lstart:
            missing_sides.append("current")
        return True, (
            "identity_inconclusive_missing_" + "_".join(missing_sides) + "_lstart"
        )
    return True, "identity_inconclusive"


def compare_fine_process_identities(
    pid: int,
    expected_identity: dict | None,
    current_identity: dict | None,
) -> tuple[bool, str]:
    """Require the fine start token before authorizing a destructive action."""
    if current_identity is None:
        return False, "dead"
    if current_identity.get("identity_probe_error"):
        return False, "identity_indeterminate"
    if not expected_identity:
        return False, "identity_indeterminate"
    if not expected_identity.get("start_token") or not current_identity.get(
        "start_token"
    ):
        return False, "identity_indeterminate"
    return compare_process_identities(pid, expected_identity, current_identity)


def identity_matches(record: dict) -> tuple[bool, str]:
    pid = (
        record.get("worker_pid")
        or record.get("claimant_pid")
        or record.get("controller_pid")
    )
    if not pid:
        return False, "no_pid"
    current = process_identity(int(pid))
    if current is None:
        return False, "dead"
    prior = (
        record.get("worker_identity")
        or record.get("claimant_identity")
        or record.get("controller_identity")
        or {}
    )
    prior_has_start = bool(prior.get("start_token"))
    current_has_start = bool(current.get("start_token"))
    fine_start_available = prior_has_start and current_has_start
    if (
        goalflight_compat.is_windows()
        and not fine_start_available
        and not current.get("identity_available", True)
    ):
        return False, "identity_indeterminate"
    if not fine_start_available and (
        not current.get("identity_available", True)
        or not prior.get("identity_available", True)
    ):
        return True, "identity_indeterminate"
    matched, reason = compare_process_identities(int(pid), prior, current)
    if reason == "identity_indeterminate":
        return matched, reason
    if matched:
        return True, "live"
    return False, reason


def worker_identity_liveness(record: dict) -> tuple[str, str]:
    """Three-state worker liveness: ``live`` / ``dead`` / ``unknown``, plus reason.

    ``identity_matches`` already collapses every maybe-live reading (coarse
    identity, a missing lstart side) to ``live``. What remains genuinely
    indeterminate is an absent recorded identity (``no_pid``) or a failed
    process-table probe (``identity_indeterminate``); those map to ``unknown``,
    never to ``dead`` -- "could not find out" is not non-existence. A pid that
    now belongs to a different process (``pid_reused_*``) is ``dead``: the
    recorded worker is gone even though the pid number survives.
    """
    matched, reason = identity_matches(record)
    if reason in {"no_pid", "identity_indeterminate"}:
        return "unknown", reason
    if matched:
        return "live", reason
    return "dead", reason


def _terminal_sidecar_observation(
    record: dict,
    sidecar: dict | None = None,
) -> dict | None:
    """Return the matching sidecar when it carries a terminal verdict."""
    candidate = sidecar
    if not (
        isinstance(candidate, dict)
        and candidate.get("dispatch_id") == record.get("dispatch_id")
    ):
        status_path_value = record.get("status_path")
        if not (isinstance(status_path_value, str) and status_path_value):
            return None
        try:
            loaded = json.loads(Path(status_path_value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not (
            isinstance(loaded, dict)
            and loaded.get("dispatch_id") == record.get("dispatch_id")
        ):
            return None
        candidate = loaded
    state = candidate.get("terminal_pending_state") or candidate.get("state")
    terminal = terminal_state_for(
        state, candidate.get("reason") or candidate.get("error")
    )
    if terminal in {"", "unknown", "watcher_stopped"}:
        return None
    return candidate


def sidecar_terminal_hold(
    record: dict,
    *,
    sidecar: dict | None = None,
) -> dict[str, str] | None:
    """Describe a held terminal sidecar, or None when there is no hold.

    A hold exists when the ledger row is not already structurally terminal, a
    matching sidecar carries a terminal verdict, and worker identity is not
    dead (``live`` or ``unknown``). Identity is re-probed on every call.
    Age is never consulted: a timer that terminalizes an indeterminate
    record is the blind sweeper this reconciler forbids.

    Unknown stays a hold until a later probe can determine identity (the
    record gains a pid, a RUNNING journal instance is copied onto it, or
    the process table becomes readable) or a human / higher-authority pass
    acts. Dead identity is not a hold -- the sidecar may be committed.
    """
    if _terminal_key(record) not in {"", "unknown", "watcher_stopped"}:
        return None
    observation = _terminal_sidecar_observation(record, sidecar)
    if observation is None:
        return None
    liveness, reason = worker_identity_liveness(record)
    if liveness == "dead":
        return None
    state = str(
        observation.get("terminal_pending_state") or observation.get("state") or ""
    )
    return {
        "liveness": liveness,
        "reason": reason,
        "sidecar_state": state,
        "terminal_state": terminal_state_for(
            state, observation.get("reason") or observation.get("error")
        ),
    }


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
        if reason == "identity_indeterminate":
            return "identity_indeterminate"
        if ok:
            return "expected_live"
        if reason == "dead":
            return "worker_dead"
        if reason == "no_pid":
            return "unknown_no_pid"
        return f"stale_{reason}"
    if goalflight_dispatch_states.is_terminal_state(state):
        return state
    if state in {"queued", "waiting_capacity"}:
        # Queued for a capacity slot: no worker exists yet, so the identity
        # checks below would misread this as unknown/ambiguous. It is a live,
        # expected phase of dispatch (bounded by the capacity-wait deadline).
        return "queued_capacity"
    ok, reason = identity_matches(record)
    if reason == "identity_indeterminate":
        return "identity_indeterminate"
    if ok:
        return "expected_live"
    if state == "watcher_stopped":
        return "watcher_stopped"
    if reason == "no_pid":
        return "unknown_no_pid"
    return f"stale_{reason}"


def record_path(dispatch_id: str, *, create: bool = True) -> Path:
    return runs_dir(create=create) / f"{goalflight_compat.safe_dispatch_filename(dispatch_id)}.json"


def _is_usable_terminal_time(value: object) -> bool:
    """True when *value* is a stored terminal time, not a missing or poisoned field.

    The journal idempotent reread used to stringify SQL NULL as ``"None"``.
    That token is not a time; freezing it would block later journal backfill.
    """
    if value in (None, ""):
        return False
    return str(value).strip() not in {"None", "null"}


def preserve_first_terminal_time(record: dict, terminal_at: object = None) -> str | None:
    """Keep the first terminal instant; backfill only from journal authority.

    The journal commits ``terminal_at`` atomically with the winning transition.
    Projectors may run much later or retry indefinitely, so their wall clock is
    never terminal-ordering evidence.
    """
    existing = record.get("ended_at")
    if _is_usable_terminal_time(existing):
        return str(existing)
    if _is_usable_terminal_time(terminal_at):
        record["ended_at"] = str(terminal_at)
        return str(terminal_at)
    return None


def write_record(record: dict) -> Path:
    if record.get("project_root") not in (None, ""):
        record["project_root"] = canonicalize_project_root_on_store(
            record["project_root"]
        )
    path = record_path(record["dispatch_id"])
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict):
            current_ended_at = current.get("ended_at")
            if current_ended_at not in (None, ""):
                # The on-disk value won the first projection. A stale caller
                # cannot replace it, even if that caller carries a timestamp.
                record["ended_at"] = current_ended_at
    record["updated_at"] = utc_now()
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


def _stamp_nonterminal_fields(record: dict, fields: dict) -> None:
    """Persist derived fields onto a still-non-terminal ledger row.

    Used to durably copy a RUNNING journal identity onto the record (the
    unknown-hold resolution path: the record gains a pid) and to stamp a
    sidecar hold so operator surfaces can name ``held: live`` vs
    ``held: unknown``. Never writes over an already-terminal row.
    """
    dispatch_id = str(record.get("dispatch_id") or "")
    if not dispatch_id or not fields:
        return
    with StateLock():
        current_path = record_path(dispatch_id)
        if current_path.exists():
            try:
                current = json.loads(current_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = dict(record)
        else:
            current = dict(record)
        if not isinstance(current, dict):
            current = dict(record)
        if _terminal_key(current) not in {"", "unknown", "watcher_stopped"}:
            return
        current.update(fields)
        write_record(current)
        record.update(fields)
        if current.get("updated_at"):
            record["updated_at"] = current["updated_at"]


def record_engine_session_id(dispatch_id: str, session_id: str) -> Path:
    """Attach a harvested engine session handle without replacing launch metadata."""
    with StateLock():
        path = record_path(dispatch_id)
        if not path.exists():
            raise FileNotFoundError(f"missing dispatch ledger record: {dispatch_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        existing = record.get("engine_session_id") or record.get("codex_session_id")
        if existing not in (None, "", session_id):
            raise ValueError(
                f"dispatch {dispatch_id} already records engine session {existing}"
            )
        record["engine_session_id"] = session_id
        engine = infer_engine(record.get("engine") or record.get("agent"))
        if engine == "codex":
            record["codex_session_id"] = session_id
        return write_record(record)


def record_codex_session_id(dispatch_id: str, session_id: str) -> Path:
    """Attach a harvested Codex rollout UUID without replacing launch metadata."""
    return record_engine_session_id(dispatch_id, session_id)


def read_records() -> list[dict]:
    records: list[dict] = []
    path = runs_dir(create=False)
    if not path.exists():
        return records
    # pathlib.Path.glob on an unreadable directory returns [] without raising
    # (observed). os.listdir raises OSError, which callers that distinguish
    # UNKNOWN from empty must see -- an unlistable ledger is not "no records".
    names = os.listdir(path)
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        p = path / name
        try:
            records.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            records.append({"schema": SCHEMA, "dispatch_id": p.stem, "state": "unreadable", "path": str(p)})
    return records


def record_is_unreadable(record: dict | None) -> bool:
    """True when *record* is the placeholder ``read_records`` inserts for a bad file.

    Production never raises on corrupt ledger JSON. Callers that treat the
    placeholder as a settled terminal invent a verdict they did not observe.
    """
    return isinstance(record, dict) and record.get("state") == "unreadable"


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
    if state == "unreadable":
        # Placeholder from read_records(): the file could not be parsed. That is
        # "I could not find out", never a settled error/terminal.
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


def commit_terminal_authority(
    record: dict,
    *,
    state: str,
    reason: object,
    terminal_state: str | None = None,
    worker_still_alive: bool | None = None,
    headline: str | None = None,
) -> goalflight_journal.WriteResult[goalflight_journal.TerminalCommit]:
    """Sole journal emitter used by every terminal classifier."""
    dispatch_id = str(record.get("dispatch_id") or "")
    project_root = record.get("project_root") or Path.cwd()
    authority = goalflight_journal.open_or_create_journal(project_root)
    attempt = authority.attempt_for_dispatch(dispatch_id)
    if attempt is None:
        owner_label = str(record.get("controller_label") or "").strip()
        owner_session = str(record.get("controller_session_id") or "").strip()
        prepared = authority.prepare_attempt(
            dispatch_id,
            owner_controller_label=owner_label if owner_label and owner_session else None,
            owner_session_nonce=owner_session if owner_label and owner_session else None,
        )
        if not prepared.committed or prepared.value is None:
            return goalflight_journal.WriteResult(
                prepared.disposition,
                attempts=prepared.attempts,
                reason=prepared.reason,
            )
        attempt = prepared.value
    resolved_terminal = terminal_state or terminal_state_for(state, reason)
    marker_kind = reason.get("marker_kind") if isinstance(reason, dict) else None
    event_type = (
        "user_need"
        if marker_kind == "USER-NEED"
        else "user_confirm"
        if marker_kind == "USER-CONFIRM"
        else "result"
        if resolved_terminal == "complete"
        else "blocked"
    )
    observation: dict[str, object] = {
        "state": state,
        "terminal_state": resolved_terminal,
        "outcome": failure_envelope(reason) or {},
        "worker_still_alive": worker_still_alive,
    }
    if isinstance(headline, str) and headline.strip():
        observation["headline"] = headline.strip()
    observation = goalflight_output_redact.redact_data(observation)
    return authority.commit_terminal(
        attempt.attempt_id,
        terminal_state=resolved_terminal,
        observation=observation,
        event_type=event_type,
    )


def claim_attempt_running(
    project_root: Path | str,
    dispatch_id: str,
    worker_pid: int,
) -> goalflight_journal.AttemptIdentity:
    """Mark STARTING -> RUNNING from the unsandboxed watcher after spawn.

    Spawn return is the RUNNING moment: the OS has already created the
    process. This does not infer liveness from later I/O. If the worker
    died between spawn and this call, stamp ``{"pid": worker_pid}`` so
    reconciler classifies worker_dead instead of abandoning a launch
    that did start. A second call on an already-RUNNING attempt is a
    no-op so handshake retries and mocked spawn paths can share one
    owner.
    """
    if not isinstance(worker_pid, int) or worker_pid <= 0:
        raise ValueError("worker_pid must be a positive int")
    authority = goalflight_journal.Journal(project_root)
    attempt = authority.attempt_for_dispatch(dispatch_id)
    if attempt is None:
        raise RuntimeError(f"prepared attempt missing for {dispatch_id}")
    if attempt.lifecycle_state == goalflight_journal.ATTEMPT_RUNNING:
        return attempt
    if attempt.lifecycle_state != goalflight_journal.ATTEMPT_STARTING:
        raise RuntimeError(
            f"attempt {attempt.attempt_id} entered {attempt.lifecycle_state} before RUNNING"
        )
    result = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=attempt.launch_epoch,
        worker_instance=process_identity(worker_pid) or {"pid": worker_pid},
    )
    if not result.committed or result.value is None:
        raise RuntimeError(f"RUNNING claim lost for {dispatch_id}: {result.reason}")
    return result.value


def wait_attempt_running(
    project_root: Path | str,
    dispatch_id: str,
    *,
    timeout_s: float = 10.0,
) -> goalflight_journal.AttemptIdentity:
    authority = goalflight_journal.Journal.open_reader(project_root)
    deadline = time.monotonic() + timeout_s
    while True:
        attempt = authority.attempt_for_dispatch(dispatch_id)
        if attempt is None:
            raise RuntimeError(f"prepared attempt missing for {dispatch_id}")
        if attempt.lifecycle_state == goalflight_journal.ATTEMPT_RUNNING:
            return attempt
        if attempt.lifecycle_state != goalflight_journal.ATTEMPT_STARTING:
            raise RuntimeError(
                f"attempt {attempt.attempt_id} entered {attempt.lifecycle_state} before RUNNING"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"worker did not claim attempt {attempt.attempt_id} RUNNING within {timeout_s:.1f}s"
            )
        time.sleep(0.01)


#: Disposition reported when a state="running" record arrives before the
#: worker's asynchronous RUNNING claim has landed in the journal. This is a
#: startup race, not a lost compare-and-swap: the claim is still in flight,
#: so the refusal is retryable by waiting. Callers must key on this
#: structured field, never on the human-readable error text. The string must
#: match cmd_record's state="running" not-yet-RUNNING payload (committed
#: behaviour; do not re-fabricate "cas_lost").
RECORD_DISPOSITION_ATTEMPT_NOT_YET_RUNNING = "attempt_not_yet_running"

#: Bounded deadline for re-recording a state="running" transition refused
#: with RECORD_DISPOSITION_ATTEMPT_NOT_YET_RUNNING.
#:
#: Derivation:
#:   premise — the missing event is the worker's RUNNING claim, exactly one
#:     journal write: claim_attempt_running is open + read + one bounded CAS
#:     transaction (mark_attempt_running).
#:   cost — the journal bounds a single write transaction at
#:     transaction_budget_s = 1.0s (goalflight_journal); the observed
#:     uncontended cost of the claim itself is milliseconds.
#:   contention — roughly nine controllers share this box, and a project
#:     journal's writers (controller, watchers, fleet sweep) serialize on the
#:     writer lock. A pathological queue of ~10 writers each burning their
#:     entire 1s budget clears in ~10s; the realistic contended wait for one
#:     claim is far under a second.
#:   anchor — wait_attempt_running above already defaults to timeout_s=10.0
#:     for this exact wait (worker claims RUNNING); 10s is the codebase's
#:     sanctioned bound for the claim to land.
#:   sanity — 10s is 10x the journal's own worst-case single-write budget and
#:     matches the pathological ~10-writer contention estimate, and the poll
#:     below uses a read-only peek (Journal.open_reader takes no writer lock),
#:     so the retry loop itself adds no write contention.
RECORD_STARTUP_RACE_RETRY_BUDGET_S = 10.0


def parse_record_refusal(captured_stdout: str) -> dict | None:
    """Return the structured refusal payload cmd_record printed, if any.

    cmd_record emits one JSON object per refused transition
    ({"ok": False, "disposition": ..., "retryable": ..., "error": ...}).
    Parse that payload — never pattern-match the human error text — so
    callers key on ``disposition``/``retryable``.
    """
    refusal = None
    for line in captured_stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("ok") is False:
            refusal = payload
    return refusal


def is_retryable_startup_race(refusal: dict | None) -> bool:
    """True when the refusal is the startup race, i.e. retryable by waiting."""
    return (
        isinstance(refusal, dict)
        and refusal.get("disposition") == RECORD_DISPOSITION_ATTEMPT_NOT_YET_RUNNING
        and refusal.get("retryable") is True
    )


def worker_spawn_state(worker_pid: int | None) -> str:
    """Three-state answer to "was a worker process spawned on this path?"

    The spawn helpers either return the child's positive pid or raise, so
    None is definitive: no spawn happened here. A positive int names the
    spawned process. Anything else is indeterminate — and indeterminate is
    NOT "no worker": callers must take the safe branch (write the status file
    and warn), because a spurious status file costs an operator one warning
    read, while a missing one costs duplicated work and a two-writer tree
    collision.
    """
    if worker_pid is None:
        return "none"
    if isinstance(worker_pid, bool) or not isinstance(worker_pid, int):
        return "unknown"
    return "spawned" if worker_pid > 0 else "unknown"


def retry_record_after_startup_race(
    record_once,
    initial_code: int,
    initial_refusal: dict | None,
    *,
    project_root: Path | str,
    dispatch_id: str,
    timeout_s: float = RECORD_STARTUP_RACE_RETRY_BUDGET_S,
    poll_s: float = 0.05,
) -> tuple[int, dict | None]:
    """Re-record a startup-race-refused transition once the attempt is RUNNING.

    ``record_once`` performs one record attempt and returns
    ``(exit_code, refusal_payload_or_None)``; the initial attempt's result is
    passed in so the original refusal survives a deadline that expires before
    the attempt ever reaches RUNNING. An attempt that reaches a final state
    can never be claimed, so the wait ends early instead of burning the whole
    budget. Returns the last ``(exit_code, refusal)``; ``(0, None)`` means
    the re-record committed. The peek is read-only (open_reader takes no
    writer lock), so polling does not contend with the in-flight claim.
    """
    deadline = time.monotonic() + timeout_s
    code, refusal = initial_code, initial_refusal
    while time.monotonic() < deadline:
        attempt = None
        with contextlib.suppress(Exception):
            attempt = goalflight_journal.Journal.open_reader(
                project_root
            ).attempt_for_dispatch(dispatch_id)
        if attempt is None:
            time.sleep(poll_s)
            continue
        if attempt.lifecycle_state in goalflight_journal.ATTEMPT_FINAL_STATES:
            break
        if attempt.lifecycle_state == goalflight_journal.ATTEMPT_RUNNING:
            code, refusal = record_once()
            if code == 0 or not is_retryable_startup_race(refusal):
                break
        time.sleep(poll_s)
    return code, refusal


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
    # Ownership comes from the long-lived controller beacon. This command is
    # commonly called by short-lived dispatcher/runner processes, whose pids
    # are implementation details rather than evidence of a controller.
    controller_pid = getattr(args, "controller_pid", None)
    controller_session_id = getattr(args, "controller_session_id", None)
    controller_label = getattr(args, "controller_label", None)
    owner_controller_label = None
    if not controller_session_id or controller_pid is None:
        controller_pid = None
        controller_session_id = None
        controller_label = None
    else:
        controller_session_id = str(controller_session_id)
        owner_controller_label = str(controller_label) if controller_label else None
        controller_label = owner_controller_label[:64] if owner_controller_label else None
    claimant_pid = getattr(args, "claimant_pid", None)
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
        "controller_session_id": controller_session_id,
        "controller_label": controller_label,
        "controller_identity": process_identity(controller_pid),
        "claimant_pid": claimant_pid,
        "claimant_identity": process_identity(claimant_pid),
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
    effective_account = getattr(args, "effective_account", None)
    if effective_account:
        record["effective_account"] = effective_account
    for key in (
        "engine_session_id",
        "codex_session_id",
        "codex_home",
        "codex_home_owner_dispatch_id",
        "parent_dispatch_id",
    ):
        value = getattr(args, key, None)
        if value:
            record[key] = value
    if record.get("codex_session_id") and not record.get("engine_session_id"):
        record["engine_session_id"] = record["codex_session_id"]
    request_envelope_json = getattr(args, "request_envelope_json", None)
    if request_envelope_json:
        try:
            request_envelope = json.loads(request_envelope_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            request_envelope = None
        if isinstance(request_envelope, dict):
            record["request_envelope"] = request_envelope
    worker_cwd = getattr(args, "worker_cwd", None)
    if worker_cwd:
        record["worker_cwd"] = str(worker_cwd)
    dispatch_argv = getattr(args, "dispatch_argv", None)
    if isinstance(dispatch_argv, list) and dispatch_argv:
        record["dispatch_argv"] = [str(part) for part in dispatch_argv]
    task_ids = task_ids_from_args(args)
    if task_ids:
        record["task_ids"] = task_ids
    if getattr(args, "detached", False):
        record["detached"] = True
    if getattr(args, "queue_launch_token", None):
        record["queue_launch_token"] = args.queue_launch_token
    if args.state in {"waiting_capacity", "starting", "running"}:
        authority = goalflight_journal.open_or_create_journal(args.project_root)
        prepared = authority.prepare_attempt(
            dispatch_id,
            launch_token=getattr(args, "queue_launch_token", None),
            defer_start_deadline=args.state == "waiting_capacity",
            owner_controller_label=owner_controller_label,
            owner_session_nonce=(
                controller_session_id if owner_controller_label is not None else None
            ),
            effective_account=effective_account,
            engine=engine,
        )
        if not prepared.committed or prepared.value is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "dispatch_id": dispatch_id,
                        "disposition": prepared.disposition.value,
                        "error": prepared.reason,
                    },
                    sort_keys=True,
                )
            )
            return 3 if prepared.cas_lost else 2
        identity = prepared.value
        if args.state == "starting":
            started = authority.start_attempt(
                identity.attempt_id,
                identity.launch_token,
                expected_launch_epoch=identity.launch_epoch,
            )
            if not started.committed or started.value is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "dispatch_id": dispatch_id,
                            "disposition": started.disposition.value,
                            "error": started.reason,
                        },
                        sort_keys=True,
                    )
                )
                return 3 if started.cas_lost else 2
            identity = started.value
        elif args.state == "running":
            if identity.lifecycle_state != goalflight_journal.ATTEMPT_RUNNING:
                # NOT a CAS loss. Nothing lost a compare-and-swap here: the
                # attempt simply has not reached RUNNING yet, because the worker
                # claims RUNNING asynchronously after it is spawned. This branch
                # previously reported the fabricated disposition "cas_lost",
                # which sent readers hunting for contention that never happened
                # and, worse, made a launch race indistinguishable from a real
                # concurrent-writer loss (:920 and :940, which are genuine).
                #
                # Callers must be able to tell "not yet" from "lost", because
                # the correct responses differ: "not yet" is retryable against
                # the worker's own startup, a genuine loss is not. Keep exit 3
                # for compatibility with existing callers, but say what happened.
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "dispatch_id": dispatch_id,
                            "disposition": "attempt_not_yet_running",
                            "retryable": True,
                            "error": (
                                f"attempt state is {identity.lifecycle_state}, worker has not "
                                "claimed RUNNING yet; this is a startup race, not a lost CAS, "
                                "and the worker may already be alive"
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 3
        record["attempt_id"] = identity.attempt_id
        record["launch_token"] = identity.launch_token
        record["launch_epoch"] = identity.launch_epoch
    with StateLock():
        path = record_path(dispatch_id)
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("started_at"):
                record["started_at"] = existing["started_at"]
            if (
                "request_envelope" not in record
                and isinstance(existing.get("request_envelope"), dict)
            ):
                record["request_envelope"] = existing["request_envelope"]
            if "worker_cwd" not in record and existing.get("worker_cwd"):
                record["worker_cwd"] = existing["worker_cwd"]
            if "dispatch_argv" not in record and isinstance(
                existing.get("dispatch_argv"), list
            ):
                record["dispatch_argv"] = existing["dispatch_argv"]
            existing_controller_session_id = existing.get("controller_session_id")
            existing_controller_pid = existing.get("controller_pid")
            if (
                record.get("controller_session_id") is None
                and existing_controller_session_id
                and existing_controller_pid is not None
            ):
                record["controller_session_id"] = existing_controller_session_id
                record["controller_pid"] = existing_controller_pid
                record["controller_label"] = existing.get("controller_label")
                record["controller_identity"] = existing.get("controller_identity")
            elif (
                record.get("controller_label") is None
                and record.get("controller_session_id") == existing_controller_session_id
                and record.get("controller_pid") == existing_controller_pid
            ):
                record["controller_label"] = existing.get("controller_label")
            if record.get("claimant_pid") is None and existing.get("claimant_pid"):
                record["claimant_pid"] = existing["claimant_pid"]
                record["claimant_identity"] = existing.get("claimant_identity")
            for key in (
                "engine_session_id",
                "codex_session_id",
                "codex_home",
                "codex_home_owner_dispatch_id",
                "parent_dispatch_id",
            ):
                if key not in record and existing.get(key):
                    record[key] = existing[key]
            if record.get("codex_session_id") and not record.get("engine_session_id"):
                record["engine_session_id"] = record["codex_session_id"]
        path = write_record(record)
    try:
        # Prompt mirrors are immutable and write-once. This derived projection
        # must never make a dispatch fail; the hourly producer catch-up repairs
        # a missed write.
        goalflight_fleet_console_history.project_prompt(record)
    except Exception:
        pass
    payload = {"ok": True, "dispatch_id": dispatch_id, "path": str(path), "state": record["state"]}
    print(json.dumps(payload, indent=None if args.json else 2, sort_keys=True))
    return 0


_RETRY_DISPATCH_ID_RE = re.compile(r"^(?P<base>.+)-retry-[0-9a-f]{8}$")


def retry_base_dispatch_id(dispatch_id: str) -> str | None:
    match = _RETRY_DISPATCH_ID_RE.fullmatch(str(dispatch_id or ""))
    return match.group("base") if match else None


def lookup_requeue_base(dispatch_id: str) -> dict[str, str | None]:
    """Name the base ledger row a retry id belongs to, when that can be proven.

    Intent match on a readable parent is a determination. The `-retry-<hex>`
    parse is a hint from the id shape, not proof the base row exists. An
    unlistable or unreadable ledger is UNKNOWN and is reported as such.
    """
    parsed = retry_base_dispatch_id(dispatch_id)
    found_id: str | None = None
    listing = "ok"
    try:
        records = read_records()
    except OSError:
        listing = "unknown"
        records = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record_is_unreadable(record):
            listing = "unknown"
            continue
        intent = record.get("requeue")
        if isinstance(intent, dict) and intent.get("child_id") == dispatch_id:
            parent = str(record.get("dispatch_id") or "")
            if parent:
                found_id = parent
                break
    if found_id:
        source = "ledger_intent"
        base_id = found_id
    elif parsed:
        source = "id_pattern"
        base_id = parsed
    else:
        source = None
        base_id = None
    return {
        "base_dispatch_id": base_id,
        "base_source": source,
        "listing": listing,
    }


def _missing_dispatch_payload(dispatch_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": False,
        "error": "missing_dispatch",
        "dispatch_id": dispatch_id,
    }
    lookup = lookup_requeue_base(dispatch_id)
    if lookup.get("base_dispatch_id"):
        payload["base_dispatch_id"] = lookup["base_dispatch_id"]
        payload["base_source"] = lookup["base_source"]
        payload["hint"] = (
            "retry ids have no ledger row; act on the base dispatch_id"
        )
    if lookup.get("listing") == "unknown":
        payload["ledger_listing"] = "unknown"
    return payload


def cmd_finish(args: argparse.Namespace) -> int:
    path = record_path(args.dispatch_id, create=False)
    if not path.exists():
        print(json.dumps(_missing_dispatch_payload(args.dispatch_id), sort_keys=True))
        return 1
    record = json.loads(path.read_text())
    requested_terminal = getattr(args, "terminal_state", None)
    terminal_state = requested_terminal or terminal_state_for(args.state, args.reason)
    committed = commit_terminal_authority(
        record,
        state=args.state,
        reason=args.reason,
        terminal_state=terminal_state,
        worker_still_alive=getattr(args, "worker_still_alive", None),
        headline=getattr(args, "headline", None),
    )
    if not committed.committed or committed.value is None:
        print(json.dumps({
            "ok": False,
            "dispatch_id": args.dispatch_id,
            "disposition": committed.disposition.value,
            "error": committed.reason,
        }, sort_keys=True))
        return 3 if committed.cas_lost else 2
    winner = committed.value
    if (
        winner.idempotent
        and requested_terminal
        and requested_terminal != winner.terminal_state
    ):
        # Journal already has a terminal winner. Applying a *different*
        # explicit --terminal-state would be a silent no-op: ok:true with
        # the original state. Same-state retries stay quiet so crash-retry
        # callers keep working. Refuse, do not ACT — journal first-writer
        # wins, and the requeue intent (not finish) is the lever for quota
        # retry hygiene.
        print(json.dumps({
            "ok": False,
            "error": "already_terminal",
            "dispatch_id": args.dispatch_id,
            "current_state": record.get("state"),
            "current_terminal_state": winner.terminal_state,
            "requested_terminal_state": requested_terminal,
        }, sort_keys=True))
        return 2
    with StateLock():
        record = json.loads(path.read_text())
        terminal_state = winner.terminal_state
        existing_terminal = _terminal_key(record)
        winner_state = str(winner.observation.get("state") or terminal_state)
        effective_state = (
            record.get("state")
            if winner.idempotent and existing_terminal == terminal_state
            else winner_state
        )
        record["state"] = effective_state
        ended_at = preserve_first_terminal_time(record, winner.terminal_at)
        record["terminal_state"] = terminal_state
        record["liveness_state"] = goalflight_terminal.terminal_liveness_state(effective_state)
        elapsed_s = getattr(args, "elapsed_s", None)
        if elapsed_s is None:
            elapsed_s = elapsed_seconds(record, ended_at)
        if elapsed_s is not None:
            record["elapsed_s"] = round(float(elapsed_s), 3)
        if hasattr(args, "worker_still_alive"):
            record["worker_still_alive"] = args.worker_still_alive
        winner_outcome = winner.observation.get("outcome")
        envelope = dict(winner_outcome) if isinstance(winner_outcome, dict) else None
        record["outcome"] = {"terminal_state": terminal_state}
        if envelope:
            record.update(envelope)
            record["outcome"].update(envelope)
        headline = winner.observation.get("headline")
        if isinstance(headline, str) and headline.strip():
            record["headline"] = headline.strip()
        winner_reason = None
        if isinstance(envelope, dict):
            winner_reason = envelope.get("error") or envelope.get("reason")
        if isinstance(winner_reason, dict) and winner_reason.get("limit_kind"):
            for key in (
                "limit_kind",
                "limit_signature",
                "reset_at",
                "retry_after",
            ):
                value = winner_reason.get(key)
                record[key] = value
                record["outcome"][key] = value
        record["attempt_id"] = winner.attempt_id
        record["transition_id"] = winner.transition_id
        record["terminal_event_uuid"] = winner.event_uuid
        write_record(record)
        # status.json is a watcher heartbeat copy, not a second lifecycle
        # home. Readers derive running/terminal from this ledger/journal
        # write. Updating the sidecar here would mint a third writer of
        # the same fact.
    try:
        import goalflight_messages

        authority = goalflight_journal.open_or_create_journal(
            record.get("project_root") or Path.cwd()
        )
        authority.project_terminal_outbox(messages_dir=goalflight_messages.default_messages_dir())
    except Exception:
        # Projection is derived and retried by reconciliation. The committed
        # journal state/outbox pair remains the terminal authority.
        pass
    try:
        goalflight_fleet_console_history.project_terminal(record)
    except (KeyboardInterrupt, SystemExit):
        # The terminal commit and wake projection above are already durable;
        # say so before honoring the interrupt, so the abort cannot be read
        # as a lost terminal.
        print(
            "history projection interrupted; terminal and wake are already durable",
            file=sys.stderr,
        )
        raise
    except BaseException:
        # Like the terminal outbox projection, history is derived and repaired
        # by the producer's slow catch-up sweep.
        pass
    try:
        import goalflight_trace_archive

        goalflight_trace_archive.archive_finished_dispatch(record, apply=True)
    except Exception:
        # Archive is best-effort. A full disk must not fail a terminal commit.
        pass
    print(json.dumps({
        "ok": True,
        "dispatch_id": args.dispatch_id,
        "state": args.state,
        "attempt_id": winner.attempt_id,
        "transition_id": winner.transition_id,
        "event_uuid": winner.event_uuid,
        "idempotent": winner.idempotent,
    }, sort_keys=True))
    return 0


def _unlink_queue_entry(dispatch_id: str) -> None:
    path = goalflight_dispatch_paths.queue_entry_path(dispatch_id)
    with contextlib.suppress(OSError):
        path.unlink()


def cmd_cancel_requeue(args: argparse.Namespace) -> int:
    """Mark a persisted requeue intent abandoned so it cannot regenerate."""
    requested_id = str(args.dispatch_id or "")
    path = record_path(requested_id, create=False)
    acted_id = requested_id
    via_retry_id = False
    if not path.exists():
        lookup = lookup_requeue_base(requested_id)
        base_id = lookup.get("base_dispatch_id")
        if not base_id:
            print(json.dumps(_missing_dispatch_payload(requested_id), sort_keys=True))
            return 1
        path = record_path(base_id, create=False)
        if not path.exists():
            payload = _missing_dispatch_payload(requested_id)
            payload["error"] = "missing_base_dispatch"
            print(json.dumps(payload, sort_keys=True))
            return 1
        acted_id = base_id
        via_retry_id = True
    with StateLock():
        record = json.loads(path.read_text(encoding="utf-8"))
        intent = record.get("requeue")
        if not isinstance(intent, dict):
            print(json.dumps({
                "ok": False,
                "error": "no_requeue_intent",
                "dispatch_id": acted_id,
            }, sort_keys=True))
            return 1
        existing = intent.get("disposition")
        if existing in {"satisfied", "abandoned", "expired"}:
            payload = {
                "ok": True,
                "dispatch_id": acted_id,
                "disposition": existing,
                "idempotent": True,
            }
            if via_retry_id:
                payload["requested_dispatch_id"] = requested_id
            print(json.dumps(payload, sort_keys=True))
            return 0
        now = utc_now()
        updated = dict(intent)
        updated["disposition"] = "abandoned"
        updated["disposition_at"] = now
        updated["disposition_reason"] = "operator_cancel"
        record["requeue"] = updated
        write_record(record)
    child_id = updated.get("child_id")
    if isinstance(child_id, str) and child_id:
        _unlink_queue_entry(child_id)
    payload = {
        "ok": True,
        "dispatch_id": acted_id,
        "disposition": "abandoned",
        "idempotent": False,
        "child_id": child_id,
    }
    if via_retry_id:
        payload["requested_dispatch_id"] = requested_id
    print(json.dumps(payload, sort_keys=True))
    return 0


def _record_sidecar_overrule(
    authority: goalflight_journal.Journal,
    record: dict,
    *,
    state: str,
    reason: object,
    terminal_state: str,
    liveness: str,
    liveness_reason: str,
    observed_at: str,
) -> dict[str, object]:
    """Surface a held sidecar verdict as a durable journal attention item.

    The sidecar said terminal and the recorded worker identity disagreed (or
    could not be read). The reconciler holds journal/ledger non-terminal; this
    is the visible half of that decision, so a sidecar verdict is never
    silently dropped. One OPEN item per (dispatch, sidecar state, liveness)
    via the deterministic item id; repeat reconciles do not pile up rows.
    """
    dispatch_id = str(record.get("dispatch_id") or "")
    payload = goalflight_output_redact.redact_data(
        {
            "dispatch_id": dispatch_id,
            "sidecar_state": state,
            "terminal_state": terminal_state,
            "sidecar_reason": reason,
            "liveness": liveness,
            "liveness_reason": liveness_reason,
            "worker_pid": record.get("worker_pid"),
            "observed_at": observed_at,
            "text": (
                f"sidecar verdict '{terminal_state}' for {dispatch_id} not "
                f"promoted: worker identity is {liveness} ({liveness_reason}); "
                "journal and ledger left non-terminal"
                + (
                    ". Held until a later reconcile can determine identity "
                    "(the record gains a pid, a RUNNING journal instance is "
                    "copied onto it, or the process table becomes readable) "
                    "or a human/higher-authority pass acts; not resolved by age"
                    if liveness == "unknown"
                    else ""
                )
            ),
        }
    )
    write = authority.record_system_attention(
        item_type="sidecar_terminal_overruled",
        reason=f"worker_identity_{liveness}",
        dedupe_namespace="sidecar-terminal-overruled",
        dedupe_key=f"{dispatch_id}:{state}:{liveness}",
        payload=payload,
    )
    return {
        "dispatch_id": dispatch_id,
        "sidecar_state": state,
        "terminal_state": terminal_state,
        "liveness": liveness,
        "liveness_reason": liveness_reason,
        "worker_pid": record.get("worker_pid"),
        "attention_item_id": (
            str(write.value["item_id"]) if write.committed and write.value else None
        ),
    }


def reconcile_terminal_outbox(
    project_root: Path | str,
    *,
    messages_dir: Path | None = None,
) -> dict[str, object]:
    """Repair terminal authority and classify provably dead workers once."""
    canonical_root = goalflight_task.resolve_project_root(str(project_root))
    authority = goalflight_journal.open_or_create_journal(canonical_root)
    reconcile_at = utc_now()
    expired_launches = {
        str(row["dispatch_id"]): row
        for row in authority.read_all(
            """SELECT attempt_id, dispatch_id, launch_token, launch_epoch,
                      lifecycle_state, start_deadline_at
               FROM dispatch_attempts
               WHERE lifecycle_state IN ('PREPARED', 'STARTING')
                 AND start_deadline_at IS NOT NULL
                 AND start_deadline_at <= ?""",
            (reconcile_at,),
        )
    }
    running_attempts = {
        str(row["dispatch_id"]): row
        for row in authority.read_all(
            """SELECT attempt_id, dispatch_id, launch_token, launch_epoch,
                      lifecycle_state, worker_instance_json
               FROM dispatch_attempts
               WHERE lifecycle_state = 'RUNNING'
                 AND worker_instance_json IS NOT NULL"""
        )
    }
    committed = 0
    already_terminal = 0
    retryable = 0
    cas_lost = 0
    overruled: list[dict[str, object]] = []
    history_records: list[dict] = []
    records = read_records()
    known_dispatch_ids = {
        str(record.get("dispatch_id"))
        for record in records
        if isinstance(record, dict) and record.get("dispatch_id")
    }
    for dispatch_id, row in expired_launches.items():
        if dispatch_id not in known_dispatch_ids:
            records.append(
                {
                    "dispatch_id": dispatch_id,
                    "project_root": str(canonical_root),
                    "state": str(row["lifecycle_state"]).lower(),
                    "attempt_id": str(row["attempt_id"]),
                    "launch_token": str(row["launch_token"]),
                    "launch_epoch": int(row["launch_epoch"]),
                }
            )
    for dispatch_id, row in running_attempts.items():
        try:
            worker_instance = json.loads(str(row["worker_instance_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(worker_instance, dict) or not worker_instance.get("pid"):
            continue
        record = None
        for candidate in records:
            if (
                not isinstance(candidate, dict)
                or str(candidate.get("dispatch_id") or "") != dispatch_id
                or not candidate.get("project_root")
            ):
                continue
            try:
                candidate_root = goalflight_task.resolve_project_root(
                    str(candidate["project_root"])
                )
            except Exception:
                continue
            if candidate_root == canonical_root:
                record = candidate
                break
        if record is None:
            record = {
                "dispatch_id": dispatch_id,
                "project_root": str(canonical_root),
                "state": "running",
            }
            records.append(record)
        if not record.get("worker_pid"):
            # Re-probe path for an unknown hold: a RUNNING journal instance
            # is trusted identity (the worker claimed itself). Copy it onto
            # the in-memory record AND the durable ledger row so the record
            # actually gains a pid. The next identity check in this pass
            # (and later reconciles / operator surfaces) can then resolve
            # live vs dead. Age is not consulted.
            hydrated = {
                "worker_pid": worker_instance["pid"],
                "worker_identity": worker_instance,
                "worker_pgid": worker_instance.get("pgid"),
                "attempt_id": str(row["attempt_id"]),
                "launch_token": str(row["launch_token"]),
                "launch_epoch": int(row["launch_epoch"]),
            }
            if _terminal_key(record) in {"", "unknown", "watcher_stopped"}:
                hydrated["state"] = "running"
            record.update(hydrated)
            _stamp_nonterminal_fields(record, hydrated)
    for record in records:
        if not isinstance(record, dict) or not record.get("dispatch_id"):
            continue
        if not record.get("project_root"):
            continue
        try:
            record_root = goalflight_task.resolve_project_root(
                str(record["project_root"])
            )
        except Exception:
            continue
        if record_root != canonical_root:
            continue
        terminal_state = _terminal_key(record)
        state = str(record.get("state") or "")
        reason: object = record.get("reason") or record.get("error")
        status_observation = _terminal_sidecar_observation(record)
        expired_launch = str(record["dispatch_id"]) in expired_launches
        needs_ledger_projection = status_observation is not None or expired_launch or terminal_state in {
            "",
            "unknown",
            "watcher_stopped",
        }
        if status_observation is not None:
            state = str(
                status_observation.get("terminal_pending_state")
                or status_observation.get("state")
            )
            reason = status_observation.get("reason") or status_observation.get("error")
            record_terminal_key = terminal_state
            terminal_state = terminal_state_for(state, reason)
            # Process identity outranks a sidecar verdict. A terminal sidecar
            # (failed / idle_timeout / complete) is a statement about the
            # dispatch channel, not about the worker: watchers write
            # idle_timeout after the worker already did its work, and a
            # status-write-error mirror says failed while the worker runs on.
            # While the recorded identity (pid AND start token, never pid
            # alone) still matches a live process, promoting the sidecar would
            # terminalize a running worker -- first-terminal-wins poisons the
            # journal, frees the capacity lease, and the worktree-GC ownership
            # predicate (keyed on non-terminal ledger state) would read the
            # tree as unowned. Hold instead, and surface the disagreement as
            # an attention item: a silently ignored sidecar is its own defect.
            #
            # Three-state: when liveness cannot be determined (no recorded
            # identity fields, unreadable process table) the verdict is
            # UNKNOWN and UNKNOWN holds. Terminalizing is the destructive
            # direction, so doubt resolves against the write. This is
            # deliberately the opposite default from admission control, which
            # treats an unproven worker as not reusable: there the cheap error
            # is a blocked reuse, here the expensive error is a terminal write
            # over live work. A genuinely dead worker -- identity gone, or the
            # pid now belongs to a different process -- still terminalizes
            # below, exactly as before.
            #
            # Unknown is a hold, not a swallow, and it is not age-based. Each
            # reconcile re-probes identity. The resolution path is: a RUNNING
            # journal instance copied onto the record (the record gains a
            # pid), or the process table becoming readable again. Either makes
            # liveness determinable -- live keeps holding, dead terminalizes.
            # A timer that terminalizes an indeterminate record would
            # terminalize live workers whose identity was merely unreadable
            # at one moment. Still-unknown stays held until that re-probe
            # succeeds or a human / higher-authority pass acts, and the hold
            # is stamped on the ledger so operator surfaces can name it.
            #
            # The gate applies only to the FIRST terminal write. When the
            # record or the journal attempt is already terminal, the terminal
            # authority has already fired: the re-commit below is idempotent
            # and first-terminal-wins keeps the original verdict, so a
            # disagreeing sidecar cannot overwrite it. Gating that path would
            # strand the ledger/journal repair this reconciler exists to do.
            if record_terminal_key in {"", "unknown", "watcher_stopped"}:
                attempt = authority.attempt_for_dispatch(str(record["dispatch_id"]))
                attempt_final = (
                    attempt is not None
                    and attempt.lifecycle_state
                    in goalflight_journal.ATTEMPT_FINAL_STATES
                )
                if not attempt_final:
                    liveness, liveness_reason = worker_identity_liveness(record)
                    if liveness != "dead":
                        authority.resolve_system_attention(
                            item_type="sidecar_terminal_overruled",
                            dispatch_id=str(record["dispatch_id"]),
                            keep_reason=f"worker_identity_{liveness}",
                        )
                        overruled.append(
                            _record_sidecar_overrule(
                                authority,
                                record,
                                state=state,
                                reason=reason,
                                terminal_state=terminal_state,
                                liveness=liveness,
                                liveness_reason=liveness_reason,
                                observed_at=reconcile_at,
                            )
                        )
                        _stamp_nonterminal_fields(
                            record,
                            {
                                "sidecar_hold": liveness,
                                "sidecar_hold_reason": liveness_reason,
                            },
                        )
                        continue
                authority.resolve_system_attention(
                    item_type="sidecar_terminal_overruled",
                    dispatch_id=str(record["dispatch_id"]),
                )
        elif expired_launch:
            state = "abandoned"
            terminal_state = "abandoned"
            reason = {
                "reason": "start_claim_deadline_expired",
                "prior_state": record.get("state"),
            }
        elif terminal_state in {"", "unknown", "watcher_stopped"}:
            classification = classify(record)
            if classification not in {"worker_dead", "stale_dead"}:
                continue
            state = "worker_dead"
            terminal_state = "worker_dead"
            reason = {
                "reason": "reconciler_observed_identity_dead",
                "prior_state": record.get("state"),
            }
        if expired_launch:
            result = authority.commit_expired_attempt(
                str(expired_launches[str(record["dispatch_id"])]["attempt_id"]),
                observed_at=reconcile_at,
                terminal_state=terminal_state,
                observation={
                    "state": state,
                    "terminal_state": terminal_state,
                    "outcome": failure_envelope(reason) or {},
                    "worker_still_alive": False,
                },
            )
        else:
            result = commit_terminal_authority(
                record,
                state=state or terminal_state,
                reason=reason,
                terminal_state=terminal_state,
                worker_still_alive=False,
            )
        if result.committed and result.value is not None:
            if result.value.idempotent:
                already_terminal += 1
            else:
                committed += 1
            if needs_ledger_projection:
                with StateLock():
                    current_path = record_path(str(record["dispatch_id"]))
                    current = (
                        json.loads(current_path.read_text())
                        if current_path.exists()
                        else dict(record)
                    )
                    current.pop("sidecar_hold", None)
                    current.pop("sidecar_hold_reason", None)
                    current.update(
                        {
                            "state": str(
                                result.value.observation.get("state")
                                or result.value.terminal_state
                            ),
                            "terminal_state": result.value.terminal_state,
                            "worker_still_alive": False,
                            "attempt_id": result.value.attempt_id,
                            "transition_id": result.value.transition_id,
                            "terminal_event_uuid": result.value.event_uuid,
                            "reason": reason,
                        }
                    )
                    preserve_first_terminal_time(current, result.value.terminal_at)
                    write_record(current)
                history_records.append(dict(current))
        elif result.cas_lost:
            cas_lost += 1
        else:
            retryable += 1
    if messages_dir is None:
        import goalflight_messages

        messages_dir = goalflight_messages.default_messages_dir()
    projected = authority.project_terminal_outbox(messages_dir=messages_dir)
    # Match cmd_finish's ordering: the wake projection is latency-sensitive,
    # while the console history blob is derived, blocking, and repairable by
    # catch-up. A slow or broken history writer must never delay or fail wake.
    for current in history_records:
        try:
            goalflight_fleet_console_history.project_terminal(current)
        except (KeyboardInterrupt, SystemExit):
            print(
                "history projection interrupted; terminals and wake are already durable",
                file=sys.stderr,
            )
            raise
        except BaseException:
            pass
    return {
        "ok": retryable == 0,
        "project_root": str(canonical_root),
        "committed": committed,
        "already_terminal": already_terminal,
        "cas_lost": cas_lost,
        "retryable": retryable,
        "overruled": overruled,
        "projected": len(projected),
    }


def cmd_reconcile_outbox(args: argparse.Namespace) -> int:
    payload = reconcile_terminal_outbox(args.project_root)
    print(json.dumps(payload, indent=None if args.json else 2, sort_keys=True))
    return 0 if payload["ok"] else 2


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
            "effective_account": r.get("effective_account"),
            "codex_session_id": r.get("codex_session_id"),
            "codex_home": r.get("codex_home"),
            "codex_home_owner_dispatch_id": r.get(
                "codex_home_owner_dispatch_id"
            ),
            "parent_dispatch_id": r.get("parent_dispatch_id"),
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
            "controller_pid": r.get("controller_pid"),
            "controller_session_id": r.get("controller_session_id"),
            "prompt_path": r.get("prompt_path"),
            "task_ids": r.get("task_ids") if isinstance(r.get("task_ids"), list) else [],
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
        hold = sidecar_terminal_hold(r)
        if hold is not None:
            row["sidecar_hold"] = hold["liveness"]
            row["sidecar_hold_reason"] = hold["reason"]
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


NONE_SANDBOX_TRIPLET = "requested=none supported=none enforced=none"


def sandbox_triplet(posture: object) -> str | None:
    if not isinstance(posture, dict):
        return None
    if not any(
        key in posture
        for key in ("requested_profile", "supported_profile", "enforced_profile")
    ):
        return None
    return (
        f"requested={posture.get('requested_profile') or 'none'}"
        f" supported={posture.get('supported_profile') or 'none'}"
        f" enforced={posture.get('enforced_profile') or 'none'}"
    )


def _record_status_line(row: dict, sandbox_suffix: str) -> str:
    return (
        f"- {row['classification']}: {row.get('dispatch_id')} "
        f"agent={row.get('agent')} pid={row.get('worker_pid')} "
        f"state={row.get('state')}{sandbox_suffix}"
    )


def format_status_lines(
    payload: dict, *, limit: int = 20, verbose: bool = False
) -> list[str]:
    """Human ledger status. Uniform none-sandbox is omitted unless --verbose."""
    lines = [f"dispatch ledger: {payload['state_dir']}"]
    rows = list(payload.get("records") or [])[:limit]
    triplets = [sandbox_triplet(row.get("os_sandbox")) for row in rows]
    interesting = {item for item in triplets if item and item != NONE_SANDBOX_TRIPLET}
    unique = set(triplets)
    if verbose:
        show_on = "all"
        summary: str | None = None
    elif not interesting:
        show_on = "none"
        summary = None
    elif unique == interesting and len(interesting) == 1:
        show_on = "none"
        summary = f"sandbox {next(iter(interesting))}"
    else:
        show_on = "interesting"
        summary = None
    if summary:
        lines.append(summary)
    for row, triplet in zip(rows, triplets):
        if show_on == "all" and triplet:
            suffix = f" sandbox {triplet}"
        elif show_on == "interesting" and triplet and triplet != NONE_SANDBOX_TRIPLET:
            suffix = f" sandbox {triplet}"
        else:
            suffix = ""
        lines.append(_record_status_line(row, suffix))
    surplus = list(payload.get("surplus_processes") or [])[:limit]
    if surplus:
        lines.append("surplus worker-like processes:")
        for proc in surplus:
            lines.append(
                f"- pid={proc['pid']} comm={proc['comm']} args={proc['args']}"
            )
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    for line in format_status_lines(
        payload,
        limit=args.limit,
        verbose=getattr(args, "verbose", False),
    ):
        print(line)
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
    rec.add_argument("--effective-account")
    rec.add_argument("--request-envelope-json", help=argparse.SUPPRESS)
    rec.add_argument("--transport", default="unknown")
    rec.add_argument("--project-root")
    rec.add_argument("--controller-pid", type=int)
    rec.add_argument("--controller-session-id")
    rec.add_argument("--controller-label")
    rec.add_argument("--claimant-pid", type=int)
    rec.add_argument("--worker-pid", type=int)
    rec.add_argument("--acp-session-id")
    rec.add_argument("--logical-session-id")
    rec.add_argument("--lease-id")
    rec.add_argument("--stdout-path")
    rec.add_argument("--stderr-path")
    rec.add_argument("--status-path")
    rec.add_argument("--os-sandbox-json")
    # RUNNING belongs to the worker's pre-exec journal claim. A bare record
    # command can truthfully prepare STARTING, but it cannot impersonate that
    # worker-owned transition.
    rec.add_argument("--state", default="starting")
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

    cancel_requeue = sub.add_parser(
        "cancel-requeue",
        help="abandon a persisted quota/auth requeue intent so it cannot regenerate",
    )
    cancel_requeue.add_argument("--dispatch-id", required=True)
    cancel_requeue.set_defaults(func=cmd_cancel_requeue)

    reconcile = sub.add_parser(
        "reconcile-outbox",
        help="repair terminal journal authority and project pending outbox rows",
    )
    reconcile.add_argument("--project-root", type=Path, default=Path.cwd())
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=cmd_reconcile_outbox)

    stat = sub.add_parser("status")
    stat.add_argument("--json", action="store_true")
    stat.add_argument("--limit", type=int, default=20)
    stat.add_argument(
        "--verbose",
        action="store_true",
        help="include per-row sandbox triplets even when they are uniform",
    )
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
