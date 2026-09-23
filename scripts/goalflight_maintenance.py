#!/usr/bin/env python3
"""Bounded, fail-closed maintenance for Goal Flight's machine artifacts.

The maintenance job is deliberately separate from dispatch and status.  It
loads one complete ledger snapshot before it considers a destructive action;
an unreadable or incomplete ledger therefore turns an apply run into a
report-only run.  Unknown artifacts and unknown process identities are kept.

The default command is a dry run.  Install ``com.goalflight.maintenance`` to
run the same command hourly outside the controller and worker lifecycles.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import goalflight_compat
import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_ledger
import goalflight_reap_dispatch_homes
import goalflight_trace_archive


SCHEMA = "goalflight.maintenance.v1"
DEFAULT_RETENTION_DAYS = 7.0
DEFAULT_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
DEFAULT_GATE_SUCCESS_COUNT = 3
DEFAULT_LOG_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_LOG_ARCHIVE_COUNT = 2

IdentityProbe = Callable[[int, dict[str, Any] | None], tuple[str, str]]


def _utc_now(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _parse_time(value: object) -> dt.datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    parsed = goalflight_ledger.parse_utc(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _record_end(record: dict[str, Any]) -> dt.datetime | None:
    # ended_at is the terminal authority.  The fallbacks are for older ledger
    # records that predate that field; they are still subject to terminal and
    # identity checks below.
    for key in ("ended_at", "finished_at", "terminal_at", "updated_at"):
        parsed = _parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_terminal(record: dict[str, Any]) -> bool:
    state = record.get("terminal_state") or record.get("state")
    return goalflight_dispatch_states.is_terminal_state(state)


def _read_ledger(ledger_dir: Path) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Read every ledger row, returning no authority on any read failure."""

    try:
        if not ledger_dir.is_dir() or ledger_dir.is_symlink():
            return None, "ledger_directory_unavailable"
        paths = sorted(ledger_dir.glob("*.json"))
    except OSError as exc:
        return None, f"ledger_directory_unreadable:{type(exc).__name__}"
    records: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"ledger_read_failed:{path.name}:{type(exc).__name__}"
        if not isinstance(payload, dict):
            return None, f"ledger_record_not_object:{path.name}"
        dispatch_id = payload.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            return None, f"ledger_record_missing_id:{path.name}"
        records[dispatch_id] = payload
    return records, None


def _default_identity_probe(pid: int, expected: dict[str, Any] | None) -> tuple[str, str]:
    """Classify one identity as live, dead, or unknown.

    A matching identity is live.  A reused PID is dead.  Every other outcome
    is unknown, including a missing expected token or a failed process probe.
    """

    if expected is None:
        return "unknown", "missing_expected_identity"
    try:
        current = goalflight_ledger.process_identity(pid)
    except Exception as exc:
        return "unknown", f"identity_probe_error:{type(exc).__name__}"
    if not isinstance(current, dict):
        return "dead", "process_missing"
    if current.get("identity_probe_error") or current.get("identity_available") is False:
        return "unknown", "identity_unavailable"
    try:
        matched, reason = goalflight_ledger.compare_process_identities(pid, expected, current)
    except Exception as exc:
        return "unknown", f"identity_compare_error:{type(exc).__name__}"
    if matched:
        return "live", "identity_matches"
    if str(reason).startswith("pid_reused_") or reason in {
        "identity_pid_mismatch",
        "identity_lstart_mismatch",
    }:
        return "dead", str(reason)
    return "unknown", str(reason)


