#!/usr/bin/env python3
"""Report or reclaim retained per-dispatch Codex homes.

Deletion is deliberately fail-closed: a home needs a readable ledger record,
an authoritative terminal state, an elapsed retention window, and a dead
recorded process identity. Dry-run is the default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

import goalflight_compat
import goalflight_dispatch_states
import goalflight_ledger


SCHEMA = "goalflight.reap-dispatch-homes.v1"
# One week preserves a normal incident/resume work cycle while bounding the
# growth that made permanent per-dispatch retention untenable.
DEFAULT_RETENTION_DAYS = 7.0

IdentityProbe = Callable[[dict[str, Any]], tuple[bool, str]]


def default_homes_dir() -> Path:
    """Resolve the dispatch-home root through the Codex-state authority."""

    state_dir = Path(os.environ.get("GOALFLIGHT_CODEX_STATE_DIR", "~/.goal-flight")).expanduser()
    return state_dir.resolve(strict=False) / "dispatch-homes"


def allocated_tree_bytes(root: Path) -> int:
    """Return allocated bytes below ``root`` without following symlinks.

    POSIX ``st_blocks`` counts 512-byte units by definition, so blocks * 512
    estimates filesystem space released better than logical ``st_size`` does
    for sparse SQLite files. Platforms without ``st_blocks`` fall back to the
    logical size.
    """

    total = 0
    stack = [root]
    while stack:
        path = stack.pop()
        stat = path.lstat()
        blocks = getattr(stat, "st_blocks", None)
        total += blocks * 512 if blocks is not None else stat.st_size
        if path.is_symlink() or not path.is_dir():
            continue
        with os.scandir(path) as entries:
            stack.extend(Path(entry.path) for entry in entries)
    return total


def _parse_ended_at(record: dict[str, Any]) -> dt.datetime | None:
    ended_at = goalflight_ledger.parse_utc(record.get("ended_at"))
    if ended_at is None:
        return None
    if ended_at.tzinfo is None:
        return ended_at.replace(tzinfo=dt.timezone.utc)
    return ended_at.astimezone(dt.timezone.utc)


def _read_record(ledger_dir: Path, dispatch_id: str) -> tuple[dict[str, Any] | None, str | None]:
    filename = f"{goalflight_compat.safe_dispatch_filename(dispatch_id)}.json"
    path = ledger_dir / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing_ledger_record"
    except (OSError, json.JSONDecodeError):
        return None, "unreadable_ledger_record"
    if not isinstance(payload, dict) or payload.get("dispatch_id") != dispatch_id:
        return None, "ledger_dispatch_mismatch"
    return payload, None


def _evaluate_home(
    home: Path,
    *,
    ledger_dir: Path,
    now: dt.datetime,
    retention: dt.timedelta,
    identity_probe: IdentityProbe,
) -> dict[str, Any]:
    dispatch_id = home.name
    result: dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "path": str(home),
        "allocated_bytes": allocated_tree_bytes(home),
    }
    record, record_error = _read_record(ledger_dir, dispatch_id)
    if record is None:
        return {**result, "eligible": False, "reason": record_error}

    state = record.get("state")
    result["state"] = state
    if not goalflight_dispatch_states.is_terminal_state(state):
        return {**result, "eligible": False, "reason": "non_terminal"}

    try:
        live, liveness_reason = identity_probe(record)
    except Exception as exc:
        # Ledger corruption or an unavailable process probe removes permission
        # to delete; it must not abort evaluation of the remaining homes.
        return {
            **result,
            "eligible": False,
            "reason": "liveness_check_error",
            "error": str(exc),
        }
    result["liveness"] = liveness_reason
    # Only an explicit dead/reused identity proves the recorded worker is no
    # longer using this home. Missing PIDs and future/unknown probe outcomes
    # are absence of evidence, not permission to delete operator state.
    identity_inactive = not live and (
        liveness_reason == "dead" or liveness_reason.startswith("pid_reused_")
    )
    if not identity_inactive:
        keep_reason = "live_worker" if live else "liveness_indeterminate"
        return {**result, "eligible": False, "reason": keep_reason}

    ended_at = _parse_ended_at(record)
    if ended_at is None:
        return {**result, "eligible": False, "reason": "missing_or_invalid_ended_at"}
    age = now - ended_at
    # Retention is elapsed wall time: 7 days = 7 * 24 * 60 * 60 seconds.
    # A future end time yields a negative age and therefore remains retained.
    if age < retention:
        return {
            **result,
            "eligible": False,
            "reason": "inside_retention",
            "age_seconds": age.total_seconds(),
        }
    return {
        **result,
        "eligible": True,
        "reason": "terminal_past_retention",
        "age_seconds": age.total_seconds(),
    }


def reap_dispatch_homes(
    *,
    homes_dir: Path,
    ledger_dir: Path,
    retention: dt.timedelta,
    delete: bool = False,
    now: dt.datetime | None = None,
    identity_probe: IdentityProbe | None = None,
) -> dict[str, Any]:
    """Evaluate direct child homes and optionally remove eligible directories."""

    if delete and (homes_dir.name != "dispatch-homes" or homes_dir.is_symlink()):
        raise ValueError("destructive homes directory must be a non-symlink named 'dispatch-homes'")
    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    else:
        current_time = current_time.astimezone(dt.timezone.utc)
    probe = identity_probe or goalflight_ledger.identity_matches

    entries: list[dict[str, Any]] = []
    if homes_dir.exists():
        for home in sorted(homes_dir.iterdir(), key=lambda path: path.name):
            if home.is_symlink() or not home.is_dir():
                continue
            try:
                evaluation = _evaluate_home(
                    home,
                    ledger_dir=ledger_dir,
                    now=current_time,
                    retention=retention,
                    identity_probe=probe,
                )
            except OSError as exc:
                entries.append(
                    {
                        "dispatch_id": home.name,
                        "path": str(home),
                        "eligible": False,
                        "reason": "scan_error",
                        "error": str(exc),
                        "allocated_bytes": 0,
                    }
                )
                continue

            if delete and evaluation["eligible"]:
                # Re-read every safety source immediately before deletion. A
                # changed ledger or newly live identity always changes the
                # outcome to keep; stale dry-run decisions are never executed.
                evaluation = _evaluate_home(
                    home,
                    ledger_dir=ledger_dir,
                    now=current_time,
                    retention=retention,
                    identity_probe=probe,
                )
                if evaluation["eligible"]:
                    shutil.rmtree(home)
                    evaluation["deleted"] = True
                else:
                    evaluation["reason"] = f"changed_before_delete:{evaluation['reason']}"
            entries.append(evaluation)

    eligible = [entry for entry in entries if entry["eligible"]]
    deleted = [entry for entry in entries if entry.get("deleted")]
    kept = [entry for entry in entries if not entry.get("deleted") and not entry["eligible"]]
    kept_reasons: dict[str, dict[str, int]] = {}
    for entry in kept:
        bucket = kept_reasons.setdefault(entry["reason"], {"count": 0, "allocated_bytes": 0})
        bucket["count"] += 1
        bucket["allocated_bytes"] += entry["allocated_bytes"]
    return {
        "schema": SCHEMA,
        "mode": "delete" if delete else "dry-run",
        "homes_dir": str(homes_dir),
        "ledger_dir": str(ledger_dir),
        "retention_seconds": retention.total_seconds(),
        "home_count": len(entries),
        "total_allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
        "eligible_count": len(eligible),
        "eligible_allocated_bytes": sum(entry["allocated_bytes"] for entry in eligible),
        "deleted_count": len(deleted),
        "deleted_allocated_bytes": sum(entry["allocated_bytes"] for entry in deleted),
        "kept_count": len(kept),
        "kept_reasons": kept_reasons,
        "entries": entries,
    }


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def format_human(payload: dict[str, Any]) -> str:
    action = "deleted" if payload["mode"] == "delete" else "would_reclaim"
    count_key = "deleted_count" if payload["mode"] == "delete" else "eligible_count"
    bytes_key = "deleted_allocated_bytes" if payload["mode"] == "delete" else "eligible_allocated_bytes"
    lines = [
        f"mode={payload['mode']} retention_seconds={payload['retention_seconds']:g}",
        f"homes={payload['home_count']} kept={payload['kept_count']} "
        f"{action}={payload[count_key]} allocated={_format_bytes(payload[bytes_key])}",
    ]
    for reason, summary in sorted(payload["kept_reasons"].items()):
        lines.append(
            f"  KEEP {reason} count={summary['count']} "
            f"allocated={_format_bytes(summary['allocated_bytes'])}"
        )
    for entry in payload["entries"]:
        if entry["eligible"] or entry.get("deleted"):
            verb = "DELETE" if entry.get("deleted") else "RECLAIM"
            lines.append(
                f"  {verb} {entry['dispatch_id']} allocated={_format_bytes(entry['allocated_bytes'])}"
            )
    if payload["mode"] == "dry-run":
        lines.append("Dry run only. Re-run with --delete to remove eligible homes.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report or reclaim retained per-dispatch Codex homes.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete eligible homes (default is dry-run only)",
    )
    parser.add_argument(
        "--retention-days",
        type=float,
        default=DEFAULT_RETENTION_DAYS,
        help=f"days to retain terminal dispatch homes (default: {DEFAULT_RETENTION_DAYS:g})",
    )
    parser.add_argument(
        "--homes-dir",
        type=Path,
        default=None,
        help="dispatch homes directory (default: <Goal Flight Codex-state>/dispatch-homes)",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="dispatch ledger runs directory (default: Goal Flight machine-state runs directory)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.retention_days < 0:
        raise SystemExit("--retention-days must be non-negative")
    homes_dir = args.homes_dir or default_homes_dir()
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)
    payload = reap_dispatch_homes(
        homes_dir=homes_dir,
        ledger_dir=ledger_dir,
        # CLI days convert once at the boundary; timedelta owns seconds math.
        retention=dt.timedelta(days=args.retention_days),
        delete=args.delete,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_human(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
