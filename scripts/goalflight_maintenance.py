#!/usr/bin/env python3
"""Explicit, bounded cleanup for Goal Flight machine artifacts.

This command is never scheduled by Goal Flight. It reports JSON rows by
default and only removes artifacts with ``--apply``. Every destructive pass is
bounded by bytes, top-level files, and wall-clock time. Unknown authority,
live identities, resumable Codex homes, live steer waiters, and symlinks are
retained.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import goalflight_codex_sessions
import goalflight_compat
import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_journal
import goalflight_ledger
import goalflight_trace_archive


SCHEMA = "goalflight.maintenance.v2"
DEFAULT_RETENTION_DAYS = 7.0
# Terminal Codex homes remain resumable for this explicit window. The window
# is deliberately longer than ordinary artifact retention and is documented
# in protocols/maintenance.md.
DEFAULT_RESUME_WINDOW_DAYS = 30.0
DEFAULT_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
DEFAULT_GATE_SUCCESS_COUNT = 3
DEFAULT_LOG_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_LOG_ARCHIVE_COUNT = 2
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_FILES = 100
DEFAULT_MAX_SECONDS = 30.0

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
    for key in ("ended_at", "finished_at", "terminal_at", "updated_at"):
        parsed = _parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_terminal(record: dict[str, Any]) -> bool:
    state = record.get("terminal_state") or record.get("state")
    return goalflight_dispatch_states.is_terminal_state(state)


def _managed_root(path: Path) -> tuple[Path | None, str | None]:
    """Resolve a configured root without accepting a symlinked root."""

    raw = Path(path).expanduser()
    try:
        if raw.is_symlink():
            return None, "managed_root_is_symlink"
        return raw.resolve(strict=False), None
    except OSError as exc:
        return None, f"managed_root_unresolvable:{type(exc).__name__}"


def _safe_child(path: Path, root: Path) -> bool:
    """Accept only non-symlink descendants whose resolved path stays inside root."""

    try:
        if path.is_symlink():
            return False
        resolved_root = root.resolve(strict=False)
        resolved_path = path.resolve(strict=False)
        return resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root)
    except OSError:
        return False


def _read_ledger(
    ledger_dir: Path,
) -> tuple[dict[str, dict[str, Any]] | None, set[str], str | None]:
    """Read ledger authority, preserving duplicate IDs as indeterminate."""

    if ledger_dir.is_symlink() or not ledger_dir.is_dir():
        return None, set(), "ledger_directory_unavailable"
    try:
        paths = sorted(ledger_dir.glob("*.json"))
    except OSError as exc:
        return None, set(), f"ledger_directory_unreadable:{type(exc).__name__}"
    records: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for path in paths:
        try:
            if path.is_symlink():
                return None, set(), f"ledger_symlink:{path.name}"
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, set(), f"ledger_read_failed:{path.name}:{type(exc).__name__}"
        if not isinstance(payload, dict):
            return None, set(), f"ledger_record_not_object:{path.name}"
        dispatch_id = payload.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            return None, set(), f"ledger_record_missing_id:{path.name}"
        if dispatch_id in records:
            ambiguous.add(dispatch_id)
            records.pop(dispatch_id, None)
        elif dispatch_id in ambiguous:
            continue
        else:
            records[dispatch_id] = payload
    return records, ambiguous, None


def _fresh_record(
    ledger_dir: Path,
    dispatch_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    records, ambiguous, error = _read_ledger(ledger_dir)
    if error:
        return None, error
    if dispatch_id in ambiguous:
        return None, "authority_ambiguous_duplicate_ledger"
    record = records.get(dispatch_id) if records is not None else None
    if record is None:
        return None, "missing_ledger_record"
    return record, None


def _default_identity_probe(pid: int, expected: dict[str, Any] | None) -> tuple[str, str]:
    if expected is None:
        return "unknown", "missing_expected_identity"
    try:
        current = goalflight_ledger.process_identity(pid)
        if current is None:
            return "dead", "process_missing"
        if current.get("identity_probe_error") or current.get("identity_available") is False:
            return "unknown", "identity_unavailable"
        matched, reason = goalflight_ledger.compare_process_identities(pid, expected, current)
    except Exception as exc:
        return "unknown", f"identity_probe_error:{type(exc).__name__}"
    if matched:
        return "live", reason
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
) -> tuple[str, str]:
    fields = (
        ("worker_pid", "worker_identity"),
        ("watcher_pid", "watcher_identity"),
        ("waiter_pid", "waiter_identity"),
        ("wait_pid", "wait_identity"),
        ("claimant_pid", "claimant_identity"),
        ("controller_pid", "controller_identity"),
    )
    unknown: list[str] = []
    saw_process = False
    for pid_key, identity_key in fields:
        raw_pid = record.get(pid_key)
        if raw_pid is None:
            continue
        saw_process = True
        if isinstance(raw_pid, bool):
            unknown.append(f"{pid_key}_invalid")
            continue
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            unknown.append(f"{pid_key}_invalid")
            continue
        state, reason = identity_probe(pid, record.get(identity_key))
        if state == "live":
            return "live", f"{pid_key}:{reason}"
        if state != "dead":
            unknown.append(f"{pid_key}:{reason}")
    if unknown:
        return "unknown", ";".join(unknown)
    if not saw_process:
        return "unknown", "no_recorded_process"
    return "dead", "all_recorded_processes_dead"


def _journal_state(record: dict[str, Any], project_root: Path) -> tuple[str, str]:
    """Read the project row when a journal exists; never create one here."""

    if not (project_root / ".git").exists():
        return "absent", "project_journal_not_configured"
    try:
        journal_path = goalflight_journal.resolve_journal_path(project_root)
    except Exception as exc:
        return "unknown", f"journal_path_unavailable:{type(exc).__name__}"
    try:
        if journal_path.is_symlink():
            return "unknown", "journal_symlink"
        if not journal_path.exists():
            return "absent", "journal_missing"
        authority = goalflight_journal.Journal(project_root)
        attempt = authority.attempt_for_dispatch(str(record.get("dispatch_id") or ""))
    except Exception as exc:
        return "unknown", f"journal_read_failed:{type(exc).__name__}"
    if attempt is None:
        return "absent", "journal_dispatch_missing"
    if attempt.lifecycle_state in goalflight_journal.ATTEMPT_FINAL_STATES:
        return "settled", attempt.lifecycle_state
    return "live", attempt.lifecycle_state


def _record_project_root(record: dict[str, Any], fallback: Path | None) -> tuple[Path | None, str | None]:
    raw = record.get("project_root")
    candidate = Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else fallback
    if candidate is None:
        return None, "missing_project_root"
    return _managed_root(candidate)


def _codex_resume_available(
    record: dict[str, Any],
    home: Path,
    homes_root: Path,
    related_records: Iterable[dict[str, Any]] = (),
) -> tuple[bool, str]:
    engine = goalflight_ledger.infer_engine(record.get("engine") or record.get("agent"))
    if engine != "codex":
        return False, "not_codex"
    try:
        session_id = goalflight_codex_sessions.valid_session_id(record.get("codex_session_id"))
        owner = record.get("codex_home_owner_dispatch_id") or record.get("dispatch_id")
        recorded_home = record.get("codex_home")
        candidate = Path(recorded_home).expanduser() if isinstance(recorded_home, str) and recorded_home else homes_root / str(owner)
        resolved = candidate.resolve(strict=False)
        if resolved != home.resolve(strict=False) or not _safe_child(resolved, homes_root):
            return False, "recorded_home_not_this_dispatch_home"
        if session_id is None:
            return False, "missing_codex_session"
        rollout = goalflight_codex_sessions.rollout_path(resolved, session_id)
        if rollout is None or rollout.is_symlink():
            return False, "missing_codex_rollout"
        for candidate in related_records:
            if candidate.get("dispatch_id") == record.get("dispatch_id") or not candidate.get("parent_dispatch_id"):
                continue
            if goalflight_ledger.infer_engine(candidate.get("engine") or candidate.get("agent")) != "codex":
                continue
            if goalflight_ledger.terminal_state_for(candidate.get("state"), candidate.get("reason") or candidate.get("error")) != "unknown":
                continue
            child_session = goalflight_codex_sessions.valid_session_id(candidate.get("codex_session_id"))
            child_home = candidate.get("codex_home")
            if child_session == session_id and isinstance(child_home, str) and Path(child_home).expanduser().resolve(strict=False) == resolved:
                return False, "codex_resume_child_active"
    except (OSError, TypeError, ValueError):
        return False, "invalid_codex_resume_source"
    return True, "codex_resume_source_available"


def _mailbox_waiter_status(
    path: Path,
    dispatch_id: str,
    *,
    identity_probe: IdentityProbe,
) -> tuple[str, str]:
    """Return mailbox waiter state without quarantining or modifying the file."""

    try:
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unknown", "steer_mailbox_unreadable"
    if any(not isinstance(entry, dict) for entry in entries):
        return "unknown", "steer_mailbox_invalid"
    arms = [
        entry for entry in entries
        if entry.get("kind") == "worker_wait_started"
        and entry.get("direction", "controller_to_worker") == "worker_to_controller"
        and entry.get("dispatch_id") == dispatch_id
    ]
    if not arms:
        return "none", "no_worker_wait"
    arm = arms[-1]
    wait_id = arm.get("question_id")
    try:
        arm_seq = int(arm.get("seq"))
    except (TypeError, ValueError):
        return "unknown", "worker_wait_invalid_sequence"
    settled = False
    for entry in entries:
        try:
            entry_seq = int(entry.get("seq", 0))
        except (TypeError, ValueError):
            return "unknown", "worker_wait_invalid_sequence"
        if (
            entry.get("kind") == "worker_wait_ended"
            and entry.get("direction", "controller_to_worker") == "worker_to_controller"
            and entry.get("reply_to") == wait_id
            and entry_seq > arm_seq
            and entry.get("decision") in {"reply", "timeout", "cancelled", "expired"}
        ):
            settled = True
            break
    if settled:
        return "dead", "worker_wait_settled"
    context = arm.get("context") if isinstance(arm.get("context"), dict) else {}
    raw_pid = context.get("waiter_pid") or arm.get("waiter_pid")
    token = context.get("waiter_start_token") or arm.get("waiter_start_token")
    expected = context.get("waiter_identity") or arm.get("waiter_identity")
    if expected is None and isinstance(token, str) and token:
        expected = {"pid": raw_pid, "start_token": token}
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        return "unknown", "waiter_identity_missing"
    state, reason = identity_probe(raw_pid, expected)
    if state == "live":
        return "live", f"live_waiter:{reason}"
    if state == "dead":
        return "dead", f"dead_waiter:{reason}"
    return "unknown", f"unknown_waiter:{reason}"


@dataclass
class Budget:
    max_bytes: int
    max_files: int
    deadline: float
    planned_bytes: int = 0
    planned_files: int = 0
    deleted_bytes: int = 0
    deleted_files: int = 0
    exhausted_reason: str | None = None

    def plan(self, byte_count: int, file_count: int) -> bool:
        if time.monotonic() >= self.deadline:
            self.exhausted_reason = "time_budget_exceeded"
            return False
        if self.planned_bytes + byte_count > self.max_bytes:
            self.exhausted_reason = "byte_budget_exceeded"
            return False
        if self.planned_files + file_count > self.max_files:
            self.exhausted_reason = "file_budget_exceeded"
            return False
        self.planned_bytes += byte_count
        self.planned_files += file_count
        return True

    def mark_deleted(self, byte_count: int, file_count: int) -> None:
        self.deleted_bytes += byte_count
        self.deleted_files += file_count


def _tree_usage(path: Path, deadline: float) -> tuple[int, int, bool]:
    """Count allocated bytes and files without following symlinks."""

    total = 0
    files = 0
    stack = [path]
    try:
        while stack:
            if time.monotonic() >= deadline:
                return total, files, False
            current = stack.pop()
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            blocks = getattr(info, "st_blocks", None)
            total += int(blocks * 512 if blocks is not None else info.st_size)
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as entries:
                    stack.extend(Path(entry.path) for entry in entries)
            else:
                files += 1
    except OSError:
        return total, files, False
    return total, files, True


def _path_bytes(path: Path, deadline: float | None = None) -> int:
    scan_deadline = deadline if deadline is not None else time.monotonic() + 5.0
    return _tree_usage(path, scan_deadline)[0] if path.exists() and not path.is_symlink() else 0


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
        path for path in children
        if path.name == safe
        or path.name.startswith(safe + ".")
        or path.name.startswith(dispatch_id + ".")
    )


def _archive_before_delete(record: dict[str, Any], *, fallback_root: Path | None, apply: bool) -> dict[str, Any]:
    root, root_error = _record_project_root(record, fallback_root)
    if root is None:
        return {"ok": False, "keep": True, "reason": root_error or "invalid_project_root"}
    try:
        archived = goalflight_trace_archive.archive_finished_dispatch(record, apply=apply, project_root=root)
        if not archived.get("keep") or not archived.get("ok"):
            return goalflight_trace_archive.ensure_receipt(record, apply=apply, project_root=root) if archived.get("ok") else archived
        receipt = goalflight_trace_archive.ensure_receipt(record, apply=apply, project_root=root)
        if not receipt.get("ok"):
            return receipt
        return {**archived, "receipt": receipt}
    except Exception as exc:
        return {"ok": False, "keep": True, "reason": f"archive_error:{type(exc).__name__}"}


def _eligible_record(
    record: dict[str, Any],
    *,
    now: dt.datetime,
    retention: dt.timedelta,
    identity_probe: IdentityProbe,
    project_root: Path | None,
) -> tuple[bool, str]:
    if not _record_terminal(record):
        return False, "non_terminal"
    ended = _record_end(record)
    if ended is None:
        return False, "missing_or_invalid_terminal_time"
    if now - ended < retention:
        return False, "inside_retention"
    if project_root is not None:
        journal_state, journal_reason = _journal_state(record, project_root)
        if journal_state in {"live", "unknown"}:
            return False, f"journal_not_settled:{journal_reason}"
    identity_state, identity_reason = _record_identity_status(record, identity_probe=identity_probe)
    if identity_state == "live":
        return False, f"live_process:{identity_reason}"
    if identity_state == "unknown":
        return False, f"unknown_process:{identity_reason}"
    return True, "terminal_past_retention"


def _candidate_row(path: Path, *, byte_count: int, eligible: bool, deleted: bool, reason: str, dispatch_id: str | None = None, component: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"path": str(path), "bytes": byte_count, "eligible": eligible, "deleted": deleted, "reason": reason}
    if dispatch_id is not None:
        row["dispatch_id"] = dispatch_id
    if component is not None:
        row["component"] = component
    return row


def _maintain_dispatch_dir(
    dispatch_dir: Path,
    records: dict[str, dict[str, Any]],
    ambiguous: set[str],
    *,
    ledger_dir: Path,
    now: dt.datetime,
    retention: dt.timedelta,
    fallback_root: Path | None,
    apply: bool,
    identity_probe: IdentityProbe,
    budget: Budget,
) -> dict[str, Any]:
    root, root_error = _managed_root(dispatch_dir)
    if root is None:
        return {"root": str(dispatch_dir), "files": [_candidate_row(dispatch_dir, byte_count=0, eligible=False, deleted=False, reason=root_error or "invalid_root", component="dispatch_dir")], "before_bytes": 0, "after_bytes": 0, "reclaimed_bytes": 0}
    before = _path_bytes(root, budget.deadline)
    rows: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for dispatch_id, record in sorted(records.items()):
        paths = _dispatch_files(root, dispatch_id)
        seen.update(paths)
        if not paths:
            continue
        project_root, project_error = _record_project_root(record, fallback_root)
        eligible, reason = _eligible_record(record, now=now, retention=retention, identity_probe=identity_probe, project_root=project_root)
        if project_root is None:
            eligible, reason = False, project_error or "invalid_project_root"
        for path in paths:
            size, files, complete = _tree_usage(path, budget.deadline)
            row_reason = reason
            row_eligible = eligible and complete and not path.is_symlink()
            if not complete:
                row_eligible, row_reason = False, "scan_budget_exceeded"
            if path.name.endswith(".steer.jsonl") and row_eligible:
                waiter_state, waiter_reason = _mailbox_waiter_status(path, dispatch_id, identity_probe=identity_probe)
                if waiter_state in {"live", "unknown"}:
                    row_eligible, row_reason = False, waiter_reason
            deleted = False
            if row_eligible:
                if not budget.plan(size, max(1, files)):
                    row_eligible, row_reason = False, budget.exhausted_reason or "budget_exceeded"
                elif apply:
                    current, current_error = _fresh_record(ledger_dir, dispatch_id)
                    if current is None:
                        row_eligible, row_reason = False, current_error or "authority_unavailable_before_delete"
                    else:
                        current_root, _ = _record_project_root(current, fallback_root)
                        current_ok, current_reason = _eligible_record(current, now=now, retention=retention, identity_probe=identity_probe, project_root=current_root)
                        if path.name.endswith(".steer.jsonl") and current_ok:
                            waiter_state, waiter_reason = _mailbox_waiter_status(path, dispatch_id, identity_probe=identity_probe)
                            if waiter_state in {"live", "unknown"}:
                                current_ok, current_reason = False, waiter_reason
                        if not current_ok:
                            row_eligible, row_reason = False, f"changed_before_delete:{current_reason}"
                        else:
                            archive_result = _archive_before_delete(current, fallback_root=fallback_root, apply=True)
                            if archive_result.get("keep") and not archive_result.get("ok"):
                                row_eligible, row_reason = False, f"archive_failed:{archive_result.get('reason', 'unknown')}"
                            else:
                                deleted = _delete_path(path, root)
                                if deleted:
                                    budget.mark_deleted(size, max(1, files))
                                else:
                                    row_eligible, row_reason = False, "delete_failed"
            rows.append(_candidate_row(path, byte_count=size, eligible=row_eligible, deleted=deleted, reason="deleted" if deleted else row_reason, dispatch_id=dispatch_id, component="dispatch_dir"))
    try:
        for path in sorted(root.iterdir()):
            if path not in seen:
                rows.append(_candidate_row(path, byte_count=_path_bytes(path, budget.deadline), eligible=False, deleted=False, reason="unknown_dispatch_artifact", component="dispatch_dir"))
    except OSError:
        rows.append(_candidate_row(root, byte_count=0, eligible=False, deleted=False, reason="dispatch_dir_unreadable", component="dispatch_dir"))
    after = _path_bytes(root, budget.deadline)
    if not apply:
        after = max(0, before - sum(row["bytes"] for row in rows if row["eligible"]))
    return {"root": str(root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _maintain_homes(
    homes_dir: Path,
    records: dict[str, dict[str, Any]],
    ambiguous: set[str],
    *,
    ledger_dir: Path,
    now: dt.datetime,
    retention: dt.timedelta,
    resume_window: dt.timedelta,
    fallback_root: Path | None,
    apply: bool,
    identity_probe: IdentityProbe,
    budget: Budget,
) -> dict[str, Any]:
    root, root_error = _managed_root(homes_dir)
    if root is None:
        return {"root": str(homes_dir), "files": [_candidate_row(homes_dir, byte_count=0, eligible=False, deleted=False, reason=root_error or "invalid_root", component="dispatch_homes")], "before_bytes": 0, "after_bytes": 0, "reclaimed_bytes": 0}
    before = _path_bytes(root, budget.deadline)
    rows: list[dict[str, Any]] = []
    try:
        homes = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        homes = []
    for home in homes:
        if home.is_symlink() or not home.is_dir():
            rows.append(_candidate_row(home, byte_count=0, eligible=False, deleted=False, reason="symlink_or_non_directory", dispatch_id=home.name, component="dispatch_homes"))
            continue
        size, files, complete = _tree_usage(home, budget.deadline)
        dispatch_id = home.name
        if dispatch_id in ambiguous:
            eligible, reason = False, "authority_ambiguous_duplicate_ledger"
        else:
            record = records.get(dispatch_id)
            if record is None:
                eligible, reason = False, "missing_ledger_record"
            else:
                project_root, project_error = _record_project_root(record, fallback_root)
                eligible, reason = _eligible_record(record, now=now, retention=retention, identity_probe=identity_probe, project_root=project_root)
                if project_root is None:
                    eligible, reason = False, project_error or "invalid_project_root"
                ended = _record_end(record)
                resumable, resume_reason = _codex_resume_available(record, home, root, records.values())
                if eligible and resume_reason == "codex_resume_child_active":
                    eligible, reason = False, resume_reason
                elif eligible and resumable and ended is not None and now - ended < resume_window:
                    eligible, reason = False, resume_reason
                elif eligible and resumable:
                    reason = "resume_window_expired"
        if not complete:
            eligible, reason = False, "scan_budget_exceeded"
        deleted = False
        if eligible:
            if not budget.plan(size, max(1, files)):
                eligible, reason = False, budget.exhausted_reason or "budget_exceeded"
            elif apply:
                current, current_error = _fresh_record(ledger_dir, dispatch_id)
                if current is None:
                    eligible, reason = False, current_error or "authority_unavailable_before_delete"
                else:
                    current_root, _ = _record_project_root(current, fallback_root)
                    current_ok, current_reason = _eligible_record(current, now=now, retention=retention, identity_probe=identity_probe, project_root=current_root)
                    fresh_records, fresh_ambiguous, fresh_error = _read_ledger(ledger_dir)
                    if fresh_error or fresh_records is None or dispatch_id in fresh_ambiguous:
                        current_ok, current_reason = False, "authority_ambiguous_or_unavailable_before_delete"
                        fresh_records = {}
                    resumable, resume_reason = _codex_resume_available(current, home, root, fresh_records.values())
                    ended = _record_end(current)
                    if current_ok and resume_reason == "codex_resume_child_active":
                        current_ok, current_reason = False, resume_reason
                    elif current_ok and resumable and ended is not None and now - ended < resume_window:
                        current_ok, current_reason = False, resume_reason
                    if not current_ok:
                        eligible, reason = False, f"changed_before_delete:{current_reason}"
                    else:
                        deleted = _delete_path(home, root)
                        if deleted:
                            budget.mark_deleted(size, max(1, files))
                        else:
                            eligible, reason = False, "delete_failed"
        rows.append(_candidate_row(home, byte_count=size, eligible=eligible, deleted=deleted, reason="deleted" if deleted else reason, dispatch_id=dispatch_id, component="dispatch_homes"))
    after = _path_bytes(root, budget.deadline)
    if not apply:
        after = max(0, before - sum(row["bytes"] for row in rows if row["eligible"]))
    return {"root": str(root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _root_groups(records: dict[str, dict[str, Any]], fallback_root: Path | None) -> dict[Path, dict[str, dict[str, Any]]]:
    groups: dict[Path, dict[str, dict[str, Any]]] = {}
    for dispatch_id, record in records.items():
        root, _ = _record_project_root(record, fallback_root)
        if root is not None:
            groups.setdefault(root, {})[dispatch_id] = record
    if fallback_root is not None:
        root, _ = _managed_root(fallback_root)
        if root is not None:
            groups.setdefault(root, {})
    return groups


def _maintain_traces(
    records: dict[str, dict[str, Any]],
    *,
    ledger_dir: Path,
    fallback_root: Path | None,
    now: dt.datetime,
    retention: dt.timedelta,
    archive_max_bytes: int,
    apply: bool,
    identity_probe: IdentityProbe,
    budget: Budget,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for project_root, scoped_records in _root_groups(records, fallback_root).items():
        trace_root = project_root / goalflight_trace_archive.TRACES_DIRNAME
        preview = goalflight_trace_archive.retain_archives(trace_root, records={**records, **scoped_records}, now=now, retention=retention, max_bytes=archive_max_bytes, apply=False, identity_probe=identity_probe, deadline=budget.deadline)
        before = int(preview.get("before_bytes", 0))
        rows = preview.get("files", [])
        for row in rows:
            row["component"] = "trace_archives"
            if not row.get("eligible"):
                continue
            path = Path(str(row.get("path")))
            size, files, complete = _tree_usage(path, budget.deadline)
            dispatch_id = str(row.get("dispatch_id") or "")
            current, current_error = _fresh_record(ledger_dir, dispatch_id) if dispatch_id else (None, "missing_dispatch_id")
            if current is None:
                row.update({"eligible": False, "reason": current_error or "authority_unavailable_before_delete", "bytes": size})
                continue
            if not complete or not _safe_child(path, trace_root):
                row.update({"eligible": False, "reason": "scan_budget_exceeded" if not complete else "unsafe_trace_path", "bytes": size})
                continue
            if not budget.plan(size, max(1, files)):
                row.update({"eligible": False, "reason": budget.exhausted_reason or "budget_exceeded", "bytes": size})
                continue
            if apply:
                latest_root, _ = _record_project_root(current, fallback_root)
                if latest_root != project_root:
                    row.update({"eligible": False, "reason": "changed_before_delete:project_root_changed"})
                    continue
                current_ok, current_reason = _eligible_record(current, now=now, retention=retention, identity_probe=identity_probe, project_root=latest_root)
                if not current_ok:
                    row.update({"eligible": False, "reason": f"changed_before_delete:{current_reason}"})
                    continue
                if _delete_path(path, trace_root):
                    row.update({"deleted": True, "reason": "deleted", "bytes": size})
                    budget.mark_deleted(size, max(1, files))
                else:
                    row.update({"eligible": False, "reason": "delete_failed", "bytes": size})
            else:
                row["bytes"] = size
        after = _path_bytes(trace_root, budget.deadline)
        if not apply:
            after = max(0, before - sum(int(row.get("bytes", 0)) for row in rows if row.get("eligible")))
        reports.append({"root": str(trace_root), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "max_bytes": archive_max_bytes, "files": rows})
    return _merge_component_reports(reports)


def _merge_component_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {"roots": [report.get("root") for report in reports], "before_bytes": sum(int(report.get("before_bytes", 0)) for report in reports), "after_bytes": sum(int(report.get("after_bytes", 0)) for report in reports), "reclaimed_bytes": sum(int(report.get("reclaimed_bytes", 0)) for report in reports), "files": [row for report in reports for row in report.get("files", [])]}


def _retain_entries(root: Path, entries: Iterable[Path], *, keep: int, reason: str, apply: bool, budget: Budget, component: str) -> dict[str, Any]:
    managed, root_error = _managed_root(root)
    if managed is None:
        return {"root": str(root), "before_bytes": 0, "after_bytes": 0, "reclaimed_bytes": 0, "files": [_candidate_row(root, byte_count=0, eligible=False, deleted=False, reason=root_error or "invalid_root", component=component)]}
    before = _path_bytes(managed, budget.deadline)
    rows: list[dict[str, Any]] = []
    try:
        ordered = sorted((path for path in entries if (path.is_dir() or path.is_file()) and not path.is_symlink()), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        ordered = []
    for index, path in enumerate(ordered):
        eligible = index >= keep
        size, files, complete = _tree_usage(path, budget.deadline)
        deleted = False
        row_reason = reason if eligible else "keep_recent"
        if eligible and not complete:
            eligible, row_reason = False, "scan_budget_exceeded"
        if eligible:
            if not budget.plan(size, max(1, files)):
                eligible, row_reason = False, budget.exhausted_reason or "budget_exceeded"
            elif apply:
                deleted = _delete_path(path, managed)
                if deleted:
                    budget.mark_deleted(size, max(1, files))
                else:
                    eligible, row_reason = False, "delete_failed"
        rows.append(_candidate_row(path, byte_count=size, eligible=eligible, deleted=deleted, reason="deleted" if deleted else row_reason, component=component))
    after = _path_bytes(managed, budget.deadline)
    if not apply:
        after = max(0, before - sum(row["bytes"] for row in rows if row["eligible"]))
    return {"root": str(managed), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _maintain_backups(project_roots: Iterable[Path], state_dir: Path, *, apply: bool, budget: Budget, keep: int, home_dir: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for root in project_roots:
        backup_root = root / "docs-private" / "log" / "project-state-backups"
        entries = backup_root.iterdir() if backup_root.is_dir() and not backup_root.is_symlink() else []
        reports.append(_retain_entries(backup_root, entries, keep=keep, reason="old_setup_backup", apply=apply, budget=budget, component="project_backups"))
    setup_root = state_dir / "setup-backups"
    entries = setup_root.iterdir() if setup_root.is_dir() and not setup_root.is_symlink() else []
    reports.append(_retain_entries(setup_root, entries, keep=keep, reason="old_machine_setup_backup", apply=apply, budget=budget, component="machine_backups"))
    for config_root, pattern in ((home_dir / ".codex", "config.toml.bak.*"), (home_dir / ".cursor", "mcp.json.bak.*"), (home_dir / ".config" / "opencode", "*.bak.*")):
        managed, root_error = _managed_root(config_root)
        if managed is None or not managed.is_dir():
            reports.append({"root": str(config_root), "before_bytes": 0, "after_bytes": 0, "reclaimed_bytes": 0, "files": [] if root_error is None else [_candidate_row(config_root, byte_count=0, eligible=False, deleted=False, reason=root_error, component="config_backups")]})
            continue
        try:
            files = sorted((path for path in managed.glob(pattern) if path.is_file() and not path.is_symlink()), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        reports.append(_retain_entries(managed, files[keep:], keep=0, reason="old_config_backup", apply=apply, budget=budget, component="config_backups"))
    return _merge_component_reports(reports)


def _retain_gate_logs(root: Path, *, apply: bool, keep_successes: int, budget: Budget) -> dict[str, Any]:
    managed, root_error = _managed_root(root)
    if managed is None:
        return {"root": str(root), "before_bytes": 0, "after_bytes": 0, "reclaimed_bytes": 0, "files": [_candidate_row(root, byte_count=0, eligible=False, deleted=False, reason=root_error or "invalid_root", component="gate_logs")]}
    before = _path_bytes(managed, budget.deadline)
    successes = 0
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(managed.glob("gate-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        paths = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            rows.append(_candidate_row(path, byte_count=0, eligible=False, deleted=False, reason="unreadable", component="gate_logs"))
            continue
        failed = bool(re.search(r"\b(?:FAILED|ERROR|GATE FAIL)\b", text))
        eligible = not failed and successes >= keep_successes
        if not failed:
            successes += 1
        reason = "old_success" if eligible else ("keep_failed" if failed else "keep_recent")
        size = _path_bytes(path, budget.deadline)
        deleted = False
        if eligible:
            if not budget.plan(size, 1):
                eligible, reason = False, budget.exhausted_reason or "budget_exceeded"
            elif apply:
                deleted = _delete_path(path, managed)
                if deleted:
                    budget.mark_deleted(size, 1)
                else:
                    eligible, reason = False, "delete_failed"
        rows.append(_candidate_row(path, byte_count=size, eligible=eligible, deleted=deleted, reason="deleted" if deleted else reason, component="gate_logs"))
    after = _path_bytes(managed, budget.deadline)
    if not apply:
        after = max(0, before - sum(row["bytes"] for row in rows if row["eligible"]))
    return {"root": str(managed), "before_bytes": before, "after_bytes": after, "reclaimed_bytes": max(0, before - after), "files": rows}


def _path_is_open(path: Path) -> bool | None:
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = subprocess.run([lsof, "-t", "--", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 1:
        return False
    if result.returncode == 0:
        return True
    return None


def rotate_log(path: Path, *, max_bytes: int = DEFAULT_LOG_MAX_BYTES, keep: int = DEFAULT_LOG_ARCHIVE_COUNT) -> dict[str, Any]:
    """Rotate a closed log at startup, retaining bounded generations."""

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


def run_maintenance(
    *,
    project_root: Path | None,
    ledger_dir: Path,
    dispatch_dir: Path,
    homes_dir: Path,
    state_dir: Path,
    retention: dt.timedelta = dt.timedelta(days=DEFAULT_RETENTION_DAYS),
    resume_window: dt.timedelta = dt.timedelta(days=DEFAULT_RESUME_WINDOW_DAYS),
    apply: bool = False,
    now: dt.datetime | None = None,
    identity_probe: IdentityProbe | None = None,
    home_dir: Path | None = None,
    gate_log_dir: Path | None = None,
    archive_max_bytes: int = DEFAULT_ARCHIVE_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> dict[str, Any]:
    current = _utc_now(now)
    fallback_root, fallback_error = _managed_root(project_root) if project_root is not None else (None, None)
    budget = Budget(max_bytes=max_bytes, max_files=max_files, deadline=time.monotonic() + max_seconds)
    records, ambiguous, ledger_error = _read_ledger(ledger_dir)
    report: dict[str, Any] = {"schema": SCHEMA, "mode": "apply" if apply else "dry-run", "now": current.isoformat().replace("+00:00", "Z"), "retention_seconds": retention.total_seconds(), "resume_window_seconds": resume_window.total_seconds(), "ledger_dir": str(ledger_dir), "ledger_read": "ok" if records is not None else "failed", "ledger_error": ledger_error, "ambiguous_dispatch_ids": sorted(ambiguous), "components": {}, "candidates": [], "budget": {"max_bytes": max_bytes, "max_files": max_files, "max_seconds": max_seconds}}
    if records is None:
        report["safety"] = "fail-closed-no-deletes"
        return report
    probe = identity_probe or _default_identity_probe
    report["record_count"] = len(records)
    report["components"]["trace_archives"] = _maintain_traces(records, ledger_dir=ledger_dir, fallback_root=fallback_root, now=current, retention=retention, archive_max_bytes=archive_max_bytes, apply=apply, identity_probe=probe, budget=budget)
    report["components"]["dispatch_homes"] = _maintain_homes(homes_dir, records, ambiguous, ledger_dir=ledger_dir, now=current, retention=retention, resume_window=resume_window, fallback_root=fallback_root, apply=apply, identity_probe=probe, budget=budget)
    report["components"]["dispatch_dir"] = _maintain_dispatch_dir(dispatch_dir, records, ambiguous, ledger_dir=ledger_dir, now=current, retention=retention, fallback_root=fallback_root, apply=apply, identity_probe=probe, budget=budget)
    roots = _root_groups(records, fallback_root).keys()
    report["components"]["backups"] = _maintain_backups(roots, state_dir, apply=apply, budget=budget, keep=backup_count, home_dir=(home_dir or Path.home()))
    gate_root = gate_log_dir or Path(tempfile.gettempdir()) / f"goalflight-gate-{os.getuid()}"
    report["components"]["gate_logs"] = _retain_gate_logs(gate_root, apply=apply, keep_successes=DEFAULT_GATE_SUCCESS_COUNT, budget=budget)
    report["before_bytes"] = sum(int(component.get("before_bytes", 0)) for component in report["components"].values())
    report["after_bytes"] = sum(int(component.get("after_bytes", component.get("before_bytes", 0))) for component in report["components"].values())
    report["reclaimed_bytes"] = max(0, report["before_bytes"] - report["after_bytes"])
    report["budget"].update({"planned_bytes": budget.planned_bytes, "planned_files": budget.planned_files, "deleted_bytes": budget.deleted_bytes, "deleted_files": budget.deleted_files, "exhausted_reason": budget.exhausted_reason})
    report["candidates"] = [row for component in report["components"].values() for row in component.get("files", [])]
    if fallback_error:
        report["project_root_error"] = fallback_error
    return report


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def format_human(report: dict[str, Any]) -> str:
    lines = [f"mode={report['mode']} ledger={report['ledger_read']} records={report.get('record_count', 0)}", f"before={_format_bytes(int(report.get('before_bytes', 0)))} after={_format_bytes(int(report.get('after_bytes', 0)))} reclaimed={_format_bytes(int(report.get('reclaimed_bytes', 0)))}"]
    for row in report.get("candidates", []):
        verb = "DELETE" if row.get("deleted") else ("REMOVE" if row.get("eligible") else "KEEP")
        lines.append(f"{verb} {row.get('path')} bytes={row.get('bytes', 0)} reason={row.get('reason')}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="remove eligible artifacts; default is dry-run")
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
    parser.add_argument("--resume-window-days", type=float, default=DEFAULT_RESUME_WINDOW_DAYS)
    parser.add_argument("--archive-max-bytes", type=int, default=DEFAULT_ARCHIVE_MAX_BYTES)
    parser.add_argument("--backup-count", type=int, default=DEFAULT_BACKUP_COUNT)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="maximum allocated bytes removed per run")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="maximum files/artifacts removed per run")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS, help="maximum cleanup wall-clock time")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    numeric = (args.retention_days, args.resume_window_days, args.archive_max_bytes, args.backup_count, args.max_bytes, args.max_files, args.max_seconds)
    if any(value < 0 for value in numeric):
        raise SystemExit("retention and limits must be non-negative")
    dispatch_state_dir = args.state_dir or goalflight_compat.resolve_state_dir()
    setup_state_dir = args.setup_state_dir or (Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "goal-flight")
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)
    dispatch_dir = args.dispatch_dir or goalflight_dispatch_paths.dispatch_base_dir(dispatch_state_dir)
    homes_dir = args.homes_dir or (Path(os.environ.get("GOALFLIGHT_CODEX_STATE_DIR", "~/.goal-flight")).expanduser() / "dispatch-homes")
    report = run_maintenance(project_root=args.project_root, ledger_dir=ledger_dir, dispatch_dir=dispatch_dir, homes_dir=homes_dir, state_dir=setup_state_dir, retention=dt.timedelta(days=args.retention_days), resume_window=dt.timedelta(days=args.resume_window_days), apply=bool(args.apply), home_dir=args.home_dir, gate_log_dir=args.gate_log_dir, archive_max_bytes=args.archive_max_bytes, backup_count=args.backup_count, max_bytes=args.max_bytes, max_files=args.max_files, max_seconds=args.max_seconds)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(report))
    return 0 if report.get("ledger_read") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
