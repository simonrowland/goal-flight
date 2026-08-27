#!/usr/bin/env python3
"""Fleet-wide controller diagnostic: connected / idle / stale / disconnected.

One read-only command over every journal under the state-dir journals index.
Consumes ``controller_roster`` (tri-state holder facts, unread, owned, idle
age, last-drain, wake/supervisor, t-338 retirement proof). Does not probe
around the roster, take journal write locks, or run retire.

    python3 scripts/goalflight_controllers.py
    python3 scripts/goalflight_controllers.py --json
    python3 scripts/goalflight_controllers.py --idle-hours 4
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

import goalflight_journal
import goalflight_session_status as sessions
import goalflight_task
import goalflight_wake


SCHEMA = "goalflight.controller-fleet.v1"
DEFAULT_IDLE_HOURS = 4.0
TABLE_COLUMNS = (
    "BUCKET",
    "LABEL",
    "PROJECT",
    "STATE",
    "IDLE",
    "UNREAD",
    "OWNED",
    "SUPERVISOR",
)
JSON_ROW_KEYS = (
    "bucket",
    "label",
    "project",
    "project_root",
    "state",
    "idle_seconds",
    "idle",
    "unread",
    "last_drain_at",
    "last_drain_seconds",
    "owned",
    "supervisor",
    "wake_armed",
    "occupies",
    "retire_command",
    "unknown_reason",
    "retirement_eligible",
    "retirement_reason",
)
BUCKET_ORDER = {
    "stale": 0,
    "connected": 1,
    "idle": 2,
    "disconnected": 3,
    None: 4,
}
HOLDER_LIVE = frozenset({"live-lock", "live-overdue"})
HOLDER_DEAD = frozenset({"dead-lock", "ended"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def peek_journal_project_roots(path: Path) -> tuple[list[str] | None, str | None]:
    """Read-only sqlite peek. ``(None, error)`` when the file cannot be told."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        rows = connection.execute(
            "SELECT DISTINCT project_root FROM controller_leases"
        ).fetchall()
        roots = [str(row[0]) for row in rows if isinstance(row[0], str) and row[0]]
        return roots, None
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("locked", "busy")):
            return None, "JournalBusy"
        return None, type(exc).__name__
    except sqlite3.Error as exc:
        return None, type(exc).__name__
    except OSError as exc:
        return None, type(exc).__name__
    finally:
        if connection is not None:
            connection.close()


def holder_state(incarnation_state: object) -> str:
    token = str(incarnation_state or "")
    if token in HOLDER_LIVE:
        return "live"
    if token in HOLDER_DEAD:
        return "dead"
    return "unknown"


def supervisor_display(*, armed: bool | None, supervisor: object) -> str:
    if armed is True:
        return "armed"
    token = str(supervisor or "")
    if armed is False and token == goalflight_wake.SUPERVISOR_ABSENT:
        return "absent"
    if armed is False:
        return "unarmed"
    return "unknown"


def classify_bucket(
    record: dict[str, Any],
    *,
    idle_hours: float,
) -> str | None:
    """Four healthy/fault buckets. None when the classifier cannot tell."""
    state = holder_state(record.get("incarnation_state"))
    retired = bool(record.get("retired"))
    occupies = not retired
    armed = record.get("wake_armed")
    idle_s = record.get("idle_seconds")
    threshold_s = idle_hours * 3600.0
    if retired and not occupies:
        return "disconnected"
    if state == "live" and armed is True:
        if idle_s is None:
            # Live and armed, but the idle classifier cannot run. Connected
            # with IDLE=unknown, never a fake idle verdict.
            return "connected"
        if idle_s > threshold_s:
            return "idle"
        return "connected"
    if state == "live":
        # Holder is live; supervisor may be absent or unknown. Not stale.
        return "connected"
    if occupies:
        return "stale"
    if retired:
        return "disconnected"
    return None


def retire_command(project_root: Path | str, label: str) -> str:
    return (
        "python3 scripts/goalflight_session_status.py --project-root "
        f"{_shell_token(project_root)} --retire {_shell_token(label)} "
        "--acknowledge-retirement"
    )


def _shell_token(value: object) -> str:
    import shlex

    return shlex.quote(str(value))


def _project_name(project_root: Path | str | None, *, fallback: str) -> str:
    if project_root is None:
        return fallback
    name = Path(str(project_root)).name.strip()
    return name or fallback


