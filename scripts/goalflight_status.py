#!/usr/bin/env python3
"""Compact status aggregator for goal-flight runtime state.

Terse and scoped to the current repo BY DEFAULT: the agent-facing front door is
one-line text plus an exit-code predicate (``--done``); ``--json`` is the machine
basement. Sibling projects that share the ``/tmp`` dispatch ledger are screened
out unless ``--all-projects`` is given.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_compat
import goalflight_dispatch_states as dispatch_states
import goalflight_ledger
from goalflight_liveness import cpu_confirmed_idle
import goalflight_milestone
from goalflight_watch import BLOCKING_TERMINAL_MARKERS, SUCCESS_TERMINAL_MARKERS, _final_terminal_marker

# Each aggregated record carries a precomputed ``classification`` from
# goalflight_ledger.classify(): the terminal STATE string when terminal, else one
# of these live/ambiguous labels. Do NOT re-run classify() here for normal
# records -- the aggregated record may have had identity fields stripped, so
# re-classifying would misread a live worker as unknown.
_LIVE_CLASS = "expected_live"
_AMBIGUOUS_CLASS = dispatch_states.AMBIGUOUS_LIVE_CLASSES
_LIVENESS_RECHECK_CLASSES = dispatch_states.LIVENESS_RECHECK_STATES
_OUTPUT_TAIL_SUCCESS_MARKERS = SUCCESS_TERMINAL_MARKERS
_OUTPUT_TAIL_BLOCKING_MARKERS = BLOCKING_TERMINAL_MARKERS
_OUTPUT_TAIL_TERMINAL_MARKERS = _OUTPUT_TAIL_SUCCESS_MARKERS | _OUTPUT_TAIL_BLOCKING_MARKERS
_OUTPUT_TAIL_IDLE_RECONCILE_S = 30.0
_OUTPUT_TAIL_RECONCILE_CLASSES = dispatch_states.OUTPUT_TAIL_RECONCILE_STATES
_DRAIN_LAUNCHD_LABEL = "com.goalflight.drain"
_QUEUE_PENDING_NO_DRAINER = "queue_pending_no_drainer"
# --wait anti-hang grace: how long a dispatch may stay ambiguous/stale WITH a
# confirmed-dead worker before --wait resolves it to a terminal worker_dead
# verdict (instead of polling to the wait-timeout). >= 2 default poll intervals.
_WAIT_CRASH_GRACE_S = 90.0
_WAIT_STALE_GRACE_S = 600.0
_WAIT_HEARTBEAT_S = 1200.0
_WAIT_CPU_EPSILON = 0.1
_WAIT_TAIL_COUNT_BYTES = 128 * 1024


def _has_recorded_worker_identity(record: dict) -> bool:
    ident = record.get("worker_identity")
    if not isinstance(ident, dict):
        return False
    return bool(
        (ident.get("lstart") and ident.get("comm"))
        or ident.get("creation_time")
        or ident.get("creation_time_filetime")
        or ident.get("create_time")
    )


def _needs_liveness_recheck(record: dict) -> bool:
    cls = record.get("classification")
    state = record.get("state")
    return cls in _LIVENESS_RECHECK_CLASSES or state in _LIVENESS_RECHECK_CLASSES


def _identity_record_for_liveness_recheck(record: dict) -> dict | None:
    if not record.get("worker_pid"):
        return None
    if _has_recorded_worker_identity(record):
        return record
    dispatch_id = record.get("dispatch_id")
    if not dispatch_id:
        return None
    for raw in goalflight_ledger.read_records():
        if raw.get("dispatch_id") == dispatch_id and _has_recorded_worker_identity(raw):
            return raw
    return None


def _identity_record_for_output_tail_reconcile(record: dict) -> dict | None:
    if not record.get("worker_pid"):
        return None
    if _has_recorded_worker_identity(record):
        return record
    dispatch_id = record.get("dispatch_id")
    if not dispatch_id:
        return None
    for raw in goalflight_ledger.read_records():
        if raw.get("dispatch_id") == dispatch_id and _has_recorded_worker_identity(raw):
            return raw
    return None


def _record_pid_alive(record: dict) -> bool:
    pid = record.get("worker_pid")
    if not pid:
        return False
    try:
        return goalflight_compat.pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _rechecked_worker_alive(record: dict) -> bool:
    if not _needs_liveness_recheck(record):
        return False
    identity_record = _identity_record_for_liveness_recheck(record)
    if identity_record is not None:
        ok, _reason = goalflight_ledger.identity_matches(identity_record)
        return ok
    return _record_pid_alive(record)


def _record_terminal_marker_kind(record: dict | None) -> str | None:
    if not record:
        return None
    for key in ("terminal_marker", "last_marker"):
        marker = record.get(key)
        if isinstance(marker, dict):
            value = marker.get("kind")
            if value:
                return str(value)
        elif isinstance(marker, str) and marker:
            return marker
    markers = record.get("markers")
    if isinstance(markers, list):
        for marker in reversed(markers):
            if isinstance(marker, dict) and marker.get("kind"):
                return str(marker["kind"])
    return None


def _record_has_terminal_marker(record: dict | None) -> bool:
    return _record_terminal_marker_kind(record) in _OUTPUT_TAIL_TERMINAL_MARKERS


def _output_tail_reconcile_gate(record: dict, *, tail_mtime: int | None) -> tuple[bool, str]:
    identity_record = _identity_record_for_output_tail_reconcile(record)
    if identity_record is None:
        return False, "liveness_indeterminate"
    ok, reason = goalflight_ledger.identity_matches(identity_record)
    if not ok:
        return True, f"worker_not_live:{reason}"
    idle_s = time.time() - float(tail_mtime or 0)
    if idle_s > _OUTPUT_TAIL_IDLE_RECONCILE_S:
        return False, f"worker_alive_tail_idle:{int(idle_s)}s"
    return False, f"worker_alive_tail_recent:{int(max(0.0, idle_s))}s"


def _ignore_prefix_lines(prompt_path: object) -> list[str]:
    if not prompt_path:
        return []
    try:
        return [
            ln.strip()
            for ln in Path(str(prompt_path)).expanduser().read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        ]
    except OSError:
        return []


def _tail_path_from_record(record: dict) -> Path | None:
    for key in ("stdout_path", "tail_path"):
        value = record.get(key)
        if value:
            return Path(str(value)).expanduser()
    status_path = record.get("status_path")
    if not status_path:
        return None
    try:
        payload = json.loads(Path(str(status_path)).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tail_path = payload.get("tail_path") if isinstance(payload, dict) else None
    return Path(str(tail_path)).expanduser() if tail_path else None


def _tail_mtime_plausible(tail: Path, record: dict) -> tuple[bool, int | None]:
    try:
        stat = tail.stat()
    except OSError:
        return False, None
    mtime = float(stat.st_mtime)
    started = goalflight_ledger.parse_utc(record.get("started_at"))
    if started and mtime + 2.0 < started.timestamp():
        return False, int(mtime)
    if mtime > time.time() + 300.0:
        return False, int(mtime)
    return True, int(mtime)


def _output_tail_reconcile_candidate(record: dict) -> bool:
    cls = record.get("classification") or record.get("state") or "unknown"
    if cls == _LIVE_CLASS:
        return False
    if cls in {"queued_capacity", "waiting_capacity", "queued"}:
        return False
    reason = str(record.get("reason") or record.get("error") or "")
    terminal_state = record.get("terminal_state")
    return bool(
        cls in _OUTPUT_TAIL_RECONCILE_CLASSES
        or str(cls).startswith("stale_")
        or terminal_state in _OUTPUT_TAIL_RECONCILE_CLASSES
        or "worker_dead_no_terminal_marker" in reason
    )


def _reconcile_output_tail_record(record: dict) -> dict:
    """Read-only repair for launcher/watcher death after worker success.

    The ledger/status liveness signals remain authoritative for live workers. This
    only upgrades dead/stale rows when the worker output tail itself ends in a
    success terminal marker with an mtime compatible with the ledger record.
    """
    if not _output_tail_reconcile_candidate(record):
        return record
    tail = _tail_path_from_record(record)
    if tail is None:
        return record
    plausible, mtime = _tail_mtime_plausible(tail, record)
    if not plausible:
        return record
    # Reconciliation runs on the worker-dead path, after no more output can
    # arrive, so it must scan the WHOLE tail for a terminal marker -- not just the
    # last line. Workers legitimately emit `READY:` then a trailing TL;DR/summary,
    # which left the marker off the last line and produced a false worker_dead
    # (D022). _final_terminal_marker is the watcher's own reconciliation-grade scan
    # (skips fences/diff-hunks/prompt-echoes, takes the last valid marker).
    marker = _final_terminal_marker(
        tail,
        ignore_prefix_lines=_ignore_prefix_lines(record.get("prompt_path")),
    )
    marker_kind = marker.get("kind") if isinstance(marker, dict) else None
    if marker_kind not in _OUTPUT_TAIL_TERMINAL_MARKERS:
        return record
    should_promote, gate_reason = _output_tail_reconcile_gate(record, tail_mtime=mtime)
    if not should_promote:
        # The tail HAS a terminal marker but the gate refused promotion — typically
        # because the worker is still alive (or the tail is too fresh to trust). We must
        # NOT surface that marker as `terminal_marker`/`last_marker`: those are terminal
        # SIGNALS, and a live worker carrying one re-creates the false-done this whole
        # change set fixes (done_code -> 0, normalize_state -> terminal, for a LIVE
        # worker). Keep it as a diagnostic-only observation under the reconciliation key.
        out = dict(record)
        out["tail_path"] = str(tail)
        out["tail_mtime"] = mtime
        out["output_tail_reconciliation"] = {
            "candidate": True,
            "promoted": False,
            "reason": gate_reason,
            "observed_marker": marker,  # diagnostic only — NOT a terminal signal
            "idle_threshold_s": _OUTPUT_TAIL_IDLE_RECONCILE_S,
        }
        return out
    terminal_state = "complete" if marker_kind in _OUTPUT_TAIL_SUCCESS_MARKERS else "blocked"
    out = dict(record)
    out.setdefault("raw_classification", record.get("classification"))
    out.setdefault("raw_state", record.get("state"))
    out.setdefault("raw_terminal_state", record.get("terminal_state"))
    out["classification"] = terminal_state
    out["state"] = terminal_state
    out["terminal_state"] = terminal_state
    out["reason"] = f"marker:{marker.get('kind')}:output_tail_reconciliation"
    out["terminal_marker"] = marker
    out["terminal_marker_source"] = "output_tail"
    out["tail_path"] = str(tail)
    out["tail_mtime"] = mtime
    out["output_tail_reconciliation"] = {
        "candidate": True,
        "promoted": True,
        "reason": gate_reason,
        "idle_threshold_s": _OUTPUT_TAIL_IDLE_RECONCILE_S,
    }
    return out


def _reattach_hint(record: dict) -> str:
    dispatch_id = record.get("dispatch_id") or "<id>"
    return f"worker still alive - re-attach via goalflight_status.py --done {dispatch_id}"


def _dispatch_queue_dir() -> Path:
    return goalflight_ledger.state_dir() / "dispatch-queue"


def _dispatch_queue_depth() -> int:
    try:
        return len(list(_dispatch_queue_dir().glob("*.json")))
    except OSError:
        return 0


def _launchd_drainer_loaded() -> bool:
    try:
        proc = subprocess.run(
            ["launchctl", "list", _DRAIN_LAUNCHD_LABEL],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _drain_process_running() -> bool:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for command in proc.stdout.splitlines():
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        # Match a real drain invocation only: an argv whose program token's
        # basename is goalflight_dispatch.py immediately followed by the exact
        # `drain` subcommand. Substring matching false-positives on lookalike
        # paths, the `--no-drain-on-submit` flag, or a prompt arg containing the
        # word, which would wrongly suppress the no-drainer WARN.
        for idx, token in enumerate(tokens[:-1]):
            if Path(token).name == "goalflight_dispatch.py" and tokens[idx + 1] == "drain":
                return True
    return False


def _drainer_live() -> bool:
    return _launchd_drainer_loaded() or _drain_process_running()


def _queue_drainer_warnings() -> list[dict]:
    queue_depth = _dispatch_queue_depth()
    if queue_depth <= 0 or _drainer_live():
        return []
    return [
        {
            "code": _QUEUE_PENDING_NO_DRAINER,
            "severity": "WARN",
            "queue_depth": queue_depth,
            "message": f"{queue_depth} queued dispatch request(s) with no live drain worker detected",
            "remedy": (
                "confirm with `launchctl list com.goalflight.drain`; "
                "restore the scheduled drainer or run one manual drain pass"
            ),
        }
    ]


def this_project_root() -> str | None:
    """Resolved git toplevel of CWD, or None when not inside a git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return str(Path(out.stdout.strip()).resolve())
    except Exception:
        pass
    return None