def _record_identity_status(
    record: dict[str, Any],
    *,
    identity_probe: IdentityProbe,
    sidecar_paths: Iterable[Path] = (),
) -> tuple[str, str]:
    """Return ``live``, ``dead``, or ``unknown`` for all related processes."""

    process_fields = (
        ("worker_pid", "worker_identity"),
        ("watcher_pid", "watcher_identity"),
        ("waiter_pid", "waiter_identity"),
        ("wait_pid", "wait_identity"),
        ("claimant_pid", "claimant_identity"),
        ("controller_pid", "controller_identity"),
    )
    unknown_reasons: list[str] = []
    saw_process = False
    saw_sidecar_process = False
    for pid_key, identity_key in process_fields:
        raw_pid = record.get(pid_key)
        if raw_pid is None:
            continue
        saw_process = True
        if isinstance(raw_pid, bool):
            unknown_reasons.append(f"{pid_key}_invalid")
            continue
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            unknown_reasons.append(f"{pid_key}_invalid")
            continue
        if pid <= 0:
            unknown_reasons.append(f"{pid_key}_invalid")
            continue
        state, reason = identity_probe(pid, record.get(identity_key))
        if state == "live":
            return "live", f"{pid_key}:{reason}"
        if state != "dead":
            unknown_reasons.append(f"{pid_key}:{reason}")

    for path in sidecar_paths:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return "unknown", f"sidecar_unreadable:{type(exc).__name__}"
        if not isinstance(payload, dict):
            return "unknown", "sidecar_not_object"
        for pid_key, identity_key in (
            ("watcher_pid", "watcher_identity"),
            ("waiter_pid", "waiter_identity"),
            ("wait_pid", "wait_identity"),
        ):
            raw_pid = payload.get(pid_key)
            if raw_pid is None:
                continue
            saw_sidecar_process = True
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                return "unknown", f"sidecar_{pid_key}_invalid"
            state, reason = identity_probe(pid, payload.get(identity_key))
            if state == "live":
                return "live", f"sidecar_{pid_key}:{reason}"
            if state != "dead":
                unknown_reasons.append(f"sidecar_{pid_key}:{reason}")

    if unknown_reasons:
        return "unknown", ";".join(unknown_reasons)
    if not saw_process and not saw_sidecar_process:
        return "unknown", "no_recorded_process"
    return "dead", "all_recorded_processes_dead"


def _eligible_record(
    record: dict[str, Any],
    *,
    now: dt.datetime,
    retention: dt.timedelta,
    identity_probe: IdentityProbe,
    sidecar_paths: Iterable[Path] = (),
) -> tuple[bool, str]:
    if not _record_terminal(record):
        return False, "non_terminal"
    ended = _record_end(record)
    if ended is None:
        return False, "missing_or_invalid_terminal_time"
    if now - ended < retention:
        return False, "inside_retention"
    identity_state, identity_reason = _record_identity_status(
        record, identity_probe=identity_probe, sidecar_paths=sidecar_paths
    )
    if identity_state == "live":
        return False, f"live_process:{identity_reason}"
    if identity_state == "unknown":
        return False, f"unknown_process:{identity_reason}"
    return True, "terminal_past_retention"


def _path_bytes(path: Path) -> int:
    try:
        stat = path.lstat()
    except OSError:
        return 0
    if path.is_symlink():
        return 0
    if path.is_dir():
        total = 0
        try:
            for child in path.iterdir():
                total += _path_bytes(child)
        except OSError:
            return 0
        return total
    blocks = getattr(stat, "st_blocks", None)
    return int(blocks * 512 if blocks is not None else stat.st_size)


def _safe_child(path: Path, root: Path) -> bool:
    try:
        return (
            not path.is_symlink()
            and path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
        )
    except OSError:
        return False


def _delete_path(path: Path, root: Path) -> bool:
    if not _safe_child(path, root):
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        return False


def _dispatch_files(dispatch_dir: Path, dispatch_id: str) -> list[Path]:
    safe = goalflight_compat.safe_dispatch_filename(dispatch_id)
    try:
        children = list(dispatch_dir.iterdir())
    except OSError:
        return []
    return sorted(
        path
        for path in children
        if path.name == safe
        or path.name.startswith(safe + ".")
        or path.name.startswith(dispatch_id + ".")
    )


def _sidecar_paths(record: dict[str, Any], dispatch_dir: Path) -> list[Path]:
    paths: list[Path] = []
    raw = record.get("status_path")
    if isinstance(raw, str) and raw:
        path = Path(raw).expanduser()
        if path.parent.resolve(strict=False) == dispatch_dir.resolve(strict=False):
            paths.append(path)
    return paths


def _archive_before_delete(record: dict[str, Any], *, project_root: Path, apply: bool) -> dict[str, Any]:
    try:
        archived = goalflight_trace_archive.archive_finished_dispatch(
            record, apply=apply, project_root=project_root
        )
        if not archived.get("keep"):
            return goalflight_trace_archive.ensure_receipt(
                record, apply=apply, project_root=project_root
            )
        return archived
    except Exception as exc:
        return {"ok": False, "keep": True, "reason": f"archive_error:{type(exc).__name__}"}


