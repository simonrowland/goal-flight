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
from goalflight_liveness import (
    LivenessThresholds,
    classify_liveness,
    pgroup_cpu_pct,
    process_group_id,
    write_status,
)

# `\**` tolerance: grok (and other markdown-emitting workers) write **COMPLETE:**
# etc.; without it the bold marker is never matched and the worker idle-times-out
# instead of waking the controller (grok review, 2026-05-30). Mirrors watch-dispatch-tail.sh.
MARKER_RE = re.compile(r"^\**(STATUS|RESULT|USER-NEED|USER-CONFIRM|BLOCKED|COMPLETE):\**\s*(.*)$")
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


def extract_markers(path: Path, max_bytes: int = 10 * 1024 * 1024,
                    ignore_lines: set[str] | None = None) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    markers: list[dict] = []
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip lines that are verbatim from the echoed prompt: a worker that echoes
        # its prompt (codex, grok) would otherwise trip the watcher on the prompt's
        # own "end with COMPLETE: ..." instruction before doing any real work.
        if ignore_lines and stripped in ignore_lines:
            continue
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
    parser.add_argument("--ignore-prompt-file",
                        help="Ignore marker lines appearing verbatim in this prompt file, so a worker's "
                             "echoed prompt can't trip the watcher on its own 'end with COMPLETE:' instruction.")
    args = parser.parse_args()

    ignore_lines: set[str] = set()
    if args.ignore_prompt_file:
        _pf = Path(args.ignore_prompt_file)
        if _pf.exists():
            ignore_lines = {ln.strip() for ln in
                            _pf.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()}

    tail = Path(args.tail)
    status_path = Path(args.status_json)
    last_size = -1
    last_change = time.time()
    terminal = None
    markers: list[dict] = []
    exit_reason = "unknown"
    exit_code = 1
    wedge_streak = 0
    # process_group_id/pgroup_cpu_pct use os.getpgid/ps (POSIX-only). On native
    # Windows they raise/return nothing — and the watcher IS the cross-platform
    # dispatch path, so it must NOT crash there. Degrade to existence + tail-idle
    # (no CPU-wedge) on Windows.
    _win = goalflight_compat.is_windows()
    pgid = None if _win else (args.pgid or process_group_id(args.pid) or args.pid)
    thresholds = LivenessThresholds(idle_timeout_s=args.max_idle_secs, cpu_epsilon_pct=args.cpu_epsilon)

    while True:
        markers, size = extract_markers(tail, ignore_lines=ignore_lines)
        if size != last_size:
            last_size = size
            last_change = time.time()
        now = time.time()
        seconds_since_event = now - last_change
        terminal = next((m for m in reversed(markers) if m["kind"] in TERMINAL_MARKERS), None)
        worker_alive = alive(args.pid)
        if worker_alive and not _win:
            pgid = args.pgid or process_group_id(args.pid) or pgid
            cpu_pct = pgroup_cpu_pct(pgid)
        else:
            cpu_pct = 0.0
        liveness_state = classify_liveness(worker_alive, cpu_pct, seconds_since_event, thresholds)
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "pgid": pgid,
            "worker_alive": worker_alive,
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
        if not worker_alive:
            payload["state"] = "worker_dead"
            exit_reason = "worker_dead_no_terminal_marker"
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
