#!/usr/bin/env python3
"""Report or run Goal Flight's single bounded-retention sweep."""
from __future__ import annotations
import argparse
import contextlib
import ctypes
import datetime as dt
import gzip
import io
import json
import os
from pathlib import Path
try:
    import resource
except ImportError:  # pragma: no cover - unavailable on native Windows
    resource = None
import shutil
import stat as stat_module
import sys
import subprocess
import threading
import time
from typing import Any, Callable
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows cannot run the scheduled sweep
    fcntl = None
import goalflight_compat
import goalflight_dispatch_states
import goalflight_dispatch_paths
import goalflight_ledger
import goalflight_fs
import goalflight_subprocess

SCHEMA = "goalflight.retention-sweep.v1"
DISPATCH_ARTIFACT_BUDGET_BYTES = 40_000_000_000
DISPATCH_ARTIFACT_MINIMUM_AGE = dt.timedelta(hours=24)
# Scratch is temp output; a resume after 6 h keeps its session but must recreate any scratch clone.
DISPATCH_SCRATCH_MINIMUM_AGE = dt.timedelta(hours=6)
DISPATCH_GC_SCRATCH_DAYS = 3
DISPATCH_GC_RESUMABLE_SCRATCH_DAYS = 3
DISPATCH_GC_TAIL_DAYS = 7
DISPATCH_GC_PIN_FILE = ".dispatch-gc-pins"
DISPATCH_SCRATCH_LOW_SPACE_MINIMUM_AGE = dt.timedelta(hours=1)
DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES = 20_000_000_000
DISPATCH_SCRATCH_SIZE_WARNING_BYTES = 5_000_000_000
DISPATCH_SCRATCH_WALK_MAX_ENTRIES = 100_000
DISPATCH_SCRATCH_WALK_TIMEOUT_SECONDS = 30.0
_DISPATCH_SCRATCH_IDENTITY_PROBE_TIMEOUT_SECONDS = 30.0
_PROJECT_ROOT_GIT_TIMEOUT_SECONDS = 30.0
_SWEEP_IDENTITY_TIMEOUT_SECONDS = 30.0
_LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS = 60.0
_GROK_ARCHIVE_TIMEOUT_FLOOR_SECONDS = 30.0
_GROK_ARCHIVE_TIMEOUT_CAP_SECONDS = 300.0
_GROK_ARCHIVE_BYTES_PER_SECOND = 8 * 1024 * 1024
TRACE_RETENTION_DAYS = 30
SWEEP_INTERVAL_S = 60 * 60
_SWEEP_TIMEOUT_SECONDS = 15 * 60
_SWEEP_CWD_SNAPSHOT_MAX_AGE_SECONDS = 30.0
CODEX_HOME_MINIMUM_IDLE = dt.timedelta(hours=2)
GROK_SESSION_MINIMUM_IDLE = dt.timedelta(days=7)
_CODEX_HOME_TRIM_DIRS = {
    "cache", "plugins", "shell_snapshots", "tmp", "thread-writer-locks",
}
_CODEX_HOME_TRIM_FILES = {"models_cache.json"}
IdentityProbe = Callable[[dict[str, Any]], tuple[bool, str]]
_DEAD_REASONS = {"dead", "pid_reused", "pid_reused_start_token"}
_KNOWN_NONTERMINAL_STATES = (
    goalflight_dispatch_states.RUNNING_STATES
    | goalflight_dispatch_states.ATTENTION_STATES
    | goalflight_dispatch_states.SALVAGE_NEEDED_STATES
    | set(goalflight_dispatch_states.DISPATCH_STATE_ALIASES)
    | set(goalflight_dispatch_states.DISPATCH_STATE_ALIASES.values())
    | {"claimed", "launched", goalflight_dispatch_states.WORKER_STALLED_CANDIDATE_STATE}
)


class _TreeWalkLimitExceeded(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
_DISPATCH_FILES = (
    (".watcher.log", "log"),
    (".caffeinate.log", "log"), (".prompt.md", "prompt"),
    (".prompt", "prompt"), (".tail", "tail"), (".log", "log"),
)
_RECORDED_PATH_FIELDS = (
    ("stdout_path", "tail"), ("stderr_path", "log"), ("tail_path", "tail"),
    ("prompt_path", "prompt"),
    ("watcher_log", "log"), ("log_path", "log"),
)
_darwin_priority_warning_logged = False

def default_homes_dir() -> Path:
    return Path(os.environ.get("GOALFLIGHT_CODEX_STATE_DIR", "~/.goal-flight")).expanduser().resolve(strict=False) / "dispatch-homes"


def periodic_dispatch_gc_enabled() -> bool:
    import goalflight_agent_limits

    return goalflight_agent_limits.load_local_overrides().get("dispatch_gc_enabled") is True


def _legacy_claude_scratch_dir() -> Path:
    return Path("/private/tmp") / f"claude-{os.getuid()}"


def allocated_tree_bytes(
    root: Path, *, walked_files: list[int] | None = None,
    newest_mtime: list[float] | None = None,
    max_entries: int | None = None, timeout_seconds: float | None = None,
) -> int:
    """Count filesystem allocation without following symlinks.

    An entry below ``root`` that disappears during the walk holds no allocation,
    so it is skipped; live workers churn temp files in their scratch dirs. A
    missing ``root`` and every other error still raise; the caller then keeps
    that one artifact and leaves it out of the budget total. When supplied,
    ``newest_mtime`` is updated with the newest lstat mtime seen in the same walk.
    """
    total, stack = 0, [root]
    scheduled_entries = 1
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            raise _TreeWalkLimitExceeded("walk_timeout")
        path = stack.pop()
        try:
            stat_result = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            if path is root:
                raise
            continue
        if deadline is not None and time.monotonic() >= deadline:
            raise _TreeWalkLimitExceeded("walk_timeout")
        if newest_mtime is not None:
            if newest_mtime:
                newest_mtime[0] = max(newest_mtime[0], stat_result.st_mtime)
            else:
                newest_mtime.append(stat_result.st_mtime)
        if walked_files is not None:
            if not stat_module.S_ISDIR(stat_result.st_mode):
                walked_files[0] += 1
        total += stat_result.st_blocks * 512 if hasattr(stat_result, "st_blocks") else stat_result.st_size
        if stat_module.S_ISDIR(stat_result.st_mode) and not stat_module.S_ISLNK(stat_result.st_mode):
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise _TreeWalkLimitExceeded("walk_timeout")
                        if max_entries is not None and scheduled_entries >= max_entries:
                            raise _TreeWalkLimitExceeded("walk_entry_limit")
                        stack.append(Path(entry.path))
                        scheduled_entries += 1
            except (FileNotFoundError, NotADirectoryError):
                if path is root:
                    raise
    return total

def _parse_ended_at(record: dict[str, Any]) -> dt.datetime | None:
    try:
        value = goalflight_ledger.parse_utc(record.get("ended_at"))
        return None if value is None else value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None

def _records_from_directory(
    ledger_dir: Path, *, dispatch_ids: set[str] | None = None,
    record_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    records = []
    try:
        if dispatch_ids is None and record_names is None:
            paths = ledger_dir.glob("*.json")
        else:
            names = set(record_names or ())
            if dispatch_ids is not None:
                names.update(f"{goalflight_compat.safe_dispatch_filename(item)}.json" for item in dispatch_ids)
            paths = (ledger_dir / name for name in names)
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(record, dict)
                and isinstance(record.get("dispatch_id"), str)
                and path.name == f"{goalflight_compat.safe_dispatch_filename(record['dispatch_id'])}.json"
            ):
                records.append(record)
    except OSError:
        pass
    return records

def _legacy_owner_matches(
    name: str, by_id: dict[str, dict[str, Any]], unknown_record_names: set[str],
) -> tuple[list[str], bool]:
    matches = [
        dispatch_id for dispatch_id in by_id
        if name == dispatch_id or name.endswith("-" + dispatch_id)
    ]
    transformed_names = {record_name[:-5] for record_name in unknown_record_names}
    name_suffixes = {name, *(name[index + 1:] for index, char in enumerate(name) if char == "-")}
    unknown = any(
        goalflight_compat.safe_dispatch_filename(suffix) in transformed_names
        for suffix in name_suffixes
    )
    return matches, unknown

def _unknown_record_names(record_names: set[str], records: list[dict[str, Any]]) -> set[str]:
    readable_names = {
        f"{goalflight_compat.safe_dispatch_filename(record['dispatch_id'])}.json"
        for record in records
    }
    return {name for name in record_names if name.endswith(".json")} - readable_names

def _identity_inactive(record: dict[str, Any], probe: IdentityProbe) -> tuple[bool, str]:
    worker_record = dict(record)
    # A live controller or claimant does not prove that this dispatch's worker
    # is alive. identity_matches has generic fallbacks for those identities;
    # retention must compare only the recorded worker PID and start token.
    worker_record.update(
        claimant_pid=None,
        claimant_identity=None,
        controller_pid=None,
        controller_identity=None,
    )
    try:
        live, reason = probe(worker_record)
    except Exception:
        return False, "liveness_check_error"
    reason = str(reason).strip().lower()
    if reason in {"no_pid", "identity_indeterminate"}:
        return False, "liveness_indeterminate"
    if not live and (reason in _DEAD_REASONS or reason.startswith("pid_reused_")):
        return True, reason
    return False, "live_worker" if live else "liveness_indeterminate"


def _identity_snapshot_probe(
    records: list[dict[str, Any]],
    probe: IdentityProbe,
    *,
    deadline: float | None = None,
) -> IdentityProbe:
    if probe is not goalflight_ledger.identity_matches:
        return probe
    pids = {
        int(pid)
        for record in records
        if (pid := (
            record.get("worker_pid")
            or record.get("claimant_pid")
            or record.get("controller_pid")
        ))
        and str(pid).isdigit()
    }
    identities = goalflight_ledger._process_identity_snapshot(
        sorted(pids), deadline=deadline,
    )

    def probe_snapshot(record: dict[str, Any]) -> tuple[bool, str]:
        pid = (
            record.get("worker_pid")
            or record.get("claimant_pid")
            or record.get("controller_pid")
        )
        if not pid:
            return probe(record)
        try:
            current = identities[int(pid)]
        except (KeyError, TypeError, ValueError):
            if deadline is not None:
                return False, "identity_indeterminate"
            return probe(record)
        return goalflight_ledger.identity_matches(
            record, current_identity=current,
        )

    return probe_snapshot


def _sweep_identity_snapshot_probe(
    records: list[dict[str, Any]], probe: IdentityProbe,
    *, deadline: float | None = None,
) -> IdentityProbe:
    return _identity_snapshot_probe(
        records, probe,
        deadline=(
            deadline if deadline is not None
            else time.monotonic() + _SWEEP_IDENTITY_TIMEOUT_SECONDS
        ),
    )

def _evaluate_record(
    record: dict[str, Any], *, now: dt.datetime, probe: IdentityProbe,
    active_resume_child: bool = False, probe_nonterminal: bool = True,
) -> dict[str, Any]:
    dispatch_id, state = str(record.get("dispatch_id") or ""), record.get("state")
    result: dict[str, Any] = {"dispatch_id": dispatch_id, "state": state}
    if (
        not goalflight_dispatch_states.is_terminal_state(state)
        and not probe_nonterminal
    ):
        return {
            **result, "liveness": "liveness_not_checked",
            "eligible": False, "reason": "non_terminal",
        }
    inactive, reason = _identity_inactive(record, probe)
    result["liveness"] = reason
    if not goalflight_dispatch_states.is_terminal_state(state):
        return {**result, "eligible": False, "reason": "non_terminal"}
    if active_resume_child:
        return {**result, "eligible": False, "reason": "active_resume_child"}
    if not inactive:
        return {**result, "eligible": False, "reason": reason}
    ended_at = _parse_ended_at(record)
    if ended_at is None:
        return {**result, "eligible": False, "reason": "missing_or_invalid_ended_at"}
    age = now - ended_at
    if age < DISPATCH_ARTIFACT_MINIMUM_AGE:
        return {**result, "eligible": False, "reason": "inside_minimum_age", "age_seconds": age.total_seconds(), "ended_at": ended_at}
    return {**result, "eligible": True, "reason": "terminal_past_minimum_age", "age_seconds": age.total_seconds(), "ended_at": ended_at}

def _relationships(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    by_id = {str(row["dispatch_id"]): row for row in records if isinstance(row.get("dispatch_id"), str)}
    children: dict[str, set[str]] = {}
    for child_id, record in by_id.items():
        for parent in (record.get("parent_dispatch_id"), record.get("codex_home_owner_dispatch_id")):
            if isinstance(parent, str) and parent and parent != child_id:
                children.setdefault(parent, set()).add(child_id)
    return by_id, children

def _complete_record_inventory(
    ledger_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]]] | None:
    try:
        record_names = {name for name in os.listdir(ledger_dir) if name.endswith(".json")}
    except OSError:
        return None
    records = _records_from_directory(ledger_dir, record_names=record_names)
    if not _records_complete_for_names(records, record_names):
        return None
    by_id, children = _relationships(records)
    if len(by_id) != len(records):
        return None
    return records, by_id, children

def _records_complete_for_names(
    records: list[dict[str, Any]], names: set[str],
) -> bool:
    loaded = {
        f"{goalflight_compat.safe_dispatch_filename(str(record.get('dispatch_id') or ''))}.json"
        for record in records
    }
    return loaded == names

