#!/usr/bin/env python3
"""Watch a worker log and emit compact goal-flight status JSON."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time

import goalflight_compat
import goalflight_ledger
from goalflight_liveness import (
    LivenessThresholds,
    classify_liveness,
    pgroup_cpu_pct,
    process_group_id,
    system_starved,
    write_status,
)

# `\**` tolerance: grok (and other markdown-emitting workers) write **COMPLETE:**
# etc.; without it the bold marker is never matched and the worker idle-times-out
# instead of waking the controller (grok review, 2026-05-30). Mirrors watch-dispatch-tail.sh.
MARKER_RE = re.compile(r"^\**(STATUS|STEER-ACK|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**\s*(.*)$")
TERMINAL_MARKERS = {"RESULT", "USER-NEED", "USER-CONFIRM", "BLOCKED", "COMPLETE"}
# CPU-sampling-failure grace (codex 2026-05-20 P2): require this many consecutive
# `wedged` polls before exiting with idle_timeout, so a single transient `ps`
# failure (cpu→None→wedged for one poll) can't false-positive a healthy worker.
# Watcher mirror of the runner's intra-decision re-sample grace
# (goalflight_liveness.cpu_liveness_keep_waiting) — same goal, keep aligned.
WEDGE_CONFIRM_SAMPLES = 2


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    return goalflight_compat.pid_alive(pid)


def _identity_token(identity: dict | None) -> dict | None:
    if not identity:
        return None
    return {key: identity.get(key) for key in ("pid", "lstart", "comm") if identity.get(key)}


def _load_identity(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _comm_base(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("(") and ")" in text:
        text = text[1:text.find(")")]
    # A comm may be a bare name ("grok", "(grok-0.2.11-maco)") or a full
    # executable path ("/opt/homebrew/.../python"); take the basename so a path
    # tokenizes to its program name rather than an empty leading token.
    text = text.rsplit("/", 1)[-1]
    match = re.match(r"[a-z0-9_]+", text)
    return match.group(0) if match else ""


def _comm_matches(expected: object, actual: object) -> bool:
    expected_base = _comm_base(expected)
    actual_base = _comm_base(actual)
    return bool(
        expected_base
        and actual_base
        and (expected_base.startswith(actual_base) or actual_base.startswith(expected_base))
    )


def worker_alive(pid: int | None, expected_identity: dict | None) -> tuple[bool, str, dict | None]:
    if not pid:
        return False, "no_pid", None
    current = goalflight_ledger.process_identity(pid)
    if current is None:
        return False, "dead", None
    if expected_identity:
        if expected_identity.get("pid") and int(expected_identity["pid"]) != int(pid):
            return False, "identity_pid_mismatch", current

        expected_comm = expected_identity.get("comm")
        actual_comm = current.get("comm")

        expected_lstart = expected_identity.get("lstart")
        actual_lstart = current.get("lstart")
        if expected_lstart and actual_lstart:
            if actual_lstart != expected_lstart:
                return False, "pid_reused_lstart", current
            # lstart is a SECOND-granularity wall-clock string, so a pid reused
            # within the same formatted second yields an identical lstart. Trust
            # lstart as the primary anti-reuse key, but when comm is available on
            # both sides require comm-base compatibility too, so a same-second
            # reuse by a genuinely DIFFERENT process (different base comm) is
            # caught. _comm_matches is form-tolerant (base-token prefix), so a
            # cosmetic comm change ("grok" vs "(grok-0.2.11-maco)") at the same
            # lstart still reads live -- preserving the Mode B fix.
            if expected_comm and actual_comm and not _comm_matches(expected_comm, actual_comm):
                return False, "pid_reused_lstart_comm", current
            return True, "live", current

        if not expected_comm:
            missing = ["lstart", "comm"] if not expected_lstart else ["comm"]
            return True, "identity_inconclusive_missing_expected_" + "_".join(missing), current
        if not actual_comm:
            missing = ["lstart", "comm"] if not actual_lstart else ["comm"]
            return True, "identity_inconclusive_missing_current_" + "_".join(missing), current
        if not _comm_matches(expected_comm, actual_comm):
            return False, "pid_reused_comm", current
        return True, "live", current
    return True, "identity_inconclusive", current


def extract_markers(path: Path, max_bytes: int = 10 * 1024 * 1024,
                    ignore_prefix_lines: list[str] | None = None) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    prompt_prefix = ignore_prefix_lines or []
    can_ignore_prefix = start == 0 and bool(prompt_prefix)
    markers: list[dict] = []
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip only the initial echoed-prompt span. If the worker later emits a
        # byte-identical real terminal marker, it must still wake the controller.
        if can_ignore_prefix:
            expected = prompt_prefix[idx - 1] if idx <= len(prompt_prefix) else None
            if expected is not None and stripped == expected:
                continue
            can_ignore_prefix = False
        match = MARKER_RE.match(stripped)
        if match:
            markers.append({"line": idx, "kind": match.group(1), "text": match.group(2)[:1000]})
    return markers, size


def main() -> int:
    parser = argparse.ArgumentParser(description="goal-flight compact log watcher")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--tail", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument("--max-idle-secs", type=float, default=180.0)
    parser.add_argument("--cpu-epsilon", type=float, default=0.1)
    parser.add_argument("--pgid", type=int)
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--worker-identity-json",
                        help="Process identity token captured at spawn; prevents PID-reuse false liveness.")
    parser.add_argument("--ignore-prompt-file",
                        help="Ignore marker lines appearing verbatim in this prompt file, so a worker's "
                             "echoed prompt can't trip the watcher on its own 'end with COMPLETE:' instruction.")
    args = parser.parse_args()

    ignore_prefix_lines: list[str] = []
    if args.ignore_prompt_file:
        _pf = Path(args.ignore_prompt_file)
        if _pf.exists():
            ignore_prefix_lines = [
                ln.strip()
                for ln in _pf.read_text(encoding="utf-8", errors="replace").splitlines()
            ]
    expected_identity = _load_identity(args.worker_identity_json)

    tail = Path(args.tail)
    status_path = Path(args.status_json)
    if goalflight_compat.is_windows():
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "state": "blocked_windows_dispatch",
            "reason": goalflight_compat.windows_watcher_skip(),
            "tail_path": str(tail),
            "updated_at": int(time.time()),
        }
        write_status(status_path, payload)
        print(json.dumps({"state": payload["state"], "reason": payload["reason"], "status_path": str(status_path)}, sort_keys=True))
        return 4
    last_size = -1
    last_change = time.time()
    terminal = None
    markers: list[dict] = []
    exit_reason = "unknown"
    exit_code = 1
    wedge_streak = 0
    pgid = args.pgid or process_group_id(args.pid) or args.pid
    thresholds = LivenessThresholds(idle_timeout_s=args.max_idle_secs, cpu_epsilon_pct=args.cpu_epsilon)

    while True:
        markers, size = extract_markers(tail, ignore_prefix_lines=ignore_prefix_lines)
        if size != last_size:
            last_size = size
            last_change = time.time()
        now = time.time()
        seconds_since_event = now - last_change
        terminal = next((m for m in reversed(markers) if m["kind"] in TERMINAL_MARKERS), None)
        worker_is_alive, identity_reason, current_identity = worker_alive(args.pid, expected_identity)
        if worker_is_alive:
            pgid = args.pgid or process_group_id(args.pid) or pgid
            cpu_pct = pgroup_cpu_pct(pgid)
        else:
            cpu_pct = 0.0
        low_power_relax = (
            worker_is_alive
            and cpu_pct is not None
            and cpu_pct <= args.cpu_epsilon
            and args.max_idle_secs > 0
            and seconds_since_event >= args.max_idle_secs
            and system_starved()
        )
        liveness_state = classify_liveness(
            worker_is_alive,
            cpu_pct,
            seconds_since_event,
            thresholds,
            low_power_relax=low_power_relax,
        )
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "pgid": pgid,
            "worker_alive": worker_is_alive,
            "worker_identity_reason": identity_reason,
            "worker_identity": _identity_token(current_identity),
            "expected_worker_identity": _identity_token(expected_identity),
            "pgroup_cpu_pct": cpu_pct,
            "seconds_since_event": seconds_since_event,
            "liveness_state": liveness_state,
            "tail_path": str(tail),
            "markers": markers[-20:],
            "last_marker": markers[-1] if markers else None,
            "terminal_marker": terminal,
            "state": "running_quiet" if liveness_state == "running_quiet" else "running",
            "updated_at": int(now),
        }
        if low_power_relax:
            payload["low_power_relax"] = True
        if terminal:
            payload["state"] = "complete" if terminal["kind"] in {"RESULT", "COMPLETE"} else "blocked"
            exit_reason = f"marker:{terminal['kind']}"
            exit_code = 0 if payload["state"] == "complete" else 4
            write_status(status_path, payload)
            break
        if args.controller_pid and not alive(args.controller_pid):
            payload["state"] = "orphaned"
            exit_reason = "controller_dead"
            exit_code = 3
            write_status(status_path, payload)
            break
        if not worker_is_alive:
            payload["state"] = "worker_dead"
            exit_reason = (
                "worker_dead_no_terminal_marker"
                if identity_reason == "dead"
                else f"worker_identity_mismatch:{identity_reason}"
            )
            exit_code = 1
            write_status(status_path, payload)
            break
        if liveness_state == "wedged":
            wedge_streak += 1
            if wedge_streak >= WEDGE_CONFIRM_SAMPLES:
                payload["state"] = "idle_timeout"
                exit_reason = "idle_timeout"
                exit_code = 2
                write_status(status_path, payload)
                break
        else:
            wedge_streak = 0
        write_status(status_path, payload)
        time.sleep(args.poll_secs)

    print(json.dumps({"state": payload["state"], "reason": exit_reason, "status_path": str(status_path)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
