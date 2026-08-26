#!/usr/bin/env python3
"""Kernel-backed liveness ledger for controller wake waiters.

Lockfile names are addresses and identities only.  Their contents are never
read or written: a waiter is live iff its process still holds the file lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import sys
import time
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - documented socket-backend platform.
    fcntl = None  # type: ignore[assignment]

import goalflight_compat


MONITOR_KIND = "monitor"
WATCHDOG_KIND = "watchdog"
WAITER_KINDS = frozenset({"listener", "wait", MONITOR_KIND, WATCHDOG_KIND})
LEASE_KIND = "lease"
LOCK_KINDS = WAITER_KINDS | {LEASE_KIND}
ENTRY_POLL_WINDOW_S = 1.0
ENTRY_POLL_INTERVAL_S = 0.1
_FILE_VERSION = "v3"
_LEGACY_FILE_VERSION = "v2"
_GENERATION_FILE_VERSION = "generation-v1"
_LISTENER_SLOT_FILE_VERSION = "listener-slot-v1"
_RING_STAMP_FILE_VERSION = "ring-stamp-v1"
# v1 could not distinguish claimed from output. v2 made a complete local flush
# durable but mistook it for controller receipt. Rotate each ambiguous address
# so an upgrade re-reports once instead of trusting unacknowledged high-water.
_PENDING_REPORT_FILE_VERSION = "pending-report-v3"
PENDING_REPORT_STATE_SCHEMA = "goalflight.pending-report.v3"
_WATCHDOG_DEATH_REPORT_FILE_VERSION = "watchdog-death-report-v1"
_MONITOR_STATE_FILE_VERSION = "monitor-state-v1"
MONITOR_STATE_SCHEMA = "goalflight.monitor-state.v1"
PERSISTENT_WAKE_TARGET = 3
_OBSERVED_WAITERS_UNSET = object()
_LEASE_EVENT_SCHEMA = "goalflight.lease-generation-event.v1"
# Depth is resilience, not efficiency: one event wakes exactly one slot, so
# the remaining slots are the margin for a controller that forgets to re-arm.
# 4 survives three consecutive missed re-arms. Override with
# GOALFLIGHT_LISTENER_SLOTS; MAX_LISTENER_SLOTS stays 32.
DEFAULT_LISTENER_SLOTS = 4
MAX_LISTENER_SLOTS = 32


class ListenerSlotsFull(BlockingIOError):
    """Every bounded one-shot listener slot is held by a live process."""

    def __init__(self, slots: int) -> None:
        self.slots = slots
        super().__init__(errno.EAGAIN, f"all {slots} listener slots are held")


@dataclass(frozen=True)
class WaiterRecord:
    kind: str
    label_hash: str
    pid: int
    start_hash: str
    instance_id: str
    path: Path
    generation_hash: str | None = None


class PendingReportStateError(RuntimeError):
    """A durable pending-report boundary exists but cannot be interpreted."""


@dataclass(frozen=True)
class PendingReportState:
    phase: str
    positions: dict[str, int]
    cursor_version: int | None
    stream_snapshots: dict[str, str]
    claim_token: str | None
    owner_pid: int | None
    owner_start_token: str | None


def _project_key(project_root: Path | str) -> str:
    resolved = str(Path(project_root).expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:20]


def _label_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]


def _waiter_generation_hash(generation_key: str) -> str:
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("waiter generation key is required")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def controller_session_digest(value: object) -> str | None:
    """Digest a controller capability with the established publication helper."""
    return _label_hash(value) if isinstance(value, str) and value else None


def ledger_base_dir() -> Path:
    explicit = os.environ.get("GOALFLIGHT_WAKE_LEDGER_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    state_home = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return state_home / "goal-flight" / "wake-ledger"


def ledger_dir(project_root: Path | str) -> Path:
    return ledger_base_dir() / _project_key(project_root)


def lease_generation_event_path(project_root: Path | str, *, controller_label: str) -> Path:
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    return ledger_dir(project_root) / f"lease-event-v1.{_label_hash(label)}.json"


def publish_lease_generation_event(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    generation: int,
    state: str,
) -> Path:
    """Publish the small nonce/generation token watched by the cheap beacon tick."""
    nonce = str(lease_nonce or "").strip()
    if not nonce or generation < 1:
        raise ValueError("lease nonce and positive generation are required")
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _LEASE_EVENT_SCHEMA,
        "label": str(controller_label).strip(),
        "nonce": nonce,
        "generation": generation,
        "state": str(state or "").strip(),
    }
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return path


def lease_generation_event_stamp(
    project_root: Path | str,
    *,
    controller_label: str,
) -> tuple[int, int, int] | None:
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return observed.st_ino, observed.st_mtime_ns, observed.st_size


def read_lease_generation_event(
    project_root: Path | str,
    *,
    controller_label: str,
) -> dict[str, object] | None:
    path = lease_generation_event_path(
        project_root,
        controller_label=controller_label,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"lease generation event is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _LEASE_EVENT_SCHEMA:
        raise RuntimeError(f"lease generation event is malformed: {path}")
    return payload


def _parse_waiter_path(path: Path) -> WaiterRecord | None:
    parts = path.name.split(".")
    generation_hash: str | None
    if len(parts) == 8 and parts[0] == _FILE_VERSION and parts[-1] == "lock":
        (
            _version,
            kind,
            label_hash,
            generation_hash,
            raw_pid,
            start_hash,
            instance_id,
            _suffix,
        ) = parts
        if len(generation_hash) != 24:
            return None
    elif (
        len(parts) == 7
        and parts[0] == _LEGACY_FILE_VERSION
        and parts[-1] == "lock"
    ):
        _version, kind, label_hash, raw_pid, start_hash, instance_id, _suffix = parts
        generation_hash = None
    else:
        return None
    if kind not in WAITER_KINDS:
        return None
    if len(label_hash) != 16 or len(start_hash) != 16 or len(instance_id) != 32:
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return WaiterRecord(
        kind,
        label_hash,
        pid,
        start_hash,
        instance_id,
        path,
        generation_hash,
    )


def _start_hash(start_token: object) -> str:
    value = str(start_token or "")
    if not value:
        raise ValueError("held-lock owner start token is required")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _set_cloexec(fd: int) -> None:
    """Make exec drop the witness even on runtimes without O_CLOEXEC."""
    os.set_inheritable(fd, False)
    if fcntl is not None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def _open_flags(*, create_exclusive: bool = False) -> int:
    flags = os.O_RDWR
    if create_exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _open_ledger_directory_path(directory: Path, *, create: bool) -> int:
    if create:
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
    # If writers followed a ledger-leaf symlink that readers refuse, a valid
    # waiter could be written into a permanently UNKNOWN write/read wedge.
    return os.open(directory, _directory_open_flags())


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _lock_nonblocking(fd: int) -> None:
    assert fcntl is not None
    _set_cloexec(fd)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _generation_lock_path(
    project_root: Path | str,
    *,
    kind: str,
    label: str,
    generation_key: str,
) -> Path:
    identity_hash = hashlib.sha256(
        f"{label}\0{generation_key}".encode("utf-8")
    ).hexdigest()[:32]
    return ledger_dir(project_root) / (
        f"{_GENERATION_FILE_VERSION}.{kind}.{_label_hash(label)}.{identity_hash}.lock"
    )


def _listener_slot_lock_path(
    project_root: Path | str,
    *,
    label: str,
    generation_key: str,
    slot: int,
) -> Path:
    generation_hash = hashlib.sha256(generation_key.encode("utf-8")).hexdigest()[:16]
    return ledger_dir(project_root) / (
        f"{_LISTENER_SLOT_FILE_VERSION}.{_label_hash(label)}.{generation_hash}."
        f"listener-slot-{slot}.lock"
    )


def _ring_stamp_path(project_root: Path | str, *, controller_label: str) -> Path:
    return ledger_dir(project_root) / (
        f"{_RING_STAMP_FILE_VERSION}.{_label_hash(controller_label)}.cursor"
    )


def _pending_report_path(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> Path:
    return ledger_dir(project_root) / (
        f"{_PENDING_REPORT_FILE_VERSION}.{_label_hash(controller_label)}."
        f"{_label_hash(lease_nonce)}.claimed"
    )


def _pending_report_lock_path(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> Path:
    return _pending_report_path(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    ).with_suffix(".lock")


def _monitor_state_path(
    project_root: Path | str,
    *,
    controller_label: str,
) -> Path:
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    return ledger_dir(project_root) / (
        f"{_MONITOR_STATE_FILE_VERSION}.{_label_hash(label)}.json"
    )


def monitor_state_stamp(
    project_root: Path | str,
    *,
    controller_label: str,
) -> tuple[int, int, int] | None:
    """Return the atomic state-file identity used to recognize startup leftovers."""
    path = _monitor_state_path(project_root, controller_label=controller_label)
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError:
        return None
    return observed.st_ino, observed.st_mtime_ns, observed.st_size


def _monitor_generation_digest(lease_nonce: str) -> str:
    nonce = str(lease_nonce or "").strip()
    if not nonce:
        raise ValueError("monitor lease nonce is required")
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:24]


def _validate_monitor_timing(heartbeat_s: float, dead_after_s: float) -> tuple[float, float]:
    heartbeat = float(heartbeat_s)
    dead_after = float(dead_after_s)
    if (
        not math.isfinite(heartbeat)
        or heartbeat <= 0
        or not math.isfinite(dead_after)
        or dead_after < heartbeat
    ):
        raise ValueError("monitor timing must be finite and dead-after >= heartbeat")
    return heartbeat, dead_after


def _write_monitor_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        directory_fd = _open_ledger_directory_path(path.parent, create=False)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def activate_monitor_state(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    heartbeat_s: float,
    dead_after_s: float,
    now_epoch: float | None = None,
) -> dict[str, object]:
    heartbeat, dead_after = _validate_monitor_timing(heartbeat_s, dead_after_s)
    now = time.time() if now_epoch is None else float(now_epoch)
    payload: dict[str, object] = {
        "schema": MONITOR_STATE_SCHEMA,
        "generation": _monitor_generation_digest(lease_nonce),
        "activated_at_epoch": now,
        "last_record_at_epoch": None,
        "last_kind": None,
        "heartbeat_s": heartbeat,
        "dead_after_s": dead_after,
        "fault": None,
    }
    _write_monitor_state(
        _monitor_state_path(project_root, controller_label=controller_label),
        payload,
    )
    return payload


def _load_monitor_state(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> dict[str, object] | None:
    path = _monitor_state_path(project_root, controller_label=controller_label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MONITOR_STATE_SCHEMA
        or payload.get("generation") != _monitor_generation_digest(lease_nonce)
    ):
        return None
    return payload


def record_monitor_emit(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    record_kind: str,
    now_epoch: float | None = None,
) -> None:
    payload = _load_monitor_state(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    )
    if payload is None:
        raise RuntimeError("monitor capability state is unavailable")
    payload["last_record_at_epoch"] = (
        time.time() if now_epoch is None else float(now_epoch)
    )
    payload["last_kind"] = str(record_kind or "")[:32]
    payload["fault"] = None
    _write_monitor_state(
        _monitor_state_path(project_root, controller_label=controller_label),
        payload,
    )


def record_monitor_fault(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    reason: str,
    detail: str = "",
    now_epoch: float | None = None,
) -> None:
    payload = _load_monitor_state(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    )
    if payload is None:
        raise RuntimeError("monitor capability state is unavailable")
    payload["fault"] = {
        "reason": str(reason or "unknown")[:80],
        "detail": str(detail or "")[:160],
        "at_epoch": time.time() if now_epoch is None else float(now_epoch),
    }
    _write_monitor_state(
        _monitor_state_path(project_root, controller_label=controller_label),
        payload,
    )


def monitor_status(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    now_epoch: float | None = None,
) -> dict[str, object] | None:
    payload = _load_monitor_state(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    )
    if payload is None:
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    try:
        activated = float(payload["activated_at_epoch"])
        last_raw = payload.get("last_record_at_epoch")
        last_record = float(last_raw) if last_raw is not None else None
        heartbeat, dead_after = _validate_monitor_timing(
            float(payload["heartbeat_s"]),
            float(payload["dead_after_s"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    age = max(0.0, now - (last_record if last_record is not None else activated))
    fault = payload.get("fault")
    state = (
        "fault"
        if isinstance(fault, dict)
        else "stale"
        if age >= dead_after
        else "awaiting-first-record"
        if last_record is None
        else "recent"
    )
    return {
        "capable": True,
        "state": state,
        "age_s": age,
        "heartbeat_s": heartbeat,
        "dead_after_s": dead_after,
        "last_kind": payload.get("last_kind"),
        "fault": fault if isinstance(fault, dict) else None,
    }


def _watchdog_death_report_path(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> Path:
    return ledger_dir(project_root) / (
        f"{_WATCHDOG_DEATH_REPORT_FILE_VERSION}.{_label_hash(controller_label)}."
        f"{_label_hash(lease_nonce)}.claimed"
    )


def claim_watchdog_death_report(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> bool:
    """First doorbell in this lease generation announces a missing watchdog.

    Without this the announcement is LEVEL-triggered: a missing watchdog lock
    is a standing condition, so every doorbell that arms into it fires
    watchdog-dead and exits, the controller re-arms, and the replacement fires
    immediately too. Measured across the fleet: no controller held a listener
    longer than three minutes, one was churning twelve at once, and a doorbell
    armed against an absent watchdog died after fifteen seconds having
    delivered no mail.

    Announcing an absence the controller has already been told about buys
    nothing and costs the doorbell that should have been carrying mail. So the
    first arm reports and the rest stay armed and keep delivering; coverage
    still reports the gap on every status read, which is where a standing
    condition belongs.
    """
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    path = _watchdog_death_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, _open_flags(create_exclusive=True), 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.close(fd)
    return True


def _normalize_pending_report_positions(
    positions: Mapping[str, int] | None,
    *,
    strict: bool,
) -> dict[str, int]:
    if not isinstance(positions, Mapping):
        raise ValueError("pending-report positions are invalid")
    normalized: dict[str, int] = {}
    for stream, position in positions.items():
        if strict and not isinstance(stream, str):
            raise ValueError("pending-report stream is invalid")
        stream_id = str(stream or "").strip()
        if not stream_id:
            if strict:
                raise ValueError("pending-report stream is invalid")
            continue
        if not isinstance(position, int) or isinstance(position, bool) or position < 1:
            raise ValueError("pending-report position is invalid")
        normalized[stream_id] = position
    return normalized


def _normalize_pending_report_snapshots(
    snapshots: Mapping[str, str] | None,
    *,
    strict: bool,
) -> dict[str, str]:
    if snapshots is None and not strict:
        return {}
    if not isinstance(snapshots, Mapping):
        raise ValueError("pending-report stream snapshots are invalid")
    normalized: dict[str, str] = {}
    for stream, snapshot in snapshots.items():
        if strict and not isinstance(stream, str):
            raise ValueError("pending-report snapshot stream is invalid")
        stream_id = str(stream or "").strip()
        snapshot_value = str(snapshot or "").strip()
        if not stream_id or len(snapshot_value) != 64:
            raise ValueError("pending-report stream snapshot is invalid")
        try:
            int(snapshot_value, 16)
        except ValueError as exc:
            raise ValueError("pending-report stream snapshot is invalid") from exc
        normalized[stream_id] = snapshot_value
    return normalized


def _read_pending_report_state(path: Path) -> PendingReportState | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise PendingReportStateError(
            f"pending-report state is unreadable: {path}: {exc}"
        ) from exc
    text = raw.strip()
    if not text:
        raise PendingReportStateError(f"pending-report state is incomplete: {path}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PendingReportStateError(
            f"pending-report state is incomplete: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PendingReportStateError(f"pending-report state is malformed: {path}")
    try:
        positions = _normalize_pending_report_positions(
            payload.get("positions"),
            strict=True,
        )
    except ValueError as exc:
        raise PendingReportStateError(
            f"pending-report state is malformed: {path}: {exc}"
        ) from exc

    if payload.get("schema") != PENDING_REPORT_STATE_SCHEMA:
        raise PendingReportStateError(f"pending-report state has unknown schema: {path}")
    phase = payload.get("phase")
    if phase not in {"claimed", "reported", "acknowledged"}:
        raise PendingReportStateError(f"pending-report state has invalid phase: {path}")
    cursor_version = payload.get("cursor_version")
    if cursor_version is not None and (
        not isinstance(cursor_version, int)
        or isinstance(cursor_version, bool)
        or cursor_version < 0
    ):
        raise PendingReportStateError(
            f"pending-report state has invalid cursor version: {path}"
        )
    try:
        stream_snapshots = _normalize_pending_report_snapshots(
            payload.get("stream_snapshots"),
            strict=True,
        )
    except ValueError as exc:
        raise PendingReportStateError(
            f"pending-report state is malformed: {path}: {exc}"
        ) from exc
    if cursor_version is not None and stream_snapshots.keys() != positions.keys():
        raise PendingReportStateError(
            f"pending-report state snapshot boundary is incomplete: {path}"
        )
    claim_token = payload.get("claim_token")
    owner = payload.get("owner")
    if (
        not isinstance(claim_token, str)
        or not claim_token
        or not isinstance(owner, dict)
        or not isinstance(owner.get("pid"), int)
        or isinstance(owner.get("pid"), bool)
        or int(owner["pid"]) <= 0
        or not isinstance(owner.get("start_token"), str)
        or not owner.get("start_token")
    ):
        raise PendingReportStateError(f"pending-report state owner is malformed: {path}")
    return PendingReportState(
        str(phase),
        positions,
        cursor_version,
        stream_snapshots,
        claim_token,
        int(owner["pid"]),
        str(owner["start_token"]),
    )


def pending_report_state(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> PendingReportState | None:
    """Read one atomic generation claim; malformed/partial state fails closed."""
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    return _read_pending_report_state(
        _pending_report_path(
            project_root,
            controller_label=label,
            lease_nonce=nonce,
        )
    )


def recover_pending_report_state(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> PendingReportState | None:
    """Read listener state, quarantining corruption so coverage can stay armed."""
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    path = _pending_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    try:
        return _read_pending_report_state(path)
    except PendingReportStateError:
        pass

    lock_path = _pending_report_lock_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        lock_fd = _acquire_pending_report_lock(lock_path, directory_fd=directory_fd)
        try:
            return _read_pending_report_state(path)
        except PendingReportStateError:
            quarantine = f".{path.name}.{uuid.uuid4().hex}.corrupt"
            try:
                os.replace(
                    path.name,
                    quarantine,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            os.fsync(directory_fd)
            return None
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _pending_report_owner() -> tuple[int, str]:
    pid = os.getpid()
    identity = goalflight_compat.process_start_identity(pid)
    if not isinstance(identity, dict) or not identity.get("start_token"):
        raise RuntimeError("pending-report owner process generation is unavailable")
    return pid, str(identity["start_token"])


def _pending_report_owner_liveness(state: PendingReportState) -> bool | None:
    if state.owner_pid is None or state.owner_start_token is None:
        return False
    matches = goalflight_compat.process_identity_matches(
        state.owner_pid,
        state.owner_start_token,
    )
    if matches is not True:
        return matches
    return False if goalflight_compat.pid_is_zombie(state.owner_pid) is True else True


def _acquire_pending_report_lock(path: Path, *, directory_fd: int) -> int:
    deadline = time.monotonic() + ENTRY_POLL_WINDOW_S
    while True:
        try:
            return _acquire_contended_lock(path, directory_fd=directory_fd)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise PendingReportStateError("pending-report state lock remained busy")
            time.sleep(min(0.01, ENTRY_POLL_INTERVAL_S))


def _write_pending_report_state(
    path: Path,
    payload: Mapping[str, object],
    *,
    directory_fd: int,
) -> None:
    encoded = (json.dumps(dict(payload), separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    pending = f".{path.name}.{uuid.uuid4().hex}.pending"
    pending_fd = -1
    try:
        pending_fd = os.open(
            pending,
            _open_flags(create_exclusive=True),
            0o600,
            dir_fd=directory_fd,
        )
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(pending_fd, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "short pending-report state write")
            remaining = remaining[written:]
        os.fsync(pending_fd)
        os.replace(
            pending,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        pending = ""
        os.fsync(directory_fd)
    finally:
        if pending_fd >= 0:
            os.close(pending_fd)
        if pending:
            _unlink_at(directory_fd, pending)


def acquire_pending_report(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    positions: Mapping[str, int] | None = None,
    cursor_version: int | None = None,
    stream_snapshots: Mapping[str, str] | None = None,
) -> PendingReportState | None:
    """Create or take over a provisional claim while preserving its boundary.

    A takeover is permitted only after PID/start-token evidence proves that the
    previous claimant is gone. Every rewrite is temp-file + fsync + replace +
    directory-fsync, so readers see the old complete phase or the new one.
    """
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    normalized = (
        _normalize_pending_report_positions(positions, strict=False)
        if positions is not None
        else None
    )
    normalized_snapshots = _normalize_pending_report_snapshots(
        stream_snapshots,
        strict=False,
    )
    if cursor_version is not None and (
        not isinstance(cursor_version, int)
        or isinstance(cursor_version, bool)
        or cursor_version < 0
    ):
        raise ValueError("pending-report cursor version is invalid")
    if cursor_version is not None and (
        normalized is None or normalized_snapshots.keys() != normalized.keys()
    ):
        raise ValueError("pending-report snapshot boundary is incomplete")
    path = _pending_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    lock_path = _pending_report_lock_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        lock_fd = _acquire_pending_report_lock(lock_path, directory_fd=directory_fd)
        current = _read_pending_report_state(path)
        if current is not None:
            if (
                current.phase == "acknowledged"
                or _pending_report_owner_liveness(current) is not False
            ):
                return None
            claim_positions = current.positions
            claim_cursor_version = current.cursor_version
            claim_stream_snapshots = current.stream_snapshots
        else:
            if normalized is None:
                return None
            claim_positions = normalized
            claim_cursor_version = cursor_version
            claim_stream_snapshots = normalized_snapshots
        owner_pid, owner_start_token = _pending_report_owner()
        claim_token = uuid.uuid4().hex
        claimed = PendingReportState(
            "claimed",
            dict(claim_positions),
            claim_cursor_version,
            dict(claim_stream_snapshots),
            claim_token,
            owner_pid,
            owner_start_token,
        )
        _write_pending_report_state(
            path,
            {
                "schema": PENDING_REPORT_STATE_SCHEMA,
                "phase": "claimed",
                "positions": claimed.positions,
                "cursor_version": claimed.cursor_version,
                "stream_snapshots": claimed.stream_snapshots,
                "claim_token": claim_token,
                "owner": {"pid": owner_pid, "start_token": owner_start_token},
                "claimed_at_epoch": time.time(),
                "reported_at_epoch": None,
                "acknowledged_at_epoch": None,
            },
            directory_fd=directory_fd,
        )
        return claimed
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def mark_pending_report_reported(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    claim_token: str,
) -> bool:
    """Record a complete local flush; receipt remains provisional until ack."""
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    token = str(claim_token or "").strip()
    if not label or not nonce or not token:
        raise ValueError("controller label, lease nonce, and claim token are required")
    path = _pending_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    lock_path = _pending_report_lock_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        lock_fd = _acquire_pending_report_lock(lock_path, directory_fd=directory_fd)
        current = _read_pending_report_state(path)
        if current is None:
            raise PendingReportStateError("pending-report claim disappeared before report")
        if current.claim_token != token:
            return False
        if current.phase in {"reported", "acknowledged"}:
            return True
        owner_pid, owner_start_token = _pending_report_owner()
        if (
            current.owner_pid != owner_pid
            or current.owner_start_token != owner_start_token
        ):
            return False
        _write_pending_report_state(
            path,
            {
                "schema": PENDING_REPORT_STATE_SCHEMA,
                "phase": "reported",
                "positions": current.positions,
                "cursor_version": current.cursor_version,
                "stream_snapshots": current.stream_snapshots,
                "claim_token": token,
                "owner": {"pid": owner_pid, "start_token": owner_start_token},
                "claimed_at_epoch": None,
                "reported_at_epoch": time.time(),
                "acknowledged_at_epoch": None,
            },
            directory_fd=directory_fd,
        )
        return True
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def acknowledge_pending_report(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    positions: Mapping[str, int],
) -> bool:
    """Settle a claim after authoritative cursor positions prove acknowledgement."""
    label = str(controller_label or "").strip()
    nonce = str(lease_nonce or "").strip()
    if not label or not nonce:
        raise ValueError("controller label and lease nonce are required")
    normalized = _normalize_pending_report_positions(positions, strict=False)
    path = _pending_report_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    lock_path = _pending_report_lock_path(
        project_root,
        controller_label=label,
        lease_nonce=nonce,
    )
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        lock_fd = _acquire_pending_report_lock(lock_path, directory_fd=directory_fd)
        try:
            current = _read_pending_report_state(path)
        except PendingReportStateError:
            return False
        if current is None or any(
            normalized.get(stream_id, 0) < high_water
            for stream_id, high_water in current.positions.items()
        ):
            return False
        if current.phase == "acknowledged":
            return True
        _write_pending_report_state(
            path,
            {
                "schema": PENDING_REPORT_STATE_SCHEMA,
                "phase": "acknowledged",
                "positions": current.positions,
                "cursor_version": current.cursor_version,
                "stream_snapshots": current.stream_snapshots,
                "claim_token": current.claim_token,
                "owner": {
                    "pid": current.owner_pid,
                    "start_token": current.owner_start_token,
                },
                "claimed_at_epoch": None,
                "reported_at_epoch": None,
                "acknowledged_at_epoch": time.time(),
            },
            directory_fd=directory_fd,
        )
        return True
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def claim_pending_report(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    positions: Mapping[str, int] | None = None,
) -> bool:
    """Compatibility wrapper: claim a provisional generation boundary."""
    return acquire_pending_report(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
        positions=positions or {},
    ) is not None


def pending_report_high_water(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> dict[str, int] | None:
    """Return the fixed claim boundary for every durable phase."""
    state = pending_report_state(
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    )
    return None if state is None else dict(state.positions)


def _ring_stamp_lock_path(project_root: Path | str, *, controller_label: str) -> Path:
    return ledger_dir(project_root) / (
        f"{_RING_STAMP_FILE_VERSION}.{_label_hash(controller_label)}.lock"
    )


def _acquire_contended_lock(path: Path, *, directory_fd: int) -> int:
    """Acquire one well-known lock without publishing an unlocked inode."""
    assert fcntl is not None
    name = path.name
    while True:
        try:
            fd = os.open(name, _open_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            pending = f".{name}.{uuid.uuid4().hex}.pending"
            pending_fd = os.open(
                pending,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                _lock_nonblocking(pending_fd)
                try:
                    os.link(
                        pending,
                        name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                except FileExistsError:
                    fcntl.flock(pending_fd, fcntl.LOCK_UN)
                    os.close(pending_fd)
                    pending_fd = -1
                    _unlink_at(directory_fd, pending)
                    continue
                _unlink_at(directory_fd, pending)
                return pending_fd
            except BaseException:
                if pending_fd >= 0:
                    os.close(pending_fd)
                _unlink_at(directory_fd, pending)
                raise
        try:
            _lock_nonblocking(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd


class WaiterRegistration:
    """One held-flock address.  The kernel owns the liveness fact."""

    def __init__(
        self,
        project_root: Path | str,
        label: str,
        kind: str,
        *,
        generation_key: str | None = None,
        generation_slots: int | None = None,
    ) -> None:
        self._fd = -1
        self._directory_fd = -1
        self._generation_path = None
        self._generation_fd = -1
        self.slot_index: int | None = None
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        normalized_label = str(label or "").strip()
        if not normalized_label:
            raise ValueError("waiter controller label is required")
        if kind not in LOCK_KINDS:
            raise ValueError(f"unknown held-lock kind: {kind}")
        normalized_generation = None
        generation_hash = None
        if generation_key is not None:
            normalized_generation = str(generation_key or "").strip()
            generation_hash = _waiter_generation_hash(normalized_generation)
        directory = ledger_dir(project_root)
        pending_name: str | None = None
        record_name: str | None = None
        try:
            self._directory_fd = _open_ledger_directory_path(directory, create=True)
            identity = goalflight_compat.process_start_identity(os.getpid())
            if not isinstance(identity, dict) or not identity.get("start_token"):
                raise RuntimeError("held-lock owner process generation is unavailable")
            start_hash = _start_hash(identity["start_token"])
            instance_id = uuid.uuid4().hex
            if generation_hash is None:
                record_name = (
                    f"{_LEGACY_FILE_VERSION}.{kind}.{_label_hash(normalized_label)}."
                    f"{os.getpid()}.{start_hash}.{instance_id}.lock"
                )
            else:
                record_name = (
                    f"{_FILE_VERSION}.{kind}.{_label_hash(normalized_label)}."
                    f"{generation_hash}.{os.getpid()}.{start_hash}.{instance_id}.lock"
                )
            self.record = WaiterRecord(
                kind=kind,
                label_hash=_label_hash(normalized_label),
                pid=os.getpid(),
                start_hash=start_hash,
                instance_id=instance_id,
                path=directory / record_name,
                generation_hash=generation_hash,
            )
            if normalized_generation is not None:
                if generation_slots is None:
                    candidates = [
                        _generation_lock_path(
                            project_root,
                            kind=kind,
                            label=normalized_label,
                            generation_key=normalized_generation,
                        )
                    ]
                else:
                    if kind != "listener":
                        raise ValueError("generation slots are only valid for listeners")
                    if not 1 <= generation_slots <= MAX_LISTENER_SLOTS:
                        raise ValueError(
                            f"listener slots must be between 1 and {MAX_LISTENER_SLOTS}"
                        )
                    candidates = [
                        _listener_slot_lock_path(
                            project_root,
                            label=normalized_label,
                            generation_key=normalized_generation,
                            slot=slot,
                        )
                        for slot in range(generation_slots)
                    ]
                for slot, candidate in enumerate(candidates):
                    try:
                        generation_fd = _acquire_contended_lock(
                            candidate,
                            directory_fd=self._directory_fd,
                        )
                    except BlockingIOError:
                        continue
                    self._generation_path = candidate
                    self._generation_fd = generation_fd
                    self.slot_index = slot if generation_slots is not None else None
                    break
                if self._generation_fd < 0:
                    raise ListenerSlotsFull(len(candidates))
            pending_name = f".{record_name}.{uuid.uuid4().hex}.pending"
            self._fd = os.open(
                pending_name,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=self._directory_fd,
            )
            _lock_nonblocking(self._fd)
            # Publish only after the kernel witness exists.  A concurrent probe
            # can therefore never acquire-and-prune a not-yet-locked address.
            os.replace(
                pending_name,
                record_name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            pending_name = None
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if self._generation_fd >= 0:
                fcntl.flock(self._generation_fd, fcntl.LOCK_UN)
                os.close(self._generation_fd)
                self._generation_fd = -1
            if self._directory_fd >= 0:
                if pending_name is not None:
                    _unlink_at(self._directory_fd, pending_name)
                if record_name is not None:
                    _unlink_at(self._directory_fd, record_name)
                os.close(self._directory_fd)
                self._directory_fd = -1
            raise

    def close(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        self._fd = -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            if self._directory_fd >= 0:
                _unlink_at(self._directory_fd, self.record.path.name)
        generation_fd = self._generation_fd
        self._generation_fd = -1
        if generation_fd >= 0:
            try:
                fcntl.flock(generation_fd, fcntl.LOCK_UN)
            finally:
                os.close(generation_fd)
        directory_fd = self._directory_fd
        self._directory_fd = -1
        if directory_fd >= 0:
            os.close(directory_fd)

    def __enter__(self) -> "WaiterRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - process death is authority.
        try:
            self.close()
        except (OSError, RuntimeError):
            pass


def register_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    kind: str,
    generation_key: str | None = None,
    generation_slots: int | None = None,
) -> WaiterRegistration:
    if kind not in WAITER_KINDS:
        raise ValueError(f"unknown waiter kind: {kind}")
    return WaiterRegistration(
        project_root,
        controller_label,
        kind,
        generation_key=generation_key,
        generation_slots=generation_slots,
    )


def listener_reserve_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
) -> str:
    """Operator hint when the listener pool is short of its configured depth.

    n=0 keeps the loud offline wording. Otherwise print the exact arm
    command once per missing slot (n=1/4 -> three pasteable lines).
    """
    if live_waiters == 0:
        return f"listener pool n=0; start: {command}"
    if int(live_waiters) > listener_low_water(int(target_waiters)):
        # Healthy depth: a missing slot is not news. Silence here is the
        # difference between a signal and a nag.
        return ""
    missing = max(0, int(target_waiters) - int(live_waiters))
    header = (
        f"listener pool n={live_waiters}/{target_waiters} — reserve down; "
        f"re-arm: {command}"
    )
    if missing <= 1:
        return header
    extra = "\n".join(command for _ in range(missing - 1))
    return f"{header}\n{extra}"


# One shell `&` loop is one harness task. The N doorbells it forks are
# invisible, so their exits wake nobody. The post-exit / lease-claim
# hint must be impossible to satisfy that way.
SEPARATE_TRACKED_ARM_RULE = (
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody"
)


def listener_depth_plan(
    live_waiters: int | None,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> dict[str, object]:
    """Remaining-depth machine plan after a listen exit or a lease claim.

    Entry nagging stays in ``listener_reserve_hint`` (silent above
    low-water). The numbered human list lives in ``listener_floor_hint``
    and is rendered only on the listen-exit surface. This plan carries
    the single command template: repeating it ``missing`` times is
    something a count already says.
    """
    target = int(target_waiters)
    live = 0 if live_waiters is None else int(live_waiters)
    missing = max(0, target - live)
    return {
        "live": live,
        "target": target,
        "missing": missing,
        "work_in_flight": bool(work_in_flight),
        "command": command,
        "separate_tracked_tasks": True,
    }


def listener_floor_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> str:
    """Exact remaining-depth commands after a listen exit or lease claim.

    Numbered lines so the correct response is N separate tracked calls,
    not one detached loop. Empty when there is no in-flight work or the
    pool is already at target.
    """
    if not work_in_flight:
        return ""
    live = int(live_waiters)
    target = int(target_waiters)
    missing = max(0, target - live)
    if missing == 0:
        return ""
    if live == 0:
        header = (
            f"listener floor: work in flight and live=0/{target} — "
            f"{missing} slots missing; {SEPARATE_TRACKED_ARM_RULE}:"
        )
    else:
        slot_word = "slot" if missing == 1 else "slots"
        header = (
            f"listener pool n={live}/{target} — {missing} {slot_word} missing; "
            f"{SEPARATE_TRACKED_ARM_RULE}:"
        )
    numbered = "\n".join(f"{index}. {command}" for index in range(1, missing + 1))
    return f"{header}\n{numbered}"


ACTIVITY_DEPTH_SCHEMA = "goalflight.listener-activity-signal.v1"
_ACTIVITY_DEPTH_FILE_VERSION = "activity-depth-v1"


def listener_activity_hint(
    live_waiters: int,
    target_waiters: int,
    command: str,
    *,
    work_in_flight: bool,
) -> str:
    """One-line remaining-depth cue for relay/status/next.

    The numbered list stays on listen-exit. Empty when there is no
    in-flight work or the pool is already at target.
    """
    if not work_in_flight:
        return ""
    live = int(live_waiters)
    target = int(target_waiters)
    missing = max(0, target - live)
    if missing == 0:
        return ""
    return f"listener depth {live}/{target} — {missing} missing; {command}"


def _activity_depth_state_path(
    project_root: Path | str,
    *,
    controller_label: str,
) -> Path:
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    return ledger_dir(project_root) / (
        f"{_ACTIVITY_DEPTH_FILE_VERSION}.{_label_hash(label)}.json"
    )


def _activity_depth_key(plan: dict[str, object]) -> dict[str, object]:
    return {
        "live": int(plan["live"]),
        "target": int(plan["target"]),
        "work_in_flight": bool(plan["work_in_flight"]),
    }


def _load_activity_depth_state(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ACTIVITY_DEPTH_SCHEMA:
        return None
    try:
        return _activity_depth_key(data)
    except (KeyError, TypeError, ValueError):
        return None


def _save_activity_depth_state(path: Path, key: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": ACTIVITY_DEPTH_SCHEMA, **key}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def consume_listener_activity_signal(
    project_root: Path | str,
    controller_label: str,
    plan: dict[str, object],
) -> str:
    """Return the one-line cue once per depth transition; empty otherwise.

    Same band-suppression idea as the context meter: a live controller
    runs these surfaces constantly, so a repeat of the same (live,
    target, work_in_flight) tuple is not news.
    """
    hint = listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=bool(plan["work_in_flight"]),
    )
    current = _activity_depth_key(plan)
    path = _activity_depth_state_path(
        project_root, controller_label=controller_label
    )
    last = _load_activity_depth_state(path)
    _save_activity_depth_state(path, current)
    if not hint or last == current:
        return ""
    return hint


def listener_low_water(target: int | None = None) -> int:
    """Depth at or below which the pool is 'running low' and worth a hint.

    A pool exists so a missed re-arm is survivable; nagging at the first
    missing slot (3/4) is noise, so hints stay silent while depth is
    healthy and speak only when the margin is genuinely thin. Default is
    half the target (4 -> 2), floored at 1 so a single-slot pool still
    warns when it empties.
    """
    resolved = int(target) if target is not None else listener_slot_count()
    raw = os.environ.get("GOALFLIGHT_LISTENER_LOW_WATER")
    if raw is not None:
        try:
            value = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("listener low water must be an integer") from exc
        return max(0, min(value, resolved))
    return max(1, resolved // 2)


def listener_slot_count(value: object = None) -> int:
    """Resolve and validate the bounded listener-pool size."""
    raw = value
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_LISTENER_SLOTS", str(DEFAULT_LISTENER_SLOTS))
    try:
        slots = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("listener slots must be an integer") from exc
    if not 1 <= slots <= MAX_LISTENER_SLOTS:
        raise ValueError(f"listener slots must be between 1 and {MAX_LISTENER_SLOTS}")
    return slots


def register_listener_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    generation_key: str,
    slots: int | None = None,
) -> WaiterRegistration:
    """Take the first free listener-slot-N lock for one lease generation."""
    resolved_slots = listener_slot_count(slots)
    return register_waiter(
        project_root,
        controller_label=controller_label,
        kind="listener",
        generation_key=generation_key,
        generation_slots=resolved_slots,
    )


def register_watchdog_waiter(
    project_root: Path | str,
    *,
    controller_label: str,
    generation_key: str,
) -> WaiterRegistration:
    """Hold the one watchdog lock for a lease generation, outside the doorbell pool."""
    return register_waiter(
        project_root,
        controller_label=controller_label,
        kind=WATCHDOG_KIND,
        generation_key=generation_key,
    )


def listener_slot_holder_pids(
    project_root: Path | str,
    *,
    controller_label: str,
) -> list[int]:
    waiters = live_waiters(
        project_root,
        controller_label=controller_label,
        kinds={"listener"},
    )
    return sorted({record.pid for record in (waiters or [])})


def _ring_stamp_needs_claim(observed: int | None, cursor_version: int) -> bool:
    """A cursor mutation stales the last ring even when no process owns a lock."""
    return observed != cursor_version


def claim_ring(
    project_root: Path | str,
    *,
    controller_label: str,
    cursor_version: int,
) -> bool:
    """Atomically claim the one ring allowed for a controller cursor version."""
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    if (
        not isinstance(cursor_version, int)
        or isinstance(cursor_version, bool)
        or cursor_version < 0
    ):
        raise ValueError("cursor version must be a non-negative integer")
    path = _ring_stamp_path(project_root, controller_label=label)
    lock_path = _ring_stamp_lock_path(project_root, controller_label=label)
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        try:
            lock_fd = _acquire_contended_lock(lock_path, directory_fd=directory_fd)
        except BlockingIOError:
            # Another listener is comparing/writing. It will publish the version
            # before releasing; this listener can observe the result next poll.
            return False
        stamp_fd = -1
        try:
            stamp_fd = os.open(path.name, _open_flags(), dir_fd=directory_fd)
            raw = os.read(stamp_fd, 65)
        except FileNotFoundError:
            raw = b""
        finally:
            if stamp_fd >= 0:
                os.close(stamp_fd)
        malformed = len(raw) > 64
        if raw:
            try:
                observed = int(raw.decode("ascii").strip())
            except (UnicodeDecodeError, ValueError):
                malformed = True
                observed = None
            if observed is not None and observed < 0:
                malformed = True
        else:
            observed = None
        if not malformed and not _ring_stamp_needs_claim(observed, cursor_version):
            return False

        corrupt_name: str | None = None
        if malformed:
            corrupt_epoch = time.time_ns()
            while True:
                corrupt_name = f"{path.name}.corrupt-{corrupt_epoch}"
                try:
                    os.stat(
                        corrupt_name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    break
                corrupt_epoch += 1
            os.rename(
                path.name,
                corrupt_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        encoded = f"{cursor_version}\n".encode("ascii")
        pending = f".{path.name}.{uuid.uuid4().hex}.pending"
        pending_fd = -1
        try:
            pending_fd = os.open(
                pending,
                _open_flags(create_exclusive=True),
                0o600,
                dir_fd=directory_fd,
            )
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(pending_fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "short listener ring stamp write")
                remaining = remaining[written:]
            os.fsync(pending_fd)
            os.replace(
                pending,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            pending = ""
            os.fsync(directory_fd)
        finally:
            if pending_fd >= 0:
                os.close(pending_fd)
            if pending:
                _unlink_at(directory_fd, pending)
        if corrupt_name is not None:
            print(
                f"listener ring stamp quarantined as {corrupt_name}; "
                f"rewrote cursor version {cursor_version}",
                file=sys.stderr,
            )
        return True
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def release_ring_claim(
    project_root: Path | str,
    *,
    controller_label: str,
    cursor_version: int,
) -> bool:
    """Release this cursor-version reservation after delivery failed.

    The compare under the same ring lock is load-bearing: a failed old writer
    must never erase a newer cursor claim. Removing the matching stamp makes
    the unread cursor immediately claimable by a replacement listener.
    """
    label = str(controller_label or "").strip()
    if not label:
        raise ValueError("controller label is required")
    if (
        not isinstance(cursor_version, int)
        or isinstance(cursor_version, bool)
        or cursor_version < 0
    ):
        raise ValueError("cursor version must be a non-negative integer")
    path = _ring_stamp_path(project_root, controller_label=label)
    lock_path = _ring_stamp_lock_path(project_root, controller_label=label)
    directory_fd = _open_ledger_directory_path(path.parent, create=True)
    lock_fd = -1
    try:
        deadline = time.monotonic() + 1.0
        while True:
            try:
                lock_fd = _acquire_contended_lock(
                    lock_path,
                    directory_fd=directory_fd,
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out releasing listener ring claim")
                time.sleep(0.01)
        try:
            stamp_fd = os.open(path.name, _open_flags(), dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        try:
            raw = os.read(stamp_fd, 65)
        finally:
            os.close(stamp_fd)
        try:
            observed = int(raw.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            return False
        if observed != cursor_version:
            return False
        _unlink_at(directory_fd, path.name)
        os.fsync(directory_fd)
        return True
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(directory_fd)


def _lease_lock_identity(label: str, nonce: str) -> str:
    normalized_label = str(label or "").strip()
    normalized_nonce = str(nonce or "").strip()
    if not normalized_label or not normalized_nonce:
        raise ValueError("controller lease label and nonce are required")
    return f"{normalized_label}\0{normalized_nonce}"


def register_lease_holder(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> "LeaseHolderRegistration":
    """Hold the kernel liveness witness for one controller lease generation."""
    identity = _lease_lock_identity(controller_label, lease_nonce)
    return LeaseHolderRegistration(
        _generation_lock_path(
            project_root,
            kind=LEASE_KIND,
            label=identity,
            generation_key=lease_nonce,
        ),
        label_hash=_label_hash(identity),
    )


class LeaseHolderRegistration:
    """One contended generation witness whose unlocked path is retained."""

    def __init__(self, path: Path, *, label_hash: str) -> None:
        self._fd = -1
        self._directory_fd = -1
        if fcntl is None:
            raise RuntimeError("held-flock wake ledger is unavailable on this platform")
        try:
            self._directory_fd = _open_ledger_directory_path(path.parent, create=True)
            self._fd = _acquire_contended_lock(
                path,
                directory_fd=self._directory_fd,
            )
        except BaseException:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
                self._directory_fd = -1
            raise
        self.record = WaiterRecord(
            kind=LEASE_KIND,
            label_hash=label_hash,
            pid=os.getpid(),
            start_hash="0" * 16,
            instance_id=path.stem.rsplit(".", 1)[-1],
            path=path,
        )

    def close(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        self._fd = -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            directory_fd = self._directory_fd
            self._directory_fd = -1
            if directory_fd >= 0:
                os.close(directory_fd)

    def __enter__(self) -> "LeaseHolderRegistration":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


# Removed _probe_locked_once: one-sample waiter liveness could revive the inherited-fd race stable probing fixed.


def _probe_locked_state_at(directory_fd: int, name: str) -> bool | None:
    """Return held/unheld, or UNKNOWN when the address cannot be opened."""
    if fcntl is None:
        return None
    try:
        fd = os.open(name, _open_flags(), dir_fd=directory_fd)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {11, 35}:
                return True
            return None
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _probe_locked_state(path: Path) -> bool | None:
    if fcntl is None:
        return None
    try:
        directory_fd = _open_ledger_directory_path(path.parent, create=False)
    except OSError:
        return None
    try:
        return _probe_locked_state_at(directory_fd, path.name)
    finally:
        os.close(directory_fd)


def live_waiters(
    project_root: Path | str,
    *,
    controller_label: str | None = None,
    generation_key: str | None = None,
    kinds: Iterable[str] = WAITER_KINDS,
    prune_dead: bool = True,
) -> list[WaiterRecord] | None:
    directory = ledger_dir(project_root)
    try:
        directory_fd = _open_ledger_directory_path(directory, create=False)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            return None
        # Missing/unreadable storage is also probe-unavailable, never known-zero.
        return None
    try:
        with os.scandir(directory_fd) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.name.startswith(
                    (f"{_FILE_VERSION}.", f"{_LEGACY_FILE_VERSION}.")
                )
                and entry.name.endswith(".lock")
            ]
        accepted_kinds = set(kinds)
        wanted_label = _label_hash(controller_label) if controller_label else None
        wanted_generation = (
            _waiter_generation_hash(generation_key)
            if generation_key is not None
            else None
        )
        live: list[WaiterRecord] = []
        for name in names:
            path = directory / name
            record = _parse_waiter_path(path)
            if record is None or record.kind not in accepted_kinds:
                continue
            if wanted_label is not None and record.label_hash != wanted_label:
                continue
            if (
                wanted_generation is not None
                and record.generation_hash != wanted_generation
            ):
                continue
            lock_state = _probe_locked_state_at(directory_fd, name)
            identity = goalflight_compat.process_start_identity(record.pid)
            owner_matches = bool(
                isinstance(identity, dict)
                and identity.get("start_token")
                and _start_hash(identity["start_token"]) == record.start_hash
                and goalflight_compat.pid_is_zombie(record.pid) is False
            )
            if owner_matches and lock_state is True:
                live.append(record)
            elif prune_dead:
                try:
                    _unlink_at(directory_fd, name)
                except OSError:
                    pass
        return sorted(live, key=lambda row: (row.kind, row.pid, row.instance_id))
    except OSError:
        return None
    finally:
        os.close(directory_fd)


def lease_holder_alive(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
    prune_dead: bool = True,
) -> bool | None:
    """Return held/unheld, or UNKNOWN when the active witness path vanished."""
    del prune_dead
    identity = _lease_lock_identity(controller_label, lease_nonce)
    path = _generation_lock_path(
        project_root,
        kind=LEASE_KIND,
        label=identity,
        generation_key=lease_nonce,
    )
    return _probe_locked_state(path)


def coverage_status(
    project_root: Path | str,
    *,
    controller_label: str | None,
    lease_nonce: str | None = None,
    now_epoch: float | None = None,
    observed_waiters: list[WaiterRecord] | None | object = _OBSERVED_WAITERS_UNSET,
) -> dict[str, object]:
    if not controller_label:
        target_waiters = listener_slot_count()
        return {
            "covered": False,
            "reason": "missing-label",
            "live_waiters": 0,
            "target_waiters": target_waiters,
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    durable_monitor = (
        monitor_status(
            project_root,
            controller_label=controller_label,
            lease_nonce=lease_nonce,
            now_epoch=now_epoch,
        )
        if lease_nonce
        else None
    )
    monitor_state_present = monitor_state_stamp(
        project_root,
        controller_label=controller_label,
    ) is not None
    monitor_generation_seen = False
    if lease_nonce:
        monitor_generation_seen = _probe_locked_state(
            _generation_lock_path(
                project_root,
                kind=MONITOR_KIND,
                label=controller_label,
                generation_key=lease_nonce,
            )
        ) is not None
    waiters = (
        live_waiters(
            project_root,
            controller_label=controller_label,
            generation_key=lease_nonce,
            kinds={"listener", MONITOR_KIND, WATCHDOG_KIND},
        )
        if observed_waiters is _OBSERVED_WAITERS_UNSET
        else observed_waiters
    )
    if waiters is not None and not isinstance(waiters, list):
        raise TypeError("observed_waiters must be a waiter list or None")
    if waiters is not None and lease_nonce:
        wanted_generation = _waiter_generation_hash(lease_nonce)
        waiters = [
            row for row in waiters if row.generation_hash == wanted_generation
        ]
    if waiters is None:
        persistent = (
            durable_monitor is not None
            or monitor_state_present
            or monitor_generation_seen
        )
        target_waiters = (
            PERSISTENT_WAKE_TARGET if persistent else listener_slot_count()
        )
        return {
            "covered": False,
            "reason": "waiter-probe-unavailable",
            "live_waiters": None,
            "target_waiters": target_waiters,
            "waiters": [],
            "monitor": {
                "required": persistent,
                "state": (
                    str(durable_monitor.get("state") or "unknown")
                    if durable_monitor
                    else "unavailable"
                    if persistent
                    else "not-applicable"
                ),
            },
            **({"wake_mode": "persistent"} if persistent else {}),
        }
    monitor_waiters = [row for row in waiters if row.kind == MONITOR_KIND]
    portable_waiters = [row for row in waiters if row.kind == "listener"]
    watchdog_waiters = [row for row in waiters if row.kind == WATCHDOG_KIND]
    persistent = (
        bool(monitor_waiters)
        or bool(watchdog_waiters)
        or durable_monitor is not None
        or monitor_state_present
        or monitor_generation_seen
    )
    serialized_waiters = [
        {
            "kind": row.kind,
            "pid": row.pid,
            "instance_id": row.instance_id,
            "generation_hash": row.generation_hash,
            "path": str(row.path),
        }
        for row in waiters
    ]
    if persistent:
        durable_state = (
            str(durable_monitor.get("state") or "unknown")
            if durable_monitor
            else "unavailable"
        )
        monitor_lock_live = bool(monitor_waiters)
        monitor_healthy = (
            monitor_lock_live
            and durable_monitor is not None
            and durable_state not in {"fault", "stale"}
        )
        backup_live = bool(portable_waiters)
        watchdog_live = bool(watchdog_waiters)
        monitor_state = (
            "unavailable"
            if durable_monitor is None
            else durable_state
            if durable_state in {"fault", "stale"}
            else "live"
            if monitor_lock_live
            else "missing"
        )
        missing_components = []
        if not monitor_healthy:
            missing_components.append("stream")
        if not backup_live:
            missing_components.append("backup")
        if not watchdog_live:
            missing_components.append("watchdog")
        if monitor_state == "unavailable":
            reason = "persistent-monitor-state-unavailable"
        elif monitor_state == "fault":
            reason = "persistent-monitor-fault"
        elif monitor_state == "stale":
            reason = "persistent-monitor-stale"
        elif not monitor_lock_live:
            reason = "persistent-monitor-missing"
        elif not backup_live:
            reason = "persistent-backup-missing"
        elif not watchdog_live:
            reason = "persistent-watchdog-missing"
        else:
            reason = "persistent-covered"
        return {
            "covered": monitor_healthy and backup_live and watchdog_live,
            "reason": reason,
            "live_waiters": (
                int(monitor_healthy) + int(backup_live) + int(watchdog_live)
            ),
            "target_waiters": PERSISTENT_WAKE_TARGET,
            "waiters": serialized_waiters,
            "monitor": {
                "required": True,
                "state": monitor_state,
                "lock_live": monitor_lock_live,
                "age_s": (
                    durable_monitor.get("age_s") if durable_monitor else None
                ),
                "dead_after_s": (
                    durable_monitor.get("dead_after_s")
                    if durable_monitor
                    else None
                ),
                "fault": (
                    durable_monitor.get("fault") if durable_monitor else None
                ),
            },
            "backup": {
                "required": True,
                "state": "live" if backup_live else "missing",
                "observed": len(portable_waiters),
            },
            "watchdog": {
                "required": True,
                "state": "live" if watchdog_live else "missing",
                "observed": len(watchdog_waiters),
            },
            "wake_mode": "persistent",
            "portable_live_waiters": len(portable_waiters),
            "portable_target_waiters": 1,
            "missing_components": missing_components,
        }
    target_waiters = listener_slot_count()
    return {
        "covered": bool(waiters),
        "reason": "held-flock" if waiters else "no-live-waiter-lock",
        "live_waiters": len(waiters),
        "target_waiters": target_waiters,
        "waiters": serialized_waiters,
        "monitor": {"required": False, "state": "not-applicable"},
        "backup": {"required": False, "state": "not-applicable"},
        "wake_mode": "portable",
    }


def follow_start_command(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> str:
    messages_script = goalflight_compat.advertised_script(
        "goalflight_messages.py",
        running_file=__file__,
    )
    return shlex.join(
        [
            "python3",
            str(messages_script),
            "follow",
            "--project-root",
            str(Path(project_root).expanduser().resolve(strict=False)),
            "--controller-label",
            controller_label,
            "--lease-nonce",
            lease_nonce,
        ]
    )


def persistent_backup_start_command(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> str:
    messages_script = goalflight_compat.advertised_script(
        "goalflight_messages.py",
        running_file=__file__,
    )
    return shlex.join(
        [
            "python3",
            str(messages_script),
            "listen",
            "--project-root",
            str(Path(project_root).expanduser().resolve(strict=False)),
            "--controller-label",
            controller_label,
            "--lease-nonce",
            lease_nonce,
            "--listener-slots",
            "1",
            "--report-pending",
        ]
    )


def follow_watchdog_start_command(
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str,
) -> str:
    messages_script = goalflight_compat.advertised_script(
        "goalflight_messages.py",
        running_file=__file__,
    )
    return shlex.join(
        [
            "python3",
            str(messages_script),
            "listen",
            "--project-root",
            str(Path(project_root).expanduser().resolve(strict=False)),
            "--controller-label",
            controller_label,
            "--lease-nonce",
            lease_nonce,
            "--watch-follow",
        ]
    )


def coverage_rearm_commands(
    status: dict[str, object],
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str | None = None,
) -> list[str]:
    """Return exact tracked-task commands for the missing wake components."""
    live = status.get("live_waiters")
    target = int(status.get("target_waiters") or listener_slot_count())
    missing = max(0, target - (live if isinstance(live, int) else 0))
    if status.get("wake_mode") != "persistent":
        command = listener_start_command(
            project_root,
            controller_label=controller_label,
        )
        return [command for _ in range(missing)]
    nonce = str(lease_nonce or "").strip()
    if not nonce:
        return []
    commands: list[str] = []
    components = status.get("missing_components")
    for component in components if isinstance(components, list) else []:
        if component == "stream":
            commands.append(
                follow_start_command(
                    project_root,
                    controller_label=controller_label,
                    lease_nonce=nonce,
                )
            )
        elif component == "backup":
            commands.append(
                persistent_backup_start_command(
                    project_root,
                    controller_label=controller_label,
                    lease_nonce=nonce,
                )
            )
        elif component == "watchdog":
            commands.append(
                follow_watchdog_start_command(
                    project_root,
                    controller_label=controller_label,
                    lease_nonce=nonce,
                )
            )
    return commands


def coverage_rearm_plan(
    status: dict[str, object],
    project_root: Path | str,
    *,
    controller_label: str,
    lease_nonce: str | None = None,
    work_in_flight: bool,
) -> dict[str, object]:
    """Build one plan from the shared coverage predicate for every consumer."""
    live_value = status.get("live_waiters")
    live = live_value if isinstance(live_value, int) else 0
    target = int(status.get("target_waiters") or listener_slot_count())
    commands = coverage_rearm_commands(
        status,
        project_root,
        controller_label=controller_label,
        lease_nonce=lease_nonce,
    )
    fallback = listener_start_command(
        project_root,
        controller_label=controller_label,
    )
    plan = listener_depth_plan(
        live,
        target,
        commands[0] if commands else fallback,
        work_in_flight=work_in_flight,
    )
    plan["wake_mode"] = status.get("wake_mode") or "portable"
    plan["reason"] = status.get("reason")
    if status.get("wake_mode") == "persistent":
        plan["commands"] = commands
        plan["missing_components"] = list(
            status.get("missing_components") or []
        )
    return plan


def coverage_rearm_hint(plan: dict[str, object]) -> str:
    """Render a plan without pretending stream and backup are identical slots."""
    if not bool(plan.get("work_in_flight")):
        return ""
    live = int(plan.get("live") or 0)
    target = int(plan.get("target") or 0)
    missing = int(plan.get("missing") or 0)
    if missing == 0:
        return ""
    if plan.get("wake_mode") != "persistent":
        return listener_reserve_hint(
            live,
            target,
            str(plan.get("command") or ""),
        )
    commands = [str(row) for row in plan.get("commands") or []]
    components = [str(row) for row in plan.get("missing_components") or []]
    names = ", ".join(components) if components else "unknown"
    header = f"persistent wake coverage {live}/{target} — missing {names}:"
    if not commands:
        return f"{header}\n(no safe re-arm command: controller lease unavailable)"
    numbered_rows = []
    for index, (component, command) in enumerate(
        zip(components, commands),
        1,
    ):
        instruction = (
            "arm through the host persistent stdout monitor; never ordinary "
            "backgrounding"
            if component == "stream"
            else "arm as its own tracked background task"
        )
        numbered_rows.append(f"{index}. {component}: {instruction}: {command}")
    numbered = "\n".join(numbered_rows)
    return f"{header}\n{numbered}"


def listener_start_command(
    project_root: Path | str,
    *,
    controller_label: str | None,
) -> str:
    messages_script = goalflight_compat.advertised_script(
        "goalflight_messages.py",
        running_file=__file__,
    )
    argv = [
        "python3",
        str(messages_script),
        "listen",
        "--project-root",
        str(Path(project_root).expanduser().resolve(strict=False)),
    ]
    if controller_label:
        argv.extend(["--controller-label", controller_label])
    argv.append("--report-pending")
    return shlex.join(argv)


def entry_poll_window_s() -> float:
    if os.environ.get("GOALFLIGHT_TEST_MODE") == "1":
        default = 0.05
    else:
        default = ENTRY_POLL_WINDOW_S
    raw = os.environ.get("GOALFLIGHT_WAKE_ENTRY_POLL_S", "").strip()
    try:
        return min(2.0, max(0.0, float(raw))) if raw else default
    except ValueError:
        return default


def check_tool_entry(
    project_root: Path | str,
    *,
    controller_label: str | None,
    controller_lease_nonce: str | None = None,
    controller_claimed: bool,
    mail_bearing: bool,
    pending_probe: Callable[[], str | None] | None = None,
    stream=None,
) -> dict[str, object]:
    """Warn/poll only for a claimed controller call that can surface mail."""
    if not mail_bearing:
        return {
            "covered": False,
            "reason": "not-mail-bearing",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    if not controller_claimed:
        return {
            "covered": False,
            "reason": "no-ambient-claimed-controller",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    status = coverage_status(
        project_root,
        controller_label=controller_label,
        lease_nonce=controller_lease_nonce,
    )
    live_waiters = status.get("live_waiters")
    target_waiters = int(status.get("target_waiters") or listener_slot_count())
    if isinstance(live_waiters, int) and live_waiters >= target_waiters:
        return status
    output = sys.stderr if stream is None else stream
    command = listener_start_command(project_root, controller_label=controller_label)
    if status.get("reason") == "waiter-probe-unavailable":
        print(
            "listener coverage UNKNOWN (probe unavailable); "
            f"if you have no listener, start: {command}",
            file=output,
        )
    else:
        plan = coverage_rearm_plan(
            status,
            project_root,
            controller_label=str(controller_label or ""),
            lease_nonce=controller_lease_nonce,
            work_in_flight=True,
        )
        hint = coverage_rearm_hint(plan)
        if hint:
            print(hint, file=output)
        status["rearm_plan"] = plan
        commands = plan.get("commands") or []
        if commands:
            command = str(commands[0])
    status["start_command"] = command
    if pending_probe is None:
        return status
    deadline = time.monotonic() + entry_poll_window_s()
    while True:
        pending = pending_probe()
        if pending:
            print(pending, file=output)
            status["pending_notice"] = pending
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status
        time.sleep(min(ENTRY_POLL_INTERVAL_S, remaining))
