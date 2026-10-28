#!/usr/bin/env python3
"""Report or reconcile legacy prelaunch refusals missing their status file."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import goalflight_dispatch_paths  # noqa: E402
import goalflight_fs  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_liveness  # noqa: E402

IDENTITY_KEYS = {
    "worker_identity",
    "worker_pid",
    "worker_pgid",
    "worker_instance",
    "worker_process_identity",
    "worker_cwd",
}
LIVE_STATES = {"queued", "waiting_capacity", "submitted", "claimed", "starting", "running"}
SAFE_TERMINALS = {"error", "prelaunch_failed"}


def _has_value(value: object) -> bool:
    if value is None:
        return False
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def _has_identity(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            (key in IDENTITY_KEYS and _has_value(nested)) or _has_identity(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_identity(item) for item in value)
    # session_id/logical_session_id are dispatch ids, not engine handles.
    return False


def _refusal(record: dict) -> tuple[str, object] | None:
    outcome = record.get("outcome")
    for source, values in (
        ("record", record),
        ("record.outcome", outcome if isinstance(outcome, dict) else {}),
    ):
        for key in ("refusal_reason", "reason", "error"):
            if _has_value(values.get(key)):
                return f"{source}.{key}", values[key]
    return None


def _candidate(record: object) -> dict | None:
    if not isinstance(record, dict):
        return None
    dispatch_id = record.get("dispatch_id")
    project_root = record.get("project_root")
    state = str(record.get("state") or "")
    terminal = str(record.get("terminal_state") or "")
    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        return None
    if not isinstance(project_root, str) or not project_root.strip():
        return None
    if record.get("prelaunch_failure") is not True or _has_identity(record):
        return None
    if state in LIVE_STATES or (terminal and terminal not in SAFE_TERMINALS):
        return None

    refusal = _refusal(record)
    if refusal is None:
        return None
    raw_status = record.get("status_path")
    status_path = (
        Path(raw_status).expanduser()
        if isinstance(raw_status, str) and raw_status
        else None
    )
    if status_path is None:
        name = goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)
        status_path = goalflight_dispatch_paths.dispatch_base_dir() / f"{name}.status.json"
    if not status_path.is_absolute() or goalflight_fs.path_presence(status_path) != "absent":
        return None

    try:
        journal_path = goalflight_journal.resolve_journal_path(project_root)
        presence = goalflight_fs.path_presence(journal_path)
        if presence == "unknown":
            return None
        if presence == "present":
            journal = goalflight_journal.Journal.open_reader(project_root)
            rows = journal.read_all(
                "SELECT worker_instance_json FROM dispatch_attempts WHERE dispatch_id = ?",
                (dispatch_id,),
            )
            for row in rows:
                raw_identity = row["worker_instance_json"]
                if raw_identity and _has_value(json.loads(str(raw_identity))):
                    return None
    except Exception:
        return None

    target_state = (
        (state or terminal)
        if terminal
        else "failed_worktree"
        if state == "failed_worktree"
        else "prelaunch_failed"
    )
    target_terminal = terminal or goalflight_ledger.terminal_state_for(
        target_state, refusal[1]
    )
    return {
        "dispatch_id": dispatch_id,
        "project_root": project_root,
        "seat": record.get("worktree_seat") or record.get("worktree_id"),
        "status_path": status_path,
        "reason_field": refusal[0],
        "reason": refusal[1],
        "state": target_state,
        "terminal_state": target_terminal,
        "record": record,
    }


def _emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True))


def _dry_run(candidate: dict) -> dict:
    return {
        "mode": "dry-run",
        "dispatch_id": candidate["dispatch_id"],
        "project_root": candidate["project_root"],
        "seat": candidate["seat"],
        "qualifies_because": [
            "prelaunch_failure=true",
            "worker process evidence is absent",
            f"refusal reason recorded at {candidate['reason_field']}",
            "status file is absent",
        ],
        "would_change": {
            "state": candidate["state"],
            "terminal_state": candidate["terminal_state"],
            "worker_still_alive": False,
            "prelaunch_failure": True,
            "preserve_refusal_reason": True,
            "create_status_file": str(candidate["status_path"]),
            "status_spawn_state": "none",
            "status_worktree_seat": "omitted",
        },
    }


def _apply(candidate: dict) -> dict:
    args = argparse.Namespace(
        dispatch_id=candidate["dispatch_id"],
        state=candidate["state"],
        reason=candidate["reason"],
        terminal_state=candidate["terminal_state"],
        elapsed_s=0.0,
        worker_still_alive=False,
        prelaunch_failure=True,
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = goalflight_ledger.cmd_finish(args)
    try:
        result = json.loads(output.getvalue())
    except json.JSONDecodeError:
        result = {}
    if code or result.get("ok") is not True:
        raise RuntimeError(str(result.get("error") or result.get("disposition") or code))

    record = goalflight_ledger.read_record(candidate["dispatch_id"])
    if not isinstance(record, dict) or not record.get("terminal_state"):
        raise RuntimeError("terminal ledger record missing after journal commit")
    if record.get("prelaunch_failure") is not True or record.get("worker_still_alive") is not False:
        raise RuntimeError("prelaunch liveness proof missing after journal commit")
    reason = str(candidate["reason"])
    status = {
        "schema": "goalflight.status.v1",
        "dispatch_id": candidate["dispatch_id"],
        "state": candidate["state"],
        "terminal_state": candidate["terminal_state"],
        "reason": reason,
        "error": reason,
        "worker_pid": None,
        "worker_alive": False,
        "worker_still_alive": False,
        "spawn_state": "none",
        "prelaunch_failure": True,
        "status_path": str(candidate["status_path"]),
        "updated_at": int(time.time()),
    }
    goalflight_liveness.write_status(candidate["status_path"], status)
    if goalflight_fs.path_presence(candidate["status_path"]) != "present":
        raise RuntimeError("status file was not written")
    return {
        "mode": "apply",
        "action": "reconciled",
        "dispatch_id": candidate["dispatch_id"],
        "project_root": candidate["project_root"],
        "seat": candidate["seat"],
        "terminal_state": record["terminal_state"],
        "status_path": str(candidate["status_path"]),
        "journal_event_uuid": result.get("event_uuid"),
        "idempotent_journal_write": result.get("idempotent") is True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false")
    mode.add_argument("--apply", dest="apply", action="store_true")
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)

    candidates = [
        candidate
        for row in goalflight_ledger.read_records()
        if (candidate := _candidate(row)) is not None
    ]
    if not candidates:
        _emit(
            {
                "mode": "apply" if args.apply else "dry-run",
                "action": "no_candidates",
                "candidate_count": 0,
            }
        )
        return 0
    if not args.apply:
        for candidate in candidates:
            _emit(_dry_run(candidate))
        return 0

    failed = 0
    for selected in candidates:
        with goalflight_ledger.StateLock():
            current = goalflight_ledger.read_record(selected["dispatch_id"])
            candidate = _candidate(current)
        if candidate is None:
            _emit(
                {
                    "mode": "apply",
                    "action": "skipped_changed_since_scan",
                    "dispatch_id": selected["dispatch_id"],
                }
            )
            continue
        try:
            _emit(_apply(candidate))
        except Exception as exc:
            failed += 1
            _emit(
                {
                    "mode": "apply",
                    "action": "failed",
                    "dispatch_id": candidate["dispatch_id"],
                    "error": type(exc).__name__,
                }
            )
    _emit({"mode": "apply", "action": "summary", "failed": failed})
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