def status_payload() -> dict:
    with goalflight_capacity.StateLock():
        capacity_state = goalflight_capacity.load_state()
    goalflight_capacity.prune_state(capacity_state)
    rate_pressure = goalflight_capacity.current_rate_pressure(argparse.Namespace())
    dispatch = goalflight_ledger.status_payload()
    dispatch = dict(
        dispatch,
        records=[
            _reconcile_output_tail_record(record)
            for record in dispatch.get("records", [])
        ],
    )
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": goalflight_capacity.profile(argparse.Namespace()),
        "capacity_state": capacity_state,
        "rate_pressure": rate_pressure,
        "dispatch": dispatch,
        "warnings": _queue_drainer_warnings(),
    }


def scope_payload(payload: dict, project_root: str | None) -> dict:
    """Filter dispatch records + lease details to ``project_root``. Always records
    a machine-wide active-lease count so capacity gating still sees true load."""
    leases = payload["capacity_state"].get("leases", {})
    machine_active = sum(1 for l in leases.values() if l.get("state") == "active")
    out = dict(payload)
    out["scope"] = {"project_root": project_root, "machine_active_leases": machine_active}
    if project_root is None:
        return out
    out["capacity_state"] = dict(
        payload["capacity_state"],
        leases={k: v for k, v in leases.items() if v.get("project_root") == project_root},
    )
    out["dispatch"] = dict(
        payload["dispatch"],
        records=[
            r
            for r in payload["dispatch"].get("records", [])
            if r.get("project_root") == project_root
        ],
    )
    return out