def _merge_records(
    base: list[dict[str, Any]], updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {str(record["dispatch_id"]): record for record in base}
    merged.update({str(record["dispatch_id"]): record for record in updates})
    return list(merged.values())

def _nonterminal_codex_home_ids(records: list[dict[str, Any]]) -> set[str]:
    used: set[str] = set()
    for record in records:
        if goalflight_ledger.terminal_state_for(
            record.get("state"), record.get("reason"),
        ) != "unknown":
            continue
        owner = record.get("codex_home_owner_dispatch_id")
        if isinstance(owner, str) and owner:
            used.add(owner)
        raw_home = record.get("codex_home")
        if isinstance(raw_home, str) and raw_home:
            used.add(Path(raw_home).expanduser().name)
    return used

def _has_nonterminal_dispatch_reference(
    records: list[dict[str, Any]], dispatch_id: str,
) -> bool:
    fields = ("dispatch_id", "parent_dispatch_id", "codex_home_owner_dispatch_id")
    return any(
        goalflight_ledger.terminal_state_for(
            record.get("state"), record.get("reason"),
        ) == "unknown"
        and any(record.get(field) == dispatch_id for field in fields)
        for record in records
    )

def _lineage_ids(
    seeds: set[str], by_id: dict[str, dict[str, Any]], children: dict[str, set[str]],
) -> set[str] | None:
    lineage: set[str] = set()
    pending = list(seeds)
    while pending:
        dispatch_id = pending.pop()
        if dispatch_id in lineage:
            continue
        record = by_id.get(dispatch_id)
        if record is None:
            return None
        lineage.add(dispatch_id)
        pending.extend(children.get(dispatch_id, ()))
        pending.extend(
            parent for parent in (record.get("parent_dispatch_id"), record.get("codex_home_owner_dispatch_id"))
            if isinstance(parent, str) and parent and parent != dispatch_id
        )
    return lineage

def _session_artifact(
    record: dict[str, Any], by_id: dict[str, dict[str, Any]], *,
    homes_dir: Path, dispatch_dir: Path, rollout_indexes: dict[Path, dict[str, Path]],
) -> tuple[Path | None, Path | None, bool]:
    import goalflight_codex_sessions, goalflight_engine_sessions, goalflight_dispatch
    dispatch_id = str(record.get("dispatch_id") or "")
    engine = goalflight_engine_sessions.resume_engine(record.get("engine") or record.get("agent"))
    lock, lock_required = goalflight_dispatch._resume_lock_path_info(record, dispatch_id)
    if engine == "codex":
        owner = str(record.get("codex_home_owner_dispatch_id") or dispatch_id)
        owner_record = by_id.get(owner, {})
        account = record.get("effective_account") or record.get("account") or owner_record.get("effective_account") or owner_record.get("account")
        account_home = goalflight_codex_sessions.canonical_account_home(account) if isinstance(account, str) and account else None
        try:
            session = goalflight_dispatch._recorded_codex_session_id(record, dispatch_id)
        except Exception:
            return None, None, True
        rollout_index = rollout_indexes.get(account_home, {}) if account_home is not None else {}
        if session is None and account_home is not None:
            tail = _dispatch_path(record.get("stdout_path") or record.get("tail_path"), dispatch_dir)
            session = goalflight_codex_sessions.session_id_from_tail(account_home, tail or dispatch_dir / f"{dispatch_id}.tail", rollout_index=rollout_index)
        if session is None or account_home is None:
            return None, lock, lock_required
        path = rollout_index.get(session)
        if path is None or path.is_symlink() or not path.is_file():
            return None, lock, lock_required
        try:
            if not path.resolve(strict=True).is_relative_to((account_home / "sessions").resolve(strict=True)):
                return None, lock, lock_required
        except OSError:
            return None, lock, lock_required
        if lock is None and not lock_required:
            home = record.get("codex_home") or owner_record.get("codex_home") or homes_dir / owner
            lock = goalflight_dispatch_paths.codex_resume_lock_path(Path(home).expanduser(), session)
            lock_required = True
        return path, lock, lock_required
    if engine == "grok":
        account = record.get("effective_account") or record.get("account")
        cwd = record.get("worker_cwd")
        try:
            session = goalflight_dispatch._recorded_engine_session_id(record, dispatch_id)
        except Exception:
            return None, None, True
        if not all(isinstance(value, str) and value for value in (account, cwd, session)) or Path(account).parts != (account,):
            return None, lock, lock_required
        path = goalflight_dispatch._seat_session_dir(account, engine, cwd, session)
        root = Path.home() / ".goal-flight" / "accounts" / account / engine / ".grok" / "sessions"
        if path is None or path.is_symlink():
            return None, lock, lock_required
        try:
            if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
                return None, lock, lock_required
        except OSError:
            return None, lock, lock_required
        if not path.is_dir() or any((path / name).is_symlink() or not (path / name).is_file() for name in ("summary.json", "chat_history.jsonl")):
            return None, lock, lock_required
        return path, lock, lock_required
    return None, lock, lock_required

def _add_artifact(
    artifacts: dict[str, dict[str, Any]], path: Path, kind: str, refs: set[str],
    *, walked_files: list[int] | None = None,
) -> None:
    key = os.path.abspath(path)
    item = artifacts.get(key)
    if item is None:
        newest_mtime = [float("-inf")] if kind == "scratch" else None
        walk_error = None
        try:
            if newest_mtime is None:
                size = allocated_tree_bytes(path, walked_files=walked_files)
            else:
                size = allocated_tree_bytes(
                    path, walked_files=walked_files, newest_mtime=newest_mtime,
                    max_entries=DISPATCH_SCRATCH_WALK_MAX_ENTRIES,
                    timeout_seconds=DISPATCH_SCRATCH_WALK_TIMEOUT_SECONDS,
                )
            deletable = not path.is_symlink() and (path.is_file() or path.is_dir())
        except _TreeWalkLimitExceeded as exc:
            size, deletable, walk_error = None, False, exc.reason
        except OSError:
            size, deletable = None, False
        item = artifacts[key] = {
            "path": path, "kind": kind, "refs": set(), "bytes": size,
            "deletable": deletable, "budgeted": kind != "legacy_scratch",
        }
        if newest_mtime is not None and newest_mtime[0] != float("-inf"):
            item["newest_mtime"] = newest_mtime[0]
        if walk_error is not None:
            item["walk_error"] = walk_error
    item["refs"].update(refs)

def _dispatch_path(raw: object, dispatch_dir: Path) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve(strict=True).is_relative_to(dispatch_dir.resolve(strict=False)):
            return None
    except OSError:
        return None
    return path

def _scratch_process_group_alive(record: dict[str, Any]) -> bool | None:
    """Return true for a proven producer, false for an inactive set, else unknown.

    A bare PGID can be reused after its recorded worker exits. Match every
    recorded producer by the dispatcher's PID/start-token rule, and enumerate
    a group only when the record has the dispatcher's complete group contract.
    """
    import goalflight_dispatch

    raw_pgid = record.get("worker_pgid")
    if raw_pgid in (None, ""):
        return False
    try:
        pgid = int(raw_pgid)
    except (TypeError, ValueError):
        return None
    if pgid <= 0:
        return None

    probes = [(record.get("worker_pid"), record.get("worker_identity"))]
    descendants = record.get("producer_descendants")
    if descendants is not None:
        if not isinstance(descendants, list):
            return None
        for descendant in descendants:
            if not isinstance(descendant, dict):
                return None
            probes.append((descendant.get("pid"), descendant.get("identity")))

    for pid, identity in probes:
        if pid in (None, ""):
            continue
        status, _reason = goalflight_dispatch._queue_claim_identity_status(pid, identity)
        if status == "live":
            return True
        if status == "indeterminate":
            return None

    group_leader_identity = record.get("worker_group_leader_identity")
    if not isinstance(group_leader_identity, dict):
        worker_identity = record.get("worker_identity")
        if isinstance(worker_identity, dict) and str(worker_identity.get("pid")) == str(pgid):
            group_leader_identity = worker_identity
    if (
        not isinstance(group_leader_identity, dict)
        or not group_leader_identity.get("start_token")
        or str(group_leader_identity.get("pid")) != str(pgid)
    ):
        return None
    leader_status, _leader_reason = goalflight_dispatch._queue_claim_identity_status(
        pgid, group_leader_identity,
    )
    if leader_status == "live":
        return True
    if leader_status == "indeterminate":
        return None

    if goalflight_dispatch._producer_set_contract_enumerable(record):
        producer_set = goalflight_dispatch.enumerate_token_producers(
            goalflight_dispatch._entry_with_record_identity(
                {}, record, prefer_record=True,
            )
        )
        if producer_set.state is goalflight_dispatch.ProducerSetState.LIVE:
            # A surviving group with a dead or reused leader may be the old
            # producer set or a new group that reused the PGID. Keep it, but
            # do not report it live without an identity-matched producer.
            return None
        if producer_set.state is goalflight_dispatch.ProducerSetState.INDETERMINATE:
            return None
        return False

    # The dispatcher treats a missing or partial group contract as absent once
    # all recorded PID identities are dead; the start-token-matched leader
    # above prevents a bare, reused PGID from counting as that group.
    return False

def _scan_dispatch_artifacts(
    by_id: dict[str, dict[str, Any]], *, homes_dir: Path, dispatch_dir: Path,
    pidfile_dir: Path, walked_files: list[int] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[Path | None, bool]]]:
    import goalflight_codex_sessions, goalflight_engine_sessions
    artifacts: dict[str, dict[str, Any]] = {}
    locks: dict[str, tuple[Path | None, bool]] = {}
    rollout_indexes: dict[Path, dict[str, Path]] = {}
    for record in by_id.values():
        engine = goalflight_engine_sessions.resume_engine(record.get("engine") or record.get("agent"))
        if engine != "codex":
            continue
        owner_record = by_id.get(str(record.get("codex_home_owner_dispatch_id") or record.get("dispatch_id")), {})
        account = record.get("effective_account") or record.get("account") or owner_record.get("effective_account") or owner_record.get("account")
        account_home = goalflight_codex_sessions.canonical_account_home(account)
        if account_home is None or account_home in rollout_indexes:
            continue
        rollout_indexes[account_home] = goalflight_codex_sessions.rollout_index(account_home)
    if homes_dir.is_dir() and not homes_dir.is_symlink():
        for path in homes_dir.iterdir():
            _add_artifact(artifacts, path, "home", {path.name}, walked_files=walked_files)
    if dispatch_dir.is_dir() and not dispatch_dir.is_symlink():
        for path in dispatch_dir.iterdir():
            if path.name.endswith(".scratch"):
                scratch_name = path.name[:-len(".scratch")]
                owners = {
                    dispatch_id for dispatch_id in by_id
                    if goalflight_compat.safe_dispatch_filename(dispatch_id) == scratch_name
                }
                _add_artifact(
                    artifacts, path, "scratch", owners, walked_files=walked_files,
                )
                continue
            matched = next(((suffix, kind) for suffix, kind in _DISPATCH_FILES if path.name.endswith(suffix)), None)
            if matched:
                suffix, kind = matched
                _add_artifact(artifacts, path, kind, {path.name[:-len(suffix)]}, walked_files=walked_files)
    for dispatch_id, record in by_id.items():
        for field, kind in _RECORDED_PATH_FIELDS:
            path = _dispatch_path(record.get(field), dispatch_dir)
            if path is not None:
                _add_artifact(artifacts, path, kind, {dispatch_id}, walked_files=walked_files)
        session_path, lock, lock_required = _session_artifact(
            record, by_id, homes_dir=homes_dir, dispatch_dir=dispatch_dir,
            rollout_indexes=rollout_indexes,
        )
        locks[dispatch_id] = (lock, lock_required)
        if session_path is not None:
            _add_artifact(artifacts, session_path, "grok_session" if session_path.is_dir() else "rollout", {dispatch_id}, walked_files=walked_files)
    if pidfile_dir.is_dir() and not pidfile_dir.is_symlink():
        for path in pidfile_dir.glob("*.bashtail.*.jsonl"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            owner = payload.get("dispatch_id") or payload.get("session_id") if isinstance(payload, dict) else None
            _add_artifact(artifacts, path, "pidfile", {owner} if isinstance(owner, str) and owner else set(), walked_files=walked_files)
    return artifacts, locks

def _lock_users(ids: set[str], lock_index: dict[str, tuple[Path | None, bool]], held: set[str] | None = None) -> tuple[list[int], set[str]] | None:
    if fcntl is None:
        return None
    held_paths = set(held or ())
    fds: list[int] = []
    try:
        paths = set()
        for dispatch_id in ids:
            path, required = lock_index.get(dispatch_id, (None, False))
            if required and path is None:
                raise OSError("resume lock path is unknown")
            if path is not None and str(path) not in held_paths:
                paths.add(path)
        for path in sorted(paths, key=str):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
            fds.append(fd)
            held_paths.add(str(path))
        return fds, held_paths
    except OSError:
        for fd in fds:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        return None

def _release_locks(fds: list[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

def _evaluate_records(
    records: list[dict[str, Any]], *, now: dt.datetime, probe: IdentityProbe,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]]]:
    by_id, children = _relationships(records)
    evaluations = {
        key: _evaluate_record(
            row, now=now, probe=probe,
            probe_nonterminal=any(
                isinstance(parent, str) and parent and parent != key
                for parent in (
                    row.get("parent_dispatch_id"),
                    row.get("codex_home_owner_dispatch_id"),
                )
            ),
        )
        for key, row in by_id.items()
    }
    active_live = set()
    active_unknown = set()
    for row in records:
        child_id = str(row.get("dispatch_id") or "")
        evaluation = evaluations.get(child_id, {})
        liveness = evaluation.get("liveness")
        parents = {
            parent for parent in (row.get("parent_dispatch_id"), row.get("codex_home_owner_dispatch_id"))
            if isinstance(parent, str) and parent and parent != child_id
        }
        terminal_without_worker = (
            goalflight_dispatch_states.is_terminal_state(row.get("state"))
            and goalflight_ledger.prelaunch_without_worker_evidence(row)
        )
        if liveness == "live_worker":
            active_live.update(parents)
        elif (
            liveness not in _DEAD_REASONS
            and not str(liveness or "").startswith("pid_reused_")
            and not terminal_without_worker
        ):
            active_unknown.update(parents)
    for parent in active_unknown - active_live:
        if parent in evaluations:
            evaluations[parent] = {
                **evaluations[parent], "eligible": False,
                "reason": "resume_child_liveness_indeterminate",
            }
    for parent in active_live:
        if parent in evaluations:
            evaluations[parent] = {**evaluations[parent], "eligible": False, "reason": "active_resume_child"}
    return by_id, evaluations, children

def _minimum_age_for_artifact(kind: str) -> dt.timedelta:
    return DISPATCH_SCRATCH_MINIMUM_AGE if kind == "scratch" else DISPATCH_ARTIFACT_MINIMUM_AGE

def _candidate_record_is_eligible(
    evaluation: dict[str, Any], minimum_age: dt.timedelta,
) -> bool:
    if evaluation.get("eligible"):
        return True
    return (
        evaluation.get("reason") == "inside_minimum_age"
        and evaluation.get("age_seconds", -1) >= minimum_age.total_seconds()
    )

def _candidate(
    refs: set[str], by_id: dict[str, dict[str, Any]], evaluations: dict[str, dict[str, Any]],
    children: dict[str, set[str]], *, minimum_age: dt.timedelta = DISPATCH_ARTIFACT_MINIMUM_AGE,
) -> tuple[set[str], dt.datetime] | None:
    ids = _lineage_ids(refs, by_id, children)
    if not ids or any(
        not _candidate_record_is_eligible(evaluations.get(item, {}), minimum_age)
        for item in ids
    ):
        return None
    return ids, max(evaluations[item]["ended_at"] for item in ids)


def _record_keep_refs(record: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    pending: list[Any] = [record]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if "keep_ref" in str(key).lower():
                    if isinstance(child, str) and child.strip():
                        refs.add(child.strip())
                    elif isinstance(child, (dict, list, tuple)):
                        pending.append(child)
                elif isinstance(child, (dict, list, tuple)):
                    pending.append(child)
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return refs


def _dispatch_gc_pins(
    dispatch_dir: Path, records: list[dict[str, Any]],
    *, deadline: float | None = None,
) -> tuple[set[str] | None, list[str], set[str]]:
    path = dispatch_dir / DISPATCH_GC_PIN_FILE
    errors: list[str] = []
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pins: set[str] = set()
    except OSError:
        return None, [f"pin file unreadable: {path}"], set()
    else:
        if not stat_module.S_ISREG(mode):
            return None, [f"pin file is not regular: {path}"], set()
        try:
            pins = {
                value
                for line in path.read_text(encoding="utf-8").splitlines()
                if (value := line.split("#", 1)[0].strip())
            }
        except (OSError, UnicodeDecodeError):
            return None, [f"pin file unreadable: {path}"], set()

    by_id, children = _relationships(records)
    records_by_root: dict[Path, set[str]] = {}
    root_error_dispatch_ids: set[str] = set()
    roots = _project_roots(
        records, errors, records_by_root=records_by_root,
        root_error_dispatch_ids=root_error_dispatch_ids,
        deadline=deadline,
    )
    unknown_dispatch_ids = set(root_error_dispatch_ids)
    root_by_dispatch: dict[str, set[Path]] = {}
    for root, dispatch_ids in records_by_root.items():
        for dispatch_id in dispatch_ids:
            root_by_dispatch.setdefault(dispatch_id, set()).add(root)
    for record in records:
        dispatch_id = record.get("dispatch_id")
        raw_root = record.get("project_root")
        if isinstance(dispatch_id, str) and (
            not isinstance(raw_root, str) or not raw_root.strip()
        ):
            unknown_dispatch_ids.add(dispatch_id)
    if not roots and any(
        _record_keep_refs(record)
        or record.get("worktree_head")
        or record.get("worktree_base")
        for record in records
    ):
        errors.append("recorded keep refs or heads have no readable project repository")
    refs_by_root: dict[Path, dict[str, set[str]]] = {}
    pinned_heads_by_root: dict[Path, set[str]] = {}
    git_env = os.environ.copy()
    git_env["GIT_OPTIONAL_LOCKS"] = "0"
    roots_sorted = sorted(roots, key=os.fspath)
    for root_index, root in enumerate(roots_sorted):
        if deadline is not None and time.monotonic() >= deadline:
            for remaining_root in roots_sorted[root_index:]:
                unknown_dispatch_ids.update(records_by_root.get(remaining_root, set()))
            break
        try:
            proc = goalflight_subprocess.run_with_deadline(
                [
                    "git", "-C", str(root), "-c", "core.fsmonitor=false",
                    "for-each-ref", "--format=%(refname)%09%(objectname)",
                    "refs/keep/", "refs/goalflight/keep/",
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=git_env, check=False, timeout=_PROJECT_ROOT_GIT_TIMEOUT_SECONDS,
                deadline=deadline,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{root}: keep refs unreadable: {type(exc).__name__}: {exc}")
            unknown_dispatch_ids.update(records_by_root.get(root, set()))
            continue
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"git exited {proc.returncode}"
            errors.append(f"{root}: keep refs unreadable: {detail}")
            unknown_dispatch_ids.update(records_by_root.get(root, set()))
            continue
        refs_by_name: dict[str, set[str]] = {}
        pinned_heads: set[str] = set()
        for line in proc.stdout.splitlines():
            ref, separator, head = line.partition("\t")
            if not separator or not ref.startswith(("refs/keep/", "refs/goalflight/keep/")):
                errors.append(f"{root}: malformed keep ref listing")
                unknown_dispatch_ids.update(records_by_root.get(root, set()))
                break
            refs_by_name.setdefault(ref, set()).add(head.strip())
            pinned_heads.add(head.strip())
        refs_by_root[root] = refs_by_name
        pinned_heads_by_root[root] = pinned_heads

    directly_pinned = set(pins)
    for dispatch_id, record in by_id.items():
        record_roots = root_by_dispatch.get(dispatch_id, set())
        if len(record_roots) != 1:
            unknown_dispatch_ids.add(dispatch_id)
            continue
        root = next(iter(record_roots))
        refs_by_name = refs_by_root.get(root)
        pinned_heads = pinned_heads_by_root.get(root)
        if refs_by_name is None or pinned_heads is None:
            unknown_dispatch_ids.add(dispatch_id)
            continue
        # Keep refs are repository-local: a different project's refs cannot
        # preserve this record's worktree head or recorded ref in its own repo.
        # Therefore an unreadable root only makes records rooted there unknown.
        if _record_keep_refs(record) & refs_by_name.keys():
            directly_pinned.add(dispatch_id)
            continue
        heads = {
            str(record.get(field)).strip()
            for field in ("worktree_head", "worktree_base")
            if isinstance(record.get(field), str) and record.get(field)
        }
        if heads & pinned_heads:
            directly_pinned.add(dispatch_id)

    for dispatch_id in tuple(directly_pinned):
        if dispatch_id not in by_id:
            continue
        lineage = _lineage_ids({dispatch_id}, by_id, children)
        if lineage is None:
            # The pinned row cannot be safely expanded through a missing
            # ledger record, but that uncertainty is confined to its lineage.
            pins.add(dispatch_id)
            unknown_dispatch_ids.add(dispatch_id)
            errors.append(f"pinned dispatch lineage is incomplete: {dispatch_id}")
            continue
        pins.update(lineage)
    return pins, errors, unknown_dispatch_ids


def _dispatch_gc_path_safe(path: Path, dispatch_dir: Path) -> tuple[bool, str]:
    parent_fd = None
    try:
        parent_fd, name = _open_dispatch_parent(path, dispatch_dir)
        root_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_module.S_ISLNK(root_stat.st_mode):
            return False, "symlink_refused"
        if stat_module.S_ISDIR(root_stat.st_mode):
            child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (root_stat.st_dev, root_stat.st_ino):
                    return False, "path_changed"
                return _directory_tree_safe(child_fd)
            finally:
                os.close(child_fd)
        return True, ""
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"path_unreadable:{type(exc).__name__}"
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _directory_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("O_NOFOLLOW is unavailable")
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow


def _open_directory_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    flags = _directory_open_flags()
    parts = absolute.parts
    fd = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            child_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_dispatch_parent(path: Path, dispatch_dir: Path) -> tuple[int, str]:
    root = Path(os.path.abspath(dispatch_dir))
    absolute = Path(os.path.abspath(path))
    relative = absolute.relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("artifact path is not below dispatch root")
    if stat_module.S_ISLNK(root.lstat().st_mode):
        raise OSError("dispatch root is a symlink")
    fd = _open_directory_nofollow(root)
    try:
        for part in relative.parts[:-1]:
            child_fd = os.open(part, _directory_open_flags(), dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd, relative.parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _directory_tree_safe(directory_fd: int) -> tuple[bool, str]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        return False, f"path_unreadable:{type(exc).__name__}"
    for name in names:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat_module.S_ISLNK(before.st_mode):
                return False, "symlink_refused"
            if not stat_module.S_ISDIR(before.st_mode):
                continue
            child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return False, "path_changed"
                safe, reason = _directory_tree_safe(child_fd)
                if not safe:
                    return safe, reason
            finally:
                os.close(child_fd)
        except OSError as exc:
            return False, f"path_unreadable:{type(exc).__name__}"
    return True, ""


def _dispatch_gc_decision(
    path: Path,
    refs: set[str],
    *,
    kind: str,
    by_id: dict[str, dict[str, Any]],
    evaluations: dict[str, dict[str, Any]],
    children: dict[str, set[str]],
    pins: set[str] | None,
    unknown_dispatch_ids: set[str],
    dispatch_dir: Path,
    now: dt.datetime,
    minimum_age: dt.timedelta,
    resumability_cache: dict[str, bool | None],
    identity_probe: IdentityProbe,
    resume_context: dict | None = None,
    scratch_expire_resumable_days: int | None = None,
) -> tuple[str, str, tuple[set[str], dt.datetime] | None, str | None]:
    if pins is None:
        return "skipped_unknown", "pin_file_unreadable", None, None
    ids = _lineage_ids(refs, by_id, children)
    if not ids:
        return "skipped_unknown", "missing_lineage", None, None
    if ids & unknown_dispatch_ids:
        return "skipped_unknown", "project_root_unreadable", None, None
    lineage_records: dict[str, dict[str, Any]] = {}
    for dispatch_id in ids:
        record = by_id.get(dispatch_id)
        if record is None:
            return "skipped_unknown", "missing_record", None, None
        state = record.get("state")
        evaluation = evaluations.get(dispatch_id, {})
        if evaluation.get("reason") == "active_resume_child":
            return "skipped_live", "active_resume_child", None, None
        if evaluation.get("reason") == "resume_child_liveness_indeterminate":
            return "skipped_unknown", "resume_child_liveness_indeterminate", None, None
        liveness = str(evaluation.get("liveness") or "")
        if not goalflight_dispatch_states.is_terminal_state(state):
            if isinstance(state, str) and state in _KNOWN_NONTERMINAL_STATES:
                if not record.get("worker_pid"):
                    return "skipped_unknown", "orphaned_non_terminal", None, None
                if liveness == "liveness_not_checked":
                    _inactive, liveness = _identity_inactive(record, identity_probe)
                if liveness == "live_worker":
                    return "skipped_live", "live_worker", None, None
                if liveness in _DEAD_REASONS or liveness.startswith("pid_reused_"):
                    return "skipped_unknown", "orphaned_non_terminal", None, None
                return "skipped_unknown", liveness or "liveness_indeterminate", None, None
            return "skipped_unknown", "missing_or_unknown_state", None, None
        if (
            liveness == "liveness_indeterminate"
            and goalflight_ledger.prelaunch_without_worker_evidence(record)
        ):
            # Reuse the dispatcher's proof that no spawn was attempted. A
            # terminal row without that proof may have an unpublished worker.
            liveness = "dead"
        if liveness == "live_worker":
            return "skipped_live", liveness, None, None
        if liveness not in _DEAD_REASONS and not liveness.startswith("pid_reused_"):
            return "skipped_unknown", liveness or "liveness_indeterminate", None, None
        lineage_records[dispatch_id] = record

    resumability = {
        dispatch_id: _dispatch_gc_is_resumable(
            record, resumability_cache,
            resume_context=resume_context,
        )
        for dispatch_id, record in lineage_records.items()
    }
    if any(value is None for value in resumability.values()):
        return "skipped_unknown", "resumability_indeterminate", None, None
    expire_resumable_scratch = (
        kind == "scratch"
        and scratch_expire_resumable_days is not None
        and any(
            resumability.get(dispatch_id) is True
            for dispatch_id in refs
        )
    )
    age_threshold = (
        dt.timedelta(days=scratch_expire_resumable_days)
        if expire_resumable_scratch else minimum_age
    )
    terminal_times: list[dt.datetime] = []
    age_source = "ended_at"
    for dispatch_id in ids:
        record = lineage_records[dispatch_id]
        is_resumable = resumability.get(dispatch_id)
        if is_resumable is None:
            return "skipped_unknown", "resumability_indeterminate", None, None
        if is_resumable and not expire_resumable_scratch:
            return "skipped_resumable", "resumable", None, None
        raw_ended_at = record.get("ended_at")
        if raw_ended_at in (None, ""):
            if expire_resumable_scratch:
                return "skipped_unknown", "resumable_age_unknown", None, None
            try:
                terminal_at = dt.datetime.fromtimestamp(
                    path.lstat().st_mtime, tz=dt.timezone.utc,
                )
            except OSError as exc:
                return "skipped_unknown", f"mtime_unreadable:{type(exc).__name__}", None, None
            age_source = "mtime"
        else:
            terminal_at = _parse_ended_at(record)
            if terminal_at is None:
                return "skipped_unknown", "invalid_ended_at", None, None
        terminal_times.append(terminal_at)
        if now - terminal_at < age_threshold:
            return "skipped_recent", "inside_retention_window", None, age_source
        process_group_live = _scratch_process_group_alive(record)
        if process_group_live is True:
            return "skipped_live", "worker_process_group_alive", None, age_source
        if process_group_live is None:
            return "skipped_unknown", "process_group_liveness_indeterminate", None, age_source
    if ids & pins:
        return "skipped_pinned", "keep_ref_or_pin_file", None, age_source
    if kind == "tail" and os.path.lexists(Path(str(path) + ".gz")):
        return "skipped_unknown", "compressed_destination_exists", None, age_source
    safe, reason = _dispatch_gc_path_safe(path, dispatch_dir)
    if not safe:
        return "skipped_unsafe", reason, None, age_source
    expected_type = stat_module.S_ISDIR if kind == "scratch" else stat_module.S_ISREG
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        return "skipped_unknown", f"path_unreadable:{type(exc).__name__}", None, age_source
    if not expected_type(mode):
        return "skipped_unsafe", "unexpected_artifact_type", None, age_source
    return (
        (
            "would_remove_resumable"
            if expire_resumable_scratch else "would_remove"
        ) if kind == "scratch" else "would_compress",
        f"terminal_past_{age_threshold.days}_days",
        (ids, max(terminal_times)),
        age_source,
    )


def _dispatch_gc_is_resumable(
    record: dict[str, Any], cache: dict[str, bool | None], *,
    resume_context: dict | None = None,
) -> bool | None:
    dispatch_id = str(record.get("dispatch_id") or "")
    if dispatch_id in cache:
        return cache[dispatch_id]
    if any(
        state in goalflight_dispatch_states.SALVAGE_NEEDED_STATES
        for state in (record.get("state"), record.get("terminal_state"))
        if isinstance(state, str)
    ):
        cache[dispatch_id] = True
        return True

    try:
        import goalflight_dispatch
    except Exception:
        cache[dispatch_id] = True
        return True

    # This is the validator called by the resume command itself. Treat
    # unexpected failures as resumable because the sweep cannot prove the
    # recorded session is disposable.
    try:
        if resume_context is None:
            goalflight_dispatch._validate_resume_source(dispatch_id)
        else:
            goalflight_dispatch._validate_resume_source(
                dispatch_id, _resume_context=resume_context,
            )
    except goalflight_dispatch.DispatchUsageError as exc:
        message = str(exc).lower()
        cache[dispatch_id] = (
            None
            if "liveness is indeterminate" in message
            or "record is unreadable" in message
            or "resume refused:" in message
            else False
        )
    except Exception:
        cache[dispatch_id] = True
    else:
        cache[dispatch_id] = True
    return cache[dispatch_id]


def _compress_dispatch_tail(
    path: Path, dispatch_dir: Path,
) -> int:
    parent_fd, name = _open_dispatch_parent(path, dispatch_dir)
    source_fd = -1
    temp_fd = -1
    temp_name = name + ".gz.tmp"
    destination_name = name + ".gz"
    temp_created = False
    producer_lock_held = False
    source_unlinked = False
    destination_identity: tuple[int, int] | None = None

    def source_unchanged(expected: os.stat_result) -> bool:
        try:
            current_fd = os.fstat(source_fd)
            current_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        identity = (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns)
        return all(
            (candidate.st_dev, candidate.st_ino, candidate.st_size, candidate.st_mtime_ns)
            == identity
            for candidate in (current_fd, current_path)
        )

    try:
        source_fd = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd,
        )
        source_stat = os.fstat(source_fd)
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise OSError("tail is not a regular file")
        if fcntl is None:
            raise OSError("tail_locked: flock is unavailable")
        try:
            fcntl.flock(source_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OSError("tail_locked") from exc
        producer_lock_held = True
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temp_created = True
        source_file = os.fdopen(source_fd, "rb", closefd=False)
        raw_file = os.fdopen(temp_fd, "wb")
        temp_fd = -1
        with source_file as source, raw_file as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                shutil.copyfileobj(source, compressed, length=1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        if not source_unchanged(source_stat):
            raise OSError("tail changed during compression")
        try:
            os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(destination_name)
        os.replace(
            temp_name, destination_name,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        temp_created = False
        published = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
        destination_identity = (published.st_dev, published.st_ino)
        os.fsync(parent_fd)
        # Check again after publishing: a writer can append between the first
        # post-copy check and the archive rename. Keep the source on any change.
        if not source_unchanged(source_stat):
            raise OSError("tail changed before source unlink")
        os.unlink(name, dir_fd=parent_fd)
        source_unlinked = True
        os.fsync(parent_fd)
        return published.st_blocks * 512 if hasattr(published, "st_blocks") else published.st_size
    except BaseException:
        if temp_created:
            with contextlib.suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)
        if destination_identity is not None and not source_unlinked:
            with contextlib.suppress(OSError):
                current = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == destination_identity:
                    os.unlink(destination_name, dir_fd=parent_fd)
        raise
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if producer_lock_held:
            with contextlib.suppress(OSError):
                fcntl.flock(source_fd, fcntl.LOCK_UN)
        if source_fd >= 0:
            os.close(source_fd)
        os.close(parent_fd)


def _remove_dispatch_scratch(path: Path, dispatch_dir: Path) -> bool:
    parent_fd = None
    child_fd = None
    try:
        parent_fd, name = _open_dispatch_parent(path, dispatch_dir)
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_module.S_ISLNK(before.st_mode) or not stat_module.S_ISDIR(before.st_mode):
            return False
        child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return False
        if not _directory_tree_safe(child_fd)[0]:
            return False
        if not _remove_directory_contents(child_fd):
            return False
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_module.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
            opened.st_dev, opened.st_ino,
        ):
            return False
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    except OSError:
        return False
    finally:
        if child_fd is not None:
            os.close(child_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _remove_directory_contents(directory_fd: int) -> bool:
    try:
        names = os.listdir(directory_fd)
    except OSError:
        return False
    for name in names:
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat_module.S_ISLNK(before.st_mode):
                return False
            if stat_module.S_ISDIR(before.st_mode):
                child_fd = os.open(name, _directory_open_flags(), dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        return False
                    if not _remove_directory_contents(child_fd):
                        return False
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat_module.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
                        opened.st_dev, opened.st_ino,
                    ):
                        return False
                    os.rmdir(name, dir_fd=directory_fd)
                finally:
                    os.close(child_fd)
            else:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat_module.S_ISLNK(current.st_mode):
                    return False
                os.unlink(name, dir_fd=directory_fd)
        except OSError:
            return False
    return True


def _dispatch_gc_reclassify(
    report: dict[str, Any], old: str, new: str, size: int,
) -> None:
    classes = report["dispatch_gc"]["classes"]
    if old == new:
        return
    classes[old]["count"] -= 1
    classes[old]["bytes"] -= size
    classes[new]["count"] += 1
    classes[new]["bytes"] += size


def _dispatch_gc_reclassify_artifact(
    report: dict[str, Any], item: dict[str, Any], new: str, size: int,
) -> None:
    old = item.get(
        "dispatch_gc_class",
        "would_compress" if item["kind"] == "tail" else "would_remove",
    )
    _dispatch_gc_reclassify(report, old, new, size)
    item["dispatch_gc_class"] = new


def _count_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1


def _scan_legacy_scratch(
    root: Path, by_id: dict[str, dict[str, Any]],
    evaluations: dict[str, dict[str, Any]], children: dict[str, set[str]],
    unknown_record_names: set[str], *, walked_files: list[int] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    artifacts: dict[str, dict[str, Any]] = {}
    reasons: dict[str, int] = {}
    if root.is_symlink() or not root.is_dir():
        return artifacts, reasons
    try:
        paths = list(root.iterdir())
    except OSError:
        return artifacts, {"scan_error": 1}
    for path in paths:
        if path.name.startswith("-") and not path.is_symlink() and path.is_dir():
            _count_reason(reasons, "project_path_bucket")
            continue
        matches, unknown_owner = _legacy_owner_matches(path.name, by_id, unknown_record_names)
        if unknown_owner:
            _count_reason(reasons, "unknown_owner")
            continue
        if not matches:
            _count_reason(reasons, "unmapped")
            continue
        if len(matches) != 1:
            _count_reason(reasons, "multiple_owners")
            continue
        if path.is_symlink():
            _count_reason(reasons, "symlink")
            continue
        if not path.is_dir():
            _count_reason(reasons, "not_directory")
            continue
        dispatch_id = matches[0]
        if _candidate({dispatch_id}, by_id, evaluations, children) is None:
            related = _lineage_ids({dispatch_id}, by_id, children)
            if related is None:
                _count_reason(reasons, "missing_lineage")
                continue
            ordered = [dispatch_id, *sorted(related - {dispatch_id})]
            reason = next(
                (
                    evaluations[item].get("reason", "ineligible_owner")
                    for item in ordered
                    if not evaluations.get(item, {}).get("eligible")
                ),
                "ineligible_owner",
            )
            _count_reason(reasons, str(reason))
            continue
        _add_artifact(artifacts, path, "legacy_scratch", {dispatch_id}, walked_files=walked_files)
        key = os.path.abspath(path)
        if artifacts[key]["bytes"] is None:
            del artifacts[key]
            _count_reason(reasons, "size_scan_error")
    return artifacts, reasons


def _legacy_scratch_process_snapshot(
    paths: list[Path], *, deadline: float | None = None,
) -> dict[str, tuple[bool, str]]:
    keyed_paths = {os.path.abspath(path): path for path in paths}
    if not keyed_paths:
        return {}

    def keep_all(reason: str) -> dict[str, tuple[bool, str]]:
        return {key: (False, reason) for key in keyed_paths}

    uid = os.getuid()
    try:
        result = goalflight_subprocess.run_with_deadline(
            ["lsof", "-a", "-u", str(uid), "-Fpcfnt0"],
            capture_output=True, text=True, check=False,
            timeout=_LEGACY_SCRATCH_LSOF_TIMEOUT_SECONDS,
            deadline=deadline,
        )
    except subprocess.TimeoutExpired:
        return keep_all("lsof_timeout")
    except (OSError, subprocess.SubprocessError):
        return keep_all("lsof_error")
    if (
        result.returncode != 0
        or not goalflight_subprocess.lsof_stderr_is_mount_stat_warnings_only(result.stderr or "")
        or not result.stdout.endswith("\n")
    ):
        return keep_all("lsof_error")

    open_names: list[str] = []
    process_count = 0
    in_process = False
    # These Darwin file-descriptor handlers emit non-path names by design.
    # Type spellings are from Apple's lsof implementations:
    # github.com/apple-oss-distributions/lsof/blob/main/lsof/dialects/darwin/libproc/dproc.c
    # github.com/apple-oss-distributions/lsof/blob/main/lsof/dialects/darwin/libproc/dfile.c
    # github.com/apple-oss-distributions/lsof/blob/main/lsof/dialects/darwin/libproc/dsock.c
    # github.com/apple-oss-distributions/lsof/blob/main/lsof/dialects/darwin/libproc/dnetpolicy.c
    non_file_types = {
        "IPv4", "IPv6", "unix", "systm", "sock", "rte", "ndrv", "key",
        "ppp", "vsock", "vsockp", "PIPE", "KQUEUE", "NPOLICY", "FSEVENTS",
        "PSXSHM", "PSXSEM", "ATALK", "CHAN", "NEXUS",
    }
    # XNU classifies DTYPE_CHANNEL and DTYPE_NEXUS as Skywalk kernel
    # descriptors; PROC_PIDFDCHANNELINFO resolves DTYPE_CHANNEL to
    # struct kern_channel. See file_type_t in
    # https://www.newosxbook.com/src.php?file=%2Fbsd%2Fsys%2Ffile_internal.h&tree=xnu
    # and pid_channelinfo in
    # https://www.newosxbook.com/src.php?file=%2Fbsd%2Fkern%2Fproc_info.c&tree=xnu.
    # The public Apple lsof source has no handlers for these newer types.
    gone_diagnostics = {
        f"{prefix}: {suffix}"
        for prefix in ("kqueue", "pipe", "semaphore", "POSIX shared memory", "vnode")
        for suffix in ("FD unavailable", "process unavailable")
    }
    for record in result.stdout[:-1].split("\n"):
        if not record.endswith("\0"):
            return keep_all("lsof_error")
        fields: dict[str, str] = {}
        field_order: list[str] = []
        for raw_field in record[:-1].split("\0"):
            if not raw_field or raw_field[0] not in {"p", "c", "f", "t", "n"}:
                return keep_all("lsof_error")
            code, value = raw_field[0], raw_field[1:]
            if code in fields:
                return keep_all("lsof_error")
            fields[code] = value
            field_order.append(code)

        if record.startswith("p"):
            if field_order != ["p", "c"] or not fields["c"]:
                return keep_all("lsof_error")
            if not fields["p"].isdigit() or int(fields["p"]) <= 0:
                return keep_all("lsof_error")
            process_count += 1
            in_process = True
            continue

        if not record.startswith("f") or not in_process or "f" not in fields or "n" not in fields:
            return keep_all("lsof_error")
        if field_order not in (["f", "n"], ["f", "t", "n"]):
            return keep_all("lsof_error")
        descriptor, name = fields["f"], fields["n"]
        file_type = fields.get("t")
        if not descriptor:
            return keep_all("lsof_error")
        if name.startswith("/proc/") and name.endswith("/fd: Permission denied"):
            pid = name[len("/proc/"):-len("/fd: Permission denied")]
            if pid.isdigit():
                return keep_all("lsof_error")
        if name in gone_diagnostics:
            continue
        if os.path.isabs(name):
            open_names.append(name)
        elif file_type in non_file_types:
            continue
        else:
            return keep_all("lsof_error")

    if process_count == 0:
        return keep_all("lsof_error")

    candidate_names: dict[str, dict[str, str]] = {}
    for key, path in keyed_paths.items():
        parent = os.path.abspath(path.parent)
        canonical_parent = os.path.realpath(parent)
        candidate_names.setdefault(parent, {})[path.name] = key
        candidate_names.setdefault(canonical_parent, {})[path.name] = key

    open_candidates: set[str] = set()
    for name in open_names:
        # lsof annotates unlinked open files; the path portion still identifies
        # the candidate tree. Keep both spellings to avoid stripping a literal
        # filename suffix unnecessarily.
        spellings = (name, name.removesuffix(" (deleted)"))
        for spelling in spellings:
            normalized = os.path.normpath(spelling)
            if not os.path.isabs(normalized):
                continue
            for root, names in candidate_names.items():
                prefix = root.rstrip(os.sep) + os.sep
                if not normalized.startswith(prefix):
                    continue
                relative = normalized[len(prefix):]
                candidate_name = relative.split(os.sep, 1)[0]
                key = names.get(candidate_name)
                if key is not None:
                    open_candidates.add(key)
                break

    return {
        key: (False, "lsof_hit") if key in open_candidates else (True, "")
        for key in keyed_paths
    }


def _delete_artifact(path: Path, kind: str) -> bool:
    try:
        if path.is_symlink():
            return False
        if kind == "scratch":
            return False
        if kind in {"home", "grok_session", "legacy_scratch"}:
            if not path.is_dir():
                return False
            shutil.rmtree(path)
        else:
            if not path.is_file():
                return False
            path.unlink()
        return True
    except OSError:
        return False


def _tree_snapshot(root: Path) -> dict[str, Any]:
    """Inventory a real directory without following links."""
    root_info = root.lstat()
    if not stat_module.S_ISDIR(root_info.st_mode) or stat_module.S_ISLNK(root_info.st_mode):
        raise OSError(f"not a real directory: {root}")

    def signature(path: Path, info: os.stat_result) -> tuple[Any, ...]:
        link_target = os.readlink(path) if stat_module.S_ISLNK(info.st_mode) else None
        return (
            info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, link_target,
        )

    entries = {root.name}
    fingerprints = {root.name: signature(root, root_info)}
    newest_mtime = root_info.st_mtime
    allocated_bytes = root_info.st_blocks * 512 if hasattr(root_info, "st_blocks") else root_info.st_size
    has_symlink = False

    def onerror(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        parent = Path(directory)
        for name in [*directories, *files]:
            path = parent / name
            info = path.lstat()
            relative = path.relative_to(root.parent).as_posix()
            entries.add(relative)
            fingerprints[relative] = signature(path, info)
            newest_mtime = max(newest_mtime, info.st_mtime)
            allocated_bytes += info.st_blocks * 512 if hasattr(info, "st_blocks") else info.st_size
            if stat_module.S_ISLNK(info.st_mode):
                has_symlink = True
                if name in directories:
                    directories.remove(name)

    return {
        "entries": entries,
        "fingerprints": fingerprints,
        "newest_mtime": newest_mtime,
        "allocated_bytes": allocated_bytes,
        "has_symlink": has_symlink,
    }


def _grok_archive_timeout_seconds(allocated_bytes: int) -> float:
    return min(
        _GROK_ARCHIVE_TIMEOUT_CAP_SECONDS,
        max(
            _GROK_ARCHIVE_TIMEOUT_FLOOR_SECONDS,
            _GROK_ARCHIVE_TIMEOUT_FLOOR_SECONDS
            + allocated_bytes / _GROK_ARCHIVE_BYTES_PER_SECOND,
        ),
    )


def _home_has_rollout(home: Path) -> bool:
    sessions = home / "sessions"
    try:
        info = sessions.lstat()
    except FileNotFoundError:
        return False
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
        # An unreadable or redirected sessions path cannot prove R3 is safe.
        return True

    def onerror(error: OSError) -> None:
        raise error

    for directory, _directories, files in os.walk(
        sessions, topdown=True, followlinks=False, onerror=onerror,
    ):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                (Path(directory) / name).lstat()
                return True
    return False


def _codex_home_trim_targets(home: Path) -> tuple[list[Path], dict[Path, list[Path]]]:
    targets: dict[Path, None] = {}
    databases: dict[Path, list[Path]] = {}
    for entry in home.iterdir():
        info = entry.lstat()
        if stat_module.S_ISLNK(info.st_mode):
            continue
        if stat_module.S_ISDIR(info.st_mode) and entry.name in _CODEX_HOME_TRIM_DIRS:
            targets[entry] = None
        elif stat_module.S_ISREG(info.st_mode) and entry.name in _CODEX_HOME_TRIM_FILES:
            targets[entry] = None
        elif stat_module.S_ISREG(info.st_mode) and entry.name.endswith(".sqlite"):
            sidecars = []
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{entry}{suffix}")
                try:
                    sidecar_info = sidecar.lstat()
                except FileNotFoundError:
                    continue
                if stat_module.S_ISREG(sidecar_info.st_mode):
                    sidecars.append(sidecar)
            databases[entry] = sidecars
    return sorted(targets, key=os.fspath), databases


def _remove_trim_entry(
    path: Path, *, remove_symlinks: bool = False,
) -> tuple[int, bool, bool]:
    """Remove a trim target without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0, False, False
    except OSError:
        return 0, False, True
    if stat_module.S_ISLNK(info.st_mode):
        if not remove_symlinks:
            return 0, False, False
        try:
            path.unlink()
        except OSError:
            return 0, False, True
        return info.st_blocks * 512 if hasattr(info, "st_blocks") else info.st_size, True, False
    size = info.st_blocks * 512 if hasattr(info, "st_blocks") else info.st_size
    if stat_module.S_ISDIR(info.st_mode):
        removed_bytes = 0
        changed = False
        failed = False
        try:
            with os.scandir(path) as children:
                child_paths = [Path(child.path) for child in children]
        except OSError:
            return 0, False, True
        for child in child_paths:
            child_bytes, child_changed, child_failed = _remove_trim_entry(
                child, remove_symlinks=True,
            )
            removed_bytes += child_bytes
            changed = changed or child_changed
            failed = failed or child_failed
        try:
            path.rmdir()
        except OSError:
            failed = True
        else:
            removed_bytes += size
            changed = True
        return removed_bytes, changed, failed
    if not stat_module.S_ISREG(info.st_mode):
        return 0, False, False
    try:
        path.unlink()
    except OSError:
        return 0, False, True
    return size, True, False


def _codex_home_eligibility(
    home: Path, *, records: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
    records_complete: bool, now: dt.datetime, identity_probe: IdentityProbe,
    probe: IdentityProbe | None = None, live_home_ids: set[str] | None = None,
    evaluation: dict[str, Any] | None = None,
    open_snapshot: dict[str, tuple[bool, str]] | None = None,
    held_paths: set[str] | None = None, check_open: bool = True,
    deadline: float | None = None,
) -> dict[str, Any]:
    import goalflight_dispatch
    import goalflight_engine_sessions

    def blocked(reason: str, *, applicable: bool = True, **details: Any) -> dict[str, Any]:
        return {"eligible": False, "applicable": applicable, "reason": reason, **details}

    try:
        home_info = home.lstat()
    except OSError:
        return blocked("unreadable")
    if stat_module.S_ISLNK(home_info.st_mode) or not stat_module.S_ISDIR(home_info.st_mode):
        return blocked("not_a_real_directory")
    if not records_complete:
        return blocked("ledger_unreadable")
    dispatch_id = home.name
    record = by_id.get(dispatch_id)
    if record is None:
        return blocked("missing_record")
    engine = goalflight_engine_sessions.resume_engine(record.get("engine") or record.get("agent"))
    if engine not in (None, "codex"):
        return blocked("not_codex", applicable=False)
    if goalflight_ledger.terminal_state_for(
        record.get("state"), record.get("reason"),
    ) == "unknown":
        return blocked("non_terminal", record=record)
    identity_probe = probe or _identity_snapshot_probe(
        records, identity_probe, deadline=deadline,
    )
    if evaluation is None:
        inactive, reason = _identity_inactive(record, identity_probe)
    else:
        reason = str(evaluation.get("liveness") or evaluation.get("reason") or "liveness_indeterminate")
        inactive = reason in _DEAD_REASONS or reason.startswith("pid_reused_")
    if not inactive:
        return blocked(reason, record=record)
    if live_home_ids is None:
        live_home_ids = _nonterminal_codex_home_ids(records)
    if dispatch_id in live_home_ids:
        return blocked("used_by_non_terminal_record", record=record)
    try:
        snapshot = _tree_snapshot(home)
    except OSError:
        return blocked("unreadable", record=record)
    if now.timestamp() - snapshot["newest_mtime"] < CODEX_HOME_MINIMUM_IDLE.total_seconds():
        return blocked("recently_modified", record=record, snapshot=snapshot)
    try:
        lock_path, lock_required = goalflight_dispatch._resume_lock_path_info(record, dispatch_id)
    except Exception:
        lock_path, lock_required = None, True
    if lock_required and lock_path is None:
        return blocked("resume_lock_unresolved", record=record, snapshot=snapshot)
    if held_paths is not None and lock_path is not None and str(lock_path) not in held_paths:
        return blocked("resume_lock_not_held", record=record, snapshot=snapshot)
    if check_open:
        if open_snapshot is None:
            open_snapshot = _legacy_scratch_process_snapshot(
                [home], deadline=deadline,
            )
        clear, open_reason = open_snapshot.get(os.path.abspath(home), (False, "lsof_error"))
        if not clear:
            reason = "open_file" if open_reason == "lsof_hit" else open_reason
            return blocked(reason, record=record, snapshot=snapshot)
    return {
        "eligible": True, "applicable": True, "reason": "eligible",
        "record": record, "snapshot": snapshot,
        "home_mtime_ns": home_info.st_mtime_ns,
        "lock_path": lock_path, "lock_required": lock_required,
        "identity_probe": identity_probe,
    }


def _run_codex_home_maintenance(
    *, homes_dir: Path, ledger_dir: Path, records: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]], records_complete: bool,
    now: dt.datetime,
    identity_probe: IdentityProbe, probe: IdentityProbe, delete: bool,
    protected_home_ids: set[str],
    deadline: float | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "trimmed_count": 0, "trimmed_bytes": 0,
        "deleted_count": 0, "deleted_bytes": 0,
        "would_trim_count": 0, "would_trim_bytes": 0,
        "would_delete_count": 0, "would_delete_bytes": 0,
        "kept_reasons": {},
    }
    if homes_dir.is_symlink() or not homes_dir.is_dir():
        return report

    live_home_ids = _nonterminal_codex_home_ids(records)
    candidates = []
    for home in sorted(homes_dir.iterdir(), key=os.fspath):
        if deadline is not None and time.monotonic() >= deadline:
            break
        gate = _codex_home_eligibility(
            home, records=records, by_id=by_id, records_complete=records_complete,
            now=now, identity_probe=identity_probe, probe=probe,
            live_home_ids=live_home_ids, check_open=False, deadline=deadline,
        )
        if not gate["eligible"]:
            _count_reason(report["kept_reasons"], gate["reason"])
            if gate.get("applicable", True):
                protected_home_ids.add(home.name)
            continue
        candidates.append((home, home.name, gate))

    held_paths: set[str] = set()
    locked: list[tuple[Path, str, dict[str, Any]]] = []
    all_fds: list[int] = []
    try:
        for home, dispatch_id, gate in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                break
            acquired = _lock_users(
                {dispatch_id},
                {dispatch_id: (gate["lock_path"], gate["lock_required"])},
                held_paths,
            )
            if acquired is None:
                _count_reason(report["kept_reasons"], "resume_lock_busy")
                protected_home_ids.add(dispatch_id)
                continue
            fds, held_paths = acquired
            all_fds.extend(fds)
            locked.append((home, dispatch_id, gate))
        if not locked:
            return report
        open_snapshot = _legacy_scratch_process_snapshot(
            [home for home, _dispatch_id, _gate in locked], deadline=deadline,
        )
        inventory = _complete_record_inventory(ledger_dir)
        if inventory is None:
            refreshed_records, refreshed_by_id = [], {}
            fresh_complete = False
        else:
            refreshed_records, refreshed_by_id, _fresh_children = inventory
            fresh_complete = True
        fresh_probe = _identity_snapshot_probe(
            refreshed_records, identity_probe, deadline=deadline,
        )
        fresh_live_home_ids = _nonterminal_codex_home_ids(refreshed_records)
        for home, dispatch_id, _initial_gate in locked:
            if deadline is not None and time.monotonic() >= deadline:
                break
            gate = _codex_home_eligibility(
                home, records=refreshed_records, by_id=refreshed_by_id,
                records_complete=fresh_complete, now=now,
                identity_probe=identity_probe, probe=fresh_probe,
                live_home_ids=fresh_live_home_ids, open_snapshot=open_snapshot,
                held_paths=held_paths, deadline=deadline,
            )
            if not gate["eligible"]:
                _count_reason(report["kept_reasons"], gate["reason"])
                if gate.get("applicable", True):
                    protected_home_ids.add(dispatch_id)
                continue
            record = gate["record"]
            snapshot = gate["snapshot"]
            try:
                has_rollout = _home_has_rollout(home)
            except OSError:
                _count_reason(report["kept_reasons"], "sessions_unreadable")
                protected_home_ids.add(dispatch_id)
                continue
            if not has_rollout and not snapshot["has_symlink"]:
                size = snapshot["allocated_bytes"]
                if not delete:
                    report["would_delete_count"] += 1
                    report["would_delete_bytes"] += size
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    shutil.rmtree(home)
                except OSError:
                    _count_reason(report["kept_reasons"], "delete_error")
                    protected_home_ids.add(dispatch_id)
                else:
                    report["deleted_count"] += 1
                    report["deleted_bytes"] += size
                continue
            if has_rollout:
                _count_reason(report["kept_reasons"], "rollout_present")
            if snapshot["has_symlink"]:
                _count_reason(report["kept_reasons"], "symlink_present")
            try:
                targets, databases = _codex_home_trim_targets(home)
                target_bytes = sum(allocated_tree_bytes(path) for path in targets)
                target_bytes += sum(
                    allocated_tree_bytes(database)
                    + sum(allocated_tree_bytes(sidecar) for sidecar in sidecars)
                    for database, sidecars in databases.items()
                )
            except OSError:
                _count_reason(report["kept_reasons"], "trim_target_unreadable")
                protected_home_ids.add(dispatch_id)
                continue
            if not targets and not databases:
                _count_reason(report["kept_reasons"], "nothing_to_trim")
                continue
            if not delete:
                report["would_trim_count"] += 1
                report["would_trim_bytes"] += target_bytes
                continue
            removed_bytes = 0
            changed = False
            failed = False
            for target in targets:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                size, target_changed, target_failed = _remove_trim_entry(target)
                removed_bytes += size
                changed = changed or target_changed
                failed = failed or target_failed
            for database, sidecars in databases.items():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                database_bytes, database_removed, database_failed = _remove_trim_entry(database)
                removed_bytes += database_bytes
                changed = changed or database_removed
                failed = failed or database_failed
                if database_removed:
                    for sidecar in sidecars:
                        sidecar_bytes, sidecar_removed, sidecar_failed = _remove_trim_entry(sidecar)
                        removed_bytes += sidecar_bytes
                        changed = changed or sidecar_removed
                        failed = failed or sidecar_failed
            if changed:
                try:
                    current_atime_ns = home.lstat().st_atime_ns
                    os.utime(
                        home,
                        ns=(current_atime_ns, gate["home_mtime_ns"]),
                        follow_symlinks=False,
                    )
                except OSError:
                    failed = True
            if changed:
                report["trimmed_count"] += 1
                report["trimmed_bytes"] += removed_bytes
            if failed:
                _count_reason(report["kept_reasons"], "trim_error")
                protected_home_ids.add(dispatch_id)
    finally:
        _release_locks(all_fds)
    return report


def _is_real_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat_module.S_ISDIR(info.st_mode) and not stat_module.S_ISLNK(info.st_mode)


def _ensure_real_directory(path: Path) -> bool:
    if _is_real_directory(path):
        return True
    try:
        path.lstat()
    except FileNotFoundError:
        if path.parent == path or not _ensure_real_directory(path.parent):
            return False
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            return False
        return _is_real_directory(path)
    except OSError:
        return False
    return False


def _remove_partial_archive(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if stat_module.S_ISREG(info.st_mode):
        with contextlib.suppress(OSError):
            path.unlink()


def _run_grok_orphan_archive(
    *, ledger_dir: Path, now: dt.datetime, delete: bool,
    accounts_dir: Path, archive_root: Path,
    deadline: float | None = None,
) -> dict[str, Any]:
    import goalflight_dispatch

    report: dict[str, Any] = {
        "archived_count": 0, "archived_bytes": 0, "archive_file_bytes": 0,
        "reclaimed_bytes": 0, "would_archive_count": 0, "would_archive_bytes": 0,
        "refused": False, "kept_reasons": {},
    }
    inventory = _complete_record_inventory(ledger_dir)
    if inventory is None:
        report["refused"] = True
        report["refusal_reason"] = "ledger unreadable or incomplete"
        return report
    records, _by_id, _children = inventory
    known_session_ids = set()
    for record in records:
        if deadline is not None and time.monotonic() >= deadline:
            break
        dispatch_id = str(record.get("dispatch_id") or "")
        try:
            session_id = goalflight_dispatch._recorded_engine_session_id(record, dispatch_id)
        except Exception:
            report["refused"] = True
            report["refusal_reason"] = "session identity indeterminate"
            return report
        if isinstance(session_id, str) and session_id:
            known_session_ids.add(session_id)
    if deadline is not None and time.monotonic() >= deadline:
        report["refused"] = True
        report["refusal_reason"] = "sweep deadline reached while scanning ledger sessions"
        return report
    if not known_session_ids:
        report["refused"] = True
        report["refusal_reason"] = "ledger returned no session ids"
        return report
    if not _is_real_directory(accounts_dir):
        return report

    for account_home in sorted(accounts_dir.iterdir(), key=os.fspath):
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not _is_real_directory(account_home):
            continue
        grok_home = account_home / "grok"
        hidden_home = grok_home / ".grok"
        sessions_root = hidden_home / "sessions"
        if not all(_is_real_directory(path) for path in (grok_home, hidden_home, sessions_root)):
            continue
        candidates = []
        for cwd_dir in sorted(sessions_root.iterdir(), key=os.fspath):
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not _is_real_directory(cwd_dir):
                continue
            for session in sorted(cwd_dir.iterdir(), key=os.fspath):
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if not _is_real_directory(session):
                    if session.is_symlink():
                        _count_reason(report["kept_reasons"], "symlink_session")
                    continue
                if session.name in known_session_ids:
                    _count_reason(report["kept_reasons"], "session_in_ledger")
                    continue
                try:
                    snapshot = _tree_snapshot(session)
                except OSError:
                    _count_reason(report["kept_reasons"], "session_unreadable")
                    continue
                if now.timestamp() - snapshot["newest_mtime"] < GROK_SESSION_MINIMUM_IDLE.total_seconds():
                    _count_reason(report["kept_reasons"], "session_recent")
                    continue
                candidates.append((session, cwd_dir, account_home, snapshot))
        if deadline is not None and time.monotonic() >= deadline:
            return report
        if not candidates:
            continue

        open_before = _legacy_scratch_process_snapshot(
            [session for session, _cwd, _account, _snapshot in candidates],
            deadline=deadline,
        )
        ready = []
        for session, cwd_dir, account_home, snapshot in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                break
            clear, reason = open_before.get(os.path.abspath(session), (False, "lsof_error"))
            if not clear:
                _count_reason(report["kept_reasons"], "session_open" if reason == "lsof_hit" else reason)
                continue
            source_bytes = snapshot["allocated_bytes"]
            if not delete:
                report["would_archive_count"] += 1
                report["would_archive_bytes"] += source_bytes
                continue

            dest_dir = archive_root / account_home.name / cwd_dir.name
            dest = dest_dir / f"{session.name}.tar.zst"
            part = dest.with_name(dest.name + ".partial")
            if not _ensure_real_directory(dest_dir):
                _count_reason(report["kept_reasons"], "archive_directory_unavailable")
                continue
            if dest.exists() or dest.is_symlink() or part.exists() or part.is_symlink():
                _count_reason(report["kept_reasons"], "archive_exists")
                continue
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                goalflight_subprocess.run_with_deadline(
                    ["tar", "--zstd", "-cf", str(part), "-C", str(cwd_dir), session.name],
                    check=True, capture_output=True,
                    timeout=_grok_archive_timeout_seconds(source_bytes),
                    deadline=deadline,
                )
                goalflight_subprocess.run_with_deadline(
                    ["zstd", "-t", str(part)], check=True, capture_output=True,
                    timeout=_grok_archive_timeout_seconds(source_bytes),
                    deadline=deadline,
                )
                listing = goalflight_subprocess.run_with_deadline(
                    ["tar", "--zstd", "-tf", str(part)],
                    check=True, capture_output=True,
                    timeout=_grok_archive_timeout_seconds(source_bytes),
                    deadline=deadline,
                ).stdout
                if isinstance(listing, bytes):
                    listing = listing.decode("utf-8", errors="surrogateescape")
                members = {name.rstrip("/") for name in str(listing).splitlines() if name}
                if members != snapshot["entries"]:
                    raise RuntimeError("archive member list does not match the session folder")
                archive_size = part.lstat().st_size
                ready.append((session, dest, part, snapshot, source_bytes, archive_size))
            except subprocess.TimeoutExpired:
                _remove_partial_archive(part)
                _count_reason(report["kept_reasons"], "archive_timeout")
            except Exception:
                _remove_partial_archive(part)
                _count_reason(report["kept_reasons"], "archive_verification_failed")

        if not ready:
            continue
        open_after = _legacy_scratch_process_snapshot(
            [session for session, _dest, _part, _snapshot, _bytes, _archive_bytes in ready],
            deadline=deadline,
        )
        for session, dest, part, snapshot, source_bytes, archive_size in ready:
            if deadline is not None and time.monotonic() >= deadline:
                _remove_partial_archive(part)
                continue
            clear, reason = open_after.get(os.path.abspath(session), (False, "lsof_error"))
            if not clear:
                _remove_partial_archive(part)
                _count_reason(report["kept_reasons"], "session_open_after_archive" if reason == "lsof_hit" else reason)
                continue
            try:
                current = _tree_snapshot(session)
            except OSError:
                current = None
            if current is None or current["fingerprints"] != snapshot["fingerprints"]:
                _remove_partial_archive(part)
                _count_reason(report["kept_reasons"], "session_changed_during_archive")
                continue
            try:
                os.link(part, dest, follow_symlinks=False)
                part.unlink()
            except OSError:
                _remove_partial_archive(part)
                _count_reason(report["kept_reasons"], "archive_publish_failed")
                continue
            try:
                shutil.rmtree(session)
            except OSError:
                _count_reason(report["kept_reasons"], "session_remove_failed")
            else:
                report["reclaimed_bytes"] += source_bytes
            report["archived_count"] += 1
            report["archived_bytes"] += source_bytes
            report["archive_file_bytes"] += archive_size
    return report

def _run_dispatch_scratch_retention(
    artifacts: dict[str, dict[str, Any]],
    locks: dict[str, tuple[Path | None, bool]],
    records: list[dict[str, Any]],
    *, records_complete: bool, ledger_dir: Path, dispatch_dir: Path,
    now: dt.datetime, identity_probe: IdentityProbe, probe: IdentityProbe,
    delete: bool,
    deadline: float | None = None,
) -> dict[str, Any]:
    try:
        free_bytes: int | None = shutil.disk_usage(dispatch_dir).free
    except OSError:
        free_bytes = None
    low_space = (
        None if free_bytes is None
        else free_bytes < DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES
    )
    minimum_idle = (
        DISPATCH_SCRATCH_LOW_SPACE_MINIMUM_AGE
        if low_space else DISPATCH_SCRATCH_MINIMUM_AGE
    )
    report: dict[str, Any] = {
        "scratch_free_bytes": free_bytes,
        "scratch_free_floor_bytes": DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES,
        "scratch_low_space": low_space,
        "scratch_minimum_idle_seconds": minimum_idle.total_seconds(),
        "scratch_size_warning_bytes": DISPATCH_SCRATCH_SIZE_WARNING_BYTES,
        "scratch_size_warnings": {},
        "scratch_eligible_count": 0,
        "scratch_eligible_bytes": 0,
        "scratch_would_delete_count": 0,
        "scratch_would_delete_bytes": 0,
        "scratch_deleted_count": 0,
        "scratch_deleted_bytes": 0,
        "scratch_kept_reasons": {},
        "deleted_count": 0,
        "deleted_allocated_bytes": 0,
    }
    by_id, children = _relationships(records)
    candidates: list[tuple[str, dict[str, Any], str, set[str], os.stat_result, int]] = []
    now_timestamp = now.timestamp()

    def keep(reason: str) -> None:
        _count_reason(report["scratch_kept_reasons"], reason)

    def note_size(dispatch_id: str, size: int) -> None:
        if size > DISPATCH_SCRATCH_SIZE_WARNING_BYTES:
            report["scratch_size_warnings"][dispatch_id] = size

    for key, item in artifacts.items():
        if deadline is not None and time.monotonic() >= deadline:
            break
        if item.get("kind") != "scratch":
            continue
        path = item["path"]
        refs = item["refs"]
        dispatch_id = next(iter(refs)) if len(refs) == 1 else path.name[:-len(".scratch")]
        size = item.get("bytes")
        if isinstance(size, int):
            note_size(dispatch_id, size)
        try:
            root_info = path.lstat()
        except OSError:
            keep("unreadable")
            continue
        if stat_module.S_ISLNK(root_info.st_mode) or not stat_module.S_ISDIR(root_info.st_mode):
            keep("not_a_real_directory")
            continue
        if item.get("walk_error"):
            keep(str(item["walk_error"]))
            continue
        if not item.get("deletable") or not isinstance(size, int):
            keep("size_scan_error")
            continue
        if not records_complete:
            keep("ledger_unreadable")
            continue
        if len(refs) != 1:
            keep("missing_record" if not refs else "multiple_owners")
            continue
        record = by_id.get(dispatch_id)
        if record is None:
            keep("missing_record")
            continue
        if goalflight_ledger.terminal_state_for(
            record.get("state"), record.get("reason"),
        ) == "unknown":
            keep("non_terminal")
            continue
        if _has_nonterminal_dispatch_reference(records, dispatch_id):
            keep("used_by_non_terminal_record")
            continue
        inactive, reason = _identity_inactive(record, probe)
        if not inactive:
            keep(reason)
            continue
        lineage = _lineage_ids({dispatch_id}, by_id, children)
        if not lineage:
            keep("missing_lineage")
            continue
        newest_mtime = item.get("newest_mtime")
        if not isinstance(newest_mtime, (float, int)):
            keep("size_scan_error")
            continue
        if now_timestamp - newest_mtime < minimum_idle.total_seconds():
            keep("inside_minimum_age")
            continue
        candidates.append((key, item, dispatch_id, lineage, root_info, size))

    report["scratch_eligible_count"] = len(candidates)
    report["scratch_eligible_bytes"] = sum(row[5] for row in candidates)
    if not candidates:
        return report

    if not delete:
        open_snapshot = _legacy_scratch_process_snapshot(
            [item["path"] for _key, item, _did, _ids, _root, _size in candidates],
            deadline=deadline,
        )
        for _key, item, _dispatch_id, _ids, _root, size in candidates:
            clear, reason = open_snapshot.get(
                os.path.abspath(item["path"]), (False, "lsof_error"),
            )
            if clear:
                report["scratch_would_delete_count"] += 1
                report["scratch_would_delete_bytes"] += size
            else:
                keep("open_file" if reason == "lsof_hit" else reason)
        return report

    held_paths: set[str] = set()
    lock_fds: list[int] = []
    locked: list[tuple[str, dict[str, Any], str, set[str], os.stat_result, int]] = []
    try:
        for candidate in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                break
            _key, _item, _dispatch_id, ids, _root, _size = candidate
            acquired = _lock_users(ids, locks, held_paths)
            if acquired is None:
                keep("resume_lock_busy")
                continue
            fds, held_paths = acquired
            lock_fds.extend(fds)
            locked.append(candidate)
        if not locked:
            return report

        open_snapshot = _legacy_scratch_process_snapshot(
            [item["path"] for _key, item, _did, _ids, _root, _size in locked],
            deadline=deadline,
        )
        for key, item, dispatch_id, ids, root_info, size in locked:
            if deadline is not None and time.monotonic() >= deadline:
                break
            path = item["path"]
            clear, reason = open_snapshot.get(
                os.path.abspath(path), (False, "lsof_error"),
            )
            if not clear:
                keep("open_file" if reason == "lsof_hit" else reason)
                continue

            candidate_record = by_id.get(dispatch_id)
            if candidate_record is None:
                keep("missing_record")
                continue
            identity_deadline = (
                time.monotonic() + _DISPATCH_SCRATCH_IDENTITY_PROBE_TIMEOUT_SECONDS
            )
            try:
                fresh_probe = _identity_snapshot_probe(
                    [candidate_record], identity_probe, deadline=identity_deadline,
                )
            except subprocess.TimeoutExpired:
                keep("identity_probe_timeout")
                continue
            inactive, reason = _identity_inactive(candidate_record, fresh_probe)
            if not inactive:
                if reason == "liveness_indeterminate" and time.monotonic() >= identity_deadline:
                    keep("identity_probe_timeout")
                else:
                    keep(reason)
                continue

            inventory = _complete_record_inventory(ledger_dir)
            if inventory is None:
                keep("ledger_unreadable")
                continue
            fresh_records, fresh_by_id, fresh_children = inventory
            record = fresh_by_id.get(dispatch_id)
            if record is None:
                keep("missing_record")
                continue
            if goalflight_ledger.terminal_state_for(
                record.get("state"), record.get("reason"),
            ) == "unknown":
                keep("non_terminal")
                continue
            if _has_nonterminal_dispatch_reference(fresh_records, dispatch_id):
                keep("used_by_non_terminal_record")
                continue
            identity_fields = (
                "worker_pid", "claimant_pid", "controller_pid",
                "worker_identity", "claimant_identity", "controller_identity",
            )
            if any(record.get(field) != candidate_record.get(field) for field in identity_fields):
                keep("eligibility_changed")
                continue
            fresh_ids = _lineage_ids({dispatch_id}, fresh_by_id, fresh_children)
            if not fresh_ids or not fresh_ids.issubset(ids):
                keep("eligibility_changed")
                continue
            process_group_state = _scratch_process_group_alive(record)
            if process_group_state is True:
                keep("live_worker_process")
                continue
            if process_group_state is None:
                keep("process_group_liveness_indeterminate")
                continue
            try:
                current_root = path.lstat()
                if (
                    stat_module.S_ISLNK(current_root.st_mode)
                    or not stat_module.S_ISDIR(current_root.st_mode)
                ):
                    keep("not_a_real_directory")
                    continue
                if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                    keep("changed_before_delete")
                    continue
            except OSError:
                keep("changed_before_delete")
                continue
            report["scratch_would_delete_count"] += 1
            report["scratch_would_delete_bytes"] += size
    finally:
        _release_locks(lock_fds)
    return report

def _run_dispatch_retention(
    *, homes_dir: Path, ledger_dir: Path, dispatch_dir: Path | None = None,
    pidfile_dir: Path | None = None, legacy_scratch_dir: Path | None = None,
    now: dt.datetime, identity_probe: IdentityProbe, delete: bool = True,
    timings: dict[str, Any] | None = None,
    dispatch_gc_apply: bool | None = None,
    scratch_days: int = DISPATCH_GC_SCRATCH_DAYS,
    tail_days: int = DISPATCH_GC_TAIL_DAYS,
    scratch_expire_resumable_days: int | None = None,
    include_housekeeping: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    if scratch_expire_resumable_days is None:
        scratch_expire_resumable_days = DISPATCH_GC_RESUMABLE_SCRATCH_DAYS
    if (
        scratch_days < 0 or tail_days < 0
        or (
            scratch_expire_resumable_days is not None
            and scratch_expire_resumable_days < 0
        )
    ):
        raise ValueError("retention days must be nonnegative")
    dispatch_gc_apply = delete if dispatch_gc_apply is None else dispatch_gc_apply
    started = time.monotonic()
    cpu_started = time.process_time()
    walked_files = [0]
    dispatch_dir = dispatch_dir or goalflight_dispatch_paths.dispatch_base_dir()
    # Resolve the configured alias (for example /tmp on macOS) once, before
    # scanning. Later operations open this physical root component-by-component
    # with O_NOFOLLOW, so an ancestor swap cannot redirect deletion elsewhere.
    try:
        before = dispatch_dir.lstat()
        if not stat_module.S_ISLNK(before.st_mode):
            resolved = dispatch_dir.resolve(strict=True)
            after = dispatch_dir.lstat()
            resolved_stat = resolved.stat()
            if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino) == (
                resolved_stat.st_dev, resolved_stat.st_ino,
            ):
                dispatch_dir = resolved
    except OSError:
        pass
    pidfile_dir = pidfile_dir or goalflight_dispatch_paths.pidfile_dir()
    legacy_scratch_dir = legacy_scratch_dir or _legacy_claude_scratch_dir()
    try:
        ledger_snapshot_names: set[str] | None = set(os.listdir(ledger_dir))
    except OSError:
        ledger_snapshot_names = None
    initial_record_names = {name for name in ledger_snapshot_names or () if name.endswith(".json")}
    records = _records_from_directory(
        ledger_dir,
        record_names=initial_record_names,
    )
    records_complete = (
        ledger_snapshot_names is not None
        and _records_complete_for_names(records, initial_record_names)
    )
    checkpoint_probe = _sweep_identity_snapshot_probe(
        records, identity_probe, deadline=deadline,
    )
    import goalflight_dispatch

    resume_context = goalflight_dispatch._resume_validation_context(
        records, identity_probe=checkpoint_probe,
    )
    by_id, evaluations, children = _evaluate_records(
        records, now=now, probe=checkpoint_probe,
    )
    housekeeping: dict[str, Any] = {}
    protected_codex_home_ids: set[str] = set()
    if include_housekeeping and (deadline is None or time.monotonic() < deadline):
        codex_home_maintenance = _run_codex_home_maintenance(
            homes_dir=homes_dir, ledger_dir=ledger_dir, records=records,
            by_id=by_id, records_complete=records_complete,
            now=now,
            identity_probe=identity_probe, probe=checkpoint_probe,
            delete=delete, protected_home_ids=protected_codex_home_ids,
            deadline=deadline,
        )
        state_root = Path.home() / ".goal-flight"
        grok_orphan_archive = _run_grok_orphan_archive(
            ledger_dir=ledger_dir, now=now, delete=delete,
            accounts_dir=state_root / "accounts",
            archive_root=state_root / "archive" / "grok-sessions",
            deadline=deadline,
        )
        housekeeping = {
            "codex_home_maintenance": codex_home_maintenance,
            "grok_orphan_archive": grok_orphan_archive,
        }
    artifacts, locks = _scan_dispatch_artifacts(
        by_id, homes_dir=homes_dir, dispatch_dir=dispatch_dir, pidfile_dir=pidfile_dir,
        walked_files=walked_files,
    )
    legacy_elapsed = 0.0
    legacy_cpu_elapsed = 0.0
    legacy_started = time.monotonic()
    legacy_cpu_started = time.process_time()
    legacy_artifacts, legacy_kept_reasons = _scan_legacy_scratch(
        legacy_scratch_dir, by_id, evaluations, children,
        _unknown_record_names(initial_record_names, records), walked_files=walked_files,
    )
    legacy_elapsed += time.monotonic() - legacy_started
    legacy_cpu_elapsed += time.process_time() - legacy_cpu_started
    legacy_lsof_before_delete: dict[str, tuple[bool, str]] = {}
    artifacts.update(legacy_artifacts)
    scratch_retention = _run_dispatch_scratch_retention(
        artifacts, locks, records,
        records_complete=records_complete, ledger_dir=ledger_dir,
        dispatch_dir=dispatch_dir, now=now, identity_probe=identity_probe,
        probe=checkpoint_probe, delete=delete, deadline=deadline,
    )
    budget_artifacts = {
        key: item for key, item in artifacts.items() if item.get("budgeted", True)
    }
    measurable = all(item["bytes"] is not None for item in budget_artifacts.values())
    total = sum(item["bytes"] or 0 for item in budget_artifacts.values())
    candidates = []
    legacy_candidates = []
    dispatch_gc_candidates = []
    dispatch_gc_classes = {
        name: {"count": 0, "bytes": 0}
        for name in (
            "would_remove", "would_remove_resumable", "would_compress",
            "skipped_live", "skipped_unknown",
            "skipped_pinned", "skipped_resumable", "skipped_recent", "skipped_unsafe",
        )
    }
    dispatch_gc_pins, dispatch_gc_pin_errors, dispatch_gc_unknown = _dispatch_gc_pins(
        dispatch_dir, records, deadline=deadline,
    )
    resumability_cache: dict[str, bool | None] = {}
    dispatch_gc_kept_reasons: dict[str, int] = {}
    dispatch_gc_live_reasons: dict[str, dict[str, Any]] = {}
    dispatch_gc_live_reason_examples: dict[str, set[str]] = {}
    dispatch_gc_mtime_fallbacks = 0
    for key, item in artifacts.items():
        if deadline is not None and time.monotonic() >= deadline:
            break
        if item["kind"] in {"scratch", "tail"}:
            threshold = dt.timedelta(days=scratch_days if item["kind"] == "scratch" else tail_days)
            if item["bytes"] is not None:
                class_name, reason, candidate, age_source = _dispatch_gc_decision(
                    item["path"], item["refs"], kind=item["kind"],
                    by_id=by_id, evaluations=evaluations, children=children,
                    pins=dispatch_gc_pins,
                    unknown_dispatch_ids=dispatch_gc_unknown,
                    dispatch_dir=dispatch_dir,
                    now=now, minimum_age=threshold,
                    resumability_cache=resumability_cache,
                    identity_probe=checkpoint_probe,
                    resume_context=resume_context,
                    scratch_expire_resumable_days=scratch_expire_resumable_days,
                )
            else:
                class_name, candidate, age_source = "skipped_unknown", None, None
            if age_source == "mtime":
                dispatch_gc_mtime_fallbacks += 1
            summary = dispatch_gc_classes[class_name]
            summary["count"] += 1
            summary["bytes"] += int(item["bytes"] or 0)
            if class_name == "skipped_live":
                live_summary = dispatch_gc_live_reasons.setdefault(
                    reason, {"count": 0, "bytes": 0},
                )
                live_summary["count"] += 1
                live_summary["bytes"] += int(item["bytes"] or 0)
                dispatch_gc_live_reason_examples.setdefault(reason, set()).update(
                    _lineage_ids(item["refs"], by_id, children)
                )
            item["dispatch_gc_class"] = class_name
            if candidate is not None:
                ended_at, ids = candidate[1], candidate[0]
                dispatch_gc_candidates.append((ended_at, key, ids))
        if item["kind"] in {"scratch", "tail"}:
            continue
        candidate = _candidate(
            item["refs"], by_id, evaluations, children,
            minimum_age=_minimum_age_for_artifact(item["kind"]),
        )
        if candidate is not None and item["kind"] == "home":
            group_states = [
                _scratch_process_group_alive(by_id[dispatch_id])
                for dispatch_id in candidate[0]
            ]
            if any(state is True for state in group_states):
                _count_reason(dispatch_gc_kept_reasons, "worker_process_group_alive")
                continue
            if any(state is None for state in group_states):
                _count_reason(dispatch_gc_kept_reasons, "process_group_liveness_indeterminate")
                continue
        if item["deletable"] and item["bytes"] is not None and candidate:
            ids, ended_at = candidate
            row = (ended_at, key, ids)
            if item["kind"] == "legacy_scratch":
                legacy_candidates.append(row)
            else:
                candidates.append(row)
    candidates.sort()
    legacy_candidates.sort()
    dispatch_gc_candidates.sort()
    legacy_started = time.monotonic()
    legacy_cpu_started = time.process_time()
    legacy_lsof = (
        _legacy_scratch_process_snapshot(
            [artifacts[key]["path"] for _ended_at, key, _ids in legacy_candidates],
            deadline=deadline,
        )
        if legacy_candidates else {}
    )
    legacy_elapsed += time.monotonic() - legacy_started
    legacy_cpu_elapsed += time.process_time() - legacy_cpu_started
    tail_candidates = [
        row for row in dispatch_gc_candidates
        if artifacts[row[1]]["kind"] == "tail"
    ]
    eligible_bytes = sum(
        int(artifacts[key]["bytes"] or 0)
        for _end, key, _ids in candidates
    )
    legacy_eligible_bytes = sum(
        int(artifacts[key]["bytes"] or 0)
        for _end, key, _ids in legacy_candidates
    )
    reasons: dict[str, int] = {}
    for evaluation in evaluations.values():
        if not evaluation["eligible"]:
            reason = evaluation["reason"]
            reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in dispatch_gc_kept_reasons.items():
        reasons[reason] = reasons.get(reason, 0) + count
    report = {
        "schema": SCHEMA, "mode": "dry-run" if not delete else "delete",
        "budget_bytes": DISPATCH_ARTIFACT_BUDGET_BYTES,
        "minimum_age_seconds": DISPATCH_ARTIFACT_MINIMUM_AGE.total_seconds(),
        "total_allocated_bytes": total, "budget_measurable": measurable,
        "eligible_count": len(candidates) + scratch_retention["scratch_eligible_count"],
        "eligible_allocated_bytes": eligible_bytes + scratch_retention["scratch_eligible_bytes"],
        "deleted_count": 0, "deleted_allocated_bytes": 0, "kept_reasons": reasons,
        "legacy_eligible_count": len(legacy_candidates),
        "legacy_eligible_bytes": legacy_eligible_bytes,
        "legacy_deleted_count": 0, "legacy_deleted_bytes": 0,
        "legacy_would_delete_count": 0, "legacy_would_delete_bytes": 0,
        "legacy_kept_reasons": legacy_kept_reasons, "files_walked": 0,
        "dispatch_gc": {
            "mode": "apply" if dispatch_gc_apply else "dry-run",
            "scratch_days": scratch_days,
            "tail_days": tail_days,
            "scratch_expire_resumable_days": scratch_expire_resumable_days,
            "pin_file": str(dispatch_dir / DISPATCH_GC_PIN_FILE),
            "pin_strategy": "explicit dispatch id list plus keep refs across registered project repositories; Git errors fail closed as pinned",
            "classes": dispatch_gc_classes,
            "skipped_live_by_reason": {
                reason: {
                    **summary,
                    "example_dispatch_ids": sorted(
                        dispatch_gc_live_reason_examples.get(reason, set())
                    )[:20],
                }
                for reason, summary in sorted(dispatch_gc_live_reasons.items())
            },
            "mtime_fallback_count": dispatch_gc_mtime_fallbacks,
            "would_compress_count": len(tail_candidates),
            "would_compress_bytes": sum(
                int(artifacts[key]["bytes"] or 0) for _end, key, _ids in tail_candidates
            ),
            "removed_count": 0,
            "compressed_count": 0,
            "compressed_bytes": 0,
            "errors": list(dispatch_gc_pin_errors),
        },
    }
    report.update(scratch_retention)
    report.update(housekeeping)

    if not measurable:
        report["budget_error"] = "some artifact sizes could not be measured; those artifacts are kept and left out of the total"
    if not delete:
        for _ended_at, key, _ids in legacy_candidates:
            clear, reason = legacy_lsof[key]
            if clear:
                report["legacy_would_delete_count"] += 1
                report["legacy_would_delete_bytes"] += int(artifacts[key]["bytes"] or 0)
            else:
                _count_reason(legacy_kept_reasons, reason)
        if timings is not None:
            elapsed = time.monotonic() - started
            cpu_elapsed = time.process_time() - cpu_started
            timings["legacy_scratch"] = legacy_elapsed
            timings["dispatch_artifacts_scan"] = max(0.0, elapsed - legacy_elapsed)
            timings["deletion"] = 0.0
            cpu = timings.setdefault("cpu", {})
            cpu["legacy_scratch"] = legacy_cpu_elapsed
            cpu["dispatch_artifacts_scan"] = max(0.0, cpu_elapsed - legacy_cpu_elapsed)
            cpu["deletion"] = 0.0
            timings["total"] = elapsed
            cpu["total"] = cpu_elapsed
        report["files_walked"] = walked_files[0]
        return report
    if legacy_candidates:
        legacy_started = time.monotonic()
        legacy_cpu_started = time.process_time()
        legacy_lsof_before_delete = _legacy_scratch_process_snapshot(
            [artifacts[key]["path"] for _ended_at, key, _ids in legacy_candidates],
            deadline=deadline,
        )
        legacy_elapsed += time.monotonic() - legacy_started
        legacy_cpu_elapsed += time.process_time() - legacy_cpu_started
    pending = sorted([
        *candidates,
        *legacy_candidates,
        *(dispatch_gc_candidates if dispatch_gc_apply else []),
    ])
    deletion_started = time.monotonic()
    deletion_cpu_started = time.process_time()
    deleted_count = report["deleted_count"]
    deleted_bytes = report["deleted_allocated_bytes"]
    scratch_deleted_count = report["scratch_deleted_count"]
    scratch_deleted_bytes = report["scratch_deleted_bytes"]
    legacy_deleted_count = legacy_deleted_bytes = 0
    home_budget_preheld_fds: list[int] = []
    home_budget_held_paths: set[str] = set()
    home_budget_prelocked: dict[str, set[str]] = {}
    home_budget_sizes: dict[str, int] = {}
    if total > DISPATCH_ARTIFACT_BUDGET_BYTES:
        for _ended_at, key, ids in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                break
            item = artifacts[key]
            if item["kind"] != "home":
                continue
            dispatch_id = item["path"].name
            if dispatch_id in protected_codex_home_ids:
                continue
            record = by_id.get(dispatch_id)
            if record is None:
                _count_reason(report["kept_reasons"], "missing_record")
                protected_codex_home_ids.add(dispatch_id)
                continue
            import goalflight_engine_sessions
            engine = goalflight_engine_sessions.resume_engine(record.get("engine") or record.get("agent"))
            if engine not in (None, "codex"):
                continue
            try:
                home_budget_sizes[key] = allocated_tree_bytes(
                    item["path"], walked_files=walked_files,
                )
            except OSError:
                _count_reason(report["kept_reasons"], "size_scan_error")
                protected_codex_home_ids.add(dispatch_id)
                continue
            acquired = _lock_users(ids, locks, home_budget_held_paths)
            if acquired is None:
                _count_reason(report["kept_reasons"], "resume_lock_busy")
                protected_codex_home_ids.add(dispatch_id)
                continue
            fds, home_budget_held_paths = acquired
            home_budget_preheld_fds.extend(fds)
            home_budget_prelocked[key] = set(ids)
    # Check handles first, then take one complete ledger inventory while all
    # candidate home locks are held. Reuse both snapshots for the batch.
    home_budget_open_snapshot = (
        _legacy_scratch_process_snapshot(
            [artifacts[key]["path"] for key in home_budget_prelocked],
            deadline=deadline,
        )
        if home_budget_prelocked else {}
    )
    home_budget_inventory = (
        _complete_record_inventory(ledger_dir) if home_budget_prelocked else None
    )
    deadline_skipped: list[str] = []
    while pending:
        if deadline is not None and time.monotonic() >= deadline:
            deadline_skipped.extend(
                str(artifacts[key]["path"]) for _ended_at, key, _ids in pending
            )
            break
        pending.sort()
        ended_at, key, ids = pending.pop(0)
        item = artifacts[key]
        is_legacy = item["kind"] == "legacy_scratch"
        is_dispatch_gc = item["kind"] in {"scratch", "tail"}
        is_codex_home = key in home_budget_prelocked
        if item["kind"] == "home" and item["path"].name in protected_codex_home_ids:
            continue
        # An unmeasured artifact is kept and left out of the total, which can only
        # make this pass delete less; it must not stop every other deletion.
        if not is_legacy and not is_dispatch_gc and total <= DISPATCH_ARTIFACT_BUDGET_BYTES:
            if item["kind"] == "home":
                _count_reason(report["kept_reasons"], "budget_met")
            continue
        if is_codex_home:
            size = home_budget_sizes[key]
        else:
            try:
                size = allocated_tree_bytes(item["path"], walked_files=walked_files)
            except OSError:
                if is_legacy:
                    _count_reason(legacy_kept_reasons, "size_scan_error")
                continue
        if is_codex_home and set(ids).issubset(home_budget_prelocked[key]):
            acquired = ([], set(home_budget_held_paths))
        else:
            acquired = _lock_users(ids, locks, home_budget_held_paths)
        if acquired is None:
            if is_legacy:
                _count_reason(legacy_kept_reasons, "resume_lock_busy")
            elif item["kind"] == "home":
                _count_reason(report["kept_reasons"], "resume_lock_busy")
                protected_codex_home_ids.add(item["path"].name)
            continue
        fds, held_paths = acquired
        try:
            if ledger_snapshot_names is None and not is_codex_home:
                if is_legacy:
                    _count_reason(legacy_kept_reasons, "ledger_unreadable")
                elif item["kind"] == "home":
                    _count_reason(report["kept_reasons"], "ledger_unreadable")
                continue
            import goalflight_dispatch

            locked_ids = set(ids)
            observed_names = set(ledger_snapshot_names or ())
            fresh_ids: set[str] | None = None
            fresh_by_id: dict[str, dict[str, Any]] = {}
            fresh_children: dict[str, set[str]] = {}
            home_fresh_records: list[dict[str, Any]] = []
            home_records_complete = False
            while True:
                if is_codex_home:
                    inventory = home_budget_inventory
                    if inventory is None:
                        fresh_ids = None
                        break
                    home_fresh_records, fresh_by_id, fresh_children = inventory
                    home_records_complete = True
                else:
                    try:
                        current_names = set(os.listdir(ledger_dir))
                    except OSError:
                        fresh_ids = None
                        break
                    new_record_names = {
                        name for name in current_names - observed_names if name.endswith(".json")
                    }
                    observed_names.update(current_names)
                    fresh = _records_from_directory(
                        ledger_dir,
                        dispatch_ids=None if is_legacy else locked_ids,
                        record_names=(
                            {name for name in current_names if name.endswith(".json")}
                            if is_legacy else new_record_names
                        ),
                    )
                    fresh_by_id, fresh_children = _relationships(fresh)
                if not locked_ids.issubset(fresh_by_id):
                    fresh_ids = None
                    break
                if is_legacy:
                    unknown_names = _unknown_record_names({name for name in current_names if name.endswith(".json")}, fresh)
                    owner_matches, unknown_owner = _legacy_owner_matches(item["path"].name, fresh_by_id, unknown_names)
                    if unknown_owner or not owner_matches or set(owner_matches) != set(item["refs"]):
                        reason = "unknown_owner" if unknown_owner else "unmapped" if not owner_matches else "multiple_owners"
                        _count_reason(legacy_kept_reasons, reason)
                        fresh_ids = None
                        break
                current_ids = _lineage_ids(item["refs"], fresh_by_id, fresh_children)
                if not current_ids:
                    fresh_ids = None
                    break
                extra_ids = current_ids - locked_ids
                if not extra_ids:
                    fresh_ids = current_ids
                    break
                extra_info = {
                    did: goalflight_dispatch._resume_lock_path_info(fresh_by_id[did], did)
                    for did in extra_ids - locks.keys()
                }
                acquired_more = _lock_users(extra_ids, {**locks, **extra_info}, held_paths)
                if acquired_more is None:
                    fresh_ids = None
                    break
                more_fds, held_paths = acquired_more
                fds.extend(more_fds)
                locks.update(extra_info)
                locked_ids.update(extra_ids)
                if is_codex_home:
                    home_budget_open_snapshot.update(
                        _legacy_scratch_process_snapshot(
                            [item["path"]], deadline=deadline,
                        )
                    )
                    home_budget_inventory = _complete_record_inventory(ledger_dir)
            if fresh_ids is None:
                if is_legacy:
                    _count_reason(legacy_kept_reasons, "eligibility_changed")
                if is_dispatch_gc:
                    _dispatch_gc_reclassify_artifact(
                        report, item, "skipped_unknown", size,
                    )
                elif is_codex_home:
                    reason = "ledger_unreadable" if not home_records_complete else "eligibility_changed"
                    _count_reason(report["kept_reasons"], reason)
                    protected_codex_home_ids.add(item["path"].name)
                continue
            if is_legacy:
                lineage_records = [fresh_by_id[did] for did in fresh_ids]
                eligibility_probe = _sweep_identity_snapshot_probe(
                    lineage_records, identity_probe, deadline=deadline,
                )
                eligibility_eval = {
                    did: _evaluate_record(fresh_by_id[did], now=now, probe=eligibility_probe)
                    for did in fresh_ids
                }
                if _candidate(
                    item["refs"], fresh_by_id, eligibility_eval, fresh_children,
                    minimum_age=_minimum_age_for_artifact(item["kind"]),
                ) is None:
                    reason = next(
                        (
                            str(eligibility_eval[did].get("reason", "ineligible_owner"))
                            for did in fresh_ids
                            if not eligibility_eval[did].get("eligible")
                        ),
                        "eligibility_changed",
                    )
                    _count_reason(report["kept_reasons"], reason)
                    _count_reason(legacy_kept_reasons, reason)
                    continue
                clear, reason = legacy_lsof.get(key, (False, "lsof_error"))
                if clear:
                    clear, second_reason = legacy_lsof_before_delete.get(
                        key, (False, "lsof_error"),
                    )
                    if not clear:
                        reason = second_reason
                if not clear:
                    _count_reason(legacy_kept_reasons, reason)
                    continue
            if is_dispatch_gc:
                fresh_records = list(fresh_by_id.values())
                fresh_probe = _sweep_identity_snapshot_probe(
                    fresh_records, identity_probe, deadline=deadline,
                )
                fresh_resume_context = goalflight_dispatch._resume_validation_context(
                    fresh_records, identity_probe=fresh_probe,
                )
                fresh_by_id, fresh_eval, fresh_children = _evaluate_records(
                    fresh_records, now=now, probe=fresh_probe,
                )
                fresh_pins, fresh_pin_errors, fresh_unknown_dispatch_ids = _dispatch_gc_pins(
                    dispatch_dir, fresh_records, deadline=deadline,
                )
                if fresh_pin_errors:
                    report["dispatch_gc"]["errors"].extend(fresh_pin_errors)
                class_name, reason, current, _age_source = _dispatch_gc_decision(
                    item["path"], item["refs"], kind=item["kind"],
                    by_id=fresh_by_id, evaluations=fresh_eval, children=fresh_children,
                    pins=fresh_pins,
                    unknown_dispatch_ids=fresh_unknown_dispatch_ids,
                    dispatch_dir=dispatch_dir,
                    now=now,
                    minimum_age=dt.timedelta(days=(
                        scratch_days if item["kind"] == "scratch" else tail_days
                    )),
                    resumability_cache={},
                    identity_probe=fresh_probe,
                    resume_context=fresh_resume_context,
                    scratch_expire_resumable_days=scratch_expire_resumable_days,
                )
                if current is None:
                    _dispatch_gc_reclassify_artifact(
                        report, item, class_name, size,
                    )
                    continue
                _dispatch_gc_reclassify_artifact(
                    report, item, class_name, size,
                )
            else:
                lineage_records = [fresh_by_id[did] for did in fresh_ids]
                fresh_probe = _sweep_identity_snapshot_probe(
                    lineage_records, identity_probe, deadline=deadline,
                )
                fresh_eval = {
                    did: _evaluate_record(fresh_by_id[did], now=now, probe=fresh_probe)
                    for did in fresh_ids
                }
                current = _candidate(
                    item["refs"], fresh_by_id, fresh_eval, fresh_children,
                    minimum_age=_minimum_age_for_artifact(item["kind"]),
                )
            if current is None:
                reason = next(
                    (
                        str(fresh_eval[did].get("reason", "ineligible_owner"))
                        for did in fresh_ids
                        if not fresh_eval[did].get("eligible")
                    ),
                    "eligibility_changed",
                )
                _count_reason(report["kept_reasons"], reason)
                if is_legacy:
                    _count_reason(legacy_kept_reasons, reason)
                elif is_codex_home:
                    protected_codex_home_ids.add(item["path"].name)
                continue
            fresh_ids, fresh_end = current
            if is_codex_home:
                home_gate = _codex_home_eligibility(
                    item["path"], records=home_fresh_records, by_id=fresh_by_id,
                    records_complete=home_records_complete, now=now,
                    identity_probe=identity_probe, probe=fresh_probe,
                    live_home_ids=_nonterminal_codex_home_ids(home_fresh_records),
                    evaluation=fresh_eval.get(item["path"].name),
                    open_snapshot=home_budget_open_snapshot, held_paths=held_paths,
                    deadline=deadline,
                )
                if not home_gate["eligible"]:
                    _count_reason(report["kept_reasons"], home_gate["reason"])
                    protected_codex_home_ids.add(item["path"].name)
                    continue
                size = home_gate["snapshot"]["allocated_bytes"]
            if fresh_end != ended_at:
                pending.append((fresh_end, key, fresh_ids))
                continue
            if item["kind"] in {"scratch", "legacy_scratch", "tail", "home"}:
                group_states = [
                    _scratch_process_group_alive(fresh_by_id[dispatch_id])
                    for dispatch_id in fresh_ids
                    if dispatch_id in fresh_by_id
                ]
                if any(state is True for state in group_states):
                    # A background child may still be using retained artifacts.
                    if is_dispatch_gc:
                        _dispatch_gc_reclassify_artifact(
                            report, item, "skipped_live", size,
                        )
                    else:
                        _count_reason(report["kept_reasons"], "worker_process_group_alive")
                        if is_legacy:
                            _count_reason(legacy_kept_reasons, "worker_process_group_alive")
                    continue
                if any(state is None for state in group_states):
                    if is_dispatch_gc:
                        _dispatch_gc_reclassify_artifact(
                            report, item, "skipped_unknown", size,
                        )
                    else:
                        _count_reason(report["kept_reasons"], "process_group_liveness_indeterminate")
                        if is_legacy:
                            _count_reason(legacy_kept_reasons, "process_group_liveness_indeterminate")
                    continue
            if item["kind"] == "tail":
                if deadline is not None and time.monotonic() >= deadline:
                    deadline_skipped.append(str(item["path"]))
                    break
                try:
                    compressed_size = _compress_dispatch_tail(
                        item["path"], dispatch_dir,
                    )
                except OSError as exc:
                    locked = "tail_locked" in str(exc)
                    _dispatch_gc_reclassify(
                        report, "would_compress",
                        "skipped_live" if locked else "skipped_unknown", size,
                    )
                    if locked:
                        _count_reason(report["kept_reasons"], "tail_locked")
                    report["dispatch_gc"]["errors"].append(
                        f"tail compression failed for {item['path']}: {type(exc).__name__}: {exc}"
                    )
                    continue
                report["dispatch_gc"]["compressed_count"] += 1
                report["dispatch_gc"]["compressed_bytes"] += size
                total = max(0, total - size) + compressed_size
                continue
            if item["kind"] == "scratch":
                if deadline is not None and time.monotonic() >= deadline:
                    deadline_skipped.append(str(item["path"]))
                    break
                safe, reason = _dispatch_gc_path_safe(item["path"], dispatch_dir)
                if not safe:
                    if is_dispatch_gc:
                        _dispatch_gc_reclassify_artifact(
                            report, item, "skipped_unsafe", size,
                        )
                    report["dispatch_gc"]["errors"].append(
                        f"scratch removal refused for {item['path']}: {reason}"
                    )
                    continue
            if deadline is not None and time.monotonic() >= deadline:
                deadline_skipped.append(str(item["path"]))
                break
            removed = (
                _remove_dispatch_scratch(item["path"], dispatch_dir)
                if item["kind"] == "scratch"
                else _delete_artifact(item["path"], item["kind"])
            )
            if removed:
                if is_legacy:
                    legacy_deleted_count += 1
                    legacy_deleted_bytes += size
                else:
                    deleted_count += 1
                    deleted_bytes += size
                    total = max(0, total - int(item["bytes"] or 0))
                    if item["kind"] == "scratch":
                        scratch_deleted_count += 1
                        scratch_deleted_bytes += size
                        if is_dispatch_gc:
                            report["dispatch_gc"]["removed_count"] += 1
            elif is_legacy:
                _count_reason(legacy_kept_reasons, "changed_before_delete")
            elif is_dispatch_gc:
                _dispatch_gc_reclassify_artifact(
                    report,
                    item,
                    "skipped_unsafe" if item["kind"] == "scratch" else "skipped_unknown",
                    size,
                )
                report["dispatch_gc"]["errors"].append(
                    f"dispatch artifact changed before action: {item['path']}"
                )
            elif is_codex_home:
                _count_reason(report["kept_reasons"], "changed_before_delete")
                protected_codex_home_ids.add(item["path"].name)
        finally:
            _release_locks(fds)
    _release_locks(home_budget_preheld_fds)
    report.update(
        total_allocated_bytes=total, deleted_count=deleted_count,
        deleted_allocated_bytes=deleted_bytes,
        scratch_deleted_count=scratch_deleted_count,
        scratch_deleted_bytes=scratch_deleted_bytes,
        legacy_deleted_count=legacy_deleted_count,
        legacy_deleted_bytes=legacy_deleted_bytes,
        files_walked=walked_files[0],
        budget_met=measurable and total <= DISPATCH_ARTIFACT_BUDGET_BYTES,
    )
    if deadline is not None and time.monotonic() >= deadline:
        report["deadline_skipped"] = deadline_skipped or [
            str(artifacts[key]["path"])
            for _ended_at, key, _ids in pending
        ] or ["remaining dispatch artifact candidates"]
    else:
        report["deadline_skipped"] = []
    if timings is not None:
        elapsed = time.monotonic() - started
        cpu_elapsed = time.process_time() - cpu_started
        deletion_elapsed = time.monotonic() - deletion_started
        deletion_cpu_elapsed = time.process_time() - deletion_cpu_started
        timings["legacy_scratch"] = legacy_elapsed
        timings["dispatch_artifacts_scan"] = max(
            0.0, elapsed - legacy_elapsed - deletion_elapsed,
        )
        timings["deletion"] = deletion_elapsed
        cpu = timings.setdefault("cpu", {})
        cpu["legacy_scratch"] = legacy_cpu_elapsed
        cpu["dispatch_artifacts_scan"] = max(
            0.0, cpu_elapsed - legacy_cpu_elapsed - deletion_cpu_elapsed,
        )
        cpu["deletion"] = deletion_cpu_elapsed
        timings["total"] = elapsed
        cpu["total"] = cpu_elapsed
    return report

def _project_roots(
    records: list[dict[str, Any]], errors: list[str], *,
    records_by_root: dict[Path, set[str]] | None = None,
    root_error_dispatch_ids: set[str] | None = None,
    deadline: float | None = None,
) -> set[Path]:
    import goalflight_task
    roots = {record["project_root"] for record in records if isinstance(record.get("project_root"), str) and record["project_root"]}
    record_ids_by_input: dict[str, set[str]] = {}
    for record in records:
        raw_root = record.get("project_root")
        dispatch_id = record.get("dispatch_id")
        if isinstance(raw_root, str) and raw_root.strip() and isinstance(dispatch_id, str):
            key = os.path.normcase(os.path.abspath(os.path.expanduser(raw_root)))
            record_ids_by_input.setdefault(key, set()).add(dispatch_id)

    def mark_root_error(raw_root: str) -> None:
        if root_error_dispatch_ids is None:
            return
        key = os.path.normcase(os.path.abspath(os.path.expanduser(raw_root)))
        root_error_dispatch_ids.update(record_ids_by_input.get(key, set()))

    try:
        roots.update(row["project_root"] for row in goalflight_task.read_project_registry() if isinstance(row.get("project_root"), str) and row["project_root"])
    except Exception as exc:
        errors.append(f"project registry unreadable: {type(exc).__name__}: {exc}")
    resolved: dict[tuple[int, int], Path] = {}
    for raw in roots:
        if deadline is not None and time.monotonic() >= deadline:
            break
        input_key = os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
        record_ids = record_ids_by_input.get(input_key, set())
        try:
            candidate = Path(raw).expanduser()
            if goalflight_fs.path_presence(candidate) == "absent":
                errors.append(f"{candidate}: project root is missing")
                mark_root_error(raw)
                continue
            git_env = os.environ.copy()
            git_env["GIT_OPTIONAL_LOCKS"] = "0"
            proc = goalflight_subprocess.run_with_deadline(
                [
                    "git", "-C", str(candidate), "-c", "core.fsmonitor=false",
                    "rev-parse", "--path-format=absolute", "--show-toplevel",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=git_env,
                check=False,
                timeout=_PROJECT_ROOT_GIT_TIMEOUT_SECONDS,
                deadline=deadline,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{raw}: project root canonicalization failed: {type(exc).__name__}: {exc}")
            mark_root_error(raw)
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            detail = (proc.stderr or proc.stdout).strip() or f"git exited {proc.returncode}"
            errors.append(f"{raw}: project root canonicalization failed: {detail}")
            mark_root_error(raw)
            continue
        try:
            path = Path(proc.stdout.strip())
            info = path.stat()
            if not stat_module.S_ISDIR(info.st_mode):
                errors.append(f"{path}: canonical project root is not a directory")
                mark_root_error(raw)
                continue
            canonical_root = resolved.setdefault((info.st_dev, info.st_ino), path)
            if records_by_root is not None:
                records_by_root.setdefault(canonical_root, set()).update(record_ids)
        except OSError as exc:
            errors.append(
                f"{path}: canonical project root stat failed: {type(exc).__name__}: {exc}"
            )
            mark_root_error(raw)
            continue
    return set(resolved.values())

def _expire_trace_archives(project_root: Path, *, now: dt.datetime) -> int:
    root = project_root / "docs-private" / "traces"
    if root.is_symlink() or not root.is_dir():
        return 0
    removed = 0
    for bucket in root.iterdir():
        if bucket.is_symlink() or not bucket.is_dir():
            continue
        try:
            archived = dt.datetime.strptime(bucket.name, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if now - archived <= dt.timedelta(days=TRACE_RETENTION_DAYS):
            continue
        try:
            shutil.rmtree(bucket)
        except OSError:
            continue
        removed += 1
    return removed

def _prune_task_store_snapshots() -> int:
    import goalflight_task
    stores = goalflight_task.resolve_state_base_dir() / "task-stores"
    if stores.is_symlink() or not stores.is_dir():
        return 0
    removed = 0
    for store in stores.iterdir():
        if store.is_symlink() or not store.is_dir():
            continue
        log_dir = store / "docs-private" / "log"
        if log_dir.is_dir() and not log_dir.is_symlink():
            removed += goalflight_task.prune_task_store_snapshots(log_dir)
    return removed


@contextlib.contextmanager
def _measure_external_commands():
    commands = {
        name: {"calls": 0, "wall_seconds": 0.0}
        for name in ("ps", "lsof", "git")
    }
    lock = threading.Lock()
    original_run = subprocess.run
    original_bounded_run = goalflight_subprocess.run_with_deadline

    def measure_run(original, args, positional, kwargs):
        if isinstance(args, (list, tuple)) and args:
            executable = os.fsdecode(args[0])
        elif isinstance(args, str):
            executable = args.split(None, 1)[0] if args.split(None, 1) else ""
        else:
            executable = ""
        name = Path(executable).name
        if name not in commands:
            return original(args, *positional, **kwargs)
        started = time.monotonic()
        try:
            return original(args, *positional, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            with lock:
                commands[name]["calls"] += 1
                commands[name]["wall_seconds"] += elapsed

    def measured_run(args, *positional, **kwargs):
        return measure_run(original_run, args, positional, kwargs)

    def measured_bounded_run(args, *positional, **kwargs):
        if kwargs.get("deadline") is None:
            return original_bounded_run(args, *positional, **kwargs)
        return measure_run(original_bounded_run, args, positional, kwargs)

    subprocess.run = measured_run
    goalflight_subprocess.run_with_deadline = measured_bounded_run
    try:
        yield commands
    finally:
        subprocess.run = original_run
        goalflight_subprocess.run_with_deadline = original_bounded_run


def _children_cpu_seconds() -> float | None:
    if resource is None:
        return None
    try:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (AttributeError, OSError, ValueError):
        return None
    return float(usage.ru_utime + usage.ru_stime)


def _run_sweep_body_impl(
    *,
    homes_dir: Path,
    ledger_dir: Path,
    now: dt.datetime,
    identity_probe: IdentityProbe,
    on_retention_finished: Callable[[], None] | None = None,
    measurements: dict[str, Any],
    dispatch_gc_apply: bool = False,
    scratch_days: int = DISPATCH_GC_SCRATCH_DAYS,
    tail_days: int = DISPATCH_GC_TAIL_DAYS,
    scratch_expire_resumable_days: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    sweep_started = time.monotonic()
    sweep_cpu_started = time.process_time()
    import goalflight_worktree_gc
    import goalflight_worktree_pool
    import goalflight_seat_dedupe
    timings: dict[str, Any] = {"cpu": {}}
    cwd_snapshot_count = 0
    fresh_cwd_snapshot_count = 0
    process_cwd_census = None
    fresh_process_cwd_census = None
    if deadline is not None:
        cwd_snapshot: list[tuple[int, str]] | None = None
        cwd_snapshot_at: float | None = None

        def process_cwd_census() -> list[tuple[int, str]] | None:
            nonlocal cwd_snapshot, cwd_snapshot_at, cwd_snapshot_count
            current = time.monotonic()
            if current >= deadline:
                return None
            if (
                cwd_snapshot_at is None
                or current - cwd_snapshot_at > _SWEEP_CWD_SNAPSHOT_MAX_AGE_SECONDS
            ):
                timeout_s = min(30.0, deadline - current)
                cwd_snapshot = goalflight_worktree_pool._process_cwd_snapshot(
                    timeout_s=timeout_s,
                    deadline=deadline,
                )
                cwd_snapshot_at = time.monotonic()
                cwd_snapshot_count += 1
            return cwd_snapshot

        def fresh_process_cwd_census() -> list[tuple[int, str]] | None:
            nonlocal fresh_cwd_snapshot_count
            current = time.monotonic()
            if current >= deadline:
                fresh_cwd_snapshot_count += 1
                return None
            fresh_cwd_snapshot_count += 1
            return goalflight_worktree_pool._process_cwd_snapshot(
                timeout_s=min(30.0, deadline - current),
                deadline=deadline,
            )

    artifacts = _run_dispatch_retention(
        homes_dir=homes_dir, ledger_dir=ledger_dir, now=now, identity_probe=identity_probe,
        timings=timings, dispatch_gc_apply=dispatch_gc_apply,
        scratch_days=scratch_days, tail_days=tail_days,
        scratch_expire_resumable_days=scratch_expire_resumable_days,
        include_housekeeping=True,
        deadline=deadline,
    )
    ledger_started = time.monotonic()
    deadline_skipped: list[str] = list(artifacts.get("deadline_skipped", []))
    if _deadline_reached(deadline):
        gc_ledger_index = goalflight_worktree_gc.LedgerIndex(
            [], ["sweep deadline reached before worktree GC scan"],
        )
        records: list[dict[str, Any]] = []
        deadline_skipped.extend(("worktree roots", "read-only pools", "worktree GC", "seat dedupe"))
    else:
        gc_ledger_index = goalflight_worktree_gc.ledger_index_for_dir(
            ledger_dir, deadline=deadline,
        )
        records = list(gc_ledger_index.records)
    measurements["second_ledger_read"] = {
        "calls": 1,
        "wall_seconds": time.monotonic() - ledger_started,
        "records": len(records),
        "unreadable": len(gc_ledger_index.unreadable),
    }
    errors = []
    roots_started = time.monotonic()
    roots = sorted(_project_roots(records, errors, deadline=deadline), key=os.fspath)
    measurements["project_root_discovery"] = {
        "calls": 1,
        "wall_seconds": time.monotonic() - roots_started,
        "roots": len(roots),
    }
    traces = pools = worktree_gc = 0
    worktree_gc_removals: list[str] = []
    loop_max: dict[str, tuple[float, str]] = {
        name: (0.0, "") for name in ("traces", "read_only", "gc")
    }
    for name in ("traces", "read_only", "gc", "snapshots", "dedupe"):
        timings[name] = 0.0
        timings["cpu"][name] = 0.0
    for root_index, root in enumerate(roots):
        if _deadline_reached(deadline):
            deadline_skipped.extend(
                f"project root {path} and its remaining sweep stages"
                for path in roots[root_index:]
            )
            break
        stage_started = time.monotonic()
        stage_cpu_started = time.process_time()
        traces += _expire_trace_archives(root, now=now)
        stage_elapsed = time.monotonic() - stage_started
        stage_cpu_elapsed = time.process_time() - stage_cpu_started
        timings["traces"] += stage_elapsed
        timings["cpu"]["traces"] += stage_cpu_elapsed
        loop_max["traces"] = max(loop_max.get("traces", (0.0, "")), (stage_elapsed, str(root)))
        if _deadline_reached(deadline):
            deadline_skipped.append(f"read-only pool and worktree GC for {root}")
            continue
        if not (root / ".git").exists():
            continue
        stage_started = time.monotonic()
        stage_cpu_started = time.process_time()
        try:
            read_only_kwargs = {}
            if process_cwd_census is not None:
                read_only_kwargs["process_cwd_census"] = process_cwd_census
            if fresh_process_cwd_census is not None:
                read_only_kwargs["fresh_process_cwd_census"] = fresh_process_cwd_census
            goalflight_worktree_pool.reap_read_only_worktrees(
                root, deadline=deadline, **read_only_kwargs,
            )
            pools += 1
        except Exception as exc:
            errors.append(f"{root}: read-only pool: {type(exc).__name__}: {exc}")
        stage_elapsed = time.monotonic() - stage_started
        stage_cpu_elapsed = time.process_time() - stage_cpu_started
        timings["read_only"] += stage_elapsed
        timings["cpu"]["read_only"] += stage_cpu_elapsed
        loop_max["read_only"] = max(loop_max.get("read_only", (0.0, "")), (stage_elapsed, str(root)))
        if _deadline_reached(deadline):
            deadline_skipped.append(f"worktree GC for {root}")
            continue
        stage_started = time.monotonic()
        stage_cpu_started = time.process_time()
        try:
            gc_stdout = io.StringIO()
            gc_stderr = io.StringIO()
            with contextlib.redirect_stdout(gc_stdout), contextlib.redirect_stderr(gc_stderr):
                result = goalflight_worktree_gc.main(
                    [
                        str(root), "--ledger-dir", str(ledger_dir),
                        "--apply", "--json",
                    ],
                    goalflight_managed_only=True,
                    _initial_ledger_index=gc_ledger_index,
                    deadline=deadline,
                    **(
                        {"_process_cwd_census": process_cwd_census}
                        if process_cwd_census is not None else {}
                    ),
                    **(
                        {"_fresh_process_cwd_census": fresh_process_cwd_census}
                        if fresh_process_cwd_census is not None else {}
                    ),
                )
            try:
                report = json.loads(gc_stdout.getvalue())
            except json.JSONDecodeError:
                report = {}
            worktree_gc_removals.extend(
                entry["path"]
                for entry in report.get("entries", [])
                if entry.get("outcome") in {"removed", "pruned"}
                and isinstance(entry.get("path"), str)
            )
            if result == 0:
                worktree_gc += 1
            else:
                details = []
                for entry in report.get("entries", []):
                    if entry.get("outcome") == "failed":
                        detail = entry.get("error") or entry.get("reason") or "failed without detail"
                        details.append(f"{entry.get('path', root)}: {detail}")
                if not details and gc_stderr.getvalue().strip():
                    details.append(gc_stderr.getvalue().strip())
                if not details:
                    details.append(f"exited {result}")
                errors.extend(f"{root}: worktree GC: {detail}" for detail in details)
        except Exception as exc:
            errors.append(f"{root}: worktree GC: {type(exc).__name__}: {exc}")
        stage_elapsed = time.monotonic() - stage_started
        stage_cpu_elapsed = time.process_time() - stage_cpu_started
        timings["gc"] += stage_elapsed
        timings["cpu"]["gc"] += stage_cpu_elapsed
        loop_max["gc"] = max(loop_max.get("gc", (0.0, "")), (stage_elapsed, str(root)))
        if _deadline_reached(deadline):
            deadline_skipped.append(f"remaining worktree GC candidates for {root}")
    if not _deadline_reached(deadline):
        stage_started = time.monotonic()
        stage_cpu_started = time.process_time()
        snapshots = _prune_task_store_snapshots()
        timings["snapshots"] = time.monotonic() - stage_started
        timings["cpu"]["snapshots"] = time.process_time() - stage_cpu_started
    else:
        snapshots = 0
        timings["snapshots"] = 0.0
        timings["cpu"]["snapshots"] = 0.0
        deadline_skipped.append("task snapshots")

    def dedupe_identity_probe(record: dict[str, Any], deadline: float) -> tuple[bool, str]:
        if identity_probe is goalflight_ledger.identity_matches:
            return goalflight_ledger._identity_matches_before_deadline(record, deadline)
        return identity_probe(record)

    dedupe_identity_snapshot = None
    if identity_probe is goalflight_ledger.identity_matches:
        dedupe_identity_snapshot = lambda rows, deadline: _identity_snapshot_probe(
            rows, identity_probe, deadline=deadline,
        )

    if on_retention_finished is not None and not _deadline_reached(deadline):
        on_retention_finished()

    dedupe_project_timings: dict[Path, float] = {}
    stage_started = time.monotonic()
    stage_cpu_started = time.process_time()
    if not _deadline_reached(deadline):
        copy_on_write = goalflight_seat_dedupe._dedupe_pools(
            roots,
            records=records,
            identity_probe=dedupe_identity_probe,
            identity_inactive=_identity_inactive,
            project_timings=dedupe_project_timings,
            identity_snapshot_probe=dedupe_identity_snapshot,
            deadline=deadline,
        )
    else:
        copy_on_write = {
            "seats_scanned": 0, "files_cloned": 0, "bytes_shared": 0,
            "errors": [], "skip_counts": goalflight_seat_dedupe._new_skip_counts(),
        }
        deadline_skipped.append("seat dedupe")
    timings["dedupe"] = time.monotonic() - stage_started
    timings["cpu"]["dedupe"] = time.process_time() - stage_cpu_started
    if dedupe_project_timings:
        dedupe_root, dedupe_duration = max(
            dedupe_project_timings.items(), key=lambda item: item[1],
        )
        loop_max["dedupe"] = (dedupe_duration, str(dedupe_root))
    else:
        loop_max["dedupe"] = (0.0, "")
    timings.update({name: duration for name, (duration, _root) in loop_max.items()})
    timings.setdefault("dispatch_artifacts_scan", 0.0)
    timings.setdefault("legacy_scratch", 0.0)
    timings["total"] = time.monotonic() - sweep_started
    timings["cpu"]["total"] = time.process_time() - sweep_cpu_started
    measurements["cwd_snapshot_count"] = cwd_snapshot_count
    measurements["fresh_cwd_snapshot_count"] = fresh_cwd_snapshot_count
    timings["projects"] = {name: root for name, (_duration, root) in loop_max.items()}
    errors.extend(copy_on_write["errors"])
    deadline_reached = _deadline_reached(deadline)
    if deadline_reached and not deadline_skipped:
        deadline_skipped.append("remaining retention candidates")
    return {
        "artifacts": artifacts,
        "trace_buckets": traces,
        "read_only_pools": pools,
        "worktree_gc": worktree_gc,
        "worktree_gc_removals": worktree_gc_removals,
        "copy_on_write": {
            key: copy_on_write[key]
            for key in ("seats_scanned", "files_cloned", "bytes_shared")
        },
        "copy_on_write_reasons": copy_on_write.get(
            "skip_counts", {"seats": {}, "files": {}},
        ),
        "snapshots": snapshots,
        "cleanup_errors": errors,
        "timings": timings,
        "deadline_reached": deadline_reached,
        "deadline_skipped": deadline_skipped,
    }


def _run_sweep_body(
    *,
    homes_dir: Path,
    ledger_dir: Path,
    now: dt.datetime,
    identity_probe: IdentityProbe,
    on_retention_finished: Callable[[], None] | None = None,
    dispatch_gc_apply: bool = False,
    scratch_days: int = DISPATCH_GC_SCRATCH_DAYS,
    tail_days: int = DISPATCH_GC_TAIL_DAYS,
    scratch_expire_resumable_days: int | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    measurements: dict[str, Any] = {}
    children_cpu_before = _children_cpu_seconds()
    with _measure_external_commands() as commands:
        result = _run_sweep_body_impl(
            homes_dir=homes_dir,
            ledger_dir=ledger_dir,
            now=now,
            identity_probe=identity_probe,
            on_retention_finished=on_retention_finished,
            measurements=measurements,
            dispatch_gc_apply=dispatch_gc_apply,
            scratch_days=scratch_days,
            tail_days=tail_days,
            scratch_expire_resumable_days=scratch_expire_resumable_days,
            deadline=deadline,
        )
    children_cpu_after = _children_cpu_seconds()
    measurements["external_commands"] = commands
    measurements["children_cpu_seconds"] = (
        max(0.0, children_cpu_after - children_cpu_before)
        if children_cpu_before is not None and children_cpu_after is not None
        else None
    )
    result["_measurements"] = measurements
    return result

def run_retention_sweep(
    *, homes_dir: Path, ledger_dir: Path, now: dt.datetime | None = None,
    identity_probe: IdentityProbe | None = None, dispatch_gc_apply: bool = False,
    force: bool = False,
    scratch_days: int = DISPATCH_GC_SCRATCH_DAYS,
    tail_days: int = DISPATCH_GC_TAIL_DAYS,
    scratch_expire_resumable_days: int | None = None,
) -> dict[str, Any]:
    """Run the full sweep under one nonblocking, hourly flock."""
    if fcntl is None:
        return {"ran": False, "skipped": "flock_unavailable"}
    lock = goalflight_ledger.state_dir() / "retention-sweep.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    existed = lock.exists()
    current = now or dt.datetime.now(dt.timezone.utc)
    current = current.replace(tzinfo=dt.timezone.utc) if current.tzinfo is None else current.astimezone(dt.timezone.utc)
    with lock.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ran": False, "skipped": "already_running"}
        if not force and existed and time.time() - lock.stat().st_mtime < SWEEP_INTERVAL_S:
            return {"ran": False, "skipped": "recent_sweep"}
        def retention_finished() -> None:
            os.utime(lock, None)

        deadline = time.monotonic() + _SWEEP_TIMEOUT_SECONDS
        result = _run_sweep_body(
                homes_dir=homes_dir,
                ledger_dir=ledger_dir,
                now=current,
                identity_probe=identity_probe or goalflight_ledger.identity_matches,
                on_retention_finished=retention_finished,
                dispatch_gc_apply=dispatch_gc_apply,
                scratch_days=scratch_days,
                tail_days=tail_days,
                scratch_expire_resumable_days=scratch_expire_resumable_days,
                deadline=deadline,
            )
        if result.get("deadline_reached"):
            os.utime(lock, (0, 0))
        return {"ran": True, **result}

def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB": return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def _format_dispatch_gc_report(report: dict[str, Any]) -> str:
    resumable_days = report.get("scratch_expire_resumable_days")
    lines = [
        f"dispatch artifact retention ({report.get('mode', 'dry-run')}): "
        f"scratch_days={report.get('scratch_days', DISPATCH_GC_SCRATCH_DAYS)} "
        f"tail_days={report.get('tail_days', DISPATCH_GC_TAIL_DAYS)} "
        f"scratch_expire_resumable_days={resumable_days if resumable_days is not None else 'off'}",
    ]
    for name, values in report.get("classes", {}).items():
        lines.append(
            f"  {name.replace('_', '-')}: count={values['count']} "
            f"bytes={_format_bytes(values['bytes'])}"
        )
    lines.append(f"  mtime-fallback: count={report.get('mtime_fallback_count', 0)}")
    if report.get("removed_count") or report.get("compressed_count"):
        lines.append(
            f"  applied: removed={report.get('removed_count', 0)} "
            f"compressed={report.get('compressed_count', 0)} "
            f"source-bytes={_format_bytes(report.get('compressed_bytes', 0))}"
        )
    lines.extend(f"  error: {error}" for error in report.get("errors", []))
    return "\n".join(lines)

def _format_scratch_summary(payload: dict[str, Any]) -> str:
    low_space = payload.get("scratch_low_space")
    floor_state = "unknown" if low_space is None else "below" if low_space else "clear"
    free_bytes = payload.get("scratch_free_bytes")
    free_text = _format_bytes(free_bytes) if isinstance(free_bytes, int) else "unknown"
    idle_hours = payload.get("scratch_minimum_idle_seconds", 0) / 3600
    warnings = payload.get("scratch_size_warnings", {})
    large_text = ",".join(
        f"{dispatch_id}:{_format_bytes(size)}"
        for dispatch_id, size in sorted(warnings.items())
    ) or "none"
    kept = json.dumps(payload.get("scratch_kept_reasons", {}), sort_keys=True)
    return (
        f"floor={floor_state} free={free_text}/"
        f"{_format_bytes(payload.get('scratch_free_floor_bytes', DISPATCH_SCRATCH_FREE_SPACE_FLOOR_BYTES))} "
        f"idle={idle_hours:.1f}h large={large_text} kept={kept}"
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report or run Goal Flight's bounded retention sweep.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report actions without deleting or compressing")
    mode.add_argument("--delete", action="store_true", help="run the scheduled bounded sweep")
    mode.add_argument("--apply", action="store_true", help="apply the full retention sweep, including dispatch scratch/tails")
    parser.add_argument("--homes-dir", type=Path)
    parser.add_argument("--ledger-dir", type=Path)
    parser.add_argument("--scratch-days", type=int, default=DISPATCH_GC_SCRATCH_DAYS)
    parser.add_argument("--tail-days", type=int, default=DISPATCH_GC_TAIL_DAYS)
    parser.add_argument(
        "--scratch-expire-resumable-days", type=int,
        default=DISPATCH_GC_RESUMABLE_SCRATCH_DAYS,
        help=(
            "expire inactive, unpinned resumable scratch after this many days "
            f"(default: {DISPATCH_GC_RESUMABLE_SCRATCH_DAYS})"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser

def _clear_darwin_background_role() -> None:
    global _darwin_priority_warning_logged
    try:
        if sys.platform != "darwin":
            raise OSError("darwin process priority is unavailable on this platform")
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        setpriority = libc.setpriority
        setpriority.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
        setpriority.restype = ctypes.c_int
        result = setpriority(4, 0, 0)  # PRIO_DARWIN_PROCESS
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    except Exception as exc:
        if not _darwin_priority_warning_logged:
            print(f"retention sweep could not clear darwin background role: {exc}", file=sys.stderr)
            _darwin_priority_warning_logged = True

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_apply = args.delete or args.apply
    if (
        args.scratch_days < 0 or args.tail_days < 0
        or (
            args.scratch_expire_resumable_days is not None
            and args.scratch_expire_resumable_days < 0
        )
    ):
        print("retention days must be nonnegative", file=sys.stderr)
        return 2
    homes_dir = args.homes_dir or default_homes_dir()
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)
    pass_started = time.monotonic()
    _clear_darwin_background_role()
    if run_apply:
        try:
            sweep = run_retention_sweep(
                homes_dir=homes_dir,
                ledger_dir=ledger_dir,
                dispatch_gc_apply=args.apply or periodic_dispatch_gc_enabled(),
                force=args.apply,
                scratch_days=args.scratch_days,
                tail_days=args.tail_days,
                scratch_expire_resumable_days=args.scratch_expire_resumable_days,
            )
        except OSError as exc:
            print(f"goalflight retention sweep could not start: {exc}", file=sys.stderr)
            return 1
        if not sweep.get("ran"):
            payload = {"schema": SCHEMA, "mode": "skipped", "budget_bytes": DISPATCH_ARTIFACT_BUDGET_BYTES, "skipped": sweep["skipped"]}
            print(f"retention sweep skipped: {sweep['skipped']}", file=sys.stderr)
        else:
            if sweep.get("deadline_reached"):
                skipped = json.dumps(sweep.get("deadline_skipped", []), sort_keys=True)
                print(f"retention sweep deadline reached; skipped={skipped}", file=sys.stderr)
            payload = {
                **sweep["artifacts"],
                "timings": sweep.get("timings", {}),
            }
            cow = sweep["copy_on_write"]
            duration = sweep.get("timings", {}).get("total", time.monotonic() - pass_started)
            import goalflight_seat_dedupe
            timings = sweep.get("timings", {})
            cpu_timings = timings.get("cpu", {})
            phase_names = (
                "dispatch_artifacts_scan", "legacy_scratch", "deletion", "gc",
                "read_only", "dedupe", "traces", "snapshots",
            )
            phase_wall = ",".join(
                f"{name}:{timings.get(name, 0.0):.1f}s" for name in phase_names
            )
            phase_cpu = ",".join(
                f"{name}:{cpu_timings.get(name, 0.0):.1f}s" for name in phase_names
            )
            codex_maintenance = payload.get("codex_home_maintenance", {})
            grok_archive = payload.get("grok_orphan_archive", {})
            codex_keep_reasons = json.dumps(codex_maintenance.get("kept_reasons", {}), sort_keys=True)
            budget_keep_reasons = json.dumps(payload.get("kept_reasons", {}), sort_keys=True)
            print(f"retention sweep: duration={duration:.1f}s phase_wall={phase_wall} phase_cpu={phase_cpu} files_walked={payload.get('files_walked', 0)} deleted={payload['deleted_count']} reclaimed={_format_bytes(payload['deleted_allocated_bytes'])} total={_format_bytes(payload['total_allocated_bytes'])} budget_met={payload.get('budget_met', False)} budget_keep_reasons={budget_keep_reasons} scratch_deleted={payload['scratch_deleted_count']}:{_format_bytes(payload['scratch_deleted_bytes'])} legacy_deleted={payload['legacy_deleted_count']}:{_format_bytes(payload['legacy_deleted_bytes'])} legacy_kept={json.dumps(payload['legacy_kept_reasons'], sort_keys=True)} codex_trimmed={codex_maintenance.get('trimmed_count', 0)}:{_format_bytes(codex_maintenance.get('trimmed_bytes', 0))} codex_deleted={codex_maintenance.get('deleted_count', 0)}:{_format_bytes(codex_maintenance.get('deleted_bytes', 0))} codex_keep_reasons={codex_keep_reasons} grok_archived={grok_archive.get('archived_count', 0)}:{_format_bytes(grok_archive.get('archived_bytes', 0))} grok_archive_files={_format_bytes(grok_archive.get('archive_file_bytes', 0))} grok_archive_refused={grok_archive.get('refused', False)} traces={sweep['trace_buckets']} snapshots={sweep['snapshots']} worktree_gc={sweep['worktree_gc']} read_only_pool={sweep['read_only_pools']} cow_seats={cow['seats_scanned']} cow_files={cow['files_cloned']} cow_bytes={_format_bytes(cow['bytes_shared'])} errors={len(sweep['cleanup_errors'])}", file=sys.stderr)
            print(f"retention sweep scratch: {_format_scratch_summary(payload)}", file=sys.stderr)
            if payload.get("dispatch_gc"):
                print(_format_dispatch_gc_report(payload["dispatch_gc"]), file=sys.stderr)
            measurements = sweep.get("_measurements", {})
            if measurements:
                ledger_read = measurements.get("second_ledger_read", {})
                root_discovery = measurements.get("project_root_discovery", {})
                commands = measurements.get("external_commands", {})
                command_summary = ",".join(
                    f"{name}:{commands.get(name, {}).get('calls', 0)}"
                    f"/{commands.get(name, {}).get('wall_seconds', 0.0):.1f}s"
                    for name in ("ps", "lsof", "git")
                )
                child_cpu = measurements.get("children_cpu_seconds")
                child_cpu_text = (
                    f"{child_cpu:.1f}s" if child_cpu is not None else "unavailable"
                )
                print(
                    "retention sweep measurements: "
                    f"second_ledger_read={ledger_read.get('calls', 0)}"
                    f"/{ledger_read.get('wall_seconds', 0.0):.1f}s"
                    f" records={ledger_read.get('records', 0)}"
                    f" unreadable={ledger_read.get('unreadable', 0)} "
                    f"project_root_discovery={root_discovery.get('calls', 0)}"
                    f"/{root_discovery.get('wall_seconds', 0.0):.1f}s"
                    f" roots={root_discovery.get('roots', 0)} "
                    f"commands={command_summary} children_cpu={child_cpu_text}",
                    file=sys.stderr,
                )
            reasons = sweep.get("copy_on_write_reasons", {})
            seat_reasons = " ".join(
                f"{name}={reasons.get('seats', {}).get(name, 0)}"
                for name in goalflight_seat_dedupe._SEAT_SKIP_REASONS
            )
            file_reasons = " ".join(
                f"{name}={reasons.get('files', {}).get(name, 0)}"
                for name in goalflight_seat_dedupe._FILE_SKIP_REASONS
            )
            print(
                f"seat dedupe reasons: seats{{{seat_reasons}}} files{{{file_reasons}}}",
                file=sys.stderr,
            )
            for path in sweep["worktree_gc_removals"]:
                print(f"retention sweep worktree GC removed: {path}", file=sys.stderr)
            for error in sweep["cleanup_errors"]:
                print(f"retention sweep cleanup error: {error}", file=sys.stderr)
            timing_parts = []
            for name in phase_names:
                project = timings.get("projects", {}).get(name)
                suffix = f"@{project}" if project else ""
                timing_parts.append(
                    f"{name}={timings.get(name, 0.0):.1f}s/"
                    f"{cpu_timings.get(name, 0.0):.1f}s{suffix}"
                )
            timing_parts.append(f"total={timings.get('total', 0.0):.1f}s")
            print(f"retention sweep timing: {' '.join(timing_parts)}", file=sys.stderr)
    else:
        now = dt.datetime.now(dt.timezone.utc)
        timings: dict[str, Any] = {}
        payload = _run_dispatch_retention(
            homes_dir=homes_dir, ledger_dir=ledger_dir, now=now,
            identity_probe=goalflight_ledger.identity_matches, delete=False,
            timings=timings, dispatch_gc_apply=False,
            scratch_days=args.scratch_days, tail_days=args.tail_days,
            scratch_expire_resumable_days=args.scratch_expire_resumable_days,
            include_housekeeping=True,
        )
        payload["timings"] = timings
        duration = time.monotonic() - pass_started
        print(
            f"retention report: duration={duration:.1f}s files_walked={payload.get('files_walked', 0)}",
            file=sys.stderr,
        )
        print(f"retention report scratch: {_format_scratch_summary(payload)}", file=sys.stderr)
    if args.json:
        if not run_apply:
            print(
                f"retention dry-run: scratch_deleted=0:0 legacy_deleted=0:0 "
                f"scratch_would_delete={payload.get('scratch_would_delete_count', 0)}:"
                f"{_format_bytes(payload.get('scratch_would_delete_bytes', 0))} "
                f"legacy_would_delete={payload.get('legacy_would_delete_count', 0)}:"
                f"{_format_bytes(payload.get('legacy_would_delete_bytes', 0))} "
                f"legacy_kept={json.dumps(payload.get('legacy_kept_reasons', {}), sort_keys=True)} "
                f"codex_would_trim={payload.get('codex_home_maintenance', {}).get('would_trim_count', 0)}:"
                f"{_format_bytes(payload.get('codex_home_maintenance', {}).get('would_trim_bytes', 0))} "
                f"codex_would_delete={payload.get('codex_home_maintenance', {}).get('would_delete_count', 0)}:"
                f"{_format_bytes(payload.get('codex_home_maintenance', {}).get('would_delete_bytes', 0))} "
                f"grok_would_archive={payload.get('grok_orphan_archive', {}).get('would_archive_count', 0)}:"
                f"{_format_bytes(payload.get('grok_orphan_archive', {}).get('would_archive_bytes', 0))} "
                f"grok_archive_refused={payload.get('grok_orphan_archive', {}).get('refused', False)}",
                file=sys.stderr,
            )
        json_payload = {
            key: value for key, value in payload.items()
            if not key.startswith(("scratch_", "legacy_"))
        }
        print(json.dumps(json_payload, indent=2, sort_keys=True, default=str))
    else:
        print(f"mode={payload['mode']} total={_format_bytes(payload.get('total_allocated_bytes', 0))} budget={_format_bytes(payload['budget_bytes'])} eligible={payload.get('eligible_count', 0)} would_delete={payload.get('eligible_allocated_bytes', 0)} scratch_deleted={payload.get('scratch_deleted_count', 0)}:{_format_bytes(payload.get('scratch_deleted_bytes', 0))} legacy_would_delete={payload.get('legacy_would_delete_count', 0)}:{_format_bytes(payload.get('legacy_would_delete_bytes', 0))} legacy_kept={json.dumps(payload.get('legacy_kept_reasons', {}), sort_keys=True)}")
        print(f"scratch { _format_scratch_summary(payload)}")
        if payload.get("dispatch_gc"):
            print(_format_dispatch_gc_report(payload["dispatch_gc"]), file=sys.stderr)
    return 0

if __name__ == "__main__":  # pragma: no cover - exercised by the CLI
    raise SystemExit(main())
