#!/usr/bin/env python3
"""absorb the recurring inline python3 <<PYEOF JSON-parse pattern; replace 50-80 lines of bash+python in conversation with one ~300-char JSON dict."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import goalflight_compat
import goalflight_dispatch_states as dispatch_states
import goalflight_status
from goalflight_watch import BLOCKING_TERMINAL_MARKERS, SUCCESS_TERMINAL_MARKERS

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = goalflight_compat.resolve_state_dir()
TERMINAL_DONE = dispatch_states.SUCCESS_TERMINAL_RECORD_STATES
TERMINAL_FAILED = dispatch_states.FAILURE_TERMINAL_RECORD_STATES
WEDGED_STATES = dispatch_states.WEDGED_TERMINAL_RECORD_STATES
RETRYABLE_FAILED = frozenset({"rate_limited"})


def parse_time(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_mins(value: Any) -> int | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    seconds = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()
    return max(0, int(seconds // 60))


def read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_capacity_status(state_dir: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "goalflight_capacity.py"), "status", "--json"],
        cwd=SCRIPT_DIR.parent,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return {"active": [], "state": {"leases": {}}}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"active": [], "state": {"leases": {}}}
    return payload if isinstance(payload, dict) else {"active": [], "state": {"leases": {}}}


def ledger_records(state_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((state_dir / "runs.d").glob("*.json")):
        data = read_json(path)
        if data is not None:
            data["_record_path"] = str(path)
            records.append(data)
    return records


def value_matches_slug(value: Any, slug: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return value == slug or value.startswith(slug) or slug in value.split()


def record_matches(record: dict[str, Any], slug: str) -> bool:
    for key in ("dispatch_id", "prompt_id", "lease_id", "remote_lease_id", "slug", "queue_slug"):
        if value_matches_slug(record.get(key), slug):
            return True
    return False


def status_candidates(slug: str, record: dict[str, Any] | None) -> list[Path]:
    candidates: list[Path] = []
    if record:
        for key in ("status_path", "status_json"):
            value = record.get(key)
            if isinstance(value, str) and value:
                candidates.append(Path(value).expanduser())
    candidates.append(goalflight_compat.temp_base() / f"goalflight-{slug}-dispatch" / "status.json")
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def load_status(slug: str, record: dict[str, Any] | None) -> tuple[dict[str, Any] | None, Path | None]:
    for path in status_candidates(slug, record):
        data = read_json(path)
        if data is not None:
            return data, path
    paths = status_candidates(slug, record)
    return None, paths[0] if paths else None


def last_marker_kind(status: dict[str, Any] | None) -> str | None:
    if not status:
        return None
    marker = status.get("last_marker") or status.get("terminal_marker")
    if isinstance(marker, dict):
        value = marker.get("kind")
        return str(value) if value else None
    if isinstance(marker, str):
        return marker
    markers = status.get("markers")
    if isinstance(markers, list) and markers:
        last = markers[-1]
        if isinstance(last, dict) and last.get("kind"):
            return str(last["kind"])
    return None


def canonical_state(value: str | None) -> str | None:
    if not value:
        return None
    return dispatch_states.normalize_dispatch_state(value) or value


def state_in(value: str | None, states: set[str] | frozenset[str]) -> bool:
    return bool(value and (value in states or canonical_state(value) in states))


def retryable_failure_present(record: dict[str, Any] | None, status: dict[str, Any] | None) -> bool:
    status_state = str(status.get("state")) if status and status.get("state") else None
    record_state = str(record.get("state")) if record and record.get("state") else None
    return state_in(status_state, RETRYABLE_FAILED) or state_in(record_state, RETRYABLE_FAILED)


def runtime_record(
    record: dict[str, Any] | None,
    status: dict[str, Any] | None,
    lease: dict[str, Any] | None,
    status_path: Path | None = None,
) -> dict[str, Any] | None:
    combined: dict[str, Any] = {}
    for source in (lease, record, status):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if value is not None and value != "":
                combined[key] = value
    if status_path is not None:
        combined.setdefault("status_path", str(status_path))
    return combined or None


def worker_alive_at_read_time(record: dict[str, Any] | None) -> bool:
    if not record:
        return False
    return goalflight_status._wait_worker_confirmed_alive(record)


def normalize_state(
    record: dict[str, Any] | None,
    status: dict[str, Any] | None,
    lease: dict[str, Any] | None,
    *,
    worker_live: bool = False,
) -> str:
    marker = last_marker_kind(status) or last_marker_kind(record)
    status_state = str(status.get("state")) if status and status.get("state") else None
    record_state = str(record.get("state")) if record and record.get("state") else None

    if marker in SUCCESS_TERMINAL_MARKERS or status_state in TERMINAL_DONE or record_state in TERMINAL_DONE:
        return "complete"
    if marker in BLOCKING_TERMINAL_MARKERS:
        return "failed"
    if state_in(status_state, WEDGED_STATES) or state_in(record_state, WEDGED_STATES):
        if worker_live:
            return "running"
        return "wedged"
    if state_in(status_state, TERMINAL_FAILED) or state_in(record_state, TERMINAL_FAILED):
        return "failed"
    if status_state == "watcher_stopped" or record_state == "watcher_stopped":
        return "running"
    if status_state == "waiting_capacity" or record_state == "waiting_capacity":
        return "running"
    if (status_state and status_state.startswith("running")) or (
        record_state and record_state.startswith("running")
    ):
        return "running"
    if lease:
        return "running"
    return "missing"


def latest_timestamp(record: dict[str, Any] | None, status: dict[str, Any] | None, lease: dict[str, Any] | None) -> Any:
    if status:
        if status.get("seconds_since_event") is not None:
            return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=float(status["seconds_since_event"]))
        for key in ("updated_at", "ended_at", "started_at"):
            if status.get(key) is not None:
                return status[key]
    for source in (record, lease):
        if source:
            for key in ("updated_at", "ended_at", "released_at", "started_at"):
                if source.get(key) is not None:
                    return source[key]
    return None


def choose_record(slug: str, records: list[dict[str, Any]], leases: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    matches = [record for record in records if record_matches(record, slug)]
    lease_matches = [lease for lease in leases if record_matches(lease, slug)]

    def freshness(record: dict[str, Any]) -> dt.datetime:
        for key in ("updated_at", "ended_at", "released_at", "started_at"):
            parsed = parse_time(record.get(key))
            if parsed is not None:
                return parsed
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    record = max(matches, key=freshness) if matches else None
    lease = max(lease_matches, key=freshness) if lease_matches else None
    if record is None and lease is not None:
        record = lease
    return record, lease


def decision_hint(state: str, worker_live: bool, mins: int | None, *, retryable: bool = False) -> str:
    if state == "complete":
        return "done"
    if state == "missing":
        return "investigate"
    if state == "wedged":
        return "takeover"
    if state == "failed":
        if retryable:
            return "cooldown_retry"
        return "investigate"
    if not worker_live:
        return "takeover"
    if mins is not None and mins > 30:
        return "investigate"
    return "wait"


def summarize(slug: str, state_dir: Path) -> dict[str, Any]:
    capacity = run_capacity_status(state_dir)
    leases = list(capacity.get("active") or capacity.get("state", {}).get("leases", {}).values() or [])
    records = ledger_records(state_dir)
    record, lease = choose_record(slug, records, leases)
    status, status_path = load_status(slug, record)
    merged = runtime_record(record, status, lease, status_path)
    reconciled = goalflight_status._reconcile_output_tail_record(merged) if merged else None
    worker_pid = (reconciled or {}).get("worker_pid") or (status or {}).get("worker_pid") or (record or {}).get("worker_pid") or (lease or {}).get("worker_pid")
    worker_live = worker_alive_at_read_time(reconciled or merged)
    state = normalize_state(reconciled or record, status, lease, worker_live=worker_live)
    retryable = retryable_failure_present(reconciled or record, status)
    mins = age_mins(latest_timestamp(record, status, lease))
    log_path = (reconciled or {}).get("tail_path") or (status or {}).get("tail_path") or (record or {}).get("stdout_path") or (record or {}).get("stderr_path")
    dispatch_id = (reconciled or {}).get("dispatch_id") or (record or {}).get("dispatch_id") or (lease or {}).get("dispatch_id")
    marker = last_marker_kind(status) or last_marker_kind(reconciled)
    return {
        "slug": slug,
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_pid_alive": worker_live,
        "status_path": str(status_path) if status_path else None,
        "log_path": str(log_path) if log_path else None,
        "last_marker": marker,
        "mins_since_last_event": mins,
        "decision_hint": decision_hint(state, worker_live, mins, retryable=retryable),
    }


def text_summary(payload: dict[str, Any]) -> str:
    return (
        f"{payload['slug']}: state={payload['state']} "
        f"dispatch={payload['dispatch_id'] or 'null'} "
        f"marker={payload['last_marker'] or 'null'} "
        f"age={payload['mins_since_last_event'] if payload['mins_since_last_event'] is not None else 'null'}m "
        f"hint={payload['decision_hint']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="summarize one goal-flight chunk dispatch")
    parser.add_argument("--slug", required=True, help="chunk slug or dispatch-id prefix")
    parser.add_argument("--state-dir", default=str(goalflight_compat.resolve_state_dir()))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="emit compact JSON")
    mode.add_argument("--text", action="store_true", help="emit one-line human verdict")
    args = parser.parse_args(argv)

    payload = summarize(args.slug, Path(args.state_dir).expanduser())
    if args.json:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(text_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