def done_code(record: dict, *, worker_alive: bool | None = None) -> int:
    """0 = terminal/done, 1 = live, 2 = ambiguous/unknown."""
    cls = record.get("classification") or record.get("state") or "unknown"
    if _record_has_terminal_marker(record):
        return 0
    if _needs_liveness_recheck(record):
        if worker_alive is None:
            worker_alive = _rechecked_worker_alive(record)
        return 1 if worker_alive else 0
    if cls == _LIVE_CLASS:
        return 1
    if cls in {"queued_capacity", "waiting_capacity", "queued"}:
        # Queued for a capacity slot: live by definition (the launcher is
        # polling acquire; the wait deadline bounds it). Without this branch
        # the raw-state fallback would misreport a queued dispatch as DONE.
        return 1
    if cls in _AMBIGUOUS_CLASS or cls.startswith("stale_"):
        return 2
    if (
        dispatch_states.is_terminal_state(cls)
        or dispatch_states.is_terminal_state(record.get("state"))
        or dispatch_states.is_terminal_state(record.get("terminal_state"))
    ):
        return 0
    return 2


def find_record(payload: dict, dispatch_id: str) -> dict | None:
    for r in payload["dispatch"].get("records", []):
        if r.get("dispatch_id") == dispatch_id:
            return r
    return None


