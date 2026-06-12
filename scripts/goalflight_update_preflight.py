#!/usr/bin/env python3
"""Idle gate for /goal-flight update worker-CLI binary swaps."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_status

BUSY_EXIT = 3
STATUS_ACTIVITY_GRACE_S = 300.0

_CLI_AGENT_LABELS: dict[str, set[str]] = {
    "codex": {"codex", "codex-acp"},
    "codex-acp": {"codex", "codex-acp"},
    "grok": {"grok", "grok-code", "grok-research", "grok-acp"},
    "grok-code": {"grok", "grok-code", "grok-research", "grok-acp"},
    "grok-research": {"grok", "grok-code", "grok-research", "grok-acp"},
    "grok-acp": {"grok", "grok-code", "grok-research", "grok-acp"},
    "cursor": {"cursor", "cursor-agent"},
    "cursor-agent": {"cursor", "cursor-agent"},
    "claude": {"claude", "claude-acp"},
    "claude-acp": {"claude", "claude-acp"},
    "claude-code-cli-acp": {"claude", "claude-acp"},
}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def agent_labels_for_scope(agent: str | None) -> set[str] | None:
    if not agent:
        return None
    key = _norm(agent).replace("_", "-")
    return set(_CLI_AGENT_LABELS.get(key, {key}))


def _record_labels(record: dict) -> set[str]:
    return {label for label in (_norm(record.get("agent")), _norm(record.get("engine"))) if label}


def _matches_scope(record: dict, scoped_labels: set[str] | None) -> bool:
    if scoped_labels is None:
        return True
    return bool(_record_labels(record) & scoped_labels)


def _live_dispatch_row(record: dict) -> dict:
    return {
        "id": record.get("dispatch_id"),
        "agent": record.get("agent"),
        "pid": record.get("worker_pid"),
        "status_path": record.get("status_path"),
    }


def _has_worker_pid(record: dict) -> bool:
    return bool(record.get("worker_pid"))


def _has_recent_status_evidence(record: dict) -> bool:
    status_path = record.get("status_path")
    if not status_path:
        return False
    try:
        mtime = Path(status_path).stat().st_mtime
    except OSError:
        return False
    age_s = max(0.0, time.time() - mtime)
    return age_s <= STATUS_ACTIVITY_GRACE_S


def _counts_as_in_flight(record: dict) -> bool:
    """Return true only for live or genuinely ambiguous dispatch rows."""
    cls = _norm(record.get("classification") or record.get("state") or "unknown")
    code = goalflight_status.done_code(record)
    if code == 1:
        if cls in {"queued_capacity", "waiting_capacity"}:
            return True
        return _has_worker_pid(record) or _has_recent_status_evidence(record)
    if code == 0:
        return False

    if cls.startswith("stale_"):
        return False
    return _has_worker_pid(record) or _has_recent_status_evidence(record)


def live_dispatches(agent: str | None = None) -> list[dict]:
    scoped_labels = agent_labels_for_scope(agent)
    payload = goalflight_status.status_payload()
    records: Iterable[dict] = payload.get("dispatch", {}).get("records", [])
    live_rows: list[dict] = []
    for record in records:
        if not _matches_scope(record, scoped_labels):
            continue
        # Use the reconciled status/ledger classification. Ambiguous rows still
        # fail closed, but stale_* rows are known-dead, not live workers.
        if _counts_as_in_flight(record):
            live_rows.append(_live_dispatch_row(record))
    return sorted(live_rows, key=lambda row: str(row.get("id") or ""))


def build_payload(*, agent: str | None, force: bool) -> tuple[dict, int]:
    try:
        rows = live_dispatches(agent)
    except Exception as exc:
        payload = {
            "idle": False,
            "live_dispatches": [],
            "advice": f"status unavailable; skip worker CLI updates until preflight works: {exc}",
        }
        return payload, BUSY_EXIT

    idle = not rows
    if idle:
        scope = f" for {agent}" if agent else ""
        advice = f"idle{scope}: no in-flight dispatches"
        return {"idle": True, "live_dispatches": [], "advice": advice}, 0

    ids = ",".join(str(row.get("id") or "?") for row in rows)
    scope = f"{agent} " if agent else ""
    count = len(rows)
    if force:
        advice = (
            f"force override: {count} in-flight {scope}worker(s) ({ids}); "
            "operator accepts mixed-binary risk"
        )
        exit_code = 0
    else:
        advice = f"busy: {count} in-flight {scope}worker(s) ({ids}); drain or pass --force"
        exit_code = BUSY_EXIT
    return {"idle": False, "live_dispatches": rows, "advice": advice}, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="preflight gate for worker CLI updates during /goal-flight update"
    )
    parser.add_argument("--check-idle", action="store_true", help="check dispatch idleness")
    parser.add_argument("--json", action="store_true", help="emit machine-readable payload")
    parser.add_argument("--agent", help="updatable CLI or worker label to scope")
    parser.add_argument(
        "--force",
        action="store_true",
        help="return success while preserving idle:false and warning advice",
    )
    args = parser.parse_args(argv)

    if not args.check_idle:
        parser.error("--check-idle is required")

    payload, exit_code = build_payload(agent=args.agent, force=args.force)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(payload["advice"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