def _unknown_reason(record: dict[str, Any], *, state: str, bucket: str | None) -> str | None:
    parts: list[str] = []
    incarnation = str(record.get("incarnation_state") or "")
    if state == "unknown":
        parts.append(f"holder {incarnation or 'unknown-lock'}")
    if record.get("wake_armed") is None:
        supervisor = str(record.get("supervisor") or "unknown")
        covered = record.get("wake_covered")
        parts.append(f"supervisor {supervisor}, coverage {covered}")
    if record.get("unread_addressed_mail") is None:
        parts.append("unread unmeasured")
    if record.get("nonterminal_owned_dispatches") is None:
        parts.append("owned unmeasured")
    if record.get("idle_seconds") is None and bucket in {"connected", "idle", "stale"}:
        parts.append("idle age unmeasured")
    if not parts:
        return None
    return "; ".join(parts) + "; retirement refused until it resolves"


def _idle_cell(record: dict[str, Any], *, bucket: str | None) -> str:
    compact = record.get("idle_compact") or sessions._format_idle_compact(
        record.get("idle_seconds")
    )
    if bucket == "disconnected":
        return "—"
    return f"idle {compact}"


def fleet_row(
    record: dict[str, Any],
    *,
    project_root: Path,
    idle_hours: float,
    journal_name: str | None = None,
) -> dict[str, Any]:
    state = holder_state(record.get("incarnation_state"))
    bucket = classify_bucket(record, idle_hours=idle_hours)
    occupies = not bool(record.get("retired"))
    eligible = record.get("retirement_eligible") is True
    # Unknown never qualifies, even if t-338's PID-backed gate would pass.
    show_retire = bucket == "stale" and state == "dead" and eligible and occupies
    label = str(record.get("label") or "") or None
    unknown_reason = None
    if state == "unknown" or bucket is None:
        unknown_reason = _unknown_reason(record, state=state, bucket=bucket)
        if show_retire:
            show_retire = False
        if unknown_reason is None:
            unknown_reason = (
                "state unknown; retirement refused until it resolves"
            )
    return {
        "bucket": bucket,
        "label": label,
        "project": _project_name(project_root, fallback=journal_name or "unknown"),
        "project_root": str(project_root),
        "state": state,
        "idle_seconds": record.get("idle_seconds"),
        "idle": _idle_cell(record, bucket=bucket),
        "unread": record.get("unread_addressed_mail"),
        "last_drain_at": record.get("last_drain_at"),
        "last_drain_seconds": record.get("last_drain_seconds"),
        "owned": record.get("nonterminal_owned_dispatches"),
        "supervisor": supervisor_display(
            armed=record.get("wake_armed"),
            supervisor=record.get("supervisor"),
        ),
        "wake_armed": record.get("wake_armed"),
        "occupies": occupies,
        "retire_command": (
            retire_command(project_root, label) if show_retire and label else None
        ),
        "unknown_reason": unknown_reason,
        "retirement_eligible": record.get("retirement_eligible"),
        "retirement_reason": record.get("retirement_reason"),
    }


def unknown_project_row(
    *,
    project_root: Path | str | None,
    journal_path: Path | None,
    error: str,
) -> dict[str, Any]:
    fallback = journal_path.parent.name if journal_path is not None else "unknown"
    root_text = str(project_root) if project_root is not None else None
    reason = f"journal unreadable ({error}); retirement refused until it resolves"
    return {
        "bucket": None,
        "label": None,
        "project": _project_name(project_root, fallback=fallback),
        "project_root": root_text,
        "state": "unknown",
        "idle_seconds": None,
        "idle": "idle unknown",
        "unread": None,
        "last_drain_at": None,
        "last_drain_seconds": None,
        "owned": None,
        "supervisor": "unknown",
        "wake_armed": None,
        "occupies": None,
        "retire_command": None,
        "unknown_reason": reason,
        "retirement_eligible": None,
        "retirement_reason": error,
    }