def _signal(record: dict) -> str:
    pid = record.get("worker_pid")
    if _rechecked_worker_alive(record):
        return f"pid{pid}; {_reattach_hint(record)}"
    if record.get("worker_still_alive") and pid:
        return f"pid{pid}"
    return record.get("reason") or record.get("terminal_state") or ""


def _dispatch_cells(record: dict) -> str:
    cls = record.get("classification") or record.get("state") or "?"
    agent = record.get("agent") or "?"
    sig = _signal(record)
    if sig and sig != cls:
        return f"{cls} {agent} {sig}"
    return f"{cls} {agent}"


def _milestone_payload(project_root: str | None) -> dict:
    if not project_root:
        return {
            "schema": goalflight_milestone.SCHEMA,
            "active_cadence": False,
            "commits_since": None,
            "K": None,
            "last_marker": None,
            "due": False,
            "reason": "no active cadence",
            "warnings": [],
            "error": None,
        }
    try:
        return goalflight_milestone.check_status(
            project_root=Path(project_root),
            require_active_queue=True,
        )
    except Exception as exc:
        detail = " ".join(f"{exc.__class__.__name__}: {exc}".split())
        return {
            "schema": goalflight_milestone.SCHEMA,
            "active_cadence": False,
            "commits_since": None,
            "K": None,
            "last_marker": None,
            "due": False,
            "reason": "milestone unavailable",
            "warnings": [],
            "error": detail,
        }


def _parse_wait_ids(values: list[str] | None) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for part in str(raw).split(","):
            dispatch_id = part.strip()
            if dispatch_id and dispatch_id not in seen:
                ids.append(dispatch_id)
                seen.add(dispatch_id)
    return ids


def _terminal_state(record: dict | None, *, code: int, timed_out: bool = False) -> str:
    if record is None:
        return "timeout" if timed_out else "unknown"
    if code != 0:
        return "timeout" if timed_out else (record.get("classification") or "live")
    cls = record.get("classification") or record.get("state") or "unknown"
    if cls == "idle_timeout":
        return "worker_dead"
    return str(record.get("terminal_state") or cls)


def _wait_worker_confirmed_dead(record: dict | None) -> bool:
    """True when an ambiguous/stale (done_code==2) row's worker is provably gone.

    The --wait anti-hang clause. status_payload() has already run
    reconcile-from-output, so any row that ends in a success terminal marker was
    already promoted to ``complete`` (done_code 0). A row that is STILL ambiguous
    here therefore has no success marker; if its worker is also dead the dispatch
    can never reach a clean terminal on its own, so --wait must resolve it as
    ``worker_dead`` rather than poll to the timeout.

    Liveness is checked the trustworthy way: identity match when an identity was
    recorded (survives PID reuse), else a raw pid probe, else (no pid at all) dead.
    A genuinely-running-but-ambiguous worker (live pid / matching identity) returns
    False so --wait keeps waiting -- the wait-timeout is the ultimate backstop.
    """
    if record is None:
        return True
    if _needs_liveness_recheck(record):
        return not _rechecked_worker_alive(record)
    if _has_recorded_worker_identity(record):
        ok, _ = goalflight_ledger.identity_matches(record)
        return not ok
    pid = record.get("worker_pid")
    if pid:
        try:
            return not goalflight_compat.pid_alive(int(pid))
        except (TypeError, ValueError):
            return True
    return True


def _wait_worker_confirmed_alive(record: dict | None) -> bool:
    if record is None:
        return False
    if _needs_liveness_recheck(record):
        return _rechecked_worker_alive(record)
    if _has_recorded_worker_identity(record):
        ok, _ = goalflight_ledger.identity_matches(record)
        return ok
    pid = record.get("worker_pid")
    if pid:
        try:
            return goalflight_compat.pid_alive(int(pid))
        except (TypeError, ValueError):
            return False
    return False


def _wait_record_pid(record: dict | None) -> int | None:
    if record is None:
        return None
    pid = record.get("worker_pid")
    if not pid:
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _wait_process_cpu_pct(record: dict | None) -> float | None:
    pid = _wait_record_pid(record)
    if pid is None:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-o", "%cpu=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for part in proc.stdout.split():
        try:
            return float(part)
        except ValueError:
            continue
    return None


def _wait_tail_stat(record: dict | None) -> dict:
    path = _tail_path_from_record(record or {})
    detail = {"path": None, "size": None, "mtime": None}
    if path is None:
        return detail
    detail["path"] = str(path)
    try:
        st = path.stat()
    except OSError:
        return detail
    detail["size"] = int(st.st_size)
    detail["mtime"] = float(st.st_mtime)
    return detail