def _maintain_dispatch_dir(
    dispatch_dir: Path,
    records: dict[str, dict[str, Any]],
    *,
    now: dt.datetime,
    retention: dt.timedelta,
    project_root: Path,
    apply: bool,
    identity_probe: IdentityProbe,
) -> dict[str, Any]:
    before = _path_bytes(dispatch_dir) if dispatch_dir.exists() else 0
    entries: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for dispatch_id, record in sorted(records.items()):
        paths = _dispatch_files(dispatch_dir, dispatch_id)
        if not paths:
            continue
        seen.update(paths)
        eligible, reason = _eligible_record(
            record,
            now=now,
            retention=retention,
            identity_probe=identity_probe,
            sidecar_paths=_sidecar_paths(record, dispatch_dir),
        )
        archive_result = _archive_before_delete(record, project_root=project_root, apply=False) if eligible else None
        if eligible and archive_result and archive_result.get("keep") and not archive_result.get("ok"):
            eligible = False
            reason = f"archive_failed:{archive_result.get('reason', 'unknown')}"
        for path in paths:
            size = _path_bytes(path)
            deleted = False
            if eligible and apply:
                # Re-read all safety inputs immediately before deleting the
                # path.  This closes the terminal-to-GC race for long sweeps.
                check, check_reason = _eligible_record(
                    record,
                    now=now,
                    retention=retention,
                    identity_probe=identity_probe,
                    sidecar_paths=_sidecar_paths(record, dispatch_dir),
                )
                if check:
                    archive_result = _archive_before_delete(record, project_root=project_root, apply=True)
                    if not (archive_result.get("keep") and not archive_result.get("ok")):
                        deleted = _delete_path(path, dispatch_dir)
                        if not deleted:
                            reason = "delete_failed"
                    else:
                        reason = f"archive_failed:{archive_result.get('reason', 'unknown')}"
                else:
                    reason = f"changed_before_delete:{check_reason}"
            entries.append(
                {
                    "dispatch_id": dispatch_id,
                    "path": str(path),
                    "bytes": size,
                    "eligible": eligible,
                    "deleted": deleted,
                    "reason": reason,
                }
            )

    # Files with no ledger match are explicitly retained, including stale
    # steer mailboxes and future sidecar formats.
    try:
        for path in sorted(dispatch_dir.iterdir()):
            if path not in seen and not path.is_symlink():
                entries.append(
                    {
                        "dispatch_id": None,
                        "path": str(path),
                        "bytes": _path_bytes(path),
                        "eligible": False,
                        "deleted": False,
                        "reason": "unknown_dispatch_artifact",
                    }
                )
    except OSError:
        entries.append({"path": str(dispatch_dir), "eligible": False, "deleted": False, "reason": "dispatch_dir_unreadable"})
    after = _path_bytes(dispatch_dir) if dispatch_dir.exists() else 0
    if not apply:
        after = max(0, before - sum(int(row.get("bytes", 0)) for row in entries if row.get("eligible")))
    return {
        "root": str(dispatch_dir),
        "before_bytes": before,
        "after_bytes": after,
        "reclaimed_bytes": max(0, before - after),
        "files": entries,
    }


