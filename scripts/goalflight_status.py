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
import subprocess
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger

# Each aggregated record carries a precomputed ``classification`` from
# goalflight_ledger.classify(): the terminal STATE string when terminal, else one
# of these live/ambiguous labels. Do NOT re-run classify() here for normal
# records -- the aggregated record may have had identity fields stripped, so
# re-classifying would misread a live worker as unknown.
_LIVE_CLASS = "expected_live"
_AMBIGUOUS_CLASS = {"unknown_no_pid", "identity_indeterminate", "unknown"}


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


def _identity_record_for_idle_timeout(record: dict) -> dict | None:
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


def _idle_timeout_worker_alive(record: dict) -> bool:
    if (record.get("classification") or record.get("state")) != "idle_timeout":
        return False
    identity_record = _identity_record_for_idle_timeout(record)
    if identity_record is None:
        return False
    ok, _reason = goalflight_ledger.identity_matches(identity_record)
    return ok


def _reattach_hint(record: dict) -> str:
    dispatch_id = record.get("dispatch_id") or "<id>"
    return f"worker still alive - re-attach via goalflight_status.py --done {dispatch_id}"


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
        goalflight_capacity.save_state(capacity_state)
    rate_pressure = goalflight_capacity.current_rate_pressure(argparse.Namespace())
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": goalflight_capacity.profile(argparse.Namespace()),
        "capacity_state": capacity_state,
        "rate_pressure": rate_pressure,
        "dispatch": goalflight_ledger.status_payload(),
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


def done_code(record: dict) -> int:
    """0 = terminal/done, 1 = live, 2 = ambiguous/unknown."""
    if _idle_timeout_worker_alive(record):
        return 1
    cls = record.get("classification") or "unknown"
    if cls == _LIVE_CLASS:
        return 1
    if cls in _AMBIGUOUS_CLASS or cls.startswith("stale_"):
        return 2
    return 0


def find_record(payload: dict, dispatch_id: str) -> dict | None:
    for r in payload["dispatch"].get("records", []):
        if r.get("dispatch_id") == dispatch_id:
            return r
    return None


def _signal(record: dict) -> str:
    pid = record.get("worker_pid")
    if _idle_timeout_worker_alive(record):
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
    return lines


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
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.all_projects:
        project_root = None
    elif args.project:
        project_root = str(Path(args.project).resolve())
    else:
        project_root = this_project_root()

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

    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    for line in render_text(payload, args.limit):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