def _wait_tail_activity_counts(tail_path: str | None) -> dict:
    counts = {"json_append_count": None, "tool_use_count": None}
    if not tail_path:
        return counts
    path = Path(tail_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _WAIT_TAIL_COUNT_BYTES:
                fh.seek(-_WAIT_TAIL_COUNT_BYTES, 2)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return counts
    json_count = 0
    tool_count = 0
    for line in data.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = None
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                json_count += 1
            except json.JSONDecodeError:
                parsed = None
        haystack = stripped
        if isinstance(parsed, dict):
            haystack = json.dumps(parsed, sort_keys=True)
        if "tool_use" in haystack or "tool_call" in haystack or "toolCallId" in haystack:
            tool_count += 1
    counts["json_append_count"] = json_count
    counts["tool_use_count"] = tool_count
    return counts


def _wait_progress_detail(
    dispatch_id: str,
    record: dict | None,
    *,
    now: float,
    progress_state: dict[str, dict],
    cpu_epsilon: float = _WAIT_CPU_EPSILON,
    worker_alive: bool | None = None,
) -> dict:
    state = progress_state.setdefault(dispatch_id, {})
    tail = _wait_tail_stat(record)
    size = tail.get("size")
    previous_size = state.get("tail_size")
    tail_growth_bytes: int | None = None
    tail_changed = False
    if isinstance(size, int):
        if isinstance(previous_size, int):
            tail_growth_bytes = max(0, size - previous_size)
            tail_changed = size != previous_size
        else:
            tail_growth_bytes = 0
            tail_changed = True
        state["tail_size"] = size
        if tail_changed or "last_growth_mono" not in state:
            state["last_growth_mono"] = now
    else:
        state.pop("tail_size", None)
        state["last_growth_mono"] = now

    cpu_pct = _wait_process_cpu_pct(record)
    cpu_busy = bool(cpu_pct is not None and cpu_pct > cpu_epsilon)
    cpu_idle = cpu_confirmed_idle(cpu_pct, cpu_epsilon)
    if worker_alive is None:
        worker_alive = _wait_worker_confirmed_alive(record)
    pid = _wait_record_pid(record)
    counts = _wait_tail_activity_counts(tail.get("path"))
    last_growth = float(state.get("last_growth_mono", now))
    mtime = tail.get("mtime")
    tail_append_age_s = None
    if isinstance(mtime, float):
        tail_append_age_s = max(0.0, time.time() - mtime)
    return {
        "tail_path": tail.get("path"),
        "tail_size": size,
        "tail_growth_bytes": tail_growth_bytes,
        "tail_changed": tail_changed,
        "last_growth_age_s": max(0.0, now - last_growth),
        "tail_append_age_s": tail_append_age_s,
        "worker_pid": pid,
        "worker_alive": worker_alive,
        "cpu_pct": cpu_pct,
        "cpu_busy": cpu_busy,
        "cpu_idle": cpu_idle,
        **counts,
    }


def _fmt_wait_bytes(value: int | None) -> str:
    if value is None:
        return "+?B"
    if value < 1024:
        return f"+{value}B"
    return f"+{value / 1024.0:.1f}KB"


def _fmt_wait_age(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 60:
        return f"{value:.0f}s"
    if value < 3600:
        return f"{value / 60.0:.1f}m"
    return f"{value / 3600.0:.1f}h"


def _format_wait_heartbeat(row: dict) -> str:
    progress = row.get("progress") if isinstance(row.get("progress"), dict) else {}
    cpu = progress.get("cpu_pct")
    cpu_text = "cpu ?%" if cpu is None else f"cpu {float(cpu):.1f}%"
    pid = progress.get("worker_pid")
    pid_text = "pid ?" if pid is None else f"pid {pid}"
    tool_count = progress.get("tool_use_count")
    json_count = progress.get("json_append_count")
    activity = []
    if tool_count is not None:
        activity.append(f"tool-use {tool_count}")
    if json_count is not None:
        activity.append(f"json {json_count}")
    activity_text = "" if not activity else ", " + "/".join(activity)
    suffix = ""
    if row.get("state") == "worker_stalled":
        suffix = " -> STALE (resolving)"
    return (
        f"{row['dispatch_id']}: running, "
        f"{_fmt_wait_bytes(progress.get('tail_growth_bytes'))} tail since last poll, "
        f"last append {_fmt_wait_age(progress.get('tail_append_age_s'))} ago, "
        f"{pid_text}, {cpu_text}{activity_text}{suffix}"
    )


def _wait_snapshot(
    payload: dict,
    wait_ids: list[str],
    *,
    dead_since: dict[str, float] | None = None,
    stalled_since: dict[str, float] | None = None,
    progress_state: dict[str, dict] | None = None,
    now: float | None = None,
    grace: float = _WAIT_CRASH_GRACE_S,
    stale_grace: float = _WAIT_STALE_GRACE_S,
    cpu_epsilon: float = _WAIT_CPU_EPSILON,
) -> list[dict]:
    if dead_since is None:
        dead_since = {}
    if stalled_since is None:
        stalled_since = {}
    if progress_state is None:
        progress_state = {}
    if now is None:
        now = time.monotonic()
    rows: list[dict] = []
    for dispatch_id in wait_ids:
        record = find_record(payload, dispatch_id)
        worker_alive: bool | None = None
        if record is None:
            code = 2
        elif _needs_liveness_recheck(record):
            worker_alive = _rechecked_worker_alive(record)
            code = done_code(record, worker_alive=worker_alive)
        else:
            code = done_code(record)
        terminal = code == 0
        state = _terminal_state(record, code=code)
        confirmed_dead = False
        if code == 2:
            confirmed_dead = _wait_worker_confirmed_dead(record)
            worker_alive = not confirmed_dead
        progress = (
            {}
            if terminal
            else _wait_progress_detail(
                dispatch_id,
                record,
                now=now,
                progress_state=progress_state,
                cpu_epsilon=cpu_epsilon,
                worker_alive=worker_alive,
            )
        )
        if terminal or code == 1:
            dead_since.pop(dispatch_id, None)
        elif code == 2 and confirmed_dead:
            first = dead_since.setdefault(dispatch_id, now)
            if now - first >= grace:
                terminal = True
                state = "worker_dead"
            else:
                state = "worker_dead_pending"
        else:
            dead_since.pop(dispatch_id, None)
        if terminal or state.startswith("worker_dead"):
            stalled_since.pop(dispatch_id, None)
        else:
            tail_known = isinstance(progress.get("tail_size"), int)
            tail_grew = bool(
                isinstance(progress.get("tail_growth_bytes"), int)
                and progress.get("tail_growth_bytes") > 0
            )
            tail_changed = bool(progress.get("tail_changed"))
            cpu_busy = bool(progress.get("cpu_busy"))
            cpu_idle = bool(progress.get("cpu_idle"))
            worker_alive = bool(progress.get("worker_alive"))
            if tail_grew or tail_changed or cpu_busy or not cpu_idle or not worker_alive or not tail_known:
                stalled_since.pop(dispatch_id, None)
            else:
                first = stalled_since.setdefault(
                    dispatch_id,
                    max(0.0, now - float(progress.get("last_growth_age_s") or 0.0)),
                )
                if now - first >= stale_grace:
                    terminal = True
                    state = "worker_stalled"
        rows.append(
            {
                "dispatch_id": dispatch_id,
                "done_code": code,
                "terminal": terminal,
                "state": state,
                "status_path": None if record is None else record.get("status_path"),
                "progress": progress,
            }
        )
    return rows


def wait_for_dispatches(
    wait_ids: list[str],
    *,
    project_root: str | None,
    timeout_s: float | None,
    poll_s: float,
    crash_grace_s: float | None = None,
    stale_grace_s: float | None = None,
    heartbeat_s: float | None = None,
    json_output: bool = False,
) -> int:
    if not wait_ids:
        print("wait requires at least one dispatch id", file=sys.stderr)
        return 2

    start = time.monotonic()
    poll_s = max(0.05, poll_s)
    grace = _WAIT_CRASH_GRACE_S if crash_grace_s is None else max(0.0, crash_grace_s)
    stale_grace = _WAIT_STALE_GRACE_S if stale_grace_s is None else max(0.0, stale_grace_s)
    heartbeat = _WAIT_HEARTBEAT_S if heartbeat_s is None else max(0.0, heartbeat_s)
    # Per-id monotonic timestamp of when a row first became ambiguous-and-dead.
    # The grace window means a transient post-submit blip does not flip to
    # worker_dead, but a genuine crash/premature-exit resolves in bounded time.
    dead_since: dict[str, float] = {}
    # Per-id monotonic timestamp of when an alive row last had no progress
    # evidence. Cleared by tail growth/change, CPU activity, terminal, or death.
    stalled_since: dict[str, float] = {}
    progress_state: dict[str, dict] = {}
    heartbeat_since: dict[str, float] = {dispatch_id: start for dispatch_id in wait_ids}
    while True:
        now = time.monotonic()
        payload = scope_payload(status_payload(), project_root)
        rows = _wait_snapshot(
            payload,
            wait_ids,
            dead_since=dead_since,
            stalled_since=stalled_since,
            progress_state=progress_state,
            now=now,
            grace=grace,
            stale_grace=stale_grace,
        )
        if not json_output:
            for row in rows:
                if row["terminal"]:
                    heartbeat_since.pop(row["dispatch_id"], None)
                    continue
                last = heartbeat_since.setdefault(row["dispatch_id"], start)
                if now - last >= heartbeat:
                    print(_format_wait_heartbeat(row), flush=True)
                    heartbeat_since[row["dispatch_id"]] = now
        if all(row["terminal"] for row in rows):
            if json_output:
                print(json.dumps({"ok": True, "dispatches": rows}, sort_keys=True))
            else:
                print(f"wait complete: {len(rows)}/{len(rows)} terminal")
                for row in rows:
                    print(f"{row['dispatch_id']} -> {row['state']}")
            return 0

        if timeout_s is not None and now - start >= timeout_s:
            for row in rows:
                if not row["terminal"]:
                    row["state"] = "timeout"
            terminal = sum(1 for row in rows if row["terminal"])
            pending = [row["dispatch_id"] for row in rows if not row["terminal"]]
            if json_output:
                print(
                    json.dumps(
                        {"ok": False, "timeout": True, "pending": pending, "dispatches": rows},
                        sort_keys=True,
                    )
                )
            else:
                print(f"wait timeout: {terminal}/{len(rows)} terminal; pending {','.join(pending)}")
                for row in rows:
                    print(f"{row['dispatch_id']} -> {row['state']}")
            return 1

        time.sleep(poll_s)


def render_text(payload: dict, limit: int) -> list[str]:
    scope = payload.get("scope", {})
    root = scope.get("project_root")
    label = Path(root).name if root else "all-projects"
    cap = payload.get("capacity", {})
    records = payload["dispatch"].get("records", [])
    leases = payload["capacity_state"].get("leases", {})
    cooldowns = payload["capacity_state"].get("cooldowns", {})
    running = sum(1 for r in records if done_code(r) == 1)
    done = sum(1 for r in records if done_code(r) == 0)
    ambig = sum(1 for r in records if done_code(r) == 2)
    machine = scope.get("machine_active_leases", len(leases))
    lines = [
        f"{label}: running{running} done{done} ambig{ambig} "
        f"cooldowns{len(cooldowns)}  machine:{machine}/{cap.get('operating_cap')}"
    ]
    if payload.get("milestone"):
        lines.append(goalflight_milestone.format_line(payload["milestone"]))
    # Live/ambiguous first (what the controller is waiting on), then most-recent
    # terminal; cap at --limit.
    live = [r for r in records if done_code(r) != 0]
    terminal = sorted(
        (r for r in records if done_code(r) == 0),
        key=lambda r: r.get("updated_at") or r.get("ended_at") or "",
        reverse=True,
    )
    for r in (live + terminal)[:limit]:
        did = (r.get("dispatch_id") or "?")[:30]
        lines.append(f"  {did:<30} {_dispatch_cells(r)}  {r.get('status_path') or '-'}")
    for item in list(cooldowns.values())[:limit]:
        lines.append(
            f"  cooldown {item.get('agent')}: {item.get('reason')} until {item.get('until')}"
        )
    for warning in goalflight_capacity.rate_pressure_warnings(payload.get("rate_pressure"), limit=limit):
        lines.append(f"  warning: {warning}")
    for warning in payload.get("warnings", [])[:limit]:
        lines.append(
            f"  WARN {warning.get('code')}: {warning.get('message')}; "
            f"{warning.get('remedy')}"
        )
    return lines


_MARKER_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")  # whole markdown link [text](target)
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)  # http(s)/ftp/... — not a local path
_PATH_EXT_RE = re.compile(r"[^/\\]\.[A-Za-z0-9]{1,8}$")       # ends in a file extension


def _extract_marker_paths(marker_text: str) -> list[str]:
    """Pull artifact path(s) out of a SUCCESS terminal marker's text, e.g.
    ``READY: docs-private/.../findings.md``, ``[findings.md](/abs/findings.md:1)``, or a
    bare ``findings.md``. Each token is normalized FIRST (strip ``file://`` / ``#anchor``
    / ``:line``) and then kept iff it ends in a file extension and is not a URL scheme.
    Over-extraction is harmless (a bogus path just reports MISSING); under-extraction
    (dropping a real declared artifact) is the dangerous failure, so the filter is lenient."""
    if not marker_text:
        return []
    tokens = [m.group(1) for m in _MARKER_LINK_RE.finditer(marker_text)]
    tokens += re.split(r"[\s,;`\"'<>()\[\]]+", _MARKER_LINK_RE.sub(" ", marker_text))
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip().strip("`\"'<>.,;")
        if not tok:
            continue
        if tok.startswith("file://"):
            tok = tok[len("file://"):]
        elif _URL_SCHEME_RE.match(tok):
            continue  # http(s)/ftp/etc. are not local artifacts
        tok = tok.split("#", 1)[0]                      # drop #anchor BEFORE the ext test
        tok = re.sub(r":\d+(?:-\d+)?$", "", tok)        # drop :line / :line-range
        if tok and _PATH_EXT_RE.search(tok) and tok not in out:
            out.append(tok)
    return out


def _verify_record(dispatch_id: str, project_root: str | None) -> dict | None:
    """Look up a dispatch's ledger record DIRECTLY by id (no aggregate status_payload /
    no reconcile pass) — the run-ledger lookup is by id, and artifact verification then
    opens declared paths directly, so nothing here enumerates the worker's output dir."""
    match = None
    try:
        for record in goalflight_ledger.read_records():
            if record.get("dispatch_id") != dispatch_id:
                continue
            if project_root and record.get("project_root") not in (None, project_root):
                continue
            match = record  # latest matching record wins
    except Exception:
        return None
    return match


def _direct_open_exists(path: Path) -> tuple[bool, int]:
    """Confirm a path by DIRECT OPEN (a fresh FS fetch), NEVER directory enumeration.
    On local APFS a separate process's listdir/glob/find view of a just-created file can
    be stale for MINUTES, while opening a known path by name is fresh (2026-06-23 APFS
    stale-enumeration near-miss: find+git status+grep all read complete artifacts as absent). Opening +
    reading a byte forces a real content fetch, not just a possibly-cached stat."""
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True, path.stat().st_size
    except FileNotFoundError:
        return False, 0
    except OSError:
        return False, 0


def verify_artifacts(dispatch_id: str, *, project_root: str | None) -> dict:
    """Report whether a dispatch's DECLARED artifacts (the path(s) named in its terminal
    READY/COMPLETE/RESULT marker) exist, verified by direct open of each exact path — not
    by enumerating a directory. This is the trustworthy "did the worker actually write
    its outputs?" check: a controller must never conclude an artifact is missing (and
    re-author it) from ls/find/git-status/grep, which share a stale enumeration view."""
    record = _verify_record(dispatch_id, project_root)
    if record is None:
        return {"dispatch_id": dispatch_id, "found": False,
                "reason": "no ledger record for this id/scope (try --all-projects)"}
    base = Path(record.get("project_root") or record.get("process_cwd") or Path.cwd()).expanduser()
    tail = _tail_path_from_record(record)
    marker = None
    if tail is not None:
        marker = _final_terminal_marker(
            tail, ignore_prefix_lines=_ignore_prefix_lines(record.get("prompt_path")))
    # Only a SUCCESS marker (READY/COMPLETE/RESULT) declares deliverables — a FAILED:/
    # BLOCKED: marker that happens to name a path must NOT report it as a present artifact.
    declared: list[str] = []
    if marker and marker.get("kind") in SUCCESS_TERMINAL_MARKERS:
        declared = _extract_marker_paths(marker.get("text", ""))
    results = []
    for rel in declared:
        p = Path(rel).expanduser()
        if not p.is_absolute():
            p = base / rel
        present, nbytes = _direct_open_exists(p)
        results.append({"path": str(p), "present": present, "bytes": nbytes})
    all_present = bool(results) and all(r["present"] and r["bytes"] > 0 for r in results)
    return {
        "dispatch_id": dispatch_id,
        "found": True,
        "classification": record.get("terminal_state") or record.get("state"),
        "terminal_marker": None if marker is None else marker.get("kind"),
        "declared_artifacts": declared,
        "results": results,
        "all_present": all_present,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="goal-flight compact status (terse + this-repo by default)"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine payload (scoped unless --all-projects)"
    )
    parser.add_argument(
        "--all-projects", action="store_true", help="do not scope to this repo"
    )
    parser.add_argument(
        "--project", metavar="PATH", help="scope to PATH instead of the detected git root"
    )
    parser.add_argument(
        "--dispatch", metavar="ID", help="one-line status for a single dispatch"
    )
    parser.add_argument(
        "--done",
        metavar="ID",
        help="exit 0 terminal / 1 live / 2 ambiguous|unknown; no stdout",
    )
    parser.add_argument(
        "--wait",
        metavar="IDS",
        action="append",
        help="block until all comma-separated/repeated dispatch ids are terminal",
    )
    parser.add_argument(
        "--wait-timeout",
        "--timeout-s",
        dest="wait_timeout",
        type=float,
        default=None,
        help="max seconds to wait before reporting pending ids",
    )
    parser.add_argument(
        "--poll-s",
        type=float,
        default=2.0,
        help="seconds between --wait polls",
    )
    parser.add_argument(
        "--crash-grace-s",
        dest="crash_grace_s",
        type=float,
        default=None,
        help=(
            "seconds an ambiguous/stale dispatch with a confirmed-dead worker may "
            "persist before --wait resolves it to worker_dead instead of polling to "
            f"--wait-timeout (default {int(_WAIT_CRASH_GRACE_S)})"
        ),
    )
    parser.add_argument(
        "--stale-grace-s",
        dest="stale_grace_s",
        type=float,
        default=None,
        help=(
            "seconds an alive dispatch with no tail growth and no CPU activity may "
            f"persist before --wait resolves it to worker_stalled (default {int(_WAIT_STALE_GRACE_S)})"
        ),
    )
    parser.add_argument(
        "--heartbeat-s",
        dest="heartbeat_s",
        type=float,
        default=None,
        help=(
            "seconds between --wait progress heartbeats for non-terminal dispatches "
            f"(default {int(_WAIT_HEARTBEAT_S)})"
        ),
    )
    parser.add_argument(
        "--verify-artifacts",
        metavar="ID",
        dest="verify_artifacts",
        help=(
            "verify a dispatch's DECLARED artifacts (named in its terminal marker) exist "
            "by DIRECT OPEN of each path — never directory enumeration, which can read "
            "stale for minutes on local APFS. exit 0 = all present+nonempty, 1 = not"
        ),
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.all_projects:
        project_root = None
    elif args.project:
        project_root = str(Path(args.project).resolve())
    else:
        project_root = this_project_root()

    if args.wait:
        return wait_for_dispatches(
            _parse_wait_ids(args.wait),
            project_root=project_root,
            timeout_s=args.wait_timeout,
            poll_s=args.poll_s,
            crash_grace_s=args.crash_grace_s,
            stale_grace_s=args.stale_grace_s,
            heartbeat_s=args.heartbeat_s,
            json_output=args.json,
        )

    if args.verify_artifacts is not None:
        result = verify_artifacts(args.verify_artifacts, project_root=project_root)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        elif not result.get("found"):
            print(f"{args.verify_artifacts}  {result.get('reason')}")
        else:
            for r in result["results"]:
                mark = "OK" if (r["present"] and r["bytes"] > 0) else "MISSING"
                print(f"  [{mark}] {r['path']} ({r['bytes']}B)")
            if not result["declared_artifacts"]:
                print("  (no artifact path declared in the terminal marker)")
            print(
                f"{args.verify_artifacts}  all_present={result['all_present']} "
                f"marker={result.get('terminal_marker')} classif={result.get('classification')}"
            )
        if not result.get("found"):
            return 2
        return 0 if result.get("all_present") else 1

    payload = scope_payload(status_payload(), project_root)

    if args.done is not None:
        record = find_record(payload, args.done)
        return 2 if record is None else done_code(record)

    if args.dispatch is not None:
        record = find_record(payload, args.dispatch)
        if record is None:
            print(f"{args.dispatch}  unknown (no record for this scope; try --all-projects)")
            return 2
        print(f"{args.dispatch}  {_dispatch_cells(record)}  {record.get('status_path') or '-'}")
        return 0

    payload["milestone"] = _milestone_payload(project_root)

    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    for line in render_text(payload, args.limit):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