def _maintain_research(
    root: Path,
    records: dict[str, dict[str, Any]],
    *,
    now: dt.datetime,
    retention: dt.timedelta,
    apply: bool,
    identity_probe: IdentityProbe,
) -> dict[str, Any]:
    research = root / "docs-private" / "research"
    before = _path_bytes(research) if research.exists() else 0
    entries: list[dict[str, Any]] = []
    if research.is_dir():
        for path in sorted(research.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            record = next(
                (
                    candidate
                    for dispatch_id, candidate in records.items()
                    if path.name.endswith("-dispatch-" + dispatch_id)
                    or path.name == dispatch_id
                ),
                None,
            )
            if record is None:
                entries.append({"path": str(path), "bytes": _path_bytes(path), "eligible": False, "deleted": False, "reason": "unknown_research_artifact"})
                continue
            eligible, reason = _eligible_record(record, now=now, retention=retention, identity_probe=identity_probe)
            deleted = False
            if eligible and apply:
                eligible, reason = _eligible_record(record, now=now, retention=retention, identity_probe=identity_probe)
                if eligible:
                    deleted = _delete_path(path, research)
                    if not deleted:
                        reason = "delete_failed"
                else:
                    reason = f"changed_before_delete:{reason}"
            entries.append({"path": str(path), "bytes": _path_bytes(path), "eligible": eligible, "deleted": deleted, "reason": reason if not deleted else "deleted"})
    after = _path_bytes(research) if research.exists() else 0
    if not apply:
        after = max(0, before - sum(int(row.get("bytes", 0)) for row in entries if row.get("eligible")))
    return {"root": str(research), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": entries}


def _maintain_setup_backups(root: Path, *, apply: bool, keep: int) -> dict[str, Any]:
    backup_root = root / "docs-private" / "log" / "project-state-backups"
    return _retain_numbered_dirs(backup_root, apply=apply, keep=keep, reason="old_setup_backup")


def _retain_numbered_dirs(root: Path, *, apply: bool, keep: int, reason: str) -> dict[str, Any]:
    before = _path_bytes(root) if root.exists() else 0
    if not root.is_dir():
        return {"root": str(root), "before_bytes": before, "after_bytes": before, "reclaimed_bytes": 0, "files": []}
    try:
        entries = sorted((path for path in root.iterdir() if path.is_dir() and not path.is_symlink()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return {"root": str(root), "before_bytes": before, "after_bytes": before, "reclaimed_bytes": 0, "files": [{"reason": "root_unreadable"}]}
    result: list[dict[str, Any]] = []
    for path in entries:
        eligible = entries.index(path) >= keep
        deleted = bool(eligible and apply and _delete_path(path, root))
        result.append({"path": str(path), "bytes": _path_bytes(path), "eligible": eligible, "deleted": deleted, "reason": reason if eligible else "keep_recent"})
    after = _path_bytes(root) if root.exists() else 0
    return {"root": str(root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": result}


def _retain_globbed_files(root: Path, patterns: Iterable[str], *, apply: bool, keep: int, reason: str) -> dict[str, Any]:
    before = _path_bytes(root) if root.exists() else 0
    candidates: list[Path] = []
    if root.is_dir():
        for pattern in patterns:
            candidates.extend(path for path in root.glob(pattern) if path.is_file() and not path.is_symlink())
    unique = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for index, path in enumerate(unique):
        eligible = index >= keep
        deleted = bool(eligible and apply and _delete_path(path, root))
        rows.append({"path": str(path), "bytes": _path_bytes(path), "eligible": eligible, "deleted": deleted, "reason": reason if eligible else "keep_recent"})
    after = _path_bytes(root) if root.exists() else 0
    return {"root": str(root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _path_is_open(path: Path) -> bool | None:
    """Return false only when the platform proves no process has the file open."""

    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = subprocess.run(
            [lsof, "-t", "--", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 1:
        return False
    if result.returncode == 0:
        return True
    return None


def rotate_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    keep: int = DEFAULT_LOG_ARCHIVE_COUNT,
) -> dict[str, Any]:
    """Rotate a log at startup, retaining a bounded number of generations."""

    keep = max(1, int(keep))
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size < max_bytes:
            return {"path": str(path), "rotated": False, "reason": "under_limit_or_missing"}
    except OSError as exc:
        return {"path": str(path), "rotated": False, "reason": f"stat_failed:{type(exc).__name__}"}
    if _path_is_open(path) is not False:
        return {"path": str(path), "rotated": False, "reason": "live_or_unknown_writer"}
    try:
        for index in range(keep - 1, 0, -1):
            older = path.with_name(path.name + f".{index - 1}")
            newer = path.with_name(path.name + f".{index}")
            if older.exists() and not older.is_symlink():
                older.replace(newer)
        archive = path.with_name(path.name + ".0")
        path.replace(archive)
        path.touch(mode=0o600)
    except OSError as exc:
        return {"path": str(path), "rotated": False, "reason": f"rotate_failed:{type(exc).__name__}"}
    return {"path": str(path), "rotated": True, "archive": str(archive)}


def _retain_gate_logs(root: Path, *, apply: bool, keep_successes: int) -> dict[str, Any]:
    before = _path_bytes(root) if root.exists() else 0
    paths = sorted(root.glob("gate-*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
    successes = 0
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            rows.append({"path": str(path), "bytes": _path_bytes(path), "eligible": False, "deleted": False, "reason": "unreadable"})
            continue
        failed = bool(re.search(r"\b(?:FAILED|ERROR|GATE FAIL)\b", text))
        eligible = not failed and successes >= keep_successes
        if not failed:
            successes += 1
        open_state = _path_is_open(path) if eligible else False
        if open_state is not False:
            eligible = False
            reason = "live_or_unknown_writer"
        else:
            reason = "old_success"
        deleted = bool(eligible and apply and _delete_path(path, root))
        rows.append({"path": str(path), "bytes": _path_bytes(path), "eligible": eligible, "deleted": deleted, "reason": reason if eligible else ("keep_failed" if failed else reason)})
    after = _path_bytes(root) if root.exists() else 0
    if not apply:
        after = max(0, before - sum(int(row.get("bytes", 0)) for row in rows if row.get("eligible")))
    return {"root": str(root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _identity_probe_for_record(records: dict[str, dict[str, Any]]) -> IdentityProbe:
    def probe(pid: int, expected: dict[str, Any] | None) -> tuple[str, str]:
        return _default_identity_probe(pid, expected)

    return probe


def run_maintenance(
    *,
    project_root: Path,
    ledger_dir: Path,
    dispatch_dir: Path,
    homes_dir: Path,
    state_dir: Path,
    retention: dt.timedelta = dt.timedelta(days=DEFAULT_RETENTION_DAYS),
    apply: bool = False,
    now: dt.datetime | None = None,
    identity_probe: IdentityProbe | None = None,
    home_dir: Path | None = None,
    gate_log_dir: Path | None = None,
    archive_max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> dict[str, Any]:
    """Run one idempotent maintenance pass and return its complete report."""

    current = _utc_now(now)
    project_root = project_root.expanduser().resolve(strict=False)
    ledger_dir = ledger_dir.expanduser().resolve(strict=False)
    dispatch_dir = dispatch_dir.expanduser().resolve(strict=False)
    homes_dir = homes_dir.expanduser().resolve(strict=False)
    state_dir = state_dir.expanduser().resolve(strict=False)
    records, ledger_error = _read_ledger(ledger_dir)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "apply" if apply else "dry-run",
        "now": current.isoformat().replace("+00:00", "Z"),
        "retention_seconds": retention.total_seconds(),
        "ledger_dir": str(ledger_dir),
        "ledger_read": "ok" if records is not None else "failed",
        "ledger_error": ledger_error,
        "components": {},
    }
    if records is None:
        # No component is allowed to delete when authority is unavailable.
        report["safety"] = "fail-closed-no-deletes"
        return report

    probe = identity_probe or _identity_probe_for_record(records)
    report["record_count"] = len(records)
    # Retain existing archives before dispatch-side receipts are archived in
    # this pass.  A just-created receipt must survive until the next hourly
    # pass rather than being born old and immediately collected.
    trace_root = project_root / goalflight_trace_archive.TRACES_DIRNAME
    report["components"]["trace_archives"] = goalflight_trace_archive.retain_archives(
        trace_root,
        records=records,
        now=current,
        retention=retention,
        max_bytes=archive_max_bytes,
        apply=apply,
        identity_probe=probe,
    )
    homes_report = goalflight_reap_dispatch_homes.reap_dispatch_homes(
        homes_dir=homes_dir,
        ledger_dir=ledger_dir,
        retention=retention,
        delete=False,
        now=current,
        identity_probe=lambda record: (
            (lambda state_reason: (state_reason[0] == "live", "dead" if state_reason[0] == "dead" else "identity_indeterminate"))(
                _record_identity_status(record, identity_probe=probe)
            )
        ),
    )
    if apply:
        # The existing reaper re-reads and re-checks before each deletion.  The
        # maintenance ledger pass above prevents it from deleting on a failed
        # global read; the callback keeps unknown identities fail-closed.
        homes_report = goalflight_reap_dispatch_homes.reap_dispatch_homes(
            homes_dir=homes_dir,
            ledger_dir=ledger_dir,
            retention=retention,
            delete=True,
            now=current,
            identity_probe=lambda record: (
                (lambda state_reason: (state_reason[0] == "live", "dead" if state_reason[0] == "dead" else "identity_indeterminate"))(
                    _record_identity_status(record, identity_probe=probe)
                )
            ),
        )
    homes_report["before_bytes"] = int(homes_report.get("total_allocated_bytes", 0))
    homes_removed = int(
        homes_report.get("deleted_allocated_bytes", 0)
        if apply
        else homes_report.get("eligible_allocated_bytes", 0)
    )
    homes_report["after_bytes"] = (
        max(0, int(homes_report["before_bytes"]) - homes_removed)
    )
    homes_report["reclaimed_bytes"] = max(0, homes_report["before_bytes"] - homes_report["after_bytes"])
    report["components"]["dispatch_homes"] = homes_report
    report["components"]["dispatch_dir"] = _maintain_dispatch_dir(
        dispatch_dir,
        records,
        now=current,
        retention=retention,
        project_root=project_root,
        apply=apply,
        identity_probe=probe,
    )
    report["components"]["research"] = _maintain_research(
        project_root,
        records,
        now=current,
        retention=retention,
        apply=apply,
        identity_probe=probe,
    )
    report["components"]["setup_backups"] = _maintain_setup_backups(project_root, apply=apply, keep=backup_count)
    setup_state = state_dir / "setup-backups"
    report["components"]["machine_setup_backups"] = _retain_numbered_dirs(
        setup_state, apply=apply, keep=backup_count, reason="old_machine_setup_backup"
    )
    home = (home_dir or Path.home()).expanduser().resolve(strict=False)
    config_rows = {
        "codex_config_backups": _retain_globbed_files(home / ".codex", ("config.toml.bak.*",), apply=apply, keep=backup_count, reason="old_config_backup"),
        "cursor_config_backups": _retain_globbed_files(home / ".cursor", ("mcp.json.bak.*",), apply=apply, keep=backup_count, reason="old_config_backup"),
        "opencode_config_backups": _retain_globbed_files(home / ".config" / "opencode", ("*.bak.*",), apply=apply, keep=backup_count, reason="old_config_backup"),
    }
    report["components"].update(config_rows)
    report["components"]["gate_logs"] = _retain_gate_logs(
        (gate_log_dir or Path(tempfile.gettempdir()) / f"goalflight-gate-{os.getuid()}"),
        apply=apply,
        keep_successes=DEFAULT_GATE_SUCCESS_COUNT,
    )
    report["before_bytes"] = sum(int(component.get("before_bytes", 0)) for component in report["components"].values())
    report["after_bytes"] = sum(int(component.get("after_bytes", component.get("before_bytes", 0))) for component in report["components"].values())
    report["reclaimed_bytes"] = max(0, report["before_bytes"] - report["after_bytes"])
    return report


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def format_human(report: dict[str, Any]) -> str:
    lines = [
        f"mode={report['mode']} ledger={report['ledger_read']} records={report.get('record_count', 0)}",
        f"before={_format_bytes(int(report.get('before_bytes', 0)))} after={_format_bytes(int(report.get('after_bytes', 0)))} reclaimed={_format_bytes(int(report.get('reclaimed_bytes', 0)))}",
    ]
    if report.get("ledger_error"):
        lines.append(f"KEEP all artifacts: {report['ledger_error']}")
    for name, component in report.get("components", {}).items():
        lines.append(
            f"{name}: reclaimed={_format_bytes(int(component.get('reclaimed_bytes', 0)))}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete/rotate eligible artifacts; default is dry-run")
    parser.add_argument("--delete", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--ledger-dir", type=Path, default=None)
    parser.add_argument("--dispatch-dir", type=Path, default=None)
    parser.add_argument("--homes-dir", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--setup-state-dir", type=Path, default=None)
    parser.add_argument("--home-dir", type=Path, default=Path.home())
    parser.add_argument("--gate-log-dir", type=Path, default=None)
    parser.add_argument("--retention-days", type=float, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--archive-max-bytes", type=int, default=DEFAULT_ARCHIVE_MAX_BYTES)
    parser.add_argument("--backup-count", type=int, default=DEFAULT_BACKUP_COUNT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.retention_days < 0 or args.archive_max_bytes < 0 or args.backup_count < 0:
        raise SystemExit("retention and limits must be non-negative")
    dispatch_state_dir = args.state_dir or goalflight_compat.resolve_state_dir()
    setup_state_dir = args.setup_state_dir or (
        Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
        / "goal-flight"
    )
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)
    dispatch_dir = args.dispatch_dir or goalflight_dispatch_paths.dispatch_base_dir(dispatch_state_dir)
    homes_dir = args.homes_dir or goalflight_reap_dispatch_homes.default_homes_dir()
    report = run_maintenance(
        project_root=args.project_root,
        ledger_dir=ledger_dir,
        dispatch_dir=dispatch_dir,
        homes_dir=homes_dir,
        state_dir=setup_state_dir,
        retention=dt.timedelta(days=args.retention_days),
        apply=bool(args.apply or args.delete),
        home_dir=args.home_dir,
        gate_log_dir=args.gate_log_dir,
        archive_max_bytes=args.archive_max_bytes,
        backup_count=args.backup_count,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(report))
    return 0 if report.get("ledger_read") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