def collect_controller_rows(
    *,
    idle_hours: float,
    ledger_records: list[dict] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    files = goalflight_journal.iter_journal_files()
    for path in files:
        roots, peek_error = peek_journal_project_roots(path)
        if roots is None:
            rows.append(
                unknown_project_row(
                    project_root=None,
                    journal_path=path,
                    error=peek_error or "unreadable",
                )
            )
            continue
        if not roots:
            # Journal exists but has no lease rows — nothing to report.
            continue
        for raw_root in roots:
            if raw_root in seen_roots:
                continue
            seen_roots.add(raw_root)
            project_root = Path(raw_root)
            try:
                resolved = goalflight_task.resolve_project_root(str(project_root))
            except (OSError, RuntimeError, ValueError):
                resolved = project_root
            try:
                roster = sessions.controller_roster(
                    resolved,
                    include_retired=True,
                    ledger_records=ledger_records,
                )
            except (
                goalflight_journal.JournalBusy,
                goalflight_journal.JournalDisappeared,
                goalflight_journal.JournalIOError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                rows.append(
                    unknown_project_row(
                        project_root=resolved,
                        journal_path=path,
                        error=type(exc).__name__,
                    )
                )
                continue
            measurements = roster.get("measurements") or {}
            registry = measurements.get("controller_registry") or {}
            if registry.get("measured") is False:
                rows.append(
                    unknown_project_row(
                        project_root=resolved,
                        journal_path=path,
                        error=str(registry.get("error") or "unreadable"),
                    )
                )
                continue
            for record in roster.get("controllers") or []:
                if not isinstance(record, dict):
                    continue
                rows.append(
                    fleet_row(
                        record,
                        project_root=resolved,
                        idle_hours=idle_hours,
                        journal_name=path.parent.name,
                    )
                )
    rows.sort(
        key=lambda row: (
            BUCKET_ORDER.get(row.get("bucket"), 4),
            str(row.get("label") or ""),
            str(row.get("project") or ""),
            str(row.get("project_root") or ""),
        )
    )
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no known controllers"
    counts: dict[str, int] = {
        "connected": 0,
        "idle": 0,
        "stale": 0,
        "disconnected": 0,
        "unknown": 0,
    }
    display_rows: list[tuple[str, ...]] = []
    for row in rows:
        bucket = row.get("bucket")
        if bucket in counts:
            counts[bucket] += 1
        else:
            counts["unknown"] += 1
        unread = row.get("unread")
        unread_text = "unknown" if unread is None else _unread_cell_from_row(row)
        owned = row.get("owned")
        display_rows.append(
            (
                str(bucket or "unknown"),
                str(row.get("label") or "—"),
                str(row.get("project") or "unknown"),
                str(row.get("state") or "unknown"),
                str(row.get("idle") or "idle unknown"),
                unread_text,
                "unknown" if owned is None else str(owned),
                str(row.get("supervisor") or "unknown"),
            )
        )
    headers = TABLE_COLUMNS
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in display_rows
    )
    lines.append("---")
    lines.append(
        "controllers: "
        f"{len(rows)}  connected {counts['connected']} · idle {counts['idle']} · "
        f"stale {counts['stale']} · disconnected {counts['disconnected']} · "
        f"unknown {counts['unknown']}"
    )
    retire_lines = [
        str(row["retire_command"])
        for row in rows
        if row.get("retire_command")
    ]
    if retire_lines:
        lines.append("retire (proof of death):")
        lines.extend(f"  {command}" for command in retire_lines)
    unknown_lines = []
    for row in rows:
        reason = row.get("unknown_reason")
        if not reason:
            continue
        label = row.get("label") or "—"
        project = row.get("project") or "unknown"
        unknown_lines.append(f"  {label} @ {project}: {reason}")
    if unknown_lines:
        lines.append("unknown (retirement refused):")
        lines.extend(unknown_lines)
    return "\n".join(lines)


def _unread_cell_from_row(row: dict[str, Any]) -> str:
    unread = row.get("unread")
    if unread is None:
        return "unknown"
    drain_s = row.get("last_drain_seconds")
    if drain_s is None:
        return str(unread)
    return f"{unread} · drain {sessions._format_idle_compact(drain_s)}"


def build_payload(
    rows: list[dict[str, Any]],
    *,
    idle_hours: float,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": _utc_now().isoformat(),
        "idle_hours": idle_hours,
        "last_drain_available": True,
        "controllers": [
            {key: row.get(key) for key in JSON_ROW_KEYS} for row in rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List controllers across every project journal: connected, idle, "
            "stale, or disconnected."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine payload (stable keys); default is the aligned table",
    )
    parser.add_argument(
        "--idle-hours",
        type=float,
        default=DEFAULT_IDLE_HOURS,
        help=(
            "idle-bucket threshold in hours (default 4). Displayed IDLE age "
            "is always the raw heartbeat age; this flag only classifies."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not isinstance(args.idle_hours, (int, float)) or args.idle_hours <= 0:
        parser.error("--idle-hours must be > 0")
    idle_hours = float(args.idle_hours)
    rows = collect_controller_rows(idle_hours=idle_hours)
    if args.json:
        print(json.dumps(build_payload(rows, idle_hours=idle_hours), indent=2, sort_keys=True))
        return 0
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
